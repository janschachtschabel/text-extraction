# text-extraction Helm chart

Deploys the text-extraction API, the extract workers for batches and jobs, the browser workers for browser mode and screenshots, optional Docling workers for OCR, optional ASR workers for transcription, optional NLP workers for PII handling and optional llm workers for structured output with an LLM, semantic chunking and image descriptions (all off by default), an optional single-replica Valkey (with a password), and optionally an MCP endpoint (`/mcp`) and a playground (`/ui`) on the API pods.

```bash
helm install text-extraction ./deploy/helm/text-extraction \
  --set-file secrets.apiKeys=api-keys.json
```

Without API keys the pods refuse to start (exit code 2) while `config.AUTH_MODE` is `api_key`.
Compute the SHA-256 of a key with `printf %s "$KEY" | sha256sum | cut -d' ' -f1`.

For production, start from [`values-production.example.yaml`](values-production.example.yaml): an
external Valkey, every secret from your own Secrets, pinned SemVer image tags, the measured
requests and limits, the PrometheusRule and ServiceMonitor on, MCP and the playground off.
`deploy/helm/test.sh` renders it and lints it with `--strict`.

## What it creates

| Object | When | Notes |
| --- | --- | --- |
| Deployment, ConfigMap | always | Stateless pods. Non-secret settings come from the ConfigMap through `envFrom`. |
| Worker Deployment `<release>-text-extraction-worker-extract` | `workers.extract.enabled` | Runs `text-extraction-worker`: claims job items of the pool `extract` from the queue in Valkey. Serves only port 9090. Same image, ConfigMap and Secrets as the API. |
| Worker Deployment `<release>-text-extraction-worker-browser` | `workers.browser.enabled` | Runs `text-extraction-browser-worker` from the browser image (`workers.browser.image`): claims the items of the pool `browser` and renders them in Chromium, in unblock mode in Google Chrome under Xvfb. Serves only port 9090. Same ConfigMap and Secrets as the API. |
| Worker Deployment `<release>-text-extraction-worker-docling` | `workers.docling.enabled` | Runs `text-extraction-docling-worker` from the Docling image (`workers.docling.image`): claims the items of the pool `docling` (scans, broken text layers, images, OpenDocument files) and converts them with Docling and Tesseract. An init container downloads the models first. Serves only port 9090. Same ConfigMap and Secrets as the API. See [Docling workers](#docling-workers-and-the-models-volume). |
| PersistentVolumeClaim `<release>-text-extraction-docling-models` | `workers.docling.enabled` and no `workers.docling.models.persistence.existingClaim` | The models volume of the Docling workers, `ReadWriteMany` by default. |
| Worker Deployment `<release>-text-extraction-worker-asr` | `workers.asr.enabled` | Runs `text-extraction-asr-worker` from the ASR image (`workers.asr.image`): claims the items of the pool `asr` (`transcribe: true` without subtitles, audio and video uploads) and transcribes them with faster-whisper, or on an external endpoint (`workers.asr.backend: openai`). An init container downloads the model first (local backend). Downloaded media live on a bounded emptyDir at `/scratch`. Serves only port 9090. Same ConfigMap and Secrets as the API. See [ASR workers](#asr-workers-and-the-models-volume). |
| PersistentVolumeClaim `<release>-text-extraction-asr-models` | `workers.asr.enabled`, `workers.asr.backend: local` and no `workers.asr.models.persistence.existingClaim` | The Whisper model volume of the ASR workers, `ReadWriteMany` by default. |
| Worker Deployment `<release>-text-extraction-worker-nlp` | `workers.nlp.enabled` | Runs `text-extraction-nlp-worker` from the NLP image (`workers.nlp.image`): claims the items of the pool `nlp` (results of requests with `pii`) and finds and replaces personal data with Presidio and spaCy. An init container downloads the spaCy models first. Serves only port 9090. Same ConfigMap and Secrets as the API; `PII_HASH_KEY` from `workers.nlp.hashKey.existingSecret`. See [NLP workers](#nlp-workers-and-the-models-volume). |
| Worker Deployment `<release>-text-extraction-worker-llm` | `workers.llm.enabled` | Runs `text-extraction-llm-worker` from the API image: claims the items of the pool `llm` (the last hop of a request with `structured` or `chunking` that needs the LLM) and calls the OpenAI-compatible endpoint `workers.llm.baseUrl`. No model, no volume, no init container. Serves only port 9090. Same ConfigMap and Secrets as the API; `LLM_API_KEY` from `workers.llm.apiKey.existingSecret`. See [llm workers](#llm-workers). |
| PersistentVolumeClaim `<release>-text-extraction-nlp-models` | `workers.nlp.enabled` and no `workers.nlp.models.persistence.existingClaim` | The spaCy models volume of the NLP workers, `ReadWriteMany` by default. |
| Job `<release>-text-extraction-worker-<pool>-models` (Helm hook) | `workers.<pool>.models.downloadMode: job` (pools `docling`, `asr`, `nlp`) with the download on | Fills the pool's models volume after each install and upgrade instead of an init container in every worker pod. Deleted when it succeeded. |
| Secret `<release>-text-extraction-asr` | `workers.asr.backend: openai`, `workers.asr.external.apiKey` set and no `workers.asr.external.existingSecret` | Holds `ASR_EXTERNAL_API_KEY`. |
| Service `<release>-text-extraction-worker-<pool>-internal` | `workers.<pool>.enabled` | Port `internal` 9090 of the workers of the pool, for the ServiceMonitor. Cluster-internal only. |
| ScaledObject (KEDA) `<release>-text-extraction-worker-<pool>` | `workers.<pool>.autoscaling.keda.enabled` | Scales the workers on `max(text_extraction_queue_pending_items{pool="<pool>", namespace="<release namespace>"})`, plus `max(text_extraction_queue_leased_items{…})` with `keda.countLeased` (the pool `asr`). |
| HorizontalPodAutoscaler `<release>-text-extraction-worker-<pool>` | `workers.<pool>.autoscaling.hpa.enabled` | CPU utilisation of the workers. Mutually exclusive with KEDA. |
| Secret `<release>-text-extraction-valkey-auth` | `valkey.enabled`, `valkey.auth.enabled` and no `valkey.auth.existingSecret` | Holds `VALKEY_PASSWORD`, generated once (32 alphanumeric characters) and kept across upgrades. |
| NetworkPolicy (Valkey, API, each worker pool) | `networkPolicy.enabled` | Ingress only. See [Network policies](#network-policies). |
| Service `<release>-text-extraction` | always | Public port `http` 8080. |
| Service `<release>-text-extraction-internal` | always | Port `internal` 9090 (`/health`, `/ready`, `/metrics`). Cluster-internal only. |
| Secret `<release>-text-extraction-keys` | `secrets.apiKeys` set and no `secrets.existingSecret` | Holds `API_KEYS`. |
| Valkey Deployment, Service, optional PVC | `valkey.enabled` | `redis://<release>-text-extraction-valkey:6379/0`. |
| Ingress | `ingress.enabled` | Routes to the public Service port 8080 only. |
| HorizontalPodAutoscaler | `autoscaling.enabled` | CPU utilisation. Replaces `replicaCount`. |
| ServiceMonitor | `metrics.serviceMonitor.enabled` | Scrapes the internal Services of the API pods and of the workers, port `internal`, path `/metrics`. |
| PrometheusRule | `metrics.prometheusRule.enabled` | `TextExtractionDown`, `TextExtractionHigh5xx` and, with jobs enabled, the job alerts below. |

Pods run as uid/gid 10001 with a read-only root filesystem, all capabilities dropped and the
`RuntimeDefault` seccomp profile. An `emptyDir` is mounted at `/tmp` (on the browser workers limited
to `workers.browser.tmpSizeLimit`, 2Gi: browser profiles, Chrome's caches and its `HOME`; the
browsers use it instead of `/dev/shm`). The probes use the internal
port: `/health` for liveness and `/ready` for readiness. `/ready` answers 200 only once the public
port listens and turns 503 as soon as SIGTERM arrives, so no preStop hook is needed.
The images start `tini` as PID 1, which forwards signals and reaps orphaned child processes (for
example a `tesseract` whose conversion child was killed). The worker containers therefore pass
their entry point as `args`, never as `command`, which would replace the image's `ENTRYPOINT`.

## Configuration

Every service setting is an environment variable. `config` entries are non-secret and become
variables of the same name. The chart overrides `config.PUBLIC_PORT` (8080), `config.INTERNAL_PORT`
(9090) and `config.VALKEY_URL`: values set there are ignored. Set the Valkey address with
`valkey.enabled` or `externalValkey.*`.

Besides these, the chart sets `JOBS_ENABLED` (from `jobs.enabled`), `MCP_ENABLED` (from
`mcp.enabled`) and `UI_ENABLED` (from `ui.enabled`) on the API pods (`MCP_ENABLED` and `UI_ENABLED`
on no other pod, and entries for them under `config` are left out of the ConfigMap),
`WORKER_CONCURRENCY` (from `workers.extract.concurrency`) on the extract workers,
`BROWSER_CONCURRENCY` (from `workers.browser.concurrency`) on the browser workers,
`DOCLING_CONCURRENCY` (from `workers.docling.concurrency`) and `DOCLING_MODELS_PATH` (`/models`,
the mount of the models volume) on the Docling workers, `ASR_CONCURRENCY`, `ASR_MODEL`,
`ASR_MODELS_PATH` (`/models`), `ASR_SCRATCH_PATH` (`/scratch`) and, with the external backend,
`ASR_EXTERNAL_BASE_URL` and `ASR_EXTERNAL_MODEL` on the ASR workers, `PII_CONCURRENCY`,
`PII_MODEL_SIZE` (from `workers.nlp.modelSize`) and `PII_MODELS_PATH` (`/models`) on the NLP
workers, `LLM_CONCURRENCY` (from `workers.llm.concurrency`) on the llm workers, and `BROWSER_ENABLED` (from
`workers.browser.enabled`), `BROWSER_UNBLOCK_ENABLED` (from `workers.browser.unblock`),
`DOCLING_ENABLED` (from `workers.docling.enabled`), `ASR_ENABLED` (from `workers.asr.enabled`),
`ASR_BACKEND` (from `workers.asr.backend`) and `MEDIA_UPLOAD_MAX_BYTES` (from
`workers.asr.uploadMaxBytes`), `PII_ENABLED` (from `workers.nlp.enabled`) and `LLM_ENABLED` (from
`workers.llm.enabled`) on every pod (API, extract, browser, Docling, ASR, NLP and llm workers):
entries for them under `config` are ignored. While `workers.llm.enabled` is true, `LLM_BASE_URL`,
`LLM_MODEL` and `LLM_EMBEDDING_MODEL` (from `workers.llm.baseUrl`, `.model` and `.embeddingModel`,
the last only when set) go to every pod too, and `config` entries for them are ignored;
`LLM_VISION_MODEL` (from `workers.llm.visionModel`) goes the same way while it is set. The other
`LLM_*` settings (`LLM_TEMPERATURE`, `LLM_TIMEOUT_SECONDS`, `LLM_ALLOW_HTTP`,
`LLM_VISION_MAX_IMAGE_BYTES`, ...) are set under `config`. So browser mode, OCR, transcription,
PII handling and the LLM are on exactly when the chart deploys their workers.

`config.FETCH_MAX_BYTES` (the download limit) and `config.DOCUMENT_MAX_BYTES` (the largest
document and upload) are both 20000000 by default: lower them together. A `DOCUMENT_MAX_BYTES`
above `FETCH_MAX_BYTES` makes every pod exit with code 2 at start-up.

The API, extract and browser pods convert documents (MarkItDown) in `DOCUMENT_CONCURRENCY` (2)
child processes, each with an address-space limit of `DOCUMENT_MEMORY_LIMIT_MB`. The chart
renders that limit from the container's memory limit: `floor((limits.memory in MiB -
documentParentReserveMiB) / DOCUMENT_CONCURRENCY)`, 768 MiB for every pod with the defaults
(`(2560 - 1024) / 2` for the API and extract pods, `(4608 - 3072) / 2` for the browser pods, whose
reserve covers Chromium). Each reserve holds the selector child as well
(`STRUCTURED_SELECTOR_PROCESSES` x `STRUCTURED_SELECTOR_MEMORY_MB`, 512 MiB): raise the reserve and
the limit together when you raise either. So a child runs out of memory and answers `too_large` before the
container's OOM killer ends the pod; the children also raise their `oom_score_adj` to 1000, so
the kernel picks them before the server when the pod runs out anyway. A share below 256 MiB fails
the render. `config.DOCUMENT_MEMORY_LIMIT_MB` wins when set; without a memory limit the
service's default (2048) applies.

> **Warning:** never put secrets into `config:`. It becomes a ConfigMap, which is stored and shown
> in clear text. Use `secrets.*` and `externalValkey.existingSecret.*` for secrets.

Secrets, listed separately:

| Variable | Source |
| --- | --- |
| `API_KEYS` | `secrets.apiKeys` (chart-managed Secret) or `secrets.existingSecret` + `secrets.existingSecretKey`. Only SHA-256 hashes of keys are stored. |
| `VALKEY_PASSWORD` | Only with the in-chart Valkey: the generated Secret, or `valkey.auth.existingSecret` + `valkey.auth.existingSecretKey`. API and worker pods get it as an env var and build `VALKEY_URL=redis://:$(VALKEY_PASSWORD)@<valkey>:6379/0` with it. |
| `VALKEY_URL` | Plain `externalValkey.url`, or for a URL with a password `externalValkey.existingSecret.name` + `.key`. The in-chart Valkey sets it from `VALKEY_PASSWORD`. |
| `PII_HASH_KEY` | Optional, with `workers.nlp.enabled`: your own Secret, `workers.nlp.hashKey.existingSecret` + `.existingSecretKey` (at least 32 bytes, `openssl rand -hex 32`). Rendered into the NLP worker container only (not its models init container or Job, not the API or other workers). Without it each result is hashed with a random key (`hash_scope: result`). |
| `LLM_API_KEY` | Optional, with `workers.llm.enabled`: your own Secret, `workers.llm.apiKey.existingSecret` + `.existingSecretKey` (the chart creates none, and never renders the key as a literal). Rendered into the API pods (which answer 400 when neither it nor the caller's `X-LLM-Api-Key` is there) and the llm worker container, no other pod. A caller's `X-LLM-Api-Key` wins over it. |
| `ASR_EXTERNAL_API_KEY` | Only with `workers.asr.backend: openai`, optional: `workers.asr.external.apiKey` (chart-managed Secret) or `workers.asr.external.existingSecret` + `.existingSecretKey`. Rendered into the API pods (which answer 400 when neither it nor the caller's `X-ASR-Api-Key` is there) and the ASR workers, no other pod. |

### Valkey password

The in-chart Valkey starts with `--requirepass "$(VALKEY_PASSWORD)"` (Kubernetes expands the env var;
no shell is involved) and `--maxmemory <valkey.maxmemory> --maxmemory-policy noeviction`: jobs, the
queue and counters are state, so a full Valkey refuses writes instead of evicting them. The memory
request and limit are both 1280Mi (Guaranteed QoS: the node does not evict the pod that holds all
state); `maxmemory` is 60 % of it, because it counts the data only and the rest is for allocator
fragmentation, client buffers and the fork of an append-only-file rewrite. Raise both together.

Without `valkey.auth.existingSecret` the chart creates a Secret with a random alphanumeric password
and reads it back with `lookup` on every upgrade, so it stays the same. `lookup` needs a cluster:
`helm template`, `helm install --dry-run` and GitOps tools that render without a cluster (Argo CD
renders with `helm template`) cannot find the Secret and would generate a new password on each
render. Use `valkey.auth.existingSecret` there.

**A password from `existingSecret` must be URL-safe: letters, digits and `-` `.` `_` `~` only**
(for example `openssl rand -hex 24`). The same value is passed to Valkey verbatim and put into the
URL, which redis-py parses and percent-decodes (`p%40ss` becomes `p@ss`). A password with
characters such as `@ : / ? # %` breaks the URL. Percent-encoding it does not help either: Valkey
would then check the encoded text while the client sends the decoded one, and every connection
fails with `invalid username-password pair`. This differs for `externalValkey.existingSecret`,
which holds the whole URL: there the password must be percent-encoded.

For a Valkey with a password, create a Secret and point the chart at it:

```bash
kubectl create secret generic valkey-url --from-literal=VALKEY_URL='redis://:PASSWORD@valkey.example.org:6379/0'
helm install text-extraction ./deploy/helm/text-extraction \
  --set valkey.enabled=false \
  --set externalValkey.existingSecret.name=valkey-url
```

## Values

| Key | Default | Description |
| --- | --- | --- |
| `nameOverride`, `fullnameOverride` | `""` | Override the chart or release-scoped object names. |
| `image.repository` | `ghcr.io/janschachtschabel/text-extraction` | API image. |
| `image.tag` | `""` | Image tag. Defaults to the chart `appVersion`. Production uses SemVer tags. |
| `image.pullPolicy` | `IfNotPresent` | |
| `imagePullSecrets` | `[]` | |
| `replicaCount` | `2` | Ignored when `autoscaling.enabled`. |
| `terminationGracePeriodSeconds` | unset (computed) | Time between SIGTERM and kill of the API pods. It must cover the drain plus the graceful shutdown plus a 10 s margin: `SHUTDOWN_DRAIN_SECONDS` + `max(30, EXTRACT_TIMEOUT_SECONDS + 5)`, and with browser workers at least `SHUTDOWN_DRAIN_SECONDS` + `EXTRACT_TIMEOUT_SECONDS` + `BROWSER_QUEUE_WAIT_SECONDS` + the render deadline + 10 (the deadline is `BROWSER_TIMEOUT_SECONDS`, with unblock mode the larger of it and `BROWSER_UNBLOCK_TIMEOUT_SECONDS`). With Docling workers it is also at least `SHUTDOWN_DRAIN_SECONDS` + `EXTRACT_TIMEOUT_SECONDS` + `DOCLING_SYNC_WAIT_SECONDS` + 5 + 10 (a sync request waits for OCR after its fast phase). Unset, the chart computes it from `config.*` or the defaults: 45 without browser or Docling workers, 65 with Docling workers only, 90 with browser workers, 120 with unblock mode. A value set here that does not fit fails the render. |
| `config.LOG_LEVEL` | `INFO` | Log level. |
| `config.LOG_FORMAT` | `json` | `json` or `console`. |
| `config.AUTH_MODE` | `api_key` | `api_key` or `none`. |
| `config.RATE_LIMIT_ENABLED` | `"true"` | Inbound rate limit per API key. |
| `config.RATE_LIMIT_PER_MINUTE` | `"60"` | Default limit per key. |
| `config.CONVERT_CONCURRENCY` | `"2"` | Conversions at once per pod. The memory limit is sized for it. |
| `config.POST_CONCURRENCY` | `"2"` | Threads of the post stage (patterns, chunking) per process, in every pod. |
| `config.STRUCTURED_SELECTOR_TIMEOUT_SECONDS` | `"5"` | Time all selector rules of one result may take in their child process, which is then killed (`selectors_timed_out`). |
| `config.STRUCTURED_SELECTOR_PROCESSES` | `"1"` | Selector child processes per pod (API, extract, browser and llm workers), about 40 MB each when idle. |
| `config.STRUCTURED_SELECTOR_MEMORY_MB` | `"512"` | Address space of one selector child. The default memory limits and `documentParentReserveMiB` values hold 512 MiB for it (the llm worker's limit too); raise them by `STRUCTURED_SELECTOR_PROCESSES` x this when you raise either. |
| `config.SITE_MAX_DEPTH`, `config.SITE_MAX_PAGES` | `"5"`, `"1000"` | Deepest `depth` and most `max_pages` a `POST /v1/site-structure` may ask for; more answers 422. |
| `config.SITE_MAX_SITEMAP_FILES`, `config.SITE_MAX_SITEMAP_URLS` | `"500"`, `"200000"` | Sitemap files and sitemap entries one crawl reads at most. A crawl's items live in Valkey with its job: size `valkey.maxmemory` for the open crawls (each page item with up to `SITE_MAX_LINKS_PER_PAGE` links, each sitemap entry about 100 bytes). |
| `config.SITE_SITEMAP_MAX_BYTES` | `"52428800"` | Largest sitemap file, as fetched and after a gzip inflate (the protocol's 50 MiB). |
| `config.SITE_MAX_LINKS_PER_PAGE` | `"1000"` | Most links kept of one crawled page. |
| `config.MEDIA_TRANSCRIPT_PANEL_ENABLED` | `"true"` | Use YouTube's new transcript view in the browser when the subtitles could not be read (`auto` requests, needs browser workers). |
| `config.SHUTDOWN_DRAIN_SECONDS` | `"5"` | Seconds the pod reports not ready but keeps serving after SIGTERM, before the graceful shutdown. |
| `config.FETCH_MAX_BYTES`, `config.DOCUMENT_MAX_BYTES` | unset (20000000 each) | Lower them together: `DOCUMENT_MAX_BYTES` must not exceed `FETCH_MAX_BYTES`, or the pods exit with code 2. |
| `config.MCP_ALLOWED_ORIGINS` | `""` | Browser origins that may call `/mcp`, a comma list of `http(s)://host[:port]`; empty: a request with any `Origin` header is refused (MCP clients outside a browser send none). |
| `config.MCP_WAIT_SECONDS` | `"25"` | What a tool that may become a job waits for its result, and the longest wait of `get_job` (0-60). The ingress read timeout must be at least 120 s, or this + 70 s if larger, see [MCP and playground](#mcp-and-playground). |
| `config.MCP_MAX_CONTENT_CHARS` | `"50000"` | Most content characters of one tool answer; the rest is fetched with `content_offset`. |
| `config.MCP_IMAGE_MAX_BYTES` | `"5000000"` | Largest screenshot attached to a tool answer (0: never attached, only linked). |
| `config.UI_TREE_MAX_URLS` | `"5000"` | Most URLs the site tree of one site job shows. |
| `config.UI_BATCH_FILE_MAX_BYTES`, `config.UI_BATCH_FILE_MAX_ROWS` | `"10485760"`, `"100000"` | Largest batch file (CSV or JSON of URLs) and its most rows. |
| `config.UI_IMAGE_MAX_BYTES` | `"1000000"` | Largest preview image the playground fetches. |
| `config.UI_DEFAULT_LANGUAGE` | `"en"` | Language of the playground (`en` or `de`) when the browser asks for neither. |
| `config.UI_DOCS_BASE_URL` | `"https://github.com/janschachtschabel/text-extraction/blob/main/docs/user-guide"` | The user guide the help links of the playground open (`http` or `https`; one folder per language below it). |
| `config.*` | | Any further non-secret setting is added here. |
| `secrets.apiKeys` | `""` | `API_KEYS` JSON. Creates a chart-managed Secret. |
| `secrets.existingSecret` | `""` | Existing Secret with the `API_KEYS` JSON. Takes precedence over `secrets.apiKeys`. |
| `secrets.existingSecretKey` | `API_KEYS` | Key inside that Secret. |
| `resources` | requests 1400m / 832Mi, limits 3750m / 2560Mi | Measured in the load test (docs/configuration.md, "Resource baselines"). The memory limit stays above the measured need: it covers the service, `CONVERT_CONCURRENCY` x about 450 MB (one 5 MiB page) and the `DOCUMENT_CONCURRENCY` document conversion children; raise it with either concurrency or `CONVERT_MAX_BYTES`. `DOCUMENT_MEMORY_LIMIT_MB` is rendered from it (see above). |
| `documentParentReserveMiB` | `1024` | MiB of the API container's memory limit kept for everything but the document conversion children: the server (512) and the selector child (512); the rest is split evenly among them as `DOCUMENT_MEMORY_LIMIT_MB`. |
| `autoscaling.enabled` | `false` | HorizontalPodAutoscaler of the API pods. |
| `autoscaling.minReplicas` / `maxReplicas` | `2` / `10` | |
| `autoscaling.targetCPUUtilizationPercentage` | `70` | |
| `jobs.enabled` | unset (true with extract workers, false without) | `JOBS_ENABLED` of the API pods. When `workers.extract.enabled` is false the chart renders `JOBS_ENABLED=false` so the batch and job routes answer 503 and `wait_seconds` is ignored, unless you set `jobs.enabled=true` (workers run elsewhere). `false` forces it off. Only API pods with `JOBS_ENABLED=true` export the queue gauges. |
| `mcp.enabled` | `false` | Serve the MCP endpoint at `/mcp` on the API pods (`MCP_ENABLED`); see [MCP and playground](#mcp-and-playground). A `config.MCP_ENABLED` entry is ignored. |
| `ui.enabled` | `false` | Serve the playground at `/ui` on the API pods (`UI_ENABLED`). Its password is an API key; under `config.AUTH_MODE=none` it is open to anyone who can reach the service, and the install notes warn. A `config.UI_ENABLED` entry is ignored. |
| `workers.extract.enabled` | `true` | Deploy the extract workers. |
| `workers.extract.replicaCount` | `2` | Ignored with KEDA or HPA. |
| `workers.extract.concurrency` | `8` | `WORKER_CONCURRENCY`: job items per worker pod at once. |
| `workers.extract.terminationGracePeriodSeconds` | `45` | Workers have no drain wait: items in flight get `max(30, EXTRACT_TIMEOUT_SECONDS + 5)` seconds. The render fails when that plus a 10 s margin does not fit. |
| `workers.extract.nodeSelector` | `{}` | Merged over the global `nodeSelector` for the extract workers; a key set here wins. |
| `workers.extract.resources` | requests 450m / 768Mi, limits 1750m / 2560Mi | Measured (docs/configuration.md, "Resource baselines"); the memory limit is sized like the API's: the worker, its page conversions and its document conversion children. |
| `workers.extract.documentParentReserveMiB` | `1024` | As `documentParentReserveMiB`, for the extract workers. |
| `workers.extract.autoscaling.keda.enabled` | `false` | Needs the KEDA operator and Prometheus scraping the API pods. The scaler reads the queue gauges that only API pods with `JOBS_ENABLED=true` export (see `jobs.enabled`); the render fails when `jobs.enabled=false`. The workers may scale down to `minReplicaCount` because the gauges come from the API pods. |
| `workers.extract.autoscaling.keda.serverAddress` | `http://prometheus-operated.monitoring.svc:9090` | Prometheus that KEDA queries. |
| `workers.extract.autoscaling.keda.threshold` | `50` | Pending items per replica (pending plus leased with `countLeased`). `null`: the pool's `concurrency`. Must be positive. |
| `workers.extract.autoscaling.keda.countLeased` | `false` | Also count the items the workers hold (`text_extraction_queue_leased_items`), so the replicas busy with them are part of the target. Always on while `minReplicaCount` is `0`: only a running worker takes back the items of a lost one, so they must keep a replica up. |
| `workers.extract.autoscaling.keda.minReplicaCount` / `maxReplicaCount` | `1` / `10` | |
| `workers.extract.autoscaling.hpa.enabled` | `false` | CPU-based HorizontalPodAutoscaler of the workers. **Mutually exclusive with KEDA**: enabling both fails the render, because KEDA manages its own HPA. |
| `workers.extract.autoscaling.hpa.minReplicas` / `maxReplicas` | `2` / `10` | |
| `workers.extract.autoscaling.hpa.targetCPUUtilizationPercentage` | `70` | |
| `workers.<pool>.autoscaling.scaleDownStabilizationWindowSeconds` | `null` (`asr`: `3600`) | How long the load must stay lower before KEDA or the HPA removes a replica (`advanced.horizontalPodAutoscalerConfig.behavior.scaleDown` of the ScaledObject, `behavior.scaleDown` of the HPA). `null`: the Kubernetes default (300 s). 0 to 3600, or the render fails. |
| `workers.browser.enabled` | `true` | Deploy the browser workers. Also renders `BROWSER_ENABLED` into every pod: false switches browser mode off (`mode: "browser"` and screenshots answer 400, `auto` never escalates). |
| `workers.browser.unblock` | `false` | Unblock mode (`BROWSER_UNBLOCK_ENABLED` in every pod): admins with `compliance: "ignore"` get a Chrome that clears bot challenges. Needs `workers.browser.enabled`, or the render fails. |
| `workers.browser.image.repository` | `ghcr.io/janschachtschabel/text-extraction-browser` | Browser worker image (Playwright's Chromium shell, Google Chrome stable, Xvfb; linux/amd64). |
| `workers.browser.image.tag` | `""` | Defaults to `image.tag`, then the chart `appVersion`. The pull policy is `image.pullPolicy`. |
| `workers.browser.replicaCount` | `2` | Ignored with KEDA or HPA. |
| `workers.browser.concurrency` | `4` | `BROWSER_CONCURRENCY`: pages per pod at once, also its job slots. |
| `workers.browser.terminationGracePeriodSeconds` | `150` | Renders in flight get `max(30, EXTRACT_TIMEOUT_SECONDS + 5, render deadline + 10)` seconds (40, 70 with unblock mode). The render fails when that plus a 10 s margin does not fit. |
| `workers.browser.resources` | requests 750m / 1152Mi, limits 2250m / 4608Mi | Measured with 4 pages (docs/configuration.md, "Resource baselines"); the memory limit holds the browser, the selector child and the conversions. |
| `workers.browser.documentParentReserveMiB` | `3072` | As `documentParentReserveMiB`, for the browser workers: it covers Chromium with its pages and the selector child, so each document conversion child gets `(4608 - 3072) / 2` = 768 MiB. |
| `workers.browser.tmpSizeLimit` | `2Gi` | Size limit of the `/tmp` emptyDir. |
| `workers.browser.nodeSelector` | `{kubernetes.io/arch: amd64}` | Merged over the global `nodeSelector` for the browser workers; a key set here wins. The browser image is linux/amd64 only (Google Chrome stable), so the default keeps its pods off arm64 nodes. Set the key to `null` to drop it. |
| `workers.browser.autoscaling.keda.*` | as for the extract workers, `threshold` `8` | Scales on `max(text_extraction_queue_pending_items{pool="browser"})`; the API pods export it while `JOBS_ENABLED` and `BROWSER_ENABLED` are true. Keep `minReplicaCount` at 1 or more: a sync request waits only `BROWSER_QUEUE_WAIT_SECONDS` (10) for a worker. |
| `workers.browser.autoscaling.hpa.*` | as for the extract workers | CPU-based. Mutually exclusive with KEDA. |
| `workers.docling.enabled` | `false` | Deploy the Docling workers (OCR). Also renders `DOCLING_ENABLED` into every pod: false keeps scans at their text layer (`ocr_unavailable`), images and OpenDocument files answer `unsupported_content_type`, `ocr: "always"` answers 400. Needs `JOBS_ENABLED=true` on the API pods, or the render fails. |
| `workers.docling.image.repository` | `ghcr.io/janschachtschabel/text-extraction-docling` | Docling worker image (Docling, CPU torch, Tesseract; no models; linux/amd64). |
| `workers.docling.image.tag` | `""` | Defaults to `image.tag`, then the chart `appVersion`. The pull policy is `image.pullPolicy`. |
| `workers.docling.replicaCount` | `1` | Ignored with KEDA or HPA. |
| `workers.docling.concurrency` | `1` | `DOCLING_CONCURRENCY`: documents per pod at once, each in a child process of its own. |
| `workers.docling.terminationGracePeriodSeconds` | `60` | Conversions in flight get 30 s after SIGTERM, then the children are killed and their items run again after their lease. The render fails below 40. |
| `workers.docling.resources` | requests 2250m / 1600Mi, limits 5500m / 8Gi | Measured for one conversion with `DOCLING_THREADS` (4) threads (docs/configuration.md, "Resource baselines"); the memory limit backs the child's `DOCLING_MEMORY_LIMIT_MB` (6144). |
| `workers.docling.nodeSelector` | `{kubernetes.io/arch: amd64}` | Merged over the global `nodeSelector`; the Docling image is built for linux/amd64 only. |
| `workers.docling.autoscaling.keda.*` | as for the extract workers, `threshold` `4`, `maxReplicaCount` `4` | Scales on `max(text_extraction_queue_pending_items{pool="docling"})`; the API pods export it while `JOBS_ENABLED` and `DOCLING_ENABLED` are true. With no replica a sync request answers without OCR after `DOCLING_SYNC_WAIT_SECONDS` and batch items wait. |
| `workers.docling.autoscaling.hpa.*` | as for the extract workers, `1` / `4` replicas | CPU-based. Mutually exclusive with KEDA. |
| `workers.docling.models.download` | `true` | Runs `text-extraction-docling-worker --download-models` with the volume mounted writable. `false` needs `persistence.existingClaim` (a pre-filled volume), or the render fails. |
| `workers.docling.models.downloadMode` | `init` | `init`: an init container of every worker pod (works with `ReadWriteOnce`). `job`: a Helm post-install/post-upgrade hook Job, once per release; the worker pods only check the marker (a pod that starts before the Job is done exits with code 1 and restarts). Needs `ReadWriteMany` or one node. `helm install --wait` waits for the workers before it runs the hook: install the first release without `--wait`, or use `init`. |
| `workers.docling.models.resources` | requests 100m / 256Mi, limits 1 / 1Gi | Of the init container or the hook Job (not the worker's `resources`): the download streams each file to the volume and hashes it on the way. |
| `workers.docling.models.persistence.size` | `5Gi` | The models take about 335 MiB (9 files); the download stages a full copy beside the old one when the manifest changes, so about 670 MiB at once. 5Gi leaves room for later models; 1Gi suffices today. |
| `workers.docling.models.persistence.accessModes` | `[ReadWriteMany]` | Replicas on any node share the volume. With `ReadWriteOnce` keep the replicas on one node. |
| `workers.docling.models.persistence.storageClass` | `""` | Cluster default when empty. |
| `workers.docling.models.persistence.existingClaim` | `""` | Use this PVC instead of creating one. |
| `workers.asr.enabled` | `false` | Deploy the ASR workers (transcription). Also renders `ASR_ENABLED` into every pod: false makes `transcribe: true` answer 400 and audio or video uploads `unsupported_content_type`. Needs `JOBS_ENABLED=true` on the API pods, or the render fails. |
| `workers.asr.backend` | `local` | `ASR_BACKEND` on every pod. `local`: faster-whisper with the model from the volume. `openai`: the external endpoint below; no models volume, no download. With `openai` and `workers.asr.enabled`, do not set `OPENAI_CUSTOM_HEADERS` or `OPENAI_LOG`: every pod refuses to start (exit code 2), as with the LLM. |
| `workers.asr.model` | `large-v3-turbo` | `ASR_MODEL`: `large-v3-turbo` (about 1.6 GB, better German) or `small` (about 0.5 GB, about twice as fast on CPU). Only this one is downloaded. Anything else fails the render. |
| `workers.asr.image.repository` | `ghcr.io/janschachtschabel/text-extraction-asr` | ASR worker image (faster-whisper, PyAV on an LGPL FFmpeg, yt-dlp with Deno; no models, no `ffmpeg` program; linux/amd64). |
| `workers.asr.image.tag` | `""` | Defaults to `image.tag`, then the chart `appVersion`. The pull policy is `image.pullPolicy`. |
| `workers.asr.replicaCount` | `1` | Ignored with KEDA or HPA. |
| `workers.asr.concurrency` | `1` | `ASR_CONCURRENCY`: transcriptions per pod at once, each in a child process of its own. |
| `workers.asr.uploadMaxBytes` | `25000000` | `MEDIA_UPLOAD_MAX_BYTES` on every pod: the largest audio or video upload (decision D3). Its bytes travel through Valkey; above `DOCLING_INPUT_BUDGET_BYTES` the render fails. |
| `workers.asr.terminationGracePeriodSeconds` | `60` | Transcriptions in flight get 30 s after SIGTERM, then the children are killed and their items go back to the queue at once (attempts + 1) to start again on another replica. The render fails below 40. |
| `workers.asr.resources` | requests 3050m / 1408Mi, limits 5500m / 6Gi | Measured for one transcription with `large-v3-turbo` and `ASR_THREADS` (4) threads (docs/configuration.md, "Resource baselines"); `small` was not measured. The memory limit backs the child's `ASR_MEMORY_LIMIT_MB` (4096). |
| `workers.asr.scratch.sizeLimit` | `2Gi` | The emptyDir at `/scratch` (`ASR_SCRATCH_PATH`) for downloaded media. `concurrency` × (`MEDIA_MAX_BYTES` (500000000) + 5 MB) must fit, or the render fails. |
| `workers.asr.tmpSizeLimit` | `1Gi` | sizeLimit of the `/tmp` emptyDir (`HOME`, library caches). |
| `workers.asr.nodeSelector` | `{kubernetes.io/arch: amd64}` | Merged over the global `nodeSelector`; the ASR image is published for linux/amd64. |
| `workers.asr.autoscaling.keda.*` | as for the extract workers, `threshold` `null` (= `concurrency`), `countLeased` `true`, `maxReplicaCount` `4` | Scales on `max(text_extraction_queue_pending_items{pool="asr"}) + max(text_extraction_queue_leased_items{pool="asr"})`: a replica per `concurrency` transcriptions queued or in flight. The API pods export both while `JOBS_ENABLED` and `ASR_ENABLED` are true. |
| `workers.asr.autoscaling.hpa.*` | as for the extract workers, `1` / `4` replicas | CPU-based. Mutually exclusive with KEDA. |
| `workers.asr.autoscaling.scaleDownStabilizationWindowSeconds` | `3600` | The Kubernetes maximum: an hour of lower load before a replica goes. See "ASR workers and the models volume" on interrupted transcriptions. |
| `workers.asr.models.download` | `true` | Runs `text-extraction-asr-worker --download-models` (only `model`, from huggingface.co) with the volume mounted writable. `false` needs `persistence.existingClaim`, or the render fails. Local backend only. |
| `workers.asr.models.downloadMode` | `init` | `init` or `job`, as for the Docling workers. |
| `workers.asr.models.resources` | requests 100m / 256Mi, limits 1 / 1Gi | Of the init container or the hook Job, as for the Docling workers. |
| `workers.asr.models.persistence.size` | `4Gi` | `large-v3-turbo` takes about 1.6 GiB, and an update stages a full copy beside it. |
| `workers.asr.models.persistence.accessModes` | `[ReadWriteMany]` | Replicas on any node share the volume. With `ReadWriteOnce` keep the replicas on one node. |
| `workers.asr.models.persistence.storageClass` | `""` | Cluster default when empty. |
| `workers.asr.models.persistence.existingClaim` | `""` | Use this PVC instead of creating one. |
| `workers.asr.external.baseUrl` | `""` | `ASR_EXTERNAL_BASE_URL` of `backend: openai` (required then, https only, or the render fails). |
| `workers.asr.external.model` | `whisper-1` | `ASR_EXTERNAL_MODEL`. |
| `workers.asr.external.apiKey` | `""` | `ASR_EXTERNAL_API_KEY` for a chart-managed Secret. Optional: without a key callers must send `X-ASR-Api-Key`. |
| `workers.asr.external.existingSecret` | `""` | Your own Secret with the key; takes precedence over `apiKey`. |
| `workers.asr.external.existingSecretKey` | `ASR_EXTERNAL_API_KEY` | Key inside that Secret (also used for the chart-managed one). |
| `workers.nlp.enabled` | `false` | Deploy the NLP workers (PII handling). Also renders `PII_ENABLED` into every pod: false makes a request with `pii` answer 400 `invalid_request`. Needs `JOBS_ENABLED=true` on the API pods, or the render fails. |
| `workers.llm.enabled` | `false` | Deploy the llm workers. Also renders `LLM_ENABLED` into every pod: false makes a request that needs the LLM (`structured` with strategy `llm` or `auto`, `learn_selectors`, `chunking` `semantic`, `describe_images`) answer 400 `invalid_request`. Needs `JOBS_ENABLED=true` on the API pods, or the render fails. |
| `workers.llm.baseUrl` | `https://api.openai.com/v1` | `LLM_BASE_URL` in every pod: the OpenAI-compatible endpoint (`/chat/completions`, `/embeddings`). Required, `https://` only (the pods exit with code 2 otherwise) unless `config.LLM_ALLOW_HTTP` is `"true"` (local model server, development). |
| `workers.llm.model` | `gpt-6-luna` | `LLM_MODEL` in every pod, sent with every chat request. Required. |
| `workers.llm.embeddingModel` | `""` | `LLM_EMBEDDING_MODEL` in every pod: model of the `/embeddings` route. Empty: `semantic` chunking answers 400. |
| `workers.llm.visionModel` | `""` | `LLM_VISION_MODEL` in every pod, only when set: the model of the image descriptions (`describe_images`); it must take images. Empty: `LLM_MODEL` describes them. |
| `workers.llm.apiKey.existingSecret` | `""` | Your own Secret with `LLM_API_KEY`. Empty: callers must send `X-LLM-Api-Key`. |
| `workers.llm.apiKey.existingSecretKey` | `LLM_API_KEY` | Key inside that Secret. |
| `workers.llm.replicaCount` | `1` | Ignored with KEDA or HPA. Keep one: a sync request that needs the LLM waits only `LLM_SYNC_WAIT_SECONDS` (30) for a worker. |
| `workers.llm.concurrency` | `4` | `LLM_CONCURRENCY`: items per pod at once, each with one call to the endpoint at a time. |
| `workers.llm.gracefulSeconds` | `30` | `LLM_GRACEFUL_SECONDS` on the llm workers; the render fails outside 10 to 900 (a `config.LLM_GRACEFUL_SECONDS` entry is overridden on the llm workers, but every pod validates it at start (10 to 900, else exit code 2), so leave it out). Items in flight get this long after SIGTERM, then the ones still running go back to the queue at once, run again elsewhere and pay their LLM calls again. One call may take `LLM_TIMEOUT_SECONDS` (120 s by default), so with the defaults a rollout cancels the long calls in flight; raise this and `terminationGracePeriodSeconds` together, e.g. to `LLM_TIMEOUT_SECONDS + 10` (130) and 140. 10 to 900. |
| `workers.llm.terminationGracePeriodSeconds` | `40` | The render fails below `workers.llm.gracefulSeconds` + 10 (40 by default), so the hand-back happens before the kill. |
| `workers.llm.resources` | requests 150m / 256Mi, limits 750m / 1Gi | Measured against a fake endpoint (docs/configuration.md, "Resource baselines"): the worker waits for the endpoint and holds no model; 512 MiB of the limit are its selector child, which checks the proposals of `learn_selectors`, and the rest leaves room for `describe_images`. |
| `workers.llm.nodeSelector` | `{}` | Merged over the global `nodeSelector`. The workers run the API image. |
| `workers.llm.autoscaling.keda.*` | as for the extract workers, `threshold` `null` (`concurrency`), `maxReplicaCount` `4` | Scales on `max(text_extraction_queue_pending_items{pool="llm"})`. The API pods export it while `JOBS_ENABLED` and `LLM_ENABLED` are true. Keep one replica. |
| `workers.llm.autoscaling.hpa.*` | as for the extract workers, `1` / `4` replicas | CPU-based. Mutually exclusive with KEDA. |
| `workers.llm.autoscaling.scaleDownStabilizationWindowSeconds` | `null` | As for the extract workers. |
| `workers.nlp.modelSize` | `lg` | `PII_MODEL_SIZE` on the NLP workers: `lg` (about 1 GB on the volume, 1.6 GB resident, better German names) or `md` (about 115 MB, half the memory, more names missed). Only the German and English models of this size are downloaded. Anything else fails the render. |
| `workers.nlp.image.repository` | `ghcr.io/janschachtschabel/text-extraction-nlp` | NLP worker image (Presidio and spaCy, no models). |
| `workers.nlp.image.tag` | `""` | Defaults to `image.tag`, then the chart `appVersion`. The pull policy is `image.pullPolicy`. |
| `workers.nlp.replicaCount` | `1` | Ignored with KEDA or HPA. |
| `workers.nlp.concurrency` | `1` | `PII_CONCURRENCY`: results analysed per pod at once, each in a child process of its own with the models loaded. |
| `workers.nlp.terminationGracePeriodSeconds` | `60` | Analyses in flight get 30 s after SIGTERM, then the children are killed and their items run again elsewhere once their lease expires: `2 × PII_TIMEOUT_SECONDS + 10`, 610 s by default, so a rollout during an analysis can delay that job by about 10 minutes. The render fails below 40. |
| `workers.nlp.resources` | requests 750m / 1728Mi, limits 2 / 3Gi | Measured for one analysis child with the `lg` models (docs/configuration.md, "Resource baselines"); a long text peaks at about 2.2 GB, and the limit backs `PII_MEMORY_LIMIT_MB` (3072). `md` was not measured (about half). |
| `workers.nlp.nodeSelector` | `{}` | Merged over the global `nodeSelector`. The nlp wheels are not amd64-only, but the published NLP image is built for linux/amd64 only: pin `kubernetes.io/arch: amd64` here when the cluster has arm64 nodes. |
| `workers.nlp.autoscaling.keda.*` | as for the extract workers, `threshold` `10` (not measured: the load test ran one pod per pool without autoscaling; tune it in production from the queue gauges), `maxReplicaCount` `4` | Scales on `max(text_extraction_queue_pending_items{pool="nlp"})`. The API pods export it while `JOBS_ENABLED` and `PII_ENABLED` are true. Keep one replica: a sync request with `pii` waits only `PII_SYNC_WAIT_SECONDS` (20) for a worker, and answers 202 with its job when the backlog waits longer. |
| `workers.nlp.autoscaling.hpa.*` | as for the extract workers, `1` / `4` replicas | CPU-based. Mutually exclusive with KEDA. |
| `workers.nlp.autoscaling.scaleDownStabilizationWindowSeconds` | `null` | As for the extract workers. |
| `workers.nlp.hashKey.existingSecret` | `""` | Your own Secret with `PII_HASH_KEY`. Empty: no key, `hash_scope: result`. |
| `workers.nlp.hashKey.existingSecretKey` | `PII_HASH_KEY` | Key inside that Secret. |
| `workers.nlp.models.download` | `true` | Runs `text-extraction-nlp-worker --download-models` (the models of `modelSize`, from github.com, which redirects to githubusercontent.com) with the volume mounted writable. `false` needs `persistence.existingClaim`, or the render fails. |
| `workers.nlp.models.downloadMode` | `init` | `init` or `job`, as for the Docling workers. |
| `workers.nlp.models.resources` | requests 100m / 256Mi, limits 1 / 1Gi | Of the init container or the hook Job, as for the Docling workers. |
| `workers.nlp.models.persistence.size` | `3Gi` | An update stages a full copy beside the models before it swaps them: the `lg` pair takes about 1 GB, so a manifest update needs about 2.4 GB at its peak (a smaller volume fails the init container or the Job with ENOSPC), and `lg` plus `md` (after a change of `modelSize`) needs more still. |
| `workers.nlp.models.persistence.accessModes` | `[ReadWriteMany]` | Replicas on any node share the volume. With `ReadWriteOnce` keep the replicas on one node. |
| `workers.nlp.models.persistence.storageClass` | `""` | Cluster default when empty. |
| `workers.nlp.models.persistence.existingClaim` | `""` | Use this PVC instead of creating one. |
| `networkPolicy.enabled` | `false` | Needs a CNI that enforces NetworkPolicy. |
| `networkPolicy.metricsFrom` | `[{podSelector: {}}]` | Peers (`from` entries) allowed to scrape port 9090: by default every pod of the release namespace. |
| `service.type` | `ClusterIP` | Type of the public Service. |
| `service.port` | `8080` | Public Service port (named `http`). |
| `ingress.enabled` | `false` | The Ingress targets the public Service port only. |
| `ingress.className` | `""` | |
| `ingress.annotations` | `{}` | The controller's settings. A sync request waits up to 120 s without `wait_seconds` (150 s with unblock mode and requests that combine it with `pii` and LLM work, see item 4 of [docs/handover.md](../../../docs/handover.md); with MCP or the playground at least `MCP_WAIT_SECONDS` + 70 s too), and uploads reach `DOCUMENT_MAX_BYTES` + 192 KiB (`workers.asr.uploadMaxBytes` for media). For ingress-nginx, which cuts at 60 s and 1m by default: `nginx.ingress.kubernetes.io/proxy-read-timeout: "120"`, `nginx.ingress.kubernetes.io/proxy-send-timeout: "120"` (both `"150"` with unblock mode) and `nginx.ingress.kubernetes.io/proxy-body-size: "40m"`, as in [values-production.example.yaml](values-production.example.yaml). |
| `ingress.hosts` | one example host, path `/` | List of `host` with `paths` (`path`, `pathType`). |
| `ingress.tls` | `[]` | |
| `valkey.enabled` | `true` | Deploy a single-replica Valkey. |
| `valkey.image.repository` / `tag` / `pullPolicy` | `valkey/valkey` / `8.1-alpine` / `IfNotPresent` | |
| `valkey.auth.enabled` | `true` | Valkey requires a password. |
| `valkey.auth.existingSecret` | `""` | Your own Secret with the password; without it the chart generates one. The password must be URL-safe, see [Valkey password](#valkey-password). |
| `valkey.auth.existingSecretKey` | `VALKEY_PASSWORD` | Key inside that Secret (also used for the chart-managed one). |
| `valkey.maxmemory` | `768mb` | `--maxmemory`. The eviction policy is fixed to `noeviction`. Size it for the batches open at once (about `BATCH_MAX_URLS` × the average result, plus as much in the result cache) and, with Docling workers, for the documents queued for OCR and slow uploads, at most `DOCLING_INPUT_BUDGET_BYTES` (200000000) in all; from `VALKEY_MEMORY_GUARD_RATIO` (0.8) of it the API refuses new batches and nothing more is queued for OCR. With Docling workers and the default maxmemory, the install notes say so. |
| `valkey.persistence.enabled` | `false` | `false`: `emptyDir`, data (jobs, queued items, counters) is lost on restart. `true`: PVC and append-only file. |
| `valkey.persistence.size` | `1Gi` | |
| `valkey.persistence.storageClass` | `""` | Cluster default when empty. |
| `valkey.persistence.existingClaim` | `""` | Use this PVC instead of creating one. |
| `valkey.resources` | requests = limits: 500m / 1280Mi | Guaranteed QoS; reserves 1.25Gi and half a CPU. `valkey.maxmemory` is 60 % of the memory limit: raise both together. The load test used far less (docs/configuration.md, "Resource baselines"): size it for the results and documents held at once. |
| `externalValkey.url` | `""` | Valkey or Redis URL used when `valkey.enabled` is `false`. No password in here. |
| `externalValkey.existingSecret.name` | `""` | Secret holding a `VALKEY_URL` that carries a password (percent-encoded where needed). Takes precedence over `url`. |
| `externalValkey.existingSecret.key` | `VALKEY_URL` | Key inside that Secret. |
| `metrics.serviceMonitor.enabled` | `false` | Needs the Prometheus Operator CRDs. Scrapes the API and worker internal Services. |
| `metrics.serviceMonitor.interval` / `scrapeTimeout` | `30s` / `10s` | |
| `metrics.serviceMonitor.labels` | `{}` | For example the `release` label your Prometheus selects on. |
| `metrics.prometheusRule.enabled` | `false` | Needs the Prometheus Operator CRDs and kube-state-metrics. |
| `metrics.prometheusRule.labels` | `{}` | |
| `alerts.queueBacklogItems` | `1000` | Threshold of `TextExtractionQueueBacklog` for the pools `extract` and `browser`. |
| `alerts.doclingQueueBacklogItems` | `100` | Threshold of `TextExtractionQueueBacklog` for the pool `docling` (an item takes minutes). |
| `alerts.asrQueueBacklogItems` | `50` | Threshold of `TextExtractionQueueBacklog` for the pool `asr` (an item takes up to hours). |
| `alerts.nlpQueueBacklogItems` | `200` | Threshold of `TextExtractionQueueBacklog` for the pool `nlp` (an item takes seconds). |
| `alerts.llmQueueBacklogItems` | `100` | Threshold of `TextExtractionQueueBacklog` for the pool `llm` (an item takes seconds to minutes). |
| `alerts.valkeyMemoryRatio` | `0.8` | Threshold of `TextExtractionValkeyMemoryHigh` (share of Valkey's maxmemory). Keep it at or below `config.VALKEY_MEMORY_GUARD_RATIO`. |
| `podAnnotations`, `nodeSelector`, `tolerations`, `affinity` | empty | Applied to the API pods. Workers (extract, browser, docling, asr, nlp, llm) and the models Jobs get `podAnnotations`, `nodeSelector` (merged with `workers.<pool>.nodeSelector`) and `tolerations`; Valkey gets only `nodeSelector` and `tolerations`. `affinity` is for the API pods only. |

With `valkey.enabled=false`, the render fails unless `externalValkey.url` or
`externalValkey.existingSecret.name` is set.

## Alerts

| Alert | Expression | For |
| --- | --- | --- |
| `TextExtractionDown` | `sum(kube_deployment_status_replicas_available{...}) or vector(0)` is below 1 | 5 m |
| `TextExtractionHigh5xx` | share of `http_requests_total{status="5xx"}` above 5 % | 10 m |
| `TextExtractionValkeyMemoryHigh` | `max(text_extraction_valkey_used_memory_bytes) / max(text_extraction_valkey_max_memory_bytes > 0)` over the API and worker pods, above `alerts.valkeyMemoryRatio` (0.8); no alert without a maxmemory | 10 m |
| `TextExtractionQueueBacklog` (label `pool`) | per pool: `max(text_extraction_queue_pending_items{pool="<pool>"})` above the pool's backlog threshold (below the table) while `sum(increase(text_extraction_job_items_total{<the pool's workers>, outcome!~"deferred\|lost\|escalated"}[15m])) or vector(0)` is 0 | 15 m |
| `TextExtractionWorkersDown` (label `pool`) | per pool: `max(text_extraction_queue_pending_items{pool="<pool>"}) > 0` and no available replica of the pool's worker Deployment (`kube_deployment_status_replicas_available`, `or vector(0)`) | 5 m |
| `TextExtractionJobItemFailures` | failed share of `text_extraction_job_items_total` above 50 %: the numerator leaves out `success`, `partial` (every partial result) and `deferred`/`lost`/`escalated`; the denominator is every item that recorded a result (all but `deferred`/`lost`/`escalated`; an escalated item records its result in the browser pool) | 15 m |
| `TextExtractionQueueMetricsStale` | `time() - (max(text_extraction_queue_metrics_last_success_timestamp_seconds) or vector(0)) > 120` | 5 m |

The backlog thresholds are `alerts.queueBacklogItems` (1000) for `extract` and `browser`,
`alerts.doclingQueueBacklogItems` (100), `alerts.asrQueueBacklogItems` (50) and
`alerts.nlpQueueBacklogItems` (200) and `alerts.llmQueueBacklogItems` (100).

The job alerts are rendered while jobs are enabled (`JOBS_ENABLED=true` on the API pods);
`TextExtractionWorkersDown` also needs the pool's `workers.<pool>.enabled`, and the alerts of the
pools `browser`, `docling`, `asr`, `nlp` and `llm` need `workers.browser.enabled`, `workers.docling.enabled`,
`workers.asr.enabled`, `workers.nlp.enabled` or `workers.llm.enabled` (without it there is no such queue). For the pools `docling`, `asr`, `nlp` and `llm` the two
alerts are the backlog and "workers down" (for example models missing on the volume: the workers
exit before `/ready`). The item counter has no `pool` label, so the pools are told apart by the
scrape target: the browser, docling, asr, nlp and llm pools count the series of their workers' internal
Service, the extract pool all others. The queue gauges come from the API
pods and every pod reports the same queue, so the rules use `max()`, never `sum()`. The counter
`text_extraction_job_items_total` appears with the first item a worker handles and the kube-state
series can be missing, so the rules use `or vector(0)` where "nothing yet" has to count as 0:
a lazy series must not keep an alert from firing. `TextExtractionQueueMetricsStale` also fires when
no API pod has ever read the queue (the series is exported from the first successful read on).

`TextExtractionDown` needs kube-state-metrics. It uses the Deployment's available replicas rather
than `absent(up{...})`: `up` only says that a scrape target answers, not that a pod is ready, and
the series disappears with the pod. The `or vector(0)` branch makes the alert fire when the
series is missing too, so it also fires if kube-state-metrics itself is down.

`TextExtractionHigh5xx` relies on the label values emitted by prometheus-fastapi-instrumentator,
which groups `status` as `2xx`, `4xx` and `5xx`. It selects the series by the ServiceMonitor target
labels `namespace` and `service` (the internal Service), so it works only with the ServiceMonitor
enabled. Only 5xx answers from the public API count; a target failure is a `200` with
`status: failed` and does not raise this alert.

## Network policies

With `networkPolicy.enabled` the chart creates a policy for Valkey, the API and each enabled worker
pool, all for ingress only (the API and the workers must reach arbitrary sites, so egress stays
open; that also covers the model downloads, the external ASR endpoint, which the ASR workers
call directly, and the LLM endpoint, which the llm workers call):

- Valkey accepts port 6379 from the API and worker pods (extract, browser, docling, asr, nlp, llm) of this
  release only.
- The API accepts port 8080 from anywhere and port 9090 from `networkPolicy.metricsFrom`.
- The extract, browser, Docling, ASR, NLP and llm workers accept port 9090 from `networkPolicy.metricsFrom`
  only.

`metricsFrom` is a list of NetworkPolicy peers, by default every pod of the release namespace. For a
Prometheus in another namespace add for example
`{namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: monitoring}}}`. Probes of the
kubelet come from the node, which CNIs normally allow. With the default `metricsFrom`, a Prometheus
in another namespace is cut off from port 9090 and KEDA and the alerts lose their data; NOTES.txt
warns when `networkPolicy` and the ServiceMonitor are both on with the default.

## Docling workers and the models volume

OCR is off by default (`workers.docling.enabled: false`): the Docling image is about 2 GB and the
models take about 335 MiB on a volume. With it on:

- The PVC `<release>-text-extraction-docling-models` (or `models.persistence.existingClaim`) is
  mounted at `/models` (`DOCLING_MODELS_PATH`): Docling's layout and table models in `docling/`,
  Tesseract's data in `tessdata/` (`TESSDATA_PREFIX`). The image holds no models.
- The init container `models` runs `text-extraction-docling-worker --download-models` with the
  volume mounted writable. It downloads the files of the manifest
  (`src/text_extraction/documents/models.toml`, pinned URLs), checks each SHA-256, moves them into
  place and writes a marker; with the marker present it does nothing. It needs egress to
  `raw.githubusercontent.com` and `huggingface.co` (or an `HTTPS_PROXY` in `config`). Init
  containers of several replicas that start at once each stage their own copy; one may fail and
  restart, then it finds the marker.
- The worker mounts the volume read-only. Before `/ready` it checks the files and converts a
  built-in scan; without the models it exits with code 1 and logs `docling_models_missing` with
  the path.
- Air-gapped: fill a volume with `--download-models` elsewhere (or copy a filled one), then set
  `models.persistence.existingClaim` and `models.download: false`.
- The volume must be writable by uid/gid 10001 for the download (`fsGroup` 10001 covers most
  volume types; some NFS provisioners ignore it).

## ASR workers and the models volume

Transcription is off by default (`workers.asr.enabled: false`). With it on, `transcribe: true` on
media without subtitles and every audio or video upload become jobs of the pool `asr`; nothing
waits for them, so the API pods keep their grace period.

- Local backend (default): the PVC `<release>-text-extraction-asr-models` (or
  `models.persistence.existingClaim`) is mounted at `/models` (`ASR_MODELS_PATH`), one folder per
  model; only `workers.asr.model` is downloaded (from `huggingface.co` at pinned commits, each file
  SHA-256 checked, manifest `src/text_extraction/media/models.toml`). The init container `models`
  (or, with `models.downloadMode: job`, the hook Job) runs `text-extraction-asr-worker
  --download-models` with the volume writable; the worker mounts it read-only. A worker without the
  model exits with code 1 and logs `asr_models_missing`. Air-gapped: fill a volume elsewhere, then
  set `models.persistence.existingClaim` and `models.download: false`.
- External backend (`backend: openai`): no models volume, no download. The worker posts 16 kHz mono
  Opus chunks to `external.baseUrl` with the caller's `X-ASR-Api-Key`, else
  `ASR_EXTERNAL_API_KEY` (`external.apiKey` or `external.existingSecret`), which the API pods get
  too.
- Downloaded media go to an emptyDir at `/scratch` (`ASR_SCRATCH_PATH`) bounded by
  `scratch.sizeLimit` (2Gi): Kubernetes evicts a pod that writes more. `concurrency` ×
  (`MEDIA_MAX_BYTES` + 5 MB) must fit, which the render checks.
- Before `/ready` each child checks that PyAV's FFmpeg is an LGPL build, that neither `ffmpeg` nor
  `rtmpdump` is on `PATH`, and transcribes a built-in clip (external backend: encodes it as Opus).
  The image is built for exactly that (PyAV against BtbN's LGPL FFmpeg, no FFmpeg programs).
- The length limits of decision D6 come from `config` (service defaults:
  `MEDIA_MAX_DURATION_SECONDS` 7200, `ASR_TIMEOUT_SECONDS` 10800, `ASR_INPUT_TTL_SECONDS` 28800);
  the render fails when the input TTL does not outlast the lease (2 × `ASR_TIMEOUT_SECONDS` + 10).
  Upload bytes wait in Valkey for up to `ASR_INPUT_TTL_SECONDS` and count in
  `DOCLING_INPUT_BUDGET_BYTES`: size `valkey.maxmemory` for it.
- **Removing a pod interrupts its transcriptions.** A scale-down, a rolling update (`helm
  upgrade` with a changed pod template) or a node drain sends SIGTERM: the transcriptions in flight
  get 30 s, then their items go back to the queue at once (`attempts` + 1) and start again from the
  beginning on another replica; with `backend: openai` the endpoint bills them again. An item
  interrupted `JOB_ITEM_MAX_ATTEMPTS` times fails as lost. Kubernetes does not pick the idle pod
  when it scales down. So: for steady load prefer fixed replicas (both scalers off,
  `replicaCount`) and upgrade when the queue is quiet. With a scaler, KEDA counts the items in
  flight as well (`countLeased`, threshold = `concurrency`), so it does not scale below the
  transcriptions running, and both scalers wait `scaleDownStabilizationWindowSeconds` (3600, the
  Kubernetes maximum) of lower load before they remove a replica.

## NLP workers and the models volume

PII handling is off by default (`workers.nlp.enabled: false`). With it on, a request with `pii`
has its result analysed by the pool `nlp` (personal data found with Presidio and spaCy, then
masked, redacted, hashed or annotated), and every pod gets `PII_ENABLED=true`; without the
workers such a request answers 400. The API pods keep their grace period: a sync request waits
`PII_SYNC_WAIT_SECONDS` (20) for a worker, then it becomes a job.

- The PVC `<release>-text-extraction-nlp-models` (or `models.persistence.existingClaim`) is
  mounted at `/models` (`PII_MODELS_PATH`), one folder per spaCy model. Only the German and
  English models of `workers.nlp.modelSize` (`lg` or `md`) are downloaded, from the
  explosion/spacy-models releases on `github.com` (which redirects to `githubusercontent.com`;
  pinned URLs, size and SHA-256 checked, manifest `src/text_extraction/pii/models.toml`). The init container `models` (or, with
  `models.downloadMode: job`, the hook Job) runs `text-extraction-nlp-worker --download-models`
  with the volume writable; the worker mounts it read-only. The image holds no models.
- Before `/ready` each child loads both models and analyses a built-in German and English
  sentence; a worker without the models exits with code 1 and logs `nlp_models_missing` with the
  path. Air-gapped: fill a volume elsewhere, then set `models.persistence.existingClaim` and
  `models.download: false`.
- `workers.nlp.hashKey.existingSecret` names a Secret with `PII_HASH_KEY` (at least 32 bytes) for
  `mode: hash`; only the NLP worker container gets it. Without it each result is hashed with a
  random key. Changing it changes every hash. The checksum annotation of the pods does not cover
  your own Secret: after rotating `PII_HASH_KEY` in it, restart the NLP worker pods
  (`kubectl rollout restart`).
- The text of a result never reaches a log, a metric label or the ConfigMap; the workers count
  found entities by type only.

## llm workers

The LLM is off by default (`workers.llm.enabled: false`). With it on, a request with `structured`
(strategy `llm` or `auto`, or `learn_selectors`), `chunking` `semantic` or `describe_images` has its
last hop run in the pool `llm` (the page and its images are data for the model: no tool calls, its
answer is validated against the caller's schema or the description's), and every pod gets
`LLM_ENABLED=true`; without the workers such a request answers 400. A sync request waits `LLM_SYNC_WAIT_SECONDS` (30) for a worker, then it becomes a job (202).

- The workers run the API image (`text-extraction-llm-worker` as its args): no model, no volume,
  no init container. They call `workers.llm.baseUrl` (`/chat/completions`, and `/embeddings` with
  `workers.llm.embeddingModel`); the endpoint is the operator's, so it is exempt from the SSRF
  guard. Egress stays open, as for the other workers.
- For `describe_images` the workers also fetch a page's images (at most `max_images`, 5 by
  default, each at most `config.LLM_VISION_MAX_IMAGE_BYTES`) through the SSRF guard, the
  compliance checks of the request and the per-domain outbound limits, with the service's user
  agent, and send them to the endpoint with `workers.llm.visionModel` (empty: `workers.llm.model`,
  which then must take images). An image upload's bytes wait for them as the job's input, within
  `config.DOCLING_INPUT_BUDGET_BYTES`.
- `workers.llm.apiKey.existingSecret` names a Secret with `LLM_API_KEY`; the API pods and the llm
  worker container get it (the chart never renders it as a literal, and creates no Secret). A
  caller's `X-LLM-Api-Key` wins over it. The checksum annotation of the pods does not cover your
  own Secret: after rotating the key, restart the API and llm worker pods (`kubectl rollout
  restart`).
- A worker leases an item for `5.5 x LLM_TIMEOUT_SECONDS + 10` s (670 s by default: the longest
  deadline, of an item with image descriptions, plus one timeout). On SIGTERM it
  stops claiming and gives items in flight `workers.llm.gracefulSeconds` (30 s), then hands the
  ones still running back to the queue at once. Calls to the endpoint are billed again when an
  item runs twice; with the default 120 s `LLM_TIMEOUT_SECONDS` every rollout can do that.
  Raise `workers.llm.gracefulSeconds` and `workers.llm.terminationGracePeriodSeconds` together
  (e.g. 130 and 140) to let calls in flight end.
- Do not set `OPENAI_CUSTOM_HEADERS` or `OPENAI_LOG` (under `config` or otherwise): the OpenAI
  SDK would read them, so with `workers.llm.enabled` (or `workers.asr.enabled` with `workers.asr.backend: openai`) every pod
  refuses to start (exit code 2). `OPENAI_ORG_ID` and `OPENAI_PROJECT_ID` are never sent to the
  endpoint.
- `kubectl exec <llm worker pod> -- text-extraction-llm-worker --check` sends one tiny extraction
  to the endpoint and exits 0 when its answer fits the schema (an operator's smoke test).
- A prompt, a page text, an extracted value, an image, its URL or description, or a key never
  reaches a log, a metric label or the ConfigMap; the workers count calls and tokens by outcome
  only, images by outcome (`text_extraction_vision_images_total`).

## MCP and playground

`mcp.enabled=true` serves an MCP server at `/mcp` and `ui.enabled=true` a playground at `/ui`, both
on the public port 8080 of the API pods: no Deployment, Service or ingress path of their own, the
API's keys, roles, rate limits and scaling. Both are off by default; the install notes print their
URLs while they are on. Their settings are the `config.MCP_*` and `config.UI_*` entries
(docs/configuration.md, "MCP" and "UI").

- **Access.** Both use the API keys of `secrets.apiKeys`: an MCP client sends one as
  `Authorization: Bearer <key>`, and the playground's login takes one. Under `config.AUTH_MODE=none`
  anyone who can reach the service can use the API, the MCP endpoint and the playground. For a
  shared deployment keep `ui.enabled=false` or use `config.AUTH_MODE=api_key`; the install notes
  warn when `ui.enabled` goes with `AUTH_MODE=none`.
- **The ingress** already routes `/` to the public Service, so `/mcp` and `/ui` need no paths of
  their own. Its timeouts and body size must fit them:
  - a read and send timeout of at least 120 s (150 s with unblock mode and requests that combine
    it with `pii` and LLM work, see item 4 of [docs/handover.md](../../../docs/handover.md)), or
    `MCP_WAIT_SECONDS` + 70 s if larger (95 s with the default 25 s): a tool call or a playground
    form waits up to `MCP_WAIT_SECONDS` (the forms 25 s) plus the route's own hand-over time
    before it answers a job;
  - a body size of at least the largest upload: `/mcp` takes a file as base64,
    `ceil(DOCUMENT_MAX_BYTES / 3) x 4 + 64 KiB` (about 27 MB by default, about 33 MB with ASR
    workers, where `workers.asr.uploadMaxBytes` sets the upload limit), the playground's file form
    `DOCUMENT_MAX_BYTES` + 192 KiB and its batch-file form `UI_BATCH_FILE_MAX_BYTES` + 64 KiB.

  For ingress-nginx, for example:

  ```yaml
  ingress:
    enabled: true
    annotations:
      nginx.ingress.kubernetes.io/proxy-read-timeout: "120"
      nginx.ingress.kubernetes.io/proxy-send-timeout: "120"
      nginx.ingress.kubernetes.io/proxy-body-size: "40m"
  ```
- **Logs.** A playground call logs two access lines under one trace id: the outer `/ui/run/...`
  line without a principal and the inner `/v1/...` line with it. An MCP call logs its access line
  and one `mcp_tool_call` line (tool, outcome, duration, job id); tool arguments are never logged.

## Local checks

```bash
docker run --rm -v "$PWD/deploy/helm:/apps" -w /apps alpine/helm:3.19.0 lint --strict text-extraction
docker run --rm -v "$PWD/deploy/helm:/apps" -w /apps alpine/helm:3.19.0 lint --strict text-extraction --set workers.docling.enabled=true
docker run --rm -v "$PWD/deploy/helm:/apps" -w /apps alpine/helm:3.19.0 lint --strict text-extraction --set workers.asr.enabled=true
docker run --rm -v "$PWD/deploy/helm:/apps" -w /apps alpine/helm:3.19.0 lint --strict text-extraction --set workers.nlp.enabled=true
docker run --rm -v "$PWD/deploy/helm:/apps" -w /apps alpine/helm:3.19.0 lint --strict text-extraction --set workers.llm.enabled=true
docker run --rm -v "$PWD/deploy/helm:/apps" -w /apps alpine/helm:3.19.0 lint --strict text-extraction --set mcp.enabled=true --set ui.enabled=true
docker run --rm -v "$PWD/deploy/helm:/apps" -w /apps alpine/helm:3.19.0 lint --strict text-extraction -f text-extraction/values-production.example.yaml
docker run --rm -v "$PWD/deploy/helm:/apps" -w /apps alpine/helm:3.19.0 template rel text-extraction
deploy/helm/test.sh   # render checks (needs helm on PATH, or HELM=/path/to/helm)
```
