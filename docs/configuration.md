# Configuration

Every setting is an environment variable with a default. Variable names are case-insensitive.
[`.env.example`](../.env.example) lists them for docker compose, and the Helm
chart README has the [full values table](../deploy/helm/text-extraction/README.md#values).

## Environment variables

Non-secret settings. In Helm, put a variable under `config.<NAME>` in the values; it lands in the
chart's ConfigMap with the same name (values are strings). The chart overrides `PUBLIC_PORT`,
`INTERNAL_PORT` and `VALKEY_URL`, `JOBS_ENABLED` on the API pods (value `jobs.enabled`),
`WORKER_CONCURRENCY` on the extract workers (value `workers.extract.concurrency`),
`BROWSER_CONCURRENCY` on the browser workers (value `workers.browser.concurrency`), and
`BROWSER_ENABLED` and `BROWSER_UNBLOCK_ENABLED` on every pod (values `workers.browser.enabled` and
`workers.browser.unblock`); values for them under `config` are ignored.

| Variable | Default | Meaning |
| --- | --- | --- |
| `PUBLIC_HOST` | `0.0.0.0` | Bind address of the public API. |
| `PUBLIC_PORT` | `8080` | Port of the public API. Fixed to 8080 by the Helm chart. |
| `INTERNAL_HOST` | `0.0.0.0` | Bind address of the internal listener. |
| `INTERNAL_PORT` | `9090` | Port of the internal listener (`/health`, `/ready`, `/metrics`). Fixed to 9090 by the Helm chart. Workers serve it too: an API and a worker run on one host (outside compose and Helm, which give each its own container) need distinct values, e.g. `INTERNAL_PORT=9091` for the worker. |
| `LOG_LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` (case-insensitive). Anything else makes the service exit with code 2. |
| `LOG_FORMAT` | `json` | `json` (stdout, one object per line) or `console` (plain text for local development). |
| `AUTH_MODE` | `api_key` | `api_key` requires a key on every public request; `none` disables authentication and gives every caller the `admin` role. With `api_key` the service exits with code 2 at start-up when no key is configured. |
| `RATE_LIMIT_ENABLED` | `true` | Switch the inbound rate limit on or off. |
| `RATE_LIMIT_PER_MINUTE` | `60` | Default requests per minute per API key. A key can override it with `rate_limit_per_minute`. |
| `REQUEST_MAX_BODY_BYTES` | `65536` | Largest request body the public API accepts, in bytes. A larger one answers 413 `invalid_request` before it is read whole, parsed or authenticated: at once when its `Content-Length` says so, else when the bytes read pass the limit. `POST /v1/batch` has its own bound instead, see `BATCH_MAX_URLS`, and so have `POST /v1/extract/file` (`DOCUMENT_MAX_BYTES` + 192 KiB: the file, the form fields `structured` and `chunking` of up to 64 KiB each and the other fields of up to 1 KiB), `POST /v1/chunk` (`DOCUMENT_MAX_BYTES` + 64 KiB), `/mcp` (see MCP) and some `/ui/run` routes (see UI). The internal port has no body limit (it has no route that takes a body). Minimum 1024. |
| `VALKEY_URL` | `redis://localhost:6379/0` | Valkey (Redis protocol) that holds all shared state: rate-limit counters, the per-host outbound state, the compliance file cache, cached results and fingerprints, jobs and the job queue. Needs Valkey 7.2+ or Redis 7.0+ (the queue scripts use script flags). A secret when it carries a password, see below. |
| `VALKEY_MEMORY_GUARD_RATIO` | `0.8` | Share of Valkey's `maxmemory` from which the API pods take on no new bulk: `POST /v1/batch` and `POST /v1/site-structure` answer 503 `internal_error` (retryable), a running site job appends no further pages or sitemaps (warning `site_memory_guard`), a slow `POST /v1/extract` with `wait_seconds` is not handed over to a job and keeps waiting inline (200), and the result cache skips its writes, on the API pods and on the workers. Workers keep storing the results of accepted jobs in the job store. Each API pod and each worker reads `INFO memory` every 5 s (API pods also with `JOBS_ENABLED=false`); with `maxmemory` 0 there is no guard. While the read fails the last verdict stays. 0.1 to 1.0. |
| `FETCH_TIMEOUT_SECONDS` | `20` | Total time budget of one outbound fetch, in seconds. Minimum 1 (the least time a target fetch is started with). |
| `FETCH_MAX_BYTES` | `20000000` | Largest response body the fetcher accepts, in bytes. Minimum 1024. |
| `FETCH_MAX_REDIRECTS` | `5` | Redirects followed per fetch (0 to 20). Every hop passes the SSRF guard. |
| `FETCH_ALLOW_PRIVATE_NETWORKS` | `false` | **Unsafe.** `true` switches the SSRF guard off for private, loopback and link-local addresses. For local development and tests only; never set it in a deployment. |
| `OUTBOUND_MAX_CONCURRENCY_PER_HOST` | `2` | Requests in flight to one host at the same time, across all pods. Minimum 1. |
| `OUTBOUND_MIN_DELAY_MS` | `1000` | Least gap between the starts of two requests to one host, in milliseconds. 0 switches the spacing off. |
| `OUTBOUND_MAX_CRAWL_DELAY_SECONDS` | `10` | Upper limit for the `Crawl-delay` of a robots.txt, in seconds. A longer value is cut to this. |
| `OUTBOUND_MAX_WAIT_SECONDS` | `10` | Longest time a request waits for its turn at a host. Longer waits are refused as `OutboundBusy`. |
| `OUTBOUND_BACKOFF_SECONDS` | `30` | Pause for a host after 429 or 503 without a usable `Retry-After`. Minimum 1. |
| `OUTBOUND_MAX_BACKOFF_SECONDS` | `300` | Upper limit for any pause after 429 or 503, including `Retry-After`. Minimum 1. |
| `CIRCUIT_FAILURE_THRESHOLD` | `5` | Consecutive connection failures or 5xx answers from a host that open its circuit. Minimum 1. |
| `CIRCUIT_OPEN_SECONDS` | `60` | How long an open circuit refuses requests to the host. Minimum 1. |
| `OUTBOUND_MAX_CONNECTIONS` | `200` | Open outbound connections of one pod at most, over all hosts (the fetcher's connection pool). Minimum 1. |
| `OUTBOUND_MAX_KEEPALIVE` | `50` | Idle outbound connections a pod keeps for reuse; an idle connection is closed after 30 s. Minimum 0. |
| `COMPLIANCE_DEFAULT` | `enforce` | Compliance mode when a request does not choose one: `enforce` blocks what robots.txt, TDMRep, ai.txt or `noai` forbid; `report` only reports it; `ignore` skips the checks. |
| `COMPLIANCE_ALLOW_OVERRIDE` | `true` | Whether non-admin callers may choose `enforce` or `report` per request. `ignore` stays admin-only (unless it is the default). Admins may always choose. A refused choice answers 403 `forbidden`. |
| `COMPLIANCE_CACHE_SECONDS` | `3600` | How long robots.txt, `tdmrep.json` and ai.txt of an origin are cached in Valkey. 0 switches the cache off. A file that could not be fetched (5xx, 429, network error) is cached at most 300 s, so a short outage does not block a site for the whole period. |
| `QUALITY_MIN_WORDS` | `50` | Fewest words (tokens with a letter or digit) the extracted content must have. Fewer ends as `partial` with `content_too_short`; no words at all is `failed` with `empty_content`. 0 switches the length check off. |
| `RESULT_CACHE_TTL_SECONDS` | `3600` | How long a stored extraction result counts as fresh and is served without asking the site, measured from the time it was produced. 0 means a stored result is never served unchecked; it can still be revalidated. A fresh hit skips the compliance checks too: it carries the compliance report of the extraction that produced it, so a new robots.txt or TDM opt-out reaches cached results only after up to `RESULT_CACHE_TTL_SECONDS` plus `COMPLIANCE_CACHE_SECONDS`. Setting this above `COMPLIANCE_CACHE_SECONDS` lengthens that lag. |
| `RESULT_STORE_TTL_SECONDS` | `86400` | How long a result stays in Valkey so a stale one can be revalidated with ETag/Last-Modified or the raw-content hash instead of being extracted again. 0 stores no results. Results over 2 MB as JSON are never stored. |
| `FINGERPRINT_TTL_SECONDS` | `2592000` | How long the last text fingerprint of a URL is kept for change detection (`changed`, `similarity_to_previous`). 30 days by default. 0 stores no fingerprints, so nothing is reported as changed. |
| `CONVERT_MAX_BYTES` | `5242880` | Largest HTML or text body that is converted, in bytes (5 MiB; minimum 1024). A larger page fails with `too_large` (not retryable) before it is decoded. `FETCH_MAX_BYTES` stays the download limit. |
| `CONVERT_CONCURRENCY` | `2` | Conversions (decoding, HTML conversion, metadata and licences, fingerprint) running at once in one pod, on a thread pool of their own; DNS lookups keep the default pool. A request waits for a free slot within its `EXTRACT_TIMEOUT_SECONDS`, else it fails with `timeout`. Python cannot stop a running thread: a conversion that outlives its request runs to its end and holds its slot until then, so this bound is what limits CPU and memory. Such a conversion also holds the shutdown up, because Python joins its worker threads when the process exits. That is usually seconds, but the worst case can run to the end of the grace period, where the pod is killed (Trafilatura is quadratic on repeated paragraphs: one 5.7 MB page took 125 s). That is harmless, because pods are stateless: the request it served has already been answered `timeout`. Peak memory is roughly `CONVERT_CONCURRENCY` times the peak of one page (about 450 MB for a 5 MB page), see the Helm memory limit. Minimum 1. |
| `EXTRACT_TIMEOUT_SECONDS` | `25` | Time budget of one `POST /v1/extract`, from the compliance checks to the finished result, in seconds. Minimum 2, so that the fetch can always get its 1 s minimum. The fetch gets at most the time that is left minus 0.25 s (and never more than `FETCH_TIMEOUT_SECONDS`); with less than 1 s left when the host's turn comes, the target is not fetched. Running out answers `status: failed` with `timeout` (retryable). A fetch timeout counts against the host's circuit breaker only when the fetch had at least half of `FETCH_TIMEOUT_SECONDS` once the outbound gate let its request go (after a redirect: what was left of the chain's `FETCH_TIMEOUT_SECONDS` when the hop's request passed the gate); a long wait at the gate followed by a slow host does not count. The graceful shutdown lasts `max(30, EXTRACT_TIMEOUT_SECONDS + 5)` seconds (30 by default) with `BROWSER_ENABLED=false`; with it on, at least `EXTRACT_TIMEOUT_SECONDS + BROWSER_QUEUE_WAIT_SECONDS + render deadline + 10` seconds, because a sync request may wait for a browser after its fast phase (75 s by default, 105 s with unblock mode; see `BROWSER_ENABLED` and Response times); the Helm chart derives the API pods' `terminationGracePeriodSeconds` from it when that value is unset, and a value you set must cover it (see Response times). After it, the lifespan shutdown waits for extractions that became a job (`wait_seconds`) for up to `EXTRACT_TIMEOUT_SECONDS` + 7 s (5 s of grace after the deadline, then it cancels them and allows 2 s for their writes); see Response times. |
| `JOB_TTL_SECONDS` | `86400` | How long a job (its status, URL list and results) stays in Valkey, in seconds. The time starts at creation and starts again when the job completes, so a caller has this long to fetch the results of a finished job. Minimum 60. |
| `JOB_ITEM_MAX_BYTES` | `2000000` | Largest stored result of one job item, as UTF-8 JSON. A larger result is stored without its `content` (`data.content` is null and `data.warnings` has `content_not_stored_too_large`). If it is still larger, it is stored without its `chunks` and its `structured.tables` too (both null, with the warnings `chunks_not_stored_too_large` and `tables_not_stored_too_large`): chunks repeat the content, with `overlap` many times over. What is left (the structured values, metadata, links) is stored as it is, so an item may still be somewhat larger; size Valkey from this setting with that margin. A result that moves to the llm worker as a draft and loses its content this way cannot get its LLM work or `semantic` chunks: it answers `partial`/`llm_failed`, not retryable, "The document is too large for the LLM step." This also bounds synchronous answers that a browser worker rendered (`mode: browser`, screenshots, browser options, an escalated `auto` request): their result travels through a job item, so a page whose result is larger answers without its content, where fast mode would return it whole. Such a result is never put in the result cache. It bounds the result a request or job with `pii` hands to the NLP worker too (its draft): a larger one loses its content before the NLP worker sees it, and answers `success` with `data.content` null and the warning `content_not_stored_too_large` (its other fields processed), not `too_large`; see `PII_MAX_CHARS`. Minimum 10000. |
| `JOB_ITEM_MAX_ATTEMPTS` | `5` | How often one job item may lose its worker lease (the worker died or hung past the lease) or be deferred (its host was paused or busy) before it fails. The counter covers both; a lost lease beyond it fails the item with `internal_error` (retryable). Minimum 1. |
| `SCHEDULER_CLAIM_SCAN_BUDGET` | `200` | Client-domain pairs one claim of the job queue looks at, at most. A claim that finds nothing within the budget answers "try again in 0.05 s", and the next claim goes on where it stopped (a client whose domains the budget ran out in moves to the end of the rotation), so fairness holds across claims. Workers back off from 0.05 s up to 1 s while such answers repeat. It bounds the time one claim blocks Valkey when many domains are deferred or at their limit. Minimum 1, maximum 5000. |
| `WORKER_CONCURRENCY` | `8` | Job items one extract worker pod (`text-extraction-worker`) processes at once. Conversions are still capped by `CONVERT_CONCURRENCY` in that pod, and requests to one host by `OUTBOUND_MAX_CONCURRENCY_PER_HOST` across all pods. Minimum 1. Helm: `workers.extract.concurrency`. |
| `BATCH_MAX_URLS` | `1000` | Most URLs one `POST /v1/batch` may carry. More answers 422 `invalid_request`. A body over `BATCH_MAX_URLS` × (`MAX_URL_LENGTH` (2048) + 8) bytes plus 4 KiB answers 413 `invalid_request` before it is read whole or parsed (see `REQUEST_MAX_BODY_BYTES`). Minimum 1, maximum 100000. |
| `BATCH_MAX_OPEN_PER_CLIENT` | `5` | Batches and site jobs (`POST /v1/site-structure`) one API key may have unfinished (queued or running) at a time, both kinds counted together. The next `POST /v1/batch` or `POST /v1/site-structure` answers 429 `rate_limited` (retryable) until one completes or expires. With `AUTH_MODE=none` all callers share one anonymous owner, so the limit applies to the whole deployment, and every caller can read every job. Minimum 1. |
| `JOBS_ENABLED` | `true` | `false` makes `POST /v1/batch`, `POST /v1/site-structure`, `GET /v1/jobs/{id}` and `DELETE /v1/jobs/{id}` answer 503 `internal_error` ("Jobs are disabled on this deployment."), for API pods without workers. `POST /v1/extract` then ignores `wait_seconds` and answers when the extraction ends. Screenshots are still taken, but their files cannot be downloaded (the artifact route is a job route), so keep jobs on with browser mode. Helm: `jobs.enabled`; unset, the chart renders `true` while `workers.extract.enabled` is true and `false` when the extract workers are off, so an install without workers does not accept jobs nobody works on. Set `jobs.enabled=true` to keep jobs on with workers that run elsewhere. KEDA and the queue alerts read the queue gauges, which only API pods with `JOBS_ENABLED=true` export. |
| `EXTRACT_MAX_BACKGROUND` | `32` | Extractions one API pod holds for `POST /v1/extract` requests with `wait_seconds` (the ones that became a job and the ones still waiting), at most. Beyond it a request does not become a job: it keeps waiting for its result and answers 200 (or its error). Bounds the pod's memory and the tasks the shutdown waits for. Minimum 1. |
| `BROWSER_ENABLED` | `false` | Switch browser mode on: pages are rendered by the browser workers (`text-extraction-browser-worker`, pool `browser`); the API pods queue them and wait. Off, `mode: browser`, `screenshot: true`, `actions`, `cookies`, `headers`, `POST /v1/screenshot` and a batch with `mode: browser` answer 400 `invalid_request` ("Browser mode is not enabled on this service."), and `mode: auto` never uses a browser (its answers are those of fast mode). The image of an API pod has no browser; the browser worker image has. On, the graceful shutdown of the API covers a sync request that waits for the browser: the fast phase of an escalating `auto` request (`EXTRACT_TIMEOUT_SECONDS`) + `BROWSER_QUEUE_WAIT_SECONDS` + the render deadline + 10 s, that is 75 s by default and 105 s with unblock mode. The Kubernetes `terminationGracePeriodSeconds` of the API pods must cover `SHUTDOWN_DRAIN_SECONDS` plus that plus a margin: the Helm chart sets it to 90 s (120 s with unblock mode) unless you set it, and docker compose stops the API with a 120 s grace period. Extract workers keep their own bound; browser workers have theirs (see Ports and probes). Helm: `workers.browser.enabled` (default true) deploys the browser workers and renders this variable into the API, extract worker and browser worker pods. docker compose always starts a browser worker and sets it to true for every service. |
| `BROWSER_TIMEOUT_SECONDS` | `30` | Time budget of one page in the browser, from navigation to the finished text, in seconds. 5 to 120. |
| `BROWSER_QUEUE_WAIT_SECONDS` | `10` | How long a synchronous request (`POST /v1/extract`, `POST /v1/screenshot`) waits for a browser worker to take its page, in seconds; then it waits up to the render deadline (`BROWSER_TIMEOUT_SECONDS`, in unblock mode `BROWSER_UNBLOCK_TIMEOUT_SECONDS`) + 5 s for the result. If no worker takes it in time (none running, or all busy), `mode: auto` answers its fast result with the warning `browser_unavailable`, and `mode: browser` and screenshots answer `failed` with `timeout` (retryable). Batches are not bound by it. 0 to 60. |
| `BROWSER_CONCURRENCY` | `4` | Pages one browser worker pod (`text-extraction-browser-worker`) renders at the same time, standard and unblock mode together; also its number of job slots (`WORKER_CONCURRENCY` does not apply there). Minimum 1. Helm: `workers.browser.concurrency`; the pod resources (`workers.browser.resources`) are sized for 4. |
| `BROWSER_IDLE_WAIT_SECONDS` | `8` | Longest time a page is given to become quiet (no network traffic) after it has loaded, in seconds. 0 to 30. |
| `BROWSER_RECYCLE_AFTER_PAGES` | `200` | Pages a browser process renders before it is restarted, to bound its memory. Minimum 1. |
| `BROWSER_MAX_BYTES` | `50000000` | Largest total of all responses one page may load, in bytes. Minimum 1000000. |
| `BROWSER_BLOCKED_RESOURCES` | `image,media,font` | Comma-separated Playwright resource types that the browser does not load: `document`, `stylesheet`, `image`, `media`, `font`, `script`, `texttrack`, `xhr`, `fetch`, `eventsource`, `websocket`, `manifest`, `other`. `document` cannot be blocked. Empty blocks nothing. Anything else makes the service exit with code 2. |
| `BROWSER_AUTOCONSENT` | `true` | Opt out of cookie banners automatically (decline what is optional) before the text is read; a cookie dialog that is still in the way is removed from the page (warning `consent_overlay_removed`). |
| `BROWSER_EXECUTABLE_PATH` | none | Path of a local Chromium. For development only; deployments use the browser the image ships. |
| `BROWSER_UNBLOCK_ENABLED` | `false` | Allow unblock mode: a second browser that clears bot challenges. It applies only to requests of an admin key whose compliance mode is `ignore`, so robots.txt and TDM checks are off for them. A user key never gets it, not even where `COMPLIANCE_DEFAULT=ignore` (R9). Off, a challenge stays a `bot_protection` failure. The unblock browser is a real Chrome: its own background requests (to Google services) also go through the egress proxy. Helm: `workers.browser.unblock` (default false, needs `workers.browser.enabled`), rendered into every pod. docker compose passes the value from `.env` or the shell to every service. |
| `BROWSER_UNBLOCK_CHANNEL` | `chrome` | Browser of unblock mode: `chrome` (installed Google Chrome) or `chromium` (Patchright's own Chromium, which `patchright install chromium` must have installed). The browser worker image ships Google Chrome only. |
| `BROWSER_UNBLOCK_HEADLESS` | `false` | Run the unblock browser without a window. Many challenges need a headed browser, which needs a display in the container. |
| `BROWSER_UNBLOCK_TIMEOUT_SECONDS` | `60` | Time budget of one page in unblock mode, in seconds. 10 to 180. |
| `BROWSER_UNBLOCK_CHALLENGE_WAIT_SECONDS` | `20` | How long unblock mode waits for a challenge to clear, in seconds. 0 to 60. |
| `BROWSER_UNBLOCK_COOKIE_TTL_SECONDS` | `1800` | How long the cookies of a cleared challenge are reused for the same host, in seconds. 0 means no reuse. |
| `SCREENSHOT_MAX_BYTES` | `10485760` | Largest screenshot file, in bytes (10 MiB). Minimum 1. |
| `SCREENSHOT_MAX_HEIGHT_PX` | `16384` | Tallest screenshot, in pixels; a longer page is cut. 1080 to 32768. |
| `ARTIFACT_TTL_SECONDS` | `3600` | How long a screenshot stays available for download (`GET /v1/jobs/{id}/artifacts/screenshot.png`), in seconds. The download is a job route: it needs `JOBS_ENABLED=true` (otherwise 503), so a deployment that takes screenshots keeps jobs on. Minimum 60. |
| `SHUTDOWN_DRAIN_SECONDS` | `5` | After SIGTERM, how long `/ready` answers 503 while both ports keep serving, so Kubernetes can take the pod out of the Service before connections are refused. The graceful shutdown starts afterwards (a second signal starts it at once). Drain plus graceful shutdown (35 s by default, 55 s with OCR only, 80 s with browser mode, 110 s with unblock mode) must stay below the API pods' `terminationGracePeriodSeconds` minus a 10 s margin; the Helm chart computes it from both unless you set it (45, 65, 90 or 120 s), see Response times. Minimum 0. |

Result cache and fingerprint keys are `res:{<sha256>}` and `fp:{<sha256>}` in Valkey. The hash is
taken over the normalised URL: lower-case scheme and host, no fragment, no default port (80 for
`http`, 443 for `https`); the path and the query are kept exactly as given. For results it also
covers the extraction options, in sorted key order (`mode`, `format`, `metadata`, `links` and the
compliance mode that applied). Fingerprints are kept per URL and output format, since Markdown,
text and HTML of one page are different texts; a revalidated result renews its fingerprint's TTL.
Validators (`ETag`, `Last-Modified`) are only stored and sent back when they are printable ASCII
of at most 1 KB. A Valkey outage turns the cache and the change
detection off for the affected requests (logged without the URL) and never fails them.

The fetcher identifies itself on every outbound request with a fixed, honest user agent,
`text-extraction/<version> (+https://github.com/janschachtschabel/text-extraction)`. It is not
configurable. The robots.txt product token is `text-extraction`; robots.txt is read per RFC 9309
(4xx and unfollowable redirects mean no rules; 5xx, 429 or a network error means "unavailable",
which `enforce` treats as disallowed; a file over 500 KiB is parsed from its first 500 KiB). The
compliance files of an origin are loaded once per pod for concurrent requests, one after another
and inside the page's outbound turn (see "Outbound limits per host"). The three share one time
budget of 10 s: each gets what the ones before left, and one whose budget is spent is not requested
and counts as unavailable, like a timeout (cached at most 300 s). Every redirect
target is checked like the requested URL (robots.txt is path-specific) before it is fetched; under
`enforce` a disallowed target ends the request with `blocked_by_robots` or `tdm_reserved`, under
`report` its findings join the report. Client trace headers
(`x-client-trace-id`, B3) are logged but never forwarded to target sites, and proxy environment
variables are ignored.

## Secrets

Keep these out of ConfigMaps, images and logs.

| Variable | Content | Helm |
| --- | --- | --- |
| `API_KEYS` | JSON list of `{"name", "role", "sha256", "rate_limit_per_minute"}`. `role` is `user` or `admin`; `rate_limit_per_minute` is optional. Only the SHA-256 of a key is stored. | `secrets.apiKeys` (chart-managed Secret) or `secrets.existingSecret` + `secrets.existingSecretKey` |
| `API_KEYS_FILE` | Path to a JSON file with the same shape as `API_KEYS`, merged with it. The file content is the secret. | None. The chart mounts no key file; use `API_KEYS`. |
| `VALKEY_PASSWORD` | Password of the Valkey that docker compose and the Helm chart start. Not read by the service itself: API and worker pods use it to build `VALKEY_URL`. | `valkey.auth.existingSecret` + `valkey.auth.existingSecretKey`, or a Secret the chart generates (default). docker compose: required in `.env` (no default). |
| `VALKEY_URL` | Only when the URL carries a password. | The in-chart Valkey builds `redis://:$(VALKEY_PASSWORD)@<valkey>:6379/0` itself. For an external Valkey with a password: `externalValkey.existingSecret.name` + `.key`; without a password use `externalValkey.url`. |
| `ASR_EXTERNAL_API_KEY` | API key of the external transcription endpoint (`ASR_BACKEND=openai`). A request header `X-ASR-Api-Key` wins over it. The ASR workers send it; the API pods need it too, only to know whether there is one (without it, a request that needs a transcription and has no header is 400). Empty counts as unset. Never logged, never stored in a job. | `workers.asr.external.apiKey` (chart-managed Secret) or `workers.asr.external.existingSecret` + `.existingSecretKey`; rendered into the API pods and the ASR workers only, and only with `workers.asr.backend=openai`. docker compose: `.env` (every service). |
| `PII_HASH_KEY` | Key of `mode: hash` (at least 32 bytes), so that the same value gets the same hash in every result of the service. Optional: without it each result is hashed with a random key. Needed on the NLP workers only. Never logged. | `workers.nlp.hashKey.existingSecret` + `.existingSecretKey` (your own Secret; the chart creates none); rendered into the NLP worker container only, and only with `workers.nlp.enabled`. docker compose: `.env` (every service). |
| `LLM_API_KEY` | API key of the LLM endpoint (`LLM_BASE_URL`). A request header `X-LLM-Api-Key` wins over it. The llm workers send it; the API pods need it too, only to know whether there is one. Empty counts as unset. Never logged, never in a repr, never in an answer. | `workers.llm.apiKey.existingSecret` + `.existingSecretKey` (your own Secret; the chart creates none and never renders the key as a literal); rendered into the API pods and the llm worker container only, and only with `workers.llm.enabled`. docker compose: `.env` (every service). |

**The Valkey password must be URL-safe** (letters, digits and `-` `.` `_` `~`; for example
`openssl rand -hex 24`). The compose file and the Helm chart put the same value into
`VALKEY_URL` (`redis://:<password>@host:6379/0`) and pass it to Valkey as `--requirepass`. The
client percent-decodes the password in the URL (`p%40ss` is `p@ss`) but Valkey compares the text
as it is, so a percent-encoded password never matches: do not encode it, choose a password that
needs no encoding. This applies to the generated password (alphanumeric) and to
`valkey.auth.existingSecret`. A `VALKEY_URL` you write yourself (`externalValkey.existingSecret`,
or the variable on its own) carries the real password, and there reserved characters must be
percent-encoded. In `.env` also avoid `$`, which compose treats as a variable.

Generate a key with `openssl rand -hex 32` and compute its hash with
`printf %s "$KEY" | sha256sum | cut -d' ' -f1`. Clients send the key as
`Authorization: Bearer <key>`. Key names and hashes must be unique.

## Ports and probes

| Port | Serves | Exposed |
| --- | --- | --- |
| `8080` | Public API (`/v1/...`, OpenAPI, docs). | Ingress and the public Service. |
| `9090` | `/health`, `/ready`, `/metrics`. Never part of the OpenAPI spec. | Cluster-internal only. |

- `/health` is the liveness probe. It never touches Valkey, so a Valkey outage does not restart pods.
- `/ready` is the readiness probe. It answers 200 only when the public port listens (after the
  start-up, which loads the language model), no shutdown has begun and Valkey answers. Pods
  without Valkey leave the load balancer, and so does a pod as soon as it gets SIGTERM.
- `/metrics` is Prometheus text and is not reachable on 8080.

The extract worker (`text-extraction-worker`) serves only port 9090, with the same three paths.
Its `/ready` answers 200 once it has loaded the language model and its loops claim items, while
Valkey answers. On SIGTERM `/ready` turns 503 at once, the worker stops claiming and gives the
items in flight `max(30, EXTRACT_TIMEOUT_SECONDS + 5)` seconds to finish; a claim already sent
when the signal arrives counts as in flight, so its item is processed too. An item still running
then is cancelled, and its lease expires so that another worker runs it. There is no drain wait,
since no Service sends traffic to the worker.

The browser worker (`text-extraction-browser-worker`, image `text-extraction-browser`) also serves
only port 9090. Its `/ready` answers 200 once it has loaded the language model and started the
egress proxy, Xvfb (unblock mode with a headful Chrome only) and the browsers. On SIGTERM it stops
claiming like the extract worker and gives the renders in flight
`max(30, EXTRACT_TIMEOUT_SECONDS + 5, render deadline + 10)` seconds, where the render deadline is
`BROWSER_TIMEOUT_SECONDS` and with unblock mode the larger of it and
`BROWSER_UNBLOCK_TIMEOUT_SECONDS`: 40 s by default, 70 s with unblock mode. Then the browsers, Xvfb
and the proxy stop. `text-extraction-browser-worker --check` launches each enabled browser once and
exits 0 (1 when one does not start, 2 for invalid settings); it needs no Valkey, only valid settings
(for example `AUTH_MODE=none`). The image build runs it. The image runs as uid 10001 with `HOME`,
`XDG_CONFIG_HOME` and `XDG_CACHE_HOME` under `/tmp`, so a read-only root filesystem with a writable
`/tmp` is all Chrome needs; it uses `/tmp` instead of `/dev/shm`. The image is linux/amd64 only
(the Helm chart pins the browser pods with `workers.browser.nodeSelector`). Its Chromium headless
shell is the one the locked Playwright version names, but Google Chrome is not pinned: each image
build installs the Chrome stable of that day, so two builds of the same commit can differ in
Chrome's version.

docker compose runs the API, one extract worker, one browser worker (image target `browser`,
`/tmp` a 1 GiB tmpfs, `shm_size` 1 GiB) and a Valkey with a password (`VALKEY_PASSWORD`
from `.env`, required). It sets `BROWSER_ENABLED=true` for all three and passes
`BROWSER_UNBLOCK_ENABLED` (default false) from `.env` or the shell. Their stop grace periods are
120 s (API), 45 s (extract worker) and 90 s (browser worker). These are fixed in
`docker-compose.yml` and cover the shutdowns, unblock mode included, only at the default timeouts:
the API needs `SHUTDOWN_DRAIN_SECONDS` + its graceful phase (110 s with unblock mode), the extract
worker `max(30, EXTRACT_TIMEOUT_SECONDS + 5)`, the browser worker
`max(30, EXTRACT_TIMEOUT_SECONDS + 5, render deadline + 10)` (see Response times). If `.env` raises
`SHUTDOWN_DRAIN_SECONDS`, `EXTRACT_TIMEOUT_SECONDS`, `BROWSER_QUEUE_WAIT_SECONDS`,
`BROWSER_TIMEOUT_SECONDS` or `BROWSER_UNBLOCK_TIMEOUT_SECONDS`, raise the `stop_grace_period` of the
affected services to at least that plus a few seconds, or compose kills them before their graceful
shutdown ends. The worker needs the same `API_KEYS` as the API while `AUTH_MODE` is
`api_key`. docker compose assumes the default ports 8080 and 9090: its port mapping and health check are
written for them. Change `PUBLIC_PORT` or `INTERNAL_PORT` there and you must edit
`docker-compose.yml` as well. The Helm chart pins both ports.

The ASR worker (`text-extraction-asr-worker`, image `text-extraction-asr`) serves only port 9090.
Its `/ready` answers 200 once it has checked the model under `ASR_MODELS_PATH` (local backend),
started the egress proxy and its children, and each child passed the self-check: PyAV's FFmpeg is
an LGPL build, neither `ffmpeg` nor `rtmpdump` is on `PATH`, and the built-in clip transcribes
(with the external backend: encodes as Ogg Opus). Without the model it exits with code 1 (log
`asr_models_missing`). On SIGTERM it stops claiming and gives the transcriptions in flight 30 s,
then kills its children and gives their items back to the queue at once (`attempts` + 1; nothing
waits for a transcription, which is always a job), so they do not wait hours for their leases.
Another replica starts them again from the beginning, and with `ASR_BACKEND=openai` the endpoint
bills them again; an item interrupted `JOB_ITEM_MAX_ATTEMPTS` times fails as lost. A scale-down, a
rolling update or a node drain therefore interrupts the transcriptions in flight: prefer fixed
replicas for steady load. With a scaler, the chart's KEDA query counts the items in flight as well
(threshold = `workers.asr.concurrency`), and KEDA and the HPA wait an hour of lower load before
they remove a replica (`workers.asr.autoscaling.scaleDownStabilizationWindowSeconds`). The image builds PyAV against an LGPL FFmpeg
(BtbN/FFmpeg-Builds `lgpl-shared`, pinned by release and SHA-256 in the `Dockerfile`, decision D5)
whose shared libraries are its only FFmpeg: no `ffmpeg` program. It runs as uid 10001 with `HOME`
under `/tmp`, `ASR_MODELS_PATH=/models` and `ASR_SCRATCH_PATH=/scratch`, and holds no models.
Deno, yt-dlp's JavaScript runtime, is the binary of the `deno` package in the image's venv; yt-dlp
is given its path, so it needs no `PATH` entry. `text-extraction-asr-worker --check` runs the
self-check in one child and exits 0 (1 on a failure, 2 for invalid settings); it needs no Valkey.

docker compose starts the ASR worker only with the profile `asr`:
`docker compose --profile asr up -d --wait` with `ASR_ENABLED=true` in `.env` (compose passes
`ASR_ENABLED` from `.env` or the shell to every service, default false; the other ASR settings,
such as `ASR_BACKEND`, `ASR_MODEL` and `ASR_EXTERNAL_API_KEY`, reach every service from `.env`).
The service `asr-models` runs `--download-models` into the volume `asr-models` first (it needs
huggingface.co once; with `ASR_BACKEND=openai` it does nothing), then `asr-worker` mounts it
read-only and keeps downloaded media on the volume `asr-scratch` (`/scratch`, not the tmpfs `/tmp`,
which is memory); compose sets `ASR_MODELS_PATH` and `ASR_SCRATCH_PATH` for both, over `.env`. Its
stop grace period is 45 s.

The Docling worker (`text-extraction-docling-worker`, image `text-extraction-docling`) serves only
port 9090 as well. Its `/ready` answers 200 once it has checked the models under
`DOCLING_MODELS_PATH`, started its conversion children and converted a built-in scan; without the
models it exits with code 1 (log `docling_models_missing`). On SIGTERM it stops claiming and gives
the conversions in flight 30 s, then kills its children; their leases expire and the items run
again. The image runs as uid 10001 with `HOME` under `/tmp` and `TESSDATA_PREFIX=/models/tessdata`,
and holds no models: they come from a volume (Helm: a PVC filled by an init container; compose:
the volume `docling-models` filled by the one-shot service `docling-models`). All images start
`tini` as PID 1, which forwards signals and reaps orphaned child processes.

docker compose starts the Docling worker only with the profile `docling`:
`docker compose --profile docling up -d --wait` with `DOCLING_ENABLED=true` in `.env` (compose
passes `DOCLING_ENABLED` from `.env` or the shell to every service, default false). The service
`docling-models` runs `--download-models` into the volume first (it needs the internet once), then
`docling-worker` mounts it read-only; its stop grace period is 45 s.

## Response times

`/v1/info` answers in under 50 ms (one Valkey round trip for the rate limit, no other I/O).
`POST /v1/extract` takes at most `EXTRACT_TIMEOUT_SECONDS` (25 s); a cached result comes back in a
few milliseconds. A caller that does not want to hold the connection sends `wait_seconds` (0-60):
an extraction still running after that long continues in the pod and becomes a job (202,
`GET /v1/jobs/{id}`); `JOBS_ENABLED=false` ignores `wait_seconds`. On SIGTERM `/ready` turns 503
at once and both ports keep serving for `SHUTDOWN_DRAIN_SECONDS` (5); then the public port stops
accepting connections and gives requests in flight `max(30, EXTRACT_TIMEOUT_SECONDS + 5)` seconds
(30 by default) to finish; then the app's own shutdown waits for extractions that became a job,
for at most `EXTRACT_TIMEOUT_SECONDS` + 7 s (it cancels those still running 5 s after
`EXTRACT_TIMEOUT_SECONDS` and gives the writes of their error results 2 s); the internal port
(`/health`, `/metrics`) stops last. Such an extraction ends within `EXTRACT_TIMEOUT_SECONDS` of
its start and runs while the graceful phase does, so normally the whole sequence fits in the API
pods' `terminationGracePeriodSeconds`; its worst case does not, and then the pod is killed
and the job's lease expires (the job then reports a lost extraction).

A sync request that waits for the nlp or llm workers (`pii`, or `structured` and `chunking` work
that needs the LLM; with a long `wait_seconds` this wait may outlast the graceful phase) answers
202 with its nlp or llm job as soon as the graceful phase begins, as it would when its wait runs
out; the worker completes the job, and the caller polls `GET /v1/jobs/{id}` (issue #19). A
request that waits for a browser or Docling job (also one whose item moves on to the nlp or llm
workers) is not answered early: it ends within the graceful phase only while that phase is at
least its wait, which with Docling workers only (50 s) a `wait_seconds` of 60 can exceed.

While `BROWSER_ENABLED`, the graceful phase is at least
`EXTRACT_TIMEOUT_SECONDS + BROWSER_QUEUE_WAIT_SECONDS + render deadline + 10` seconds (75 s by
default, 105 s with unblock mode; the render deadline is `BROWSER_TIMEOUT_SECONDS`, with unblock
mode the larger of it and `BROWSER_UNBLOCK_TIMEOUT_SECONDS`), because a sync request may wait for
a browser worker and its render after the fast phase. While `DOCLING_ENABLED`, it is also at least
`EXTRACT_TIMEOUT_SECONDS + DOCLING_SYNC_WAIT_SECONDS + 5` seconds (50 s by default), because a
sync request with a document that needs OCR waits for the Docling worker after its fast phase.

The grace periods in the Helm chart, each with a 10 s margin:

| Pods | Value | Default | Must cover |
| --- | --- | --- | --- |
| API | `terminationGracePeriodSeconds` | unset: computed, 45 s without browser or Docling workers, 65 s with Docling workers only, 90 s with browser workers, 120 s with unblock mode | `SHUTDOWN_DRAIN_SECONDS` + the graceful phase above |
| Extract workers | `workers.extract.terminationGracePeriodSeconds` | 45 | `max(30, EXTRACT_TIMEOUT_SECONDS + 5)` (30 s) |
| Browser workers | `workers.browser.terminationGracePeriodSeconds` | 150 | `max(30, EXTRACT_TIMEOUT_SECONDS + 5, render deadline + 10)` (40 s, 70 s with unblock mode) |
| Docling workers | `workers.docling.terminationGracePeriodSeconds` | 60 | 30 s for the conversions in flight |
| ASR workers | `workers.asr.terminationGracePeriodSeconds` | 60 | 30 s for the transcriptions in flight (then their items go back to the queue at once and start again elsewhere) |
| NLP workers | `workers.nlp.terminationGracePeriodSeconds` | 60 | 30 s for the analyses in flight (then their items run again elsewhere once their lease expires, `2 × PII_TIMEOUT_SECONDS + 10`) |
| llm workers | `workers.llm.terminationGracePeriodSeconds` | 40 | `workers.llm.gracefulSeconds` (`LLM_GRACEFUL_SECONDS`, 30 s) for the items in flight (then the ones still running go back to the queue at once and pay their LLM calls again elsewhere) |

The chart reads the settings from `config.*`, else their defaults, and fails to render when a grace
period is too short. Raise a value you set whenever you raise one of the timeouts, or leave the API
value unset.

The in-chart Valkey reserves 1280Mi of memory (request equals limit) and runs with
`--maxmemory 768mb --maxmemory-policy noeviction`, 60 % of the limit, which leaves room for
fragmentation, client buffers and the fork of an append-only-file rewrite. Raise limit and
`valkey.maxmemory` together.

## Browser mode

Browser mode needs three things: `BROWSER_ENABLED=true` on the API pods, the extract workers and the
browser workers (the Helm chart and docker compose set it everywhere together), at least one browser
worker (`text-extraction-browser-worker`, image `text-extraction-browser`) and Valkey. Screenshots
also need `JOBS_ENABLED=true`: the PNG is a job artifact. How it works is in
[architecture.md](architecture.md) ("Browser worker").

The request fields it adds (all in the OpenAPI spec):

| Field | Where | Meaning |
| --- | --- | --- |
| `mode` | `POST /v1/extract`, `POST /v1/batch` | `fast`, `auto` (default) or `browser`. Batches take it too but no browser options. |
| `actions` | `POST /v1/extract`, `POST /v1/screenshot` | Up to 20 page actions. `evaluate` is for admins. A click that starts a download discards the page. |
| `cookies`, `headers` | same | Session cookies and extra headers for logged-in pages. Headers go only to the requested URL's host and port. What Chromium refuses to set answers 422, without the value: a control character (also NUL, tab, CR, LF) or DEL or a semicolon in a cookie's `name` or `value`, a space at either end of them, an `=` in the name, and a name and value of more than 4096 UTF-8 bytes together. Spaces inside, commas, backslashes, double quotes and non-ASCII text are set as given. Never logged or shown in a job. |
| `screenshot`, `full_page` | `POST /v1/extract` | `data.screenshot` references the PNG for `GET /v1/jobs/{id}/artifacts/screenshot.png`, kept for `ARTIFACT_TTL_SECONDS`. |
| `compliance: ignore` | all | Admins only. Together with `BROWSER_UNBLOCK_ENABLED` it switches on unblock mode. |

The Helm keys, with the variables they render (the full table is in the
[chart README](../deploy/helm/text-extraction/README.md#values)):

| Key | Default | Variable or effect |
| --- | --- | --- |
| `workers.browser.enabled` | `true` | Deploys the browser workers; renders `BROWSER_ENABLED` into every pod. |
| `workers.browser.unblock` | `false` | Renders `BROWSER_UNBLOCK_ENABLED` into every pod. Needs `workers.browser.enabled`. |
| `workers.browser.concurrency` | `4` | `BROWSER_CONCURRENCY` on the browser workers. |
| `workers.browser.replicaCount` | `2` | Replicas, unless KEDA or HPA scale them. |
| `workers.browser.terminationGracePeriodSeconds` | `150` | See Response times. |
| `workers.browser.resources`, `workers.browser.tmpSizeLimit` | 750m / 1152Mi to 2250m / 4608Mi, `2Gi` | Measured with four pages (see [Resource baselines](#resource-baselines)); the memory limit holds the selector child and the conversions too. |
| `workers.browser.nodeSelector` | `kubernetes.io/arch: amd64` | The image exists for linux/amd64 only. |
| `workers.browser.autoscaling.keda.*`, `.hpa.*` | off | Scale on the `browser` queue or on CPU. |
| `workers.browser.image.repository`, `.tag` | `ghcr.io/janschachtschabel/text-extraction-browser`, chart version | The browser image. |

All other browser variables go under `config.<NAME>` like any other setting.

## Documents and OCR

Documents (PDF, DOCX, PPTX, XLSX, XLS, EPUB, CSV, ODF and images) are converted to text by
child processes of the pod that holds the bytes (MarkItDown); OCR and ODF files need the Docling
worker. Docling is off by default: without it a scan keeps its text layer and an image answers
`unsupported_content_type`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DOCUMENT_MAX_BYTES` | `20000000` | Largest document that is converted, in bytes (also the largest upload). At least 1024 and at most `FETCH_MAX_BYTES`: a larger value makes the service exit with code 2 at start-up, so lower `DOCUMENT_MAX_BYTES` too when you lower `FETCH_MAX_BYTES`. A larger document fails with `too_large` (not retryable). |
| `DOCUMENT_MAX_PAGES` | `100` | Pages of a PDF that are converted; later pages are cut (warning `pages_truncated`). Minimum 1. |
| `DOCUMENT_MAX_UNCOMPRESSED_BYTES` | `200000000` | Largest total size of all entries of a ZIP-based document (DOCX, PPTX, XLSX, EPUB, ODT, ODS, ODP) once unpacked, in bytes. A document over it, with more than 10,000 entries or with an entry that unpacks to over 100 times its size fails with `too_large`. Minimum 1024. |
| `DOCUMENT_CONCURRENCY` | `2` | Conversion child processes per pod; each converts one document at a time. A request waits for a free one within its time budget. Minimum 1. |
| `DOCUMENT_MEMORY_LIMIT_MB` | `2048` | Address space limit (`RLIMIT_AS`) of one conversion child, in MiB. A child that exceeds it answers `too_large`. Minimum 256. Keep `DOCUMENT_CONCURRENCY` x this below the container's memory limit minus what the process itself needs; the Helm chart renders it that way from the memory limit (768 with the defaults) unless set in `config`. |
| `DOCLING_ENABLED` | `false` | Switch OCR on: scans, broken text layers, images and ODF files are converted by the Docling worker (`text-extraction-docling-worker`, pool `docling`). Needs `JOBS_ENABLED=true`. Off, `ocr: always` answers 400 `invalid_request`. The Helm chart sets it from `workers.docling.enabled` on every pod. |
| `DOCLING_TIMEOUT_SECONDS` | `300` | Time budget of one Docling conversion, in seconds; the conversion child is killed after it. Docling itself stops at 0.9 of it and answers the pages it read (warning `pages_partial`). 30 to 1800. |
| `DOCLING_SYNC_WAIT_SECONDS` | `20` | How long a synchronous request that needs OCR waits for the Docling worker, in seconds. Then it answers the text layer with the warning `ocr_unavailable` (an image fails with `timeout`, retryable). 0 to 25. |
| `DOCLING_INPUT_TTL_SECONDS` | `3600` | How long the bytes of a document queued for the Docling worker, or of an upload that became a job (`wait_seconds`), stay in Valkey, in seconds; never longer than the job. They are deleted when the item is done. Minimum 600. |
| `DOCLING_INPUT_BUDGET_BYTES` | `200000000` | The most bytes the inputs of all jobs may hold in Valkey at once: documents queued for the Docling worker, media queued for the ASR worker (an upload's bytes), uploads that became a job (`wait_seconds`) and image uploads waiting for their description (`describe_images`), across all pods. A document that would exceed it is not queued: a sync request answers its text layer with `ocr_unavailable` (an image fails with `rate_limited`, retryable), a batch item keeps its text layer the same way, and a slow upload stays synchronous instead of becoming a job. Media for transcription that would exceed it is not queued either (`asr_unavailable`, or `rate_limited` for a media file or an upload). An image upload whose description would exceed it answers its content without it (`partial`/`llm_failed`, retryable). Counted as `input_budget` in `text_extraction_docling_dispatch_total`, `text_extraction_asr_dispatch_total` and `text_extraction_llm_dispatch_total`. Inputs leave it when their item is done or their TTL ends. Plan Valkey's `maxmemory` for it (see the chart's `valkey.maxmemory`). At least 1024 and at least `DOCUMENT_MAX_BYTES`, or the service exits with code 2 at start-up. |
| `DOCLING_MAX_OPEN_PER_CLIENT` | `5` | Unfinished Docling jobs one API key may have. Beyond it nothing is queued for OCR and the request answers without it (`ocr_unavailable`). Minimum 1. |
| `DOCLING_CONCURRENCY` | `1` | Documents one Docling worker pod converts at once, each in a child process of its own. Minimum 1. |
| `DOCLING_THREADS` | `4` | CPU threads of one Docling conversion. Minimum 1. |
| `DOCLING_MAX_PAGES` | `100` | Pages of a document the Docling worker converts; later pages are cut (warning `pages_truncated`). Minimum 1. |
| `DOCLING_MEMORY_LIMIT_MB` | `6144` | Address space limit of one Docling conversion child, in MiB. Minimum 256. |
| `DOCLING_OCR_LANGUAGES` | `deu,eng` | Tesseract language codes for OCR, comma-separated, lower case with underscores (`deu_frak` is valid). Their `.traineddata` files must be in `tessdata/` under `DOCLING_MODELS_PATH`. Anything else makes the service exit with code 2. |
| `DOCLING_MODELS_PATH` | `/models` | Volume with the OCR models: Docling's layout and table models in `docling/`, Tesseract's data in `tessdata/`. The image holds no models; the Docling worker mounts the volume read-only. |

The request field `ocr` (`auto`, `never` or `always`; default `auto`) is part of
`POST /v1/extract`, `POST /v1/batch` and the form of `POST /v1/extract/file`. `auto` reads scans, broken text layers and images with
OCR where it is enabled, `never` uses the text layer only, `always` also reads every PDF with OCR.

`FETCH_MAX_BYTES` and `DOCUMENT_MAX_BYTES` are both 20000000 by default: lower them together.

OCR needs `DOCLING_ENABLED=true` on every pod, `JOBS_ENABLED=true` on the API pods, at least one
Docling worker and its models volume. The Helm keys (the full table is in the
[chart README](../deploy/helm/text-extraction/README.md#docling-workers-and-the-models-volume)):

| Key | Default | Variable or effect |
| --- | --- | --- |
| `workers.docling.enabled` | `false` | Deploys the Docling workers and the models PVC; renders `DOCLING_ENABLED` into every pod. Needs jobs on the API pods. |
| `workers.docling.concurrency` | `1` | `DOCLING_CONCURRENCY` on the Docling workers. |
| `workers.docling.replicaCount` | `1` | Replicas, unless KEDA or HPA scale them. |
| `workers.docling.terminationGracePeriodSeconds` | `60` | See Response times. |
| `workers.docling.resources` | 2250m / 1600Mi to 5500m / 8Gi | Measured for one conversion (see [Resource baselines](#resource-baselines)); the memory limit backs `DOCLING_MEMORY_LIMIT_MB`. |
| `workers.docling.nodeSelector` | `kubernetes.io/arch: amd64` | The image is built for linux/amd64 only. |
| `workers.docling.autoscaling.keda.*`, `.hpa.*` | off | Scale on the `docling` queue or on CPU. |
| `workers.docling.models.download`, `.downloadMode` | `true`, `init` | An init container (`init`) or a post-install/post-upgrade hook Job (`job`, needs `ReadWriteMany` or one node) fills the volume (`--download-models`); `false` needs `persistence.existingClaim`. |
| `workers.docling.models.resources` | 100m / 256Mi to 1 / 1Gi | Of the init container or the hook Job (the download streams). |
| `workers.docling.models.persistence.*` | `5Gi`, `ReadWriteMany`, cluster default class | The models PVC, or `existingClaim`. Mounted at `/models` (`DOCLING_MODELS_PATH`), read-only in the worker. |
| `workers.docling.image.repository`, `.tag` | `ghcr.io/janschachtschabel/text-extraction-docling`, chart version | The Docling image. |

## Audio and video

Audio and video become text in three tiers. A URL of a supported platform (YouTube, Vimeo, ARD,
ZDF, ARTE, 3sat) answers with the video's subtitles (manual first, then automatic captions), its
title, description, duration and licence; without subtitles it answers the metadata
(`transcript_source: metadata_only`, `partial`/`no_transcript_available`). Subtitles are read by
child processes of the pod that holds the request (youtube-transcript-api, yt-dlp). Speech
recognition (`transcribe: true`, and every uploaded audio or video file) runs in the ASR worker and
is always a job. ASR is off by default: the API image has neither Whisper nor models.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MEDIA_ENABLED` | `true` | Switch audio and video on: URLs of supported platforms (YouTube, Vimeo, ARD, ZDF, ARTE, 3sat) answer with the video's subtitles or metadata, media files are recognised by their bytes, and pages gain the text of their HTML5 `<track>` elements and the list of their embedded players (`data.media`). Off, these URLs and files are handled as before. |
| `MEDIA_CONCURRENCY` | `2` | Media child processes per pod (subtitle and metadata calls of youtube-transcript-api and yt-dlp); each handles one call at a time. A request waits for a free one within its time budget. Minimum 1. |
| `MEDIA_MEMORY_LIMIT_MB` | `1024` | Address space limit of one media child, in MiB. Minimum 256. Keep `MEDIA_CONCURRENCY` x this below the container's memory limit minus what the process itself needs. |
| `MEDIA_EXTRACT_MAX_BYTES` | `30000000` | Most bytes one media call may pull from the platform (player APIs, caption files), counted at the egress proxy, in bytes. A call over it fails. Minimum 1024. |
| `MEDIA_SUBTITLE_MAX_BYTES` | `5000000` | Largest subtitle track that is read, in bytes: a platform's caption file or an HTML5 `<track>`. A larger track is skipped. Minimum 1024. |
| `MEDIA_MAX_TRACKS` | `3` | HTML5 `<track>` subtitle tracks added to one page, one per `<video>` or `<audio>` element. More are cut (warning `tracks_truncated`). 0 to 10; 0 adds none. |
| `MEDIA_MAX_DURATION_SECONDS` | `7200` | Longest audio or video that is transcribed, in seconds (2 hours, decision D6). A longer one fails with `media_too_long` (not retryable) before transcription: the platform's duration before the download, a media file's or an upload's (PyAV's probe) before it is decoded, and the decoded audio itself. Minimum 60. |
| `MEDIA_MAX_BYTES` | `500000000` | Largest media file the ASR worker downloads for transcription, in bytes. The download's egress context allows it plus 5 MB for headers and segment overhead; a platform video's info call has its own context capped at `MEDIA_EXTRACT_MAX_BYTES`. Must fit the worker's scratch volume (`ASR_SCRATCH_PATH`). Minimum 1000000. |
| `MEDIA_UPLOAD_MAX_BYTES` | `25000000` | Largest audio or video file that can be uploaded for transcription, in bytes (about 25 minutes of MP3 at 128 kbit/s). The bytes travel through Valkey and count in the input budget (`DOCLING_INPUT_BUDGET_BYTES`, which holds the inputs of all kinds), so with `ASR_ENABLED=true` it must not exceed it, or the service exits with code 2 at start-up. At least 1000000. Larger media: send the URL. The Helm chart sets it from `workers.asr.uploadMaxBytes`. |
| `MEDIA_DOWNLOAD_TIMEOUT_SECONDS` | `600` | Time budget of one media download by the ASR worker, in seconds (a platform video's info and its download together), within the item's `ASR_TIMEOUT_SECONDS`: the download gets this or what is left of the item's budget, whichever is less. A download that runs out of it fails with `timeout`, retryable (out of the item's budget: not retryable). A media file's download holds its host's turn at the outbound gate, so with `ASR_ENABLED` a turn's lease lasts at least this long plus 30 s. 30 to 3600. |
| `ASR_ENABLED` | `false` | Switch transcription on: audio and video without subtitles are transcribed by the ASR worker (`text-extraction-asr-worker`, pool `asr`) when the request says `transcribe: true`, and uploaded audio and video are transcribed. Needs `JOBS_ENABLED=true`. Off, `transcribe: true` answers 400 `invalid_request`. The Helm chart sets it from `workers.asr.enabled` on every pod. |
| `ASR_BACKEND` | `local` | `local`: faster-whisper on the CPU of the ASR worker, with the model from `ASR_MODELS_PATH`. `openai`: an OpenAI-compatible transcription endpoint (`ASR_EXTERNAL_BASE_URL`, called directly, not through the egress proxy); the worker then needs no models (no model check, `--download-models` downloads nothing, the start-up self-check and `--check` encode the built-in clip as Opus instead of transcribing it, and still check FFmpeg's licence and the programs on `PATH`). The worker decodes the media itself, encodes it as 16 kHz mono Opus at 32 kbit/s in Ogg and posts it in chunks of at most 20 minutes, cut in pauses. The key is the caller's `X-ASR-Api-Key` (on `POST /v1/extract`, `POST /v1/extract/file` and `POST /v1/batch`; stored with the job, never shown or logged), else `ASR_EXTERNAL_API_KEY`; without either, `transcribe: true` and audio or video uploads answer 400 `invalid_request`. Set it on the API pods too. With `openai` and no `ASR_EXTERNAL_BASE_URL` the ASR worker exits with code 2. The client is the OpenAI SDK, as for the LLM: it never sends the headers of `OPENAI_ORG_ID` and `OPENAI_PROJECT_ID`, and with `ASR_ENABLED=true` and `openai` start-up refuses `OPENAI_CUSTOM_HEADERS` and `OPENAI_LOG` when set and not empty (exit code 2; see LLM). |
| `ASR_MODEL` | `large-v3-turbo` | Whisper model of the local backend: `large-v3-turbo` (about 1.6 GB, better German) or `small` (about 0.5 GB, faster on CPU, more errors). Any other value makes the service exit with code 2. |
| `ASR_MODELS_PATH` | `/models` | Volume with the Whisper models, one folder per model. The image holds no models; the ASR worker mounts the volume read-only. |
| `ASR_COMPUTE_TYPE` | `int8` | CTranslate2 compute type of the local backend (`int8` is the fast and small choice on CPU). |
| `ASR_THREADS` | `4` | CPU threads of one transcription. Minimum 1. |
| `ASR_CONCURRENCY` | `1` | Transcriptions one ASR worker pod runs at once, each in a child process of its own. Minimum 1. |
| `ASR_MEMORY_LIMIT_MB` | `4096` | Address space limit of one ASR child, in MiB. Minimum 256. |
| `ASR_TIMEOUT_SECONDS` | `10800` | Time budget of one transcription item (the wait for the host's turn, the download, decoding and recognition), in seconds (3 hours, decision D6; the local backend took about 0.9 s per second of audio with `large-v3-turbo` and 4 threads in the spike). The transcription gets what the download left; the child is killed at the end of the budget and the item fails with `timeout` (not retryable: the same media would time out again). The ASR worker's lease of an item is 2 × this + 10 s. 60 to 14400. |
| `ASR_INPUT_TTL_SECONDS` | `28800` | How long the input of an item queued for transcription waits in Valkey for the ASR worker, in seconds (8 hours, decision D6); never longer than the job. The input is an upload's bytes, or for a URL what the worker needs to fetch it (its media metadata). It is deleted when the item is done. An input that expired before the worker claimed the item answers with the video's details and `asr_unavailable` (a platform video), or fails `timeout`, retryable (a media file or an upload). With `ASR_ENABLED=true` it must be longer than the ASR lease (2 × `ASR_TIMEOUT_SECONDS` + 10), so an item that runs again after a lost lease still finds its bytes, or the service exits with code 2 at start-up. Minimum 600. |
| `ASR_MAX_OPEN_PER_CLIENT` | `3` | Unfinished ASR jobs one API key may have. Beyond it nothing is queued for transcription and the request answers without it (`asr_unavailable`, or `rate_limited` for a media file). Minimum 1. |
| `ASR_SCRATCH_PATH` | `/tmp/asr` | Directory where the ASR worker keeps the media it is transcribing; the file is deleted when the item ends. The ASR image sets `/scratch`; the Helm chart mounts an emptyDir with a size limit there (`workers.asr.scratch.sizeLimit`, 2Gi, which must hold `ASR_CONCURRENCY` × (`MEDIA_MAX_BYTES` + 5 MB) or the render fails), docker compose the volume `asr-scratch`. |
| `ASR_EXTERNAL_BASE_URL` | unset | Base URL of the OpenAI-compatible transcription endpoint (`<base>/audio/transcriptions`) for `ASR_BACKEND=openai`. https only; anything else makes the service exit with code 2. Empty counts as unset. |
| `ASR_EXTERNAL_API_KEY` | unset | **Secret**, see [Secrets](#secrets): API key of the external transcription endpoint for `ASR_BACKEND=openai`; the caller's `X-ASR-Api-Key` wins over it. Empty counts as unset. |
| `ASR_EXTERNAL_MODEL` | `whisper-1` | Model name sent to the external endpoint. |
| `ASR_EXTERNAL_TIMEOUT_SECONDS` | `300` | Time budget of the external endpoint for one audio chunk of at most 20 minutes, in seconds: it bounds the chunk's whole request (upload, the endpoint's work and the answer), within the item's `ASR_TIMEOUT_SECONDS` (a chunk gets what is left of it when that is less; running out of that is `timeout`, not retryable). A chunk that runs out of its own fails the item with `transcription_failed`, retryable, as do 429 and 5xx answers; 401 and 403 (the key refused) are not retryable. The SDK does not retry. A retry runs the whole item again, so the chunks already transcribed are sent and paid for again. Minimum 1. |

Transcription needs `ASR_ENABLED=true` on every pod, `JOBS_ENABLED=true` on the API pods, at least
one ASR worker and, for the local backend, its models volume. The Helm keys (the full table is in
the [chart README](../deploy/helm/text-extraction/README.md#asr-workers-and-the-models-volume)):

| Key | Default | Variable or effect |
| --- | --- | --- |
| `workers.asr.enabled` | `false` | Deploys the ASR workers and, for the local backend, the models PVC; renders `ASR_ENABLED` into every pod. Needs jobs on the API pods. |
| `workers.asr.backend` | `local` | `ASR_BACKEND` on every pod. `openai` needs `external.baseUrl` and renders no models volume. |
| `workers.asr.model` | `large-v3-turbo` | `ASR_MODEL` on the ASR workers; only this model is downloaded. |
| `workers.asr.uploadMaxBytes` | `25000000` | `MEDIA_UPLOAD_MAX_BYTES` on every pod; at most `DOCLING_INPUT_BUDGET_BYTES`, or the render fails. |
| `workers.asr.concurrency` | `1` | `ASR_CONCURRENCY` on the ASR workers. |
| `workers.asr.replicaCount` | `1` | Replicas, unless KEDA or HPA scale them. |
| `workers.asr.terminationGracePeriodSeconds` | `60` | See Response times. |
| `workers.asr.resources` | 3050m / 1408Mi to 5500m / 6Gi | Measured for one transcription with `large-v3-turbo` (see [Resource baselines](#resource-baselines)); the memory limit backs `ASR_MEMORY_LIMIT_MB`. |
| `workers.asr.scratch.sizeLimit` | `2Gi` | The emptyDir at `/scratch` (`ASR_SCRATCH_PATH`). |
| `workers.asr.tmpSizeLimit` | `1Gi` | The emptyDir at `/tmp`. |
| `workers.asr.nodeSelector` | `kubernetes.io/arch: amd64` | The image is published for linux/amd64. |
| `workers.asr.autoscaling.keda.*`, `.hpa.*` | off | Scale on the `asr` queue (pending plus leased items, a replica per `concurrency`) or on CPU. A scale-down interrupts transcriptions (see above). |
| `workers.asr.autoscaling.scaleDownStabilizationWindowSeconds` | `3600` | An hour of lower load before KEDA or the HPA removes a replica (the Kubernetes maximum). |
| `workers.asr.models.download`, `.downloadMode` | `true`, `init` | An init container (`init`) or a post-install/post-upgrade hook Job (`job`) fills the volume (`--download-models`); `false` needs `persistence.existingClaim`. |
| `workers.asr.models.resources` | 100m / 256Mi to 1 / 1Gi | Of the init container or the hook Job (the download streams). |
| `workers.asr.models.persistence.*` | `4Gi`, `ReadWriteMany`, cluster default class | The models PVC, or `existingClaim`. Mounted at `/models` (`ASR_MODELS_PATH`), read-only in the worker. |
| `workers.asr.external.baseUrl`, `.model` | unset, `whisper-1` | `ASR_EXTERNAL_BASE_URL` (https only) and `ASR_EXTERNAL_MODEL` on the ASR workers. |
| `workers.asr.external.apiKey`, `.existingSecret` | unset | `ASR_EXTERNAL_API_KEY` from a Secret, on the API pods and the ASR workers (see Secrets). |
| `workers.asr.image.repository`, `.tag` | `ghcr.io/janschachtschabel/text-extraction-asr`, chart version | The ASR image. |

The length limits of decision D6 (`MEDIA_MAX_DURATION_SECONDS` 7200, `ASR_TIMEOUT_SECONDS` 10800,
`ASR_INPUT_TTL_SECONDS` 28800) are the service's defaults and are set in the chart's `config`; the
render fails when the input TTL does not outlast the lease (2 × `ASR_TIMEOUT_SECONDS` + 10), as the
service would at start-up.

The request fields `transcribe` (default `false`) and `languages` (preferred subtitle and
transcription languages, ISO 639 codes such as `de` or `en-GB`, at most 5; default: the video's
original language) are part of `POST /v1/extract` and `POST /v1/batch`. `transcribe: true` answers
400 `invalid_request` while the service has no transcription.

## PII

A request with `pii` finds personal data in the result (the content, the title, the metadata, the
links, the media uploader, the document's file name) and masks, redacts, hashes or lists it. The
analysis runs in the NLP worker (pool `nlp`, Presidio and spaCy), never in the API pod: the API
image has neither the libraries nor the models. PII is off by default.

The request field `pii` is part of `POST /v1/extract`, `POST /v1/batch` and (as a string) the form
of `POST /v1/extract/file`: a mode (`mask`, `redact`, `hash`, `annotate`) or an object with `mode`,
`entities` (default `PII_DEFAULT_ENTITIES`), `language` (`auto`, `de`, `en`) and `score_threshold`
(default `PII_SCORE_THRESHOLD`). `mask` turns an entity into `[PERSON]`, `redact` into
`[REDACTED]`, `hash` into `[PERSON:<12 hex>]` (HMAC-SHA-256 of the type and the value, case-folded
and with its whitespace collapsed; equal values get equal hashes), `annotate` keeps the text and
lists the entities in `data.pii.items`. The report is `data.pii`; `data.timings.pii_ms` is its
time. A result that could not be processed answers `failed`/`pii_failed` with its content withheld.

| Variable | Default | Meaning |
| --- | --- | --- |
| `PII_ENABLED` | `false` | Switch PII handling on: requests with `pii` are processed by the NLP worker (`text-extraction-nlp-worker`, pool `nlp`). Needs `JOBS_ENABLED=true`; start-up refuses `PII_ENABLED=true` without it. Off, a request with `pii` answers 400 `invalid_request`. The Helm chart sets it from `workers.nlp.enabled` on every pod. docker compose: `.env` together with the profile `nlp`. |
| `PII_DEFAULT_ENTITIES` | `PERSON,EMAIL_ADDRESS,PHONE_NUMBER,IBAN_CODE,CREDIT_CARD,IP_ADDRESS` | Entity types handled when a request names none: a comma-separated list of `PERSON`, `LOCATION`, `ORGANIZATION`, `EMAIL_ADDRESS`, `PHONE_NUMBER`, `IBAN_CODE`, `CREDIT_CARD`, `IP_ADDRESS`, `URL`, without repeats. Places, organisations and URLs are left out by default: teaching material is full of them and they are rarely personal data. An empty, unknown or repeated entry makes the service exit with code 2. |
| `PII_SCORE_THRESHOLD` | `0.4` | Least confidence of an entity, 0 to 1, when a request names none (`score_threshold`). Lower finds more and mistakes more. |
| `PII_FALLBACK_LANGUAGE` | `en` | The model (`de` or `en`) for names in a language that has none of its own, with the warning `pii_language_unsupported`. The patterns (e-mail, phone, IBAN, card, IP) work in any language. |
| `PII_SYNC_WAIT_SECONDS` | `20` | How long a request with `pii` and without `wait_seconds` waits for the NLP worker, in seconds, before it answers 202 with its job. 1 to 60. |
| `PII_TIMEOUT_SECONDS` | `300` | Time budget of one item in the NLP worker, in seconds; the analysis child is killed after it and the item fails with `pii_failed`, retryable. The worker's lease of an item is 2 × this + 10 s. 10 to 3600. **Do not set it small:** a child loads the spaCy models in its first item (once per child: at the worker's first item, and again after a child was killed, crashed or recycled), which takes about 8 s with `md` and more with `lg`, and that time is part of this budget. With a small value (the minimum is 10 s) an item that meets a fresh child can fail with `pii_failed` before the models are loaded. |
| `PII_MAX_CHARS` | `2000000` | Most characters of all fields of one result that are analysed. A result with more fails with `too_large`, its content withheld. Minimum 10000. **`JOB_ITEM_MAX_BYTES` comes first:** the result travels to the NLP worker as a draft in a job item, and a draft whose JSON is over `JOB_ITEM_MAX_BYTES` (2,000,000 bytes) is stored without its `content`. Such a result is not `too_large`: it answers `success` with `data.content` null and the warning `content_not_stored_too_large`, its other fields processed. With the defaults that is any result whose JSON is over 2 MB (about 2,000,000 characters of ASCII text, fewer of other text), so `too_large` is reached only when `JOB_ITEM_MAX_BYTES` is raised above it, or by the other fields. |
| `PII_MAX_ENTITIES` | `1000` | Most items `annotate` lists; more are cut with the warning `pii_entities_truncated` (the counts stay complete). 1 to 100000. |
| `PII_CONCURRENCY` | `1` | Items one NLP worker pod analyses at once, each in a child process of its own with the models loaded. Minimum 1. The Helm chart sets it from `workers.nlp.concurrency`. |
| `PII_MEMORY_LIMIT_MB` | `3072` | Address space limit of one analysis child, in MiB (the `lg` models need about 2.2 GB at their peak). Minimum 512. |
| `PII_MODELS_PATH` | `/models` | Volume with the spaCy models, one folder each. The image holds no models; the NLP worker mounts the volume read-only. The Helm chart mounts the PVC `<release>-text-extraction-nlp-models` there (`workers.nlp.models.*`), docker compose the volume `nlp-models`. |
| `PII_MODEL_SIZE` | `lg` | spaCy models: `lg` (about 1 GB, better German names) or `md` (about 115 MB, half the memory, more names missed). Any other value makes the service exit with code 2. The Helm chart sets it from `workers.nlp.modelSize` on the NLP workers; only the German and English models of this size are downloaded. |
| `PII_HASH_KEY` | unset | **Secret**, see [Secrets](#secrets): the key of `mode: hash`, at least 32 bytes (`openssl rand -hex 32`). With it the same value gets the same hash in every result of the service (`pii.hash_scope: service`), so documents can be linked without the value. Without it each result gets a random key (`hash_scope: result`): equal values match only within one result. A result in which nothing was hashed has `hash_scope: null`. Changing it changes every hash. Empty counts as unset; a shorter key makes the service exit with code 2. Helm: `workers.nlp.hashKey.existingSecret`. |

The other PII settings are service defaults in the chart: set them under `config` (for example `config.PII_SYNC_WAIT_SECONDS`); only `PII_ENABLED`, `PII_CONCURRENCY`, `PII_MODEL_SIZE`, `PII_MODELS_PATH` and `PII_HASH_KEY` come from the chart's own values. See the [chart README](../deploy/helm/text-extraction/README.md#nlp-workers-and-the-models-volume) for the volume and the models download.

PII needs `PII_ENABLED=true` on every pod, `JOBS_ENABLED=true` on the API pods, at least one NLP
worker (`text-extraction-nlp-worker`, image `text-extraction-nlp`) and its models volume. How it
works is in [architecture.md](architecture.md) ("PII"). Without a worker a request with `pii` is
not lost: after `PII_SYNC_WAIT_SECONDS` it answers 202 with its job, which waits in the queue. The
Helm keys (the full table is in the chart README):

| Key | Default | Variable or effect |
| --- | --- | --- |
| `workers.nlp.enabled` | `false` | Deploys the NLP workers and the models PVC; renders `PII_ENABLED` into every pod. Needs jobs on the API pods. |
| `workers.nlp.modelSize` | `lg` | `PII_MODEL_SIZE`: `lg` (about 1 GB on the volume, 1.6 GB resident) or `md` (about 115 MB, half the memory). Only the models of this size are downloaded. |
| `workers.nlp.concurrency` | `1` | `PII_CONCURRENCY` on the NLP workers; raise `resources` with it. |
| `workers.nlp.replicaCount` | `1` | Replicas, unless KEDA or HPA scale them. Keep one: a request with `pii` waits only `PII_SYNC_WAIT_SECONDS`. |
| `workers.nlp.terminationGracePeriodSeconds` | `60` | See Response times. |
| `workers.nlp.resources` | 750m / 1728Mi to 2 / 3Gi | Measured for one analysis child with the `lg` models (see [Resource baselines](#resource-baselines)); the memory limit backs `PII_MEMORY_LIMIT_MB`. `md` needs about half (not measured). |
| `workers.nlp.nodeSelector` | `{}` | Merged over the global one; the published image is linux/amd64 only. |
| `workers.nlp.autoscaling.keda.*`, `.hpa.*` | off | Scale on the `nlp` queue or on CPU. |
| `workers.nlp.hashKey.existingSecret`, `.existingSecretKey` | `""`, `PII_HASH_KEY` | Your own Secret with `PII_HASH_KEY` (an NLP worker's container only). Empty: a random key per result. |
| `workers.nlp.models.download`, `.downloadMode` | `true`, `init` | An init container (`init`) or a post-install/post-upgrade hook Job (`job`) fills the volume (`--download-models`); `false` needs `persistence.existingClaim`. |
| `workers.nlp.models.resources` | 100m / 256Mi to 1 / 1Gi | Of the init container or the hook Job. |
| `workers.nlp.models.persistence.*` | `3Gi`, `ReadWriteMany`, cluster default class | The models PVC, or `existingClaim`. Mounted at `/models` (`PII_MODELS_PATH`), read-only in the worker. 3Gi holds the staged copy of an `lg` update. |
| `workers.nlp.image.repository`, `.tag` | `ghcr.io/janschachtschabel/text-extraction-nlp`, chart version | The NLP image (Presidio and spaCy, no models). |

## LLM

Structured output with an LLM and semantic chunking call an OpenAI-compatible endpoint that the
operator runs or rents (`LLM_BASE_URL`). It is configuration, not a caller's target: the SSRF guard
does not apply to it, it is called directly (no proxy from the environment, no redirects) and `http`
is refused unless `LLM_ALLOW_HTTP` is set. The LLM only ever sees page content as data: no tool
calls, no URL in its answer is fetched, and its answer is checked against the caller's schema
before it is used. The LLM is off by default; the calls run in the llm workers (pool `llm`), so it
needs jobs.

The key is the caller's `X-LLM-Api-Key` (1 to 1,024 visible ASCII characters; stored with the job
like the ASR key, never shown or logged), else `LLM_API_KEY`. The environment's `OPENAI_API_KEY` is
never read. Failures of the endpoint answer `llm_failed`; the message names the HTTP status only,
never the endpoint's text. 400, 401, 403, 404 and 422 are not retryable; 429, 5xx, timeouts and
connection errors are (after the SDK's two retries). An answer that is no valid JSON for the
schema, or is cut off (`finish_reason: length`), gets one repair call with the error appended, then
fails with `llm_failed`, not retryable.

The OpenAI SDK reads some `OPENAI_*` variables from the environment on its own (issue #19). The
service passes the key, `LLM_BASE_URL`, the timeout and the retries itself, and never sends the
headers of `OPENAI_ORG_ID` and `OPENAI_PROJECT_ID` to the endpoint. `OPENAI_CUSTOM_HEADERS` (extra
headers for every call; an `Authorization` line would replace the caller's key) and `OPENAI_LOG`
(the SDK's own plain-text log) it reads whatever the service passes: while `LLM_ENABLED` is true,
start-up refuses either of them when it is set and not empty, naming them (exit code 2), in every
process; so it does while `ASR_ENABLED` is true with `ASR_BACKEND=openai` (the external
transcription client is the same SDK). Neither the chart nor docker compose sets them.

| Variable | Default | Meaning |
| --- | --- | --- |
| `LLM_ENABLED` | `false` | Switch the LLM on. Needs `JOBS_ENABLED=true`; start-up refuses `LLM_ENABLED=true` without it, and with `OPENAI_CUSTOM_HEADERS` or `OPENAI_LOG` set (exit code 2). |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | Base URL of the OpenAI-compatible endpoint (`<base>/chat/completions`, `<base>/embeddings`). https only unless `LLM_ALLOW_HTTP=true`; anything else makes the service exit with code 2 (the URL is not echoed, it may carry credentials). |
| `LLM_ALLOW_HTTP` | `false` | Also accept an `http` URL: a local model server in development, tests. Never in production. |
| `LLM_MODEL` | `gpt-6-luna` | Model name sent with every chat request. Chosen by the operator, never per request. |
| `LLM_API_KEY` | unset | **Secret**, see [Secrets](#secrets): the endpoint's API key; the caller's `X-LLM-Api-Key` wins over it. Empty counts as unset. |
| `LLM_EMBEDDING_MODEL` | unset | Model of the endpoint's `/embeddings` route, for `semantic` chunking. Without it `semantic` answers 400. Empty counts as unset. |
| `LLM_VISION_MODEL` | unset | Model of the image descriptions (`describe_images`, which sends an image to the endpoint as an `image_url` part). Unset: `LLM_MODEL` is used. Must take images. Chosen by the operator, never per request. Empty counts as unset. |
| `LLM_VISION_MAX_IMAGE_BYTES` | `5000000` | Largest image, in bytes, that is fetched or taken from an upload to be described; a bigger one is skipped (`image_not_described`). An image upload over it is skipped where it is extracted: it takes no llm hop for its description and keeps no input. 10000 to 20000000 (it goes to the endpoint as base64, a third larger). Memory: an llm worker holds up to `LLM_CONCURRENCY` × 2 (`DESCRIBE_CONCURRENCY`) images at once, each at least its bytes plus their base64 copy and, while the SDK builds the request, a few copies more: plan for about 5 × this per image (about 200 MB with the defaults, about 800 MB at 20000000 with `LLM_CONCURRENCY` 4). Raise `workers.llm.resources.limits.memory` (1 GiB) or lower `LLM_CONCURRENCY` along with it. |
| `LLM_TEMPERATURE` | `0` | Sampling temperature, 0 to 2. 0 keeps extractions repeatable. |
| `LLM_MAX_TOKENS` | `4096` | Most tokens of one answer (`max_tokens`). A longer answer is cut off and counts as invalid. Minimum 1. |
| `LLM_TIMEOUT_SECONDS` | `120` | Timeout of one request to the endpoint, in seconds (the SDK's, per phase of the request). Timeouts are retried twice by the SDK, so a call may take about three times this before it fails, retryable. The llm worker gives one item at most 3 × this in all, plus 1.5 × this for an item with image descriptions to make (`describe_images`); then the item answers its content without the LLM work still to do (`llm_failed`, retryable; descriptions already made stay). It leases every item for 5.5 × this + 10 s (the longest case), so an item whose worker died runs again elsewhere after that. 5 to 600. |
| `LLM_MAX_INPUT_TOKENS` | `32000` | Most input the LLM gets of one document, in tokens estimated as 4 characters each. Longer content is cut at a paragraph border with the warning `llm_input_truncated`; the cleaned HTML that `learn_selectors` shows the LLM is cut to the same size at an element border. 1000 to 1000000. |
| `LLM_GRACEFUL_SECONDS` | `30` | How long the llm worker's items in flight may finish after SIGTERM, in seconds; then the ones still running go back to the queue at once and run again on another worker, which pays their LLM calls again. One call may take `LLM_TIMEOUT_SECONDS` (120 s by default), so with the defaults every rollout cancels the long calls in flight. To avoid that, raise this and `workers.llm.terminationGracePeriodSeconds` together, e.g. to `LLM_TIMEOUT_SECONDS + 10` and that + 10 s (the chart sets it from `workers.llm.gracefulSeconds` and refuses a grace period below it + 10 s; docker compose: raise the llm worker's `stop_grace_period`, 45 s, to at least this + 10 s). Every pod validates it at start, the llm worker reads it. 10 to 900. |
| `LLM_CONCURRENCY` | `4` | LLM items one llm worker (`text-extraction-llm-worker`, pool `llm`) runs at once: its slot loops, each with one item and its calls to the endpoint at a time. Minimum 1. |
| `LLM_SYNC_WAIT_SECONDS` | `30` | How long a sync request that needs the LLM waits for the llm worker before it answers 202 with its job (without `wait_seconds`; the largest default of `transcribe`, `pii` and the LLM wins). Its waits for a browser, Docling or nlp worker get this much more, because their result moves on to the llm worker. 1 to 60. |
| `SELECTOR_LEARN_TTL_SECONDS` | `2592000` | How long learned selectors stay in Valkey (30 days), per API key, host and schema (`sel:{<key name>}:<host>:<schema id>`, the key name's `%`, `{` and `}` percent-escaped); renewed on each write, not on reads. Minimum 60. |
| `STRUCTURED_REGEX_TIMEOUT_SECONDS` | `0.5` | Time one caller-supplied pattern may take on a text (all its matches), in seconds; then its field stays missing with the warning `pattern_timeout`. All patterns of one result share a fixed total of 2 s on top of it. 0.05 to 5. |
| `CHUNK_MAX_CHUNKS` | `2000` | Most chunks of one result; more are cut with the warning `chunks_truncated`. Minimum 1. |
| `STRUCTURED_SELECTOR_TIMEOUT_SECONDS` | `5` | Time all selector rules of one result (the caller's CSS and XPath, the learned ones, or the proposals that `learn_selectors` checks) may take together, in seconds, also capped by the request's own deadline; waiting for a free selector child counts against it. Then the child is killed and replaced: the rules' fields stay missing with the warning `selectors_timed_out` (for `learn_selectors`: nothing is learned, `selector_learning_failed`), and the result is not failed, nor written to the result cache (the next identical request runs the rules again). A CSS selector's cost has no other bound: soupsieve backtracks, and a few descendant combinators on a deeply nested page run for hours. 0.1 to 60. |
| `STRUCTURED_SELECTOR_PROCESSES` | `1` | Selector child processes per pod (API, extract, browser and llm workers): selector rules run there, one result's rules at a time per child, never in the server process. Each idle child takes about 40 MB. Minimum 1. The load test's sweep found no gain from more children: 1 stays the default (see [Resource baselines](#resource-baselines)). |
| `STRUCTURED_SELECTOR_MEMORY_MB` | `512` | Address space limit (`RLIMIT_AS`) of one selector child, in MiB, as `DOCUMENT_MEMORY_LIMIT_MB` is for the document children. A run that reaches it ends with the warning `selectors_failed` (its fields missing) and the child is replaced; a 5 MB page needs about 310 MB. Minimum 256. Count `STRUCTURED_SELECTOR_PROCESSES` x this into the pod's memory beside the document children: the Helm chart's default limits and `documentParentReserveMiB` values hold 512 MiB for it. |
| `POST_CONCURRENCY` | `2` | Threads of the post stage per process (API pods and every worker): patterns, chunking, the `semantic` distances and the learning check's parsing run on a pool of their own, never on asyncio's default executor, which resolves host names. A finish point waits for a free thread. Minimum 1. |

The Helm keys (the full table is in the chart README). The chart renders `LLM_ENABLED` into every
pod from `workers.llm.enabled`, and while it is true `LLM_BASE_URL`, `LLM_MODEL`,
`LLM_EMBEDDING_MODEL` (when `.embeddingModel` is set), `LLM_VISION_MODEL` (when `.visionModel` is
set) and, on the llm workers, `LLM_CONCURRENCY` from `workers.llm.baseUrl`, `.model`,
`.embeddingModel`, `.visionModel` and `.concurrency` too: these win over `config` entries of the
same name. The other `LLM_*` settings and `SELECTOR_LEARN_TTL_SECONDS`,
`STRUCTURED_REGEX_TIMEOUT_SECONDS`, `CHUNK_MAX_CHUNKS`, `STRUCTURED_SELECTOR_TIMEOUT_SECONDS`,
`STRUCTURED_SELECTOR_PROCESSES`, `STRUCTURED_SELECTOR_MEMORY_MB` and `POST_CONCURRENCY` are service
defaults (the chart sets the last four under `config` with these values): set them under
`config` (for example `config.LLM_TIMEOUT_SECONDS`, and `config.LLM_ALLOW_HTTP` for an `http` URL).
docker compose: `.env` with `LLM_ENABLED=true` and the profile `llm`; the llm worker's
`stop_grace_period` (45 s) must stay at least `LLM_GRACEFUL_SECONDS` + 10 s: raise both together.

| Key | Default | Variable or effect |
| --- | --- | --- |
| `workers.llm.enabled` | `false` | Deploys the llm workers (API image, no volume); renders `LLM_ENABLED` into every pod. Needs jobs on the API pods. |
| `workers.llm.baseUrl` | `https://api.openai.com/v1` | `LLM_BASE_URL`. Required; `https://` unless `config.LLM_ALLOW_HTTP` is `"true"`, or the render fails. |
| `workers.llm.model` | `gpt-6-luna` | `LLM_MODEL`. Required. |
| `workers.llm.embeddingModel` | `""` | `LLM_EMBEDDING_MODEL`; empty: `semantic` answers 400. |
| `workers.llm.visionModel` | `""` | `LLM_VISION_MODEL`, only when set; empty: `LLM_MODEL` describes the images of `describe_images` (it must take images). |
| `workers.llm.apiKey.existingSecret`, `.existingSecretKey` | `""`, `LLM_API_KEY` | Your own Secret with `LLM_API_KEY` (API pods and the llm worker container only). Empty: callers send `X-LLM-Api-Key`. |
| `workers.llm.concurrency` | `4` | `LLM_CONCURRENCY` on the llm workers. |
| `workers.llm.replicaCount` | `1` | Replicas, unless KEDA or HPA scale them. Keep one: a sync request waits only `LLM_SYNC_WAIT_SECONDS`. |
| `workers.llm.gracefulSeconds` | `30` | `LLM_GRACEFUL_SECONDS` on the llm workers; the render fails outside 10 to 900 (a `config.LLM_GRACEFUL_SECONDS` entry is overridden on the llm workers, but every pod validates it at start (10 to 900, else exit code 2), so leave it out). Raise it with the grace period, e.g. to `LLM_TIMEOUT_SECONDS + 10`, so that a rollout does not pay calls in flight twice. |
| `workers.llm.terminationGracePeriodSeconds` | `40` | See Response times. At least `workers.llm.gracefulSeconds` + 10, or the render fails. |
| `workers.llm.resources` | 150m / 256Mi to 750m / 1Gi | Measured against a fake endpoint (see [Resource baselines](#resource-baselines)); 512 MiB of the limit are the selector child. |
| `workers.llm.nodeSelector` | `{}` | Merged over the global one. |
| `workers.llm.autoscaling.keda.*`, `.hpa.*` | off | Scale on the `llm` queue or on CPU. |
| `alerts.llmQueueBacklogItems` | `100` | Threshold of `TextExtractionQueueBacklog` for the pool `llm`. |

## Site structure

`POST /v1/site-structure` crawls a site's sitemaps and its link graph as a job of kind `site`: each
page, the site's robots.txt and each sitemap file is an item of the job, fetched by the extract
workers in fast mode with the checks and outbound limits of every extraction. These settings cap
one crawl. All are service defaults; the Helm chart sets them under `config` with these values.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SITE_MAX_DEPTH` | `5` | Deepest link distance from the start page that a request's `depth` may ask for; more answers 422 `invalid_request`. 0 to 10. |
| `SITE_MAX_PAGES` | `1000` | Most page items (the start page included) that a request's `max_pages` may ask for; more answers 422 `invalid_request`. Links found after a crawl's `max_pages` say `crawl: max_pages` (warning `max_pages_reached`). A crawl must finish within `JOB_TTL_SECONDS`: nothing renews the job's TTL while it runs, and a job that expires mid-crawl is gone (404). One host's pages and sitemap files are spaced by `OUTBOUND_MIN_DELAY_MS` or its `Crawl-delay` (up to `OUTBOUND_MAX_CRAWL_DELAY_SECONDS`), so (pages + sitemap files) × spacing must stay well under it: 100,000 pages at 1 s take about 28 h, more than the default 24 h; 1,000 pages and 500 sitemaps at 10 s take about 4 h. 1 to 100000. |
| `SITE_MAX_SITEMAP_FILES` | `500` | Most sitemap files (robots.txt's `Sitemap:` lines, the guessed `/sitemap.xml` and the children of sitemap indexes) one crawl reads; the ones after it are not read (warning `sitemap_files_truncated`). 1 to 50000. |
| `SITE_MAX_SITEMAP_URLS` | `200000` | Most sitemap entries one crawl keeps, over all its sitemaps; a sitemap that reaches it keeps its first entries and answers `partial` (warning `sitemap_urls_truncated`). 1 to 5000000. |
| `SITE_SITEMAP_MAX_BYTES` | `52428800` | Largest sitemap file in bytes (the protocol's 50 MiB): the fetch stops there, and a gzip sitemap is inflated to at most this many bytes. A cut sitemap keeps the entries before the cut and answers `partial` (warning `sitemap_truncated`). 1024 to 209715200. |
| `SITE_MAX_LINKS_PER_PAGE` | `1000` | Most links kept of one crawled page, in document order; the others are neither listed nor followed (warning `links_truncated`). 10 to 50000. |

A crawl's items live in Valkey with its job, until `JOB_TTL_SECONDS`. Size Valkey for the crawls
open at once: each page item holds up to `SITE_MAX_LINKS_PER_PAGE` links, each sitemap entry about
100 bytes. A crawl counts against `BATCH_MAX_OPEN_PER_CLIENT` like a batch.

**Upgrading to package 9: extract workers first, then API pods, then site jobs.** Pods of an
older release cannot parse a `site` job's options (the new field `site`; the options forbid
unknown fields), and that breaks them in two ways:

- An older extract worker that claims a site item fails before it runs it (its slot logs
  `worker_slot_error`). It writes no result and the item stays leased, so it comes back after
  each lease (2 × `EXTRACT_TIMEOUT_SECONDS` + 10 s, 60 s). It runs normally once a new worker
  claims it; after `JOB_ITEM_MAX_ATTEMPTS` (5) leases a new worker fails it instead
  (`internal_error`, "The item was lost by a worker too often."). With only older workers the
  item cycles until the job expires: the job never completes and keeps one of the owner's
  `BATCH_MAX_OPEN_PER_CLIENT` slots.
- An older API pod answers 500 to `GET /v1/jobs/{id}` of a site job. It also answers 500 to
  `POST /v1/batch` from an owner whose open set holds a site job, completed or not, until a new
  pod has removed the finished ids.

Roll out the extract workers first and the API pods after them. `helm upgrade` rolls both
Deployments at once, so send site jobs only once every pod runs the new release.

**Capacity of the extract workers.**

- Each extract worker pod parses one sitemap at a time, on a thread of its own, so sitemaps never
  hold the threads that convert pages (`CONVERT_CONCURRENCY`). Sitemap items of all crawls queue
  for that thread while each holds one of the pod's `WORKER_CONCURRENCY` slots; an item that
  waits longer than its budget (2 × `EXTRACT_TIMEOUT_SECONDS`, 50 s) plus 5 s fails `timeout`,
  retryable ("the worker was busy"). Many large sitemaps at once take slots from pages; more pods
  spread them.
- A sitemap item holds the raw file (up to `SITE_SITEMAP_MAX_BYTES`, 50 MiB), for a gzip file its
  inflated copy (up to the same) and the parsed entries; items that fetch while another parses
  add up. Leave that headroom in the extract workers' memory limit (`workers.extract.resources`):
  three 50 MiB sitemaps at once took the load test's extract worker from about 560 to 766 MiB
  (see [Resource baselines](#resource-baselines)).
- A sitemap item may take 50 s, longer than the workers' shutdown grace (30 s by default): on
  SIGTERM it is cancelled and runs again on another worker after its lease, without duplicate
  items.

## MCP

`MCP_ENABLED=true` serves an MCP server (FastMCP, streamable HTTP, stateless, JSON answers) at
exactly `/mcp` on the public port 8080; `/mcp/` is 404, `GET /mcp` 405 (a stateless server has no
event stream), and neither appears in the OpenAPI spec. Off, there is no route and `fastmcp` is not
imported. An MCP client posts JSON-RPC with `Accept: application/json, text/event-stream`.

Every request to `/mcp` passes the same checks as a REST request, in this order:

1. The body limit: `ceil(DOCUMENT_MAX_BYTES / 3) × 4 + 64 KiB` (a file of the upload limit as
   base64, or of `MEDIA_UPLOAD_MAX_BYTES` where uploaded media are transcribed); a larger body
   answers 413 `invalid_request`. A body whose `Content-Length` is over the limit is refused at
   once, before anything is read or authenticated; a body without one (chunked) is read only
   after the checks 2 and 3 and is cut with the 413 when the bytes read pass the limit.
2. A request with an `Origin` header that is not in `MCP_ALLOWED_ORIGINS` answers 403 `forbidden`
   ("This origin may not call the MCP endpoint."). A client without a browser sends no `Origin`.
3. The API key (`Authorization: Bearer <key>`, as for REST): without a valid one the answer is the
   REST 401 envelope with `WWW-Authenticate: Bearer`. Under `AUTH_MODE=none` every caller is the
   anonymous admin, as for REST.

Each `tools/call` counts once against the caller's rate limit, the bucket REST calls share;
`initialize`, `tools/list` and notifications are free. A refused or failed call is a tool result
with `isError: true` whose `structuredContent` is the REST error envelope (`rate_limited` with
`retryable`, `invalid_request` with the validation `details`, `internal_error` for our own faults,
without a traceback); a target's `failed` is a normal result, as it is a 200 in REST. Each call
logs one line `mcp_tool_call` with `tool`, `outcome`, `duration_ms` (and `job_id` when it answered
a job, or when a refused `get_job` or `cancel_job` was asked for one), the principal and the request's trace fields (`x-client-trace-id`, B3), which the
envelope's `trace_id` carries too. Tool arguments are never logged; FastMCP's and the MCP SDK's
own loggers are held at `WARNING` or above (FastMCP logs arguments at `DEBUG`).

Behind an ingress or a proxy, give `/mcp` a read timeout of at least `MCP_WAIT_SECONDS` + 70 s
(95 s by default: a tool call ends within `MCP_WAIT_SECONDS` plus the route's own hand-over time)
and a body size of at least its body limit (about 27 MB with the default `DOCUMENT_MAX_BYTES`; about 33 MB where uploads are transcribed, from `MEDIA_UPLOAD_MAX_BYTES`), or
`extract_file` calls are cut off before the service sees them. The Helm chart's README lists
annotations for ingress-nginx.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MCP_ENABLED` | `false` | Serve the MCP endpoint at `/mcp`. Helm: `mcp.enabled`, rendered on the API pods only (a `config.MCP_ENABLED` entry is ignored); docker compose switches it on for the `api` service unless `.env` or the shell sets it. |
| `MCP_ALLOWED_ORIGINS` | empty | Browser origins that may call `/mcp`, as a comma list of `http(s)://host[:port]` (exact match on scheme, host and port, in any case; a trailing slash and a default port, `:443` for https and `:80` for http, are dropped, in the setting and in the `Origin` header). Empty: a request with any `Origin` header is refused. Anything else makes the service exit with code 2. |
| `MCP_WAIT_SECONDS` | `25` | What a tool that may become a job (`extract_url`, `extract_file`, `screenshot_url`, `extract_structured`, `chunk`) waits for its result when the caller gives no `wait_seconds`, and the longest wait of these tools and of `get_job` (a larger `wait_seconds` is cut to it). 0 to 60. |
| `MCP_MAX_CONTENT_CHARS` | `50000` | Most characters of `content` in one tool answer (shared by the items of a job page); the rest is fetched with `content_offset`. 1000 to 10000000. |
| `MCP_IMAGE_MAX_BYTES` | `5000000` | Largest screenshot attached to a tool answer as an image; a larger one is only linked. 0 to 20000000; 0 never attaches one. |

## UI

`UI_ENABLED=true` serves a playground at `/ui` on the public port 8080: server-rendered pages
(Jinja2) with htmx 2.0.11 served from `/ui/static` (no CDN, no Node build). Off, `/ui` is 404 and
`jinja2` is not imported. No `/ui` route appears in the OpenAPI spec, and none is on the internal
port.

Access (the UI has no password of its own: its password is an API key of the service):

- The page `GET /ui` and its static files need no key; they hold nothing but forms.
- Under `AUTH_MODE=api_key` the page shows only a login box: the API key, and optionally the LLM
  key (`X-LLM-Api-Key`) and the ASR key (`X-ASR-Api-Key`). The page's script keeps them in the
  tab's `sessionStorage` (`te.apiKey`, `te.llmKey`, `te.asrKey`): they survive a reload and go
  away with the tab. It sends them only as request headers to `/ui/run/*`, never as a cookie, a
  form field or part of a URL, and the service never renders them into a page. The login
  forwards `GET /v1/info` with the key: a valid key shows the application with the key's name
  and role and a "Log out" button (which clears the stored keys); a refused key shows "This key
  is not valid." and nothing else. A reload logs in again with the stored key. The login is
  only a convenience: every request the UI forwards carries the key and is checked as any REST
  request.
- Under `AUTH_MODE=none` the application shows at once, under the banner "This service runs
  without authentication: anyone who can reach it can use the API and this UI." An open UI goes
  with an open API. For a shared deployment, keep `UI_ENABLED=false` (Helm `ui.enabled=false`)
  or use `AUTH_MODE=api_key`.

Every form posts to `/ui/run/<op>`, which forwards it in-process (no network) to the matching
`/v1` route with the browser's `Authorization`, `X-LLM-Api-Key`, `X-ASR-Api-Key` and B3 headers
only (no cookie, no other header) and with `x-client-trace-id` set to the outer request's trace
id, so both access lines carry one id. The outer `/ui/run/...` access line has no `principal`
(the view does not authenticate a forwarded call itself); the inner `/v1/...` line carries it. The REST route runs every check (body limit, validation,
auth, roles, rate limit, the hand-over to a job), and each forwarded call counts once against the
caller's rate limit. Views that combine several reads count one hit per view.

Requests to `/ui/run/*` answer 403 (plain text) without `HX-Request: true`, and when their
`Origin` names another host or port than the `Host` header (a default port counts as none; the
scheme is not compared, as TLS may end at the ingress), before the body is read or any route
runs. The service has no CORS middleware, so a page of another origin cannot send `HX-Request`.
The UI never sets a cookie. The page's script sends the keys only to `/ui/run/*` of its own
origin, and the UI forwards only to relative paths under `/v1/`.

Every answer under `/ui` carries `Content-Security-Policy: default-src 'none'; script-src 'self';
style-src 'self'; img-src 'self' data:; connect-src 'self'; form-action 'self'; base-uri 'none';
frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
`X-Frame-Options: DENY`, `Cross-Origin-Opener-Policy: same-origin`,
`Cross-Origin-Resource-Policy: same-origin`, `Permissions-Policy: camera=(), microphone=(),
geolocation=()` and `Cache-Control: no-store` (`public, max-age=3600` for a static file). The
templates have no inline script, style or event handler, and htmx runs with `allowEval`,
`allowScriptTags` and its history off and `selfRequestsOnly` on.

The tabs Extract, File, Screenshot, Structured, Chunk and Info each send one REST call
(`POST /v1/extract`, `POST /v1/extract/file`, `POST /v1/screenshot`, `POST /v1/structured`,
`POST /v1/chunk`, `GET /v1/info`). Their fields build the REST body; the "Options (JSON)" box
adds any further field of it. A field left at its default yields to the same key in Options (so
`format: html`, which the form does not offer, goes there); a key both the form sets and Options
give is refused ("<key> is given twice."), as is invalid JSON, and nothing is sent. The forms'
`wait_seconds` is 25. The extract form offers Markdown or Text, the mode, "Metadata" (on),
"Metadata, structured" (the `metadata` strategy of `structured` with a schema of schema.org and
LRMI names: `name`, `headline`, `description`, `author`, `datePublished`, `dateModified`,
`license`, `inLanguage`, `keywords`, `publisher`, `educationalLevel`, `learningResourceType`,
`isAccessibleForFree`) and "Describe images" (`describe_images.max_images`, 5 by default; the
REST answers apply, such as 400 without the LLM).

Every answer is shown with its HTTP status, the envelope's status and error (with the `details`
of a 422), the raw JSON and the request as a curl command, with `$API_KEY`, `$LLM_API_KEY` and
`$ASR_API_KEY` in place of the keys. A result shows its content as source, Markdown also
rendered (raw HTML shown as text, links only for http(s), images as their alt text), a download
(`.md` or `.txt`; an `html` result saves as `.html` but is served as `text/plain`), its title, language and word count, its metadata, structured values (with
their sources and the missing fields), chunks, images with their alt texts and descriptions, its
links (clickable only for http(s)) and a timeline built from its fields: the job item's claim,
compliance, fetch (attempts, upstream status, final URL, cache), render (with "escalated from
fast" after `auto`), convert, PII, LLM and the outcome, with a one-line text for each warning. A
screenshot is read from the artifact route (one more REST call) and shown inline, or a note says
why it could not be read. A large content is bounded in the page: the source, the raw JSON and
the chunks show at most 200,000 characters each (and only such a content is rendered as
Markdown), with a note when cut; the download is the full copy, up to 10,000,000 characters.

"Load previews" posts the result's image URLs to `/ui/run/images`: one rate-limit hit, then the
service fetches at most 20 of them (4 at once, 10 s each, 25 s for the whole view) itself, with the vision step's checks:
the SSRF guard on the URL and every redirect, the caller's compliance mode, the host's outbound
turn and the service's user agent, at most `UI_IMAGE_MAX_BYTES` each and only PNG, JPEG, GIF or
WebP by their bytes. A fetched image is inlined as a `data:` URI, so the image hosts never see
the viewer and the CSP stays `img-src 'self' data:`; a skipped one shows why (`blocked`,
`refused`, `too_large`, `unsupported`, `http_error`, `error`, `busy`, or `time` when the view's
25 s ran out). An unexpected failure of one image skips that image as `error`; the others go on.

The tabs Batch, Batch file and Site structure start jobs, and every job is shown with its
progress:

- **Batch**: URLs, one per line, with the format, mode, metadata and Options of `POST /v1/batch`.
  The lines are checked on the server: each must be a plain http(s) URL of at most 2048
  characters without blanks; exact duplicates are dropped. A list with other rows is answered
  with the batch preview (below): the rows that are not used, and "Start batch" for the valid
  URLs with the options chosen. The key is checked before the form is read; the form takes at
  most `max(UI_TREE_MAX_URLS, BATCH_MAX_OPEN_PER_CLIENT × BATCH_MAX_URLS)` + 64 fields (one per
  checked box of the tree), and a typed list may fill the route's whole body limit. A form that
  cannot be read is answered with "The form could not be read: " and the reason (too many
  fields, a part too large, malformed). The URLs are split into batches of `BATCH_MAX_URLS`;
  more batches than
  `BATCH_MAX_OPEN_PER_CLIENT` is refused ("Select at most <n> URLs at once.") and nothing is
  sent. Otherwise the batches are sent one after another; a refused part (such as the 429 while
  other jobs are open) stops the rest and is shown beside the jobs already started, which keep
  running. Log line `ui_batch_split` (`parts`, `urls`).
- **Batch file**: a CSV or JSON file of at most `UI_BATCH_FILE_MAX_BYTES` and
  `UI_BATCH_FILE_MAX_ROWS` rows, parsed on the server (one rate-limit hit, nothing sent yet). It
  must be UTF-8 (a BOM is dropped). A JSON file is an array of URLs or of objects with `url`
  (nested too deep or broken: "The file is not valid JSON."). A CSV is delimited by commas,
  semicolons or tabs (one that splits every one of the first 50 rows alike; else each line is one
  value); a `url` column (any case) is used, else the first column, and a first row that is not
  an http(s) URL is a header. A field over 2049 characters, or a row of more than 1000 fields,
  refuses the file. The
  preview shows the counts (rows, URLs, rows that are not URLs, duplicates, batches), the first
  50 URLs, the first 20 problems by row, and the URLs in a read-only box with the batch options
  and "Start batch". Values such as `=HYPERLINK(...)` or `@cmd` are no URLs and are only
  reported; the UI writes no CSV. Log line `ui_batch_file` (`rows`, `urls`, `invalid`,
  `duplicates`).
- **Site structure**: `POST /v1/site-structure` with the start URL, depth, most pages, scope and
  sitemaps.
- **Progress** (`/ui/run/progress`): each job's kind and state, a progress bar with done/total
  and the success, partial and failed counts (a site job's total grows while it crawls), the
  elapsed time and a page of 20 items with previous and next buttons. An item shows "queued",
  "running in <pool>, claim <n>" (its last claim, from `items[].run`) or its status and error
  code, and "Show" opens its result with the timeline. "Cancel" deletes the job
  (`DELETE /v1/jobs/{id}`). While any job shown runs, the view refreshes itself every 3 s; each
  refresh reads all its jobs (each once, at most 20 per view, two at a time and 100 items in
  all, so fewer per page when it shows more than five) through the job core with one rate-limit
  hit, and refreshing stops
  once all are completed (after a 429 it tries again after `Retry-After`). A single operation
  that continues as a job (202) has a "Show progress" button for it.
- **Site tree** (`/ui/run/site-tree`, "Show as tree" once a site job is completed, "Show tree
  now" while it runs): the job is read in pages of 20 items, so at most 20 items are held at a
  time, and only the tree's fields are kept. The tree is built by URL path: a root per host (a
  leading `www.` dropped), a section per directory, and every URL a leaf (a directory's own page
  is the leaf `/`, a query string a leaf of its own), in name order. It merges the crawled pages
  (title, link depth, status), the sitemaps' entries (`lastmod`) and links not followed for depth
  or the page limit ("linked only"), by the crawl's seen key; pages come first. It shows at most
  `UI_TREE_MAX_URLS` URLs and counts the rest ("<n> more URLs not shown (UI_TREE_MAX_URLS)").
  Checking a section checks its subtree, a section shows a mixed state while part of it is
  checked, and a counter shows the selection; "Extract selected" starts batches of the checked
  URLs as the Batch tab does.

Body limits of the `/ui/run` routes: `/ui/run/file` that of `POST /v1/extract/file`,
`/ui/run/chunk` that of `POST /v1/chunk`, `/ui/run/batch-file` `UI_BATCH_FILE_MAX_BYTES` + 64 KiB,
`/ui/run/batch-urls` `BATCH_MAX_OPEN_PER_CLIENT` times that of `POST /v1/batch`, every other route
`REQUEST_MAX_BODY_BYTES`. A larger body answers 413 `invalid_request` before it is read whole.
One field of a form may fill its route's whole limit: the Chunk tab posts its text as multipart
(no percent-encoding), so a text the chunk route takes goes through (it is forwarded as JSON,
and the JSON escapes count against that route's limit). Every single operation (`/ui/run/file`,
`/ui/run/chunk` and the others) checks the key and the rate limit before the body is read, as its
REST route does; the check does not
count, the forwarded call counts the view's one hit. A form that cannot be read is answered with
"The form could not be read: " and the reason (too many fields or files, a part too large,
malformed). An answer htmx does not swap (such as that 413, the plain 403 of the guard or a 5xx)
puts the status line "Request failed: <status> <reason>" (the envelope's message, else the
status text) at the top of the request's target, and a request that gets no answer "Network
error"; the line is text only.

Behind an ingress or a proxy, give `/ui` the read timeout of `/mcp` (at least `MCP_WAIT_SECONDS` +
70 s: the forms wait 25 s plus the route's hand-over time) and a body size of at least the largest
of these limits: the upload route's (`DOCUMENT_MAX_BYTES`, or `MEDIA_UPLOAD_MAX_BYTES` where uploads are
transcribed, + 192 KiB) and `UI_BATCH_FILE_MAX_BYTES`
+ 64 KiB.

| Variable | Default | Meaning |
| --- | --- | --- |
| `UI_ENABLED` | `false` | Serve the playground at `/ui`. Helm: `ui.enabled`, rendered on the API pods only (a `config.UI_ENABLED` entry is ignored; the install notes warn while `config.AUTH_MODE` is `none`); docker compose switches it on for the `api` service unless `.env` or the shell sets it. |
| `UI_TREE_MAX_URLS` | `5000` | Most URLs the site tree of one site job shows; the rest are counted ("<n> more URLs not shown"). 100 to 50000. |
| `UI_BATCH_FILE_MAX_BYTES` | `10485760` | Largest CSV or JSON file of URLs the batch-file form takes, in bytes. 1024 to 104857600. |
| `UI_BATCH_FILE_MAX_ROWS` | `100000` | Most rows of one batch file; a file with more is refused. 10 to 1000000. |
| `UI_IMAGE_MAX_BYTES` | `1000000` | Largest image the UI fetches for a preview, in bytes; a larger one is skipped (`too_large`). 10000 to 20000000. |

## Headers

Read from the request:

| Header | Use |
| --- | --- |
| `Authorization` | `Bearer <key>` when `AUTH_MODE=api_key`. |
| `x-client-trace-id` | Client trace id; logged on every line of the request and used as `trace_id` in the response envelope. A job keeps the trace id of the request that created it (batch or hand-over): the worker logs each of its items under it, and the items' result envelopes carry it. |
| `b3`, `x-b3-traceid`, `x-b3-spanid`, `x-b3-parentspanid`, `x-b3-sampled` | B3 trace context; logged on every line of the request. |
| `X-ASR-Api-Key` | The caller's key of the external transcription endpoint (`POST /v1/extract`, `/v1/extract/file`, `/v1/batch`, `/v1/structured`, `/v1/chunk`); wins over `ASR_EXTERNAL_API_KEY`. A secret: kept with the job, never logged or shown. |
| `X-LLM-Api-Key` | The caller's key of the LLM endpoint (`POST /v1/extract`, `/v1/extract/file`, `/v1/batch`, `/v1/structured`, `/v1/chunk`), 1 to 1,024 visible ASCII characters (else 400); wins over `LLM_API_KEY`. A secret: kept with the job (see [Job keys](#job-keys)), never logged or shown. |

Query strings are never logged.

Responses carry no custom headers. The only exceptions are `Retry-After` (seconds until the
window ends) on 429 and `WWW-Authenticate: Bearer` on 401. Trace headers are not echoed back.

## Rate limit

The limit is a fixed 60-second window per API key, counted in Valkey so all pods share it. A
caller can therefore make up to twice the limit across a window boundary: the full limit at the end
of one window and the full limit again at the start of the next. Requests over the limit get 429
with `Retry-After`. If Valkey is unavailable the request is refused with 503 (`internal_error`, retryable) and a warning is logged. Set `RATE_LIMIT_ENABLED=false` to run without the limit.

## Outbound limits per host

Every request to a target site passes a per-host gate: the page and its redirect hops, and the
origin's compliance files (robots.txt, `tdmrep.json`, ai.txt). The files are fetched only when they
are not cached (`COMPLIANCE_CACHE_SECONDS`), then one after another inside the turn of the page
that needs them, before the page. A turn has at most one request in flight, so
`OUTBOUND_MAX_CONCURRENCY_PER_HOST` holds for every request. The spacing lies between the
requests of different turns: the requests of one turn (the files, the page, a redirect hop on the
same host) go back to back, so first contact costs no more than the files themselves. Two cold
turns that share one file load still send their pages a spacing apart, since each page keeps the
spacing to the other turn's latest request. The first turn on an origin does not know its
`Crawl-delay` yet; the turns after it do and keep it. All state is in Valkey, so the pods share it.
Hosts are compared lower-cased and in IDNA form. The braces around the host in the keys are
literal (a cluster hash tag), so all keys of one host live in the same slot.

| Key | Content | Lifetime |
| --- | --- | --- |
| `out:inflight:{<host>}` | Requests in flight: one entry per lease, scored by its expiry time. | A lease expires after the longest deadline a turn can be held for (`EXTRACT_TIMEOUT_SECONDS`, `BROWSER_TIMEOUT_SECONDS`, `BROWSER_UNBLOCK_TIMEOUT_SECONDS`) plus 30 s, at least 120 s after it starts, so a crashed pod frees its slot by itself and a render that holds its turn to its deadline keeps it. |
| `out:next:{<host>}` | Earliest start time of the next request: the next turn to book. | Until the slot has passed. |
| `out:starts:{<host>}` | Real starts per turn: one entry per turn token, scored by the latest real start of a request in that turn. Set when a request is let through, moved on (never back) when its headers have gone out; entries older than twice the longest spacing are dropped. | The longest spacing plus 60 s. |
| `out:cooldown:{<host>}` | End of the pause after 429 or 503. | Until it ends. |
| `out:fail:{<host>}` | Consecutive failures (connection error or 5xx). | 10 minutes, renewed on each failure. |
| `out:circuit:{<host>}` | End of an open circuit. | Until it ends. |

A request waits for a free lease (at most `OUTBOUND_MAX_CONCURRENCY_PER_HOST`) and for its spacing
slot. The spacing is `max(OUTBOUND_MIN_DELAY_MS, min(Crawl-delay, OUTBOUND_MAX_CRAWL_DELAY_SECONDS))`
and holds between the real starts of requests of different turns. A turn books its start and
sleeps until it and until the spacing has passed since the latest request of any other turn.
Right before each of its requests the fetcher asks the gate again: the request waits out whatever
is left of the spacing since the latest request of the other turns (its own turn's requests do
not count) and is recorded under its turn, in one atomic script. The fetcher reports every
request once its headers have gone out, which moves its turn's start on to that moment. So a
request held up between the check and the send (a busy pod) delays the other turns' requests
instead of letting them follow closer; only a hold-up longer than the spacing itself can still
bring two starts closer. A request to a host whose turn is not held is not checked; its report
counts for every turn. One kind is sent today: a compliance file (robots.txt, `tdmrep.json`,
ai.txt) whose request redirects to another host follows that redirect without a lease or spacing
at the target, so it can come on top of the target's limits. That happens at most 3 times (once
per file) per origin, per pod and per `COMPLIANCE_CACHE_SECONDS`.
If a wait would exceed `OUTBOUND_MAX_WAIT_SECONDS` the request is refused as busy; the wait
before a request inside a turn counts against that fetch's time limit. A redirect to
another host gives the first host's lease back and waits for a turn at the new host; the outcome
counts for the host that answered. While a pause
or an open circuit is set, requests to that host are refused at once. A 429 or 503 sets the pause to
`Retry-After` (seconds or HTTP date) or `OUTBOUND_BACKOFF_SECONDS`, never above
`OUTBOUND_MAX_BACKOFF_SECONDS`; a new pause never shortens one that is already running.
`CIRCUIT_FAILURE_THRESHOLD` failures in a row open the circuit for `CIRCUIT_OPEN_SECONDS`; any
answer below 500 other than 429 resets the count.

Spacing, pauses and lease expiry use each pod's own clock, so the pods need NTP-synchronised
clocks.

## Job keys

A job lives in four Valkey keys. The id is a literal cluster hash tag, so one job's keys share a
slot.

| Key | Content | Lifetime |
| --- | --- | --- |
| `job:{<id>}:meta` | Hash: kind, owner, owner role, options (JSON; `ocr` only when it is not `auto`, so pods of an older release read the job during a rolling upgrade), state, counters, times, lease, and the trace id of the request that created the job. | `JOB_TTL_SECONDS` from creation, renewed on completion. |
| `job:{<id>}:urls` | List of the job's URLs in input order. | As `meta`. |
| `job:{<id>}:results` | Hash of item index to the result envelope (JSON). | As `meta`; set with the first result. |
| `job:{<id>}:done` | Bitmap of the finished item indexes. | As `meta`; set with the first result. |

A result is written once: storing the same index again changes nothing, so an item that is run a
second time after a lost lease counts once. Times are epoch seconds from the pod's clock.

Each claim of an item by a worker is recorded in one more hash, `job:{<id>}:runs`: item index to
`<claim number>|<pool>|<epoch ms>`, the item's last claim (about 40 bytes per item).
`GET /v1/jobs/{id}` shows it as the item's `run` (pool, claim number, `claimed_at`), so a job
shows where an item runs and how often it was claimed: the claim number is 1 on the first claim
and grows after a lost lease or a deferral. The worker writes it with one script per claim
before the item runs (one Valkey round trip; a failed write is logged as
`job_item_claim_not_recorded` and the item runs anyway); nothing is written for a job that is
gone. It lives as long as the job: each write takes the remaining TTL of `meta`, and the
completing result write renews it with the job's other keys (as does a site job's growth). An item
of a sync request that ran in the API pod has none.

Per API key (the job's owner) one more key lists the unfinished batches and site jobs:
`jobs:{owner:<name>}:open` is a set of job ids with a TTL of `JOB_TTL_SECONDS`, renewed on each
batch or site job. `POST /v1/batch` and `POST /v1/site-structure` remove the ids of finished or
expired jobs before they count, and refuse the job when the owner has
`BATCH_MAX_OPEN_PER_CLIENT` open ones. The name is escaped (`%` as
`%25`, `{` as `%7B`, `}` as `%7D`), so the braces always enclose exactly the name. No script
touches this key together with a job key or a queue key.

`DELETE /v1/jobs/{id}` (the owner or an admin; 404 for anyone else) cancels a job of any kind and
state. It deletes every key of the job under `job:{<id>}:` that the job store knows by name:
`meta`, `urls`, `results`, `done`, `runs`, the browser secrets, the drafts, the caller's ASR and
LLM keys, the crawl keys of a site job (below), its files (`job:{<id>}:artifact:screenshot.png`)
and the input of item 0 (an upload, whose bytes leave the input budget). Then it removes the id
from the owner's `jobs:{owner:<name>}:open`, so an admin's delete frees the owner's slot. Items
still in a queue stay there until a worker claims them; the worker drops them as items of an
expired job (`job_item_dropped`, reason `job_gone`, counted as `lost`). An item that is running, and a
handed-over request still running in an API pod, finish without storing anything: every write
checks for `meta` first, so nothing of the job comes back. The input of an item past index 0
that a batch queued for OCR or transcription (`job:{<id>}:input:<index>`) is deleted, and leaves
the budget, when a Docling or ASR worker drops the item, queued or in flight; while no such
worker runs, it expires by its own TTL. The owner's Docling and ASR sets count a deleted job out
on their next check.

The bytes of an item (an upload, a document queued for OCR, media queued for transcription) are
kept in `job:{<id>}:input:<index>` (a hash of `body` and `meta`) for `DOCLING_INPUT_TTL_SECONDS`
at most (`ASR_INPUT_TTL_SECONDS` for media queued for transcription, whose body is empty for a
URL), and deleted when the item is done. All of them together stay within `DOCLING_INPUT_BUDGET_BYTES`,
accounted in three keys of the hash tag `{budget}`: `inputs:{budget}:size` (hash of
`<id>:<index>` to the input's bytes), `inputs:{budget}:expiry` (sorted set of the same members,
scored by when the input expires, in Valkey's milliseconds) and `inputs:{budget}:total` (their
sum). A script on these keys checks and reserves an input's bytes in one step before the input is
written; it also removes the reservations whose expiry has passed, so an input that expires by
its TTL leaves the budget without anyone deleting it. Deleting an input gives its bytes back.

A job queued for transcription may keep the caller's `X-ASR-Api-Key` header in
`job:{<id>}:asr_key` (a string), for `ASR_INPUT_TTL_SECONDS` plus the ASR lease
(2 × `ASR_TIMEOUT_SECONDS` + 10) at most and never longer than the job. No read of the job shows
it; it is deleted with the job and once its item is done. `jobs:{owner:<name>}:asr` counts the
owner's unfinished ASR jobs for `ASR_MAX_OPEN_PER_CLIENT`, as `jobs:{owner:<name>}:docling` does
for `DOCLING_MAX_OPEN_PER_CLIENT`.

A job whose request may need the LLM (`structured` with strategy `llm` or `auto`, or `chunking`
`semantic`, while the service has the LLM) may keep the caller's `X-LLM-Api-Key` header in
`job:{<id>}:llm_key` (a string), as long as the job: the draft the llm worker reads lives that long,
and the item may wait for the worker until then. Every job such a request creates has it (the
request's own job, a browser, Docling, ASR, nlp or llm job, a batch's job). No read of the job shows
it; it is deleted with the job, and by whoever writes the item's result: the worker of any pool
(a sync request's job at once, a batch with its last result) and the pod that ran the whole
request. A worker that moves the item on to the llm worker keeps it.

A `site` job (`POST /v1/site-structure`) grows while it runs: its items append the pages, the
robots.txt item and the sitemaps they find to `urls`, and `total` grows with them. Its crawl lives
in four more hashes of the job's slot, each with the remaining TTL of `meta` when written, and
deleted with the job:

| Key | Content |
| --- | --- |
| `job:{<id>}:nodes` | Hash of item index to the item's role (`page`, `robots`, `sitemap`, `sitemap_part`), depth, parent, whether a sitemap was guessed, and a part's number (JSON). |
| `job:{<id>}:seen` | Hash of a URL's seen key to its item index. The key is the normal URL without its scheme and without a leading `www.`, so the http, https and `www.` spellings are one page; sitemap and robots.txt items are kept under `s:<key>`. A redirected page's final URL points to its item too. |
| `job:{<id>}:children` | Hash of an item's index to what its links or sitemap children appended; an item with more than 1,000 sends them in batches, and batch 1 and later are stored under `<index>:<batch>`. An item that runs a second time (after a lost lease) gets each batch's answer back and appends nothing. |
| `job:{<id>}:site` | Hash: the crawl's scope, the counters `pages` (against the request's `max_pages`), `files` (against `SITE_MAX_SITEMAP_FILES`) and `entries` (against `SITE_MAX_SITEMAP_URLS`), and `e:<index>` (the entries granted to one sitemap item). |

One script per batch of at most 1,000 links appends an item's finds (with batch 0 also its sitemap
parts, results included) and stores its answer, so a worker that dies halfway leaves each batch
either complete or untouched, and its rerun carries on.

## Queue keys

Batch items and the items of site jobs wait in a fair queue per worker pool (`extract` for the fast workers). The pool name is
a literal cluster hash tag, so all keys of one pool share a slot. Client names (the API key name)
and domains appear escaped in the keys (`%` as `%25`, `:` as `%3A`, tab as `%09`).

| Key | Content |
| --- | --- |
| `q:{<pool>}:clients` | List of the clients with queued items, in rotation order. |
| `q:{<pool>}:c:<client>:domains` | List of that client's domains with queued items, in rotation order. |
| `q:{<pool>}:c:<client>:d:<domain>` | List of that client's items of that domain (`<job_id>:<index>:<attempts>`). |
| `q:{<pool>}:notbefore` | Hash of domain to the time before which its items are not claimed (deferred). |
| `q:{<pool>}:running` | Hash of domain to its number of leased items. |
| `q:{<pool>}:leases` | Sorted set of leased items, scored by lease expiry. |
| `q:{<pool>}:leased` | Hash of leased item to its client, domain and lease token. |
| `q:{<pool>}:pending` | Number of queued items, claimable or deferred (leased items not counted). |

A claim rotates the clients, then the domains of the client, so a small batch is not queued behind
a large one. It skips a domain that is deferred or holds `OUTBOUND_MAX_CONCURRENCY_PER_HOST` leased
items, and looks at no more than `SCHEDULER_CLAIM_SCAN_BUDGET` client-domain pairs. Before that, it
takes back up to 20 expired leases: each item goes to the front of its domain with one more attempt.
An item past `JOB_ITEM_MAX_ATTEMPTS` is handed out once more, marked exhausted: the worker records
its failure and then completes it, and if the worker dies in between, the item comes back after its
lease. Every lease has a token, so a worker whose lease was taken back cannot complete or defer the
item under the next worker's lease. The keys have no TTL; the lists empty themselves as items are
claimed.

A worker leases an item for `EXTRACT_TIMEOUT_SECONDS * 2 + 10` seconds (60 by default) and runs it
through the fast pipeline with the job's options, as the job's owner. When the outbound gate
refuses the host (busy, or paused after errors or a 429), the item goes back to the front of its
domain, which is not claimed before the gate's retry time; an item at `JOB_ITEM_MAX_ATTEMPTS` is
stored with the gate's error (`rate_limited` or `connection_error`, retryable) instead. An item
whose job has expired is dropped. While Valkey fails, a worker backs off for 1, 2, 4 … 30 s and
then goes on; the item it held (an exhausted one too) is claimed again once its lease expires.

## Metrics

Every process exports Prometheus text on its internal port (`/metrics`, 9090). API pods and
workers are separate processes with separate registries; a metric that both record (the job
duration) has the same name, labels and buckets in each.

The job system exports these besides the HTTP and extraction metrics:

| Metric | Type | Labels | Exported by | Meaning |
| --- | --- | --- | --- | --- |
| `text_extraction_queue_pending_items` | gauge | `pool` | API pods | Items waiting in the pool's queue, claimable or deferred; leased items are not counted. Pools: `extract`, `browser` while `BROWSER_ENABLED`, `docling` while `DOCLING_ENABLED`, `asr` while `ASR_ENABLED`, `nlp` while `PII_ENABLED` and `llm` while `LLM_ENABLED` (with `JOBS_ENABLED`). |
| `text_extraction_queue_leased_items` | gauge | `pool` | API pods | Items of the pool that a worker has leased (also leases that expired and are not taken back yet). |
| `text_extraction_queue_clients` | gauge | `pool` | API pods | Clients (API key names) with items waiting. |
| `text_extraction_queue_metrics_last_success_timestamp_seconds` | gauge | `pool` | API pods | Unix time of the pod's last successful read of the queue. Exported from the first success on and left unchanged while Valkey fails. |
| `text_extraction_valkey_used_memory_bytes` | gauge | none | API pods, workers | Valkey's `used_memory` (`INFO memory`), read every 5 s; exported from the first successful read on and left unchanged while Valkey fails. Every pod reads the same Valkey: use `max()`. |
| `text_extraction_valkey_max_memory_bytes` | gauge | none | API pods, workers | Valkey's `maxmemory` (0: no limit), read with the used memory. The Helm alert `TextExtractionValkeyMemoryHigh` fires when used / max stays above `alerts.valkeyMemoryRatio` (0.8) for 10 minutes. |
| `text_extraction_event_loop_lag_seconds` | histogram | none | extract workers | How much later than asked the worker's event loop woke from a 0.5 s sleep, sampled from start to shutdown: the time it ran other work without yielding (for example packing and encoding a large sitemap's parts). Buckets 1 ms to 30 s. |
| `text_extraction_jobs_created_total` | counter | `kind` (`batch`, `extract`, `screenshot`, `file`, `site`) | API pods | Jobs accepted by the pod: a batch or a site job counts once it is enqueued and answered with 202 (a refused one, or one given back after a failed enqueue, does not); an `extract` job counts when a slow `POST /v1/extract` is handed over to a job, a `file` job when a slow `POST /v1/extract/file` is, and an `extract`, `screenshot` or `file` job when a sync request that waits for the browser, Docling, nlp or llm worker answers 202 with that job, or one that needs a transcription answers 202 with its ASR job. |
| `text_extraction_job_duration_seconds` | histogram | `kind` | workers (`batch`, `site`), API pods (`extract`) | Time from the creation of a job to its last result. Observed once per job, by the process that writes the last result. Buckets 1 s to 24 h. |
| `text_extraction_browser_dispatch_total` | counter | `outcome` (`rendered`, `not_claimed`, `no_result`, `handed_over`, `backend_error`) | API pods (with `BROWSER_ENABLED`) | Sync requests (`POST /v1/extract`, `POST /v1/screenshot`) the pod sent to the browser workers, by how the wait ended: `rendered` (the worker's result came back), `not_claimed` (no worker took the page within `BROWSER_QUEUE_WAIT_SECONDS`), `no_result` (taken, but no result by the render deadline + 5 s, or the job was gone), `handed_over` (answered 202; the worker writes the job's result) or `backend_error` (Valkey failed). `not_claimed`, `no_result` and `backend_error` answer `auto` with its fast result and the warning `browser_unavailable`, and `browser` and screenshots with `timeout` (or 503): alert on their rate to see sync requests that silently fell back to fast mode. Every outcome is exported as 0 from the start. Use `sum()` over pods. |
| `text_extraction_docling_dispatch_total` | counter | `outcome` (`answered`, `not_claimed`, `no_result`, `handed_over`, `backend_error`, `memory_guard`, `open_limit`, `input_budget`) | API pods and extract workers (with `DOCLING_ENABLED` and `JOBS_ENABLED`) | Sync requests (`POST /v1/extract`, `POST /v1/extract/file`) that needed OCR, by how the hand-over to the Docling workers ended: `answered` (the worker's result came back), `not_claimed` (no worker claimed the item within `DOCLING_SYNC_WAIT_SECONDS`), `no_result` (claimed, but no result in time, or the job was gone), `handed_over` (answered 202, or the document joined the request's own job), `backend_error` (Valkey failed), `memory_guard` (Valkey's memory guard was high), `open_limit` (the key had `DOCLING_MAX_OPEN_PER_CLIENT` unfinished Docling jobs) or `input_budget` (the inputs of all jobs would exceed `DOCLING_INPUT_BUDGET_BYTES`); the last three dispatch nothing. Extract workers count the batch items they could not move to the Docling workers (`memory_guard`, `input_budget`). Every outcome but `answered` and `handed_over` answers without OCR (`ocr_unavailable`, or a retryable failure for an image). Every outcome is exported as 0 from the start. Use `sum()` over pods. |
| `text_extraction_asr_dispatch_total` | counter | `outcome` (`handed_over`, `backend_error`, `memory_guard`, `open_limit`, `input_budget`) | API pods and extract workers (with `ASR_ENABLED` and `JOBS_ENABLED`) | Media that needed a transcription (`transcribe: true` on `POST /v1/extract`, an audio or video upload on `POST /v1/extract/file`), by how the hand-over to the ASR workers ended: `handed_over` (the job was queued and answered 202, or the media joined the request's own job), `backend_error` (Valkey failed), `memory_guard` (Valkey's memory guard was high), `open_limit` (the key had `ASR_MAX_OPEN_PER_CLIENT` unfinished ASR jobs) or `input_budget` (the inputs of all jobs would exceed `DOCLING_INPUT_BUDGET_BYTES`); the last four answer without a transcription (`asr_unavailable`, or a retryable failure for a media file or an upload). Nothing waits for a transcription, so there is no `answered`. Extract workers count the batch items they could not move to the ASR workers (`memory_guard`, `input_budget`). Every outcome is exported as 0 from the start. Use `sum()` over pods. |
| `text_extraction_nlp_dispatch_total` | counter | `outcome` (`answered`, `not_claimed`, `no_result`, `handed_over`, `backend_error`, `memory_guard`) | API pods and the workers of the other pools (with `PII_ENABLED` and `JOBS_ENABLED`) | Results with `pii` the pod sent to the nlp workers, by how the hand-over ended: `answered` (the processed result came back), `not_claimed` (no worker claimed the item in time), `no_result` (claimed, but no result in time, or the job was gone), `handed_over` (answered 202, or the result joined the request's own job), `backend_error` (Valkey failed) or `memory_guard` (Valkey's memory guard was high: nothing was dispatched; workers count their refused moves here). Every outcome but `answered` and `handed_over` answers `pii_failed` without the text. Every outcome is exported as 0 from the start. Use `sum()` over pods. |
| `text_extraction_llm_dispatch_total` | counter | `outcome` (`answered`, `not_claimed`, `no_result`, `handed_over`, `backend_error`, `memory_guard`, `input_budget`) | API pods and the workers of the other pools (with `LLM_ENABLED` and `JOBS_ENABLED`) | Results whose `structured`, `chunking` or `describe_images` needs the LLM, sent to the llm workers, by how the hand-over ended: `answered` (the llm worker's result came back), `not_claimed` (no worker claimed the item in time), `no_result` (claimed, but no result in time, or the job was gone), `handed_over` (answered 202, or the result joined the request's own job), `backend_error` (Valkey failed), `memory_guard` (Valkey's memory guard was high: nothing was dispatched; workers count their refused moves here) or `input_budget` (an image upload's bytes for `describe_images` did not fit `DOCLING_INPUT_BUDGET_BYTES`: nothing was dispatched). `not_claimed`, `no_result`, `backend_error`, `memory_guard` and `input_budget` answer the content without the LLM work (`partial`/`llm_failed`, retryable). Every outcome is exported as 0 from the start. Use `sum()` over pods. |
| `text_extraction_asr_transcriptions_total` | counter | `backend` (`ASR_BACKEND`: `local` or `openai`), `outcome` (`success` or the error code) | ASR workers | Media the ASR worker handled: `success` (a transcript), or why there is none (`media_too_long`, `transcription_failed`, `timeout`, `too_large`, `target_http_error`, `no_transcript_available` for media without speech, ...). A copy of an item whose result was already written is not counted. |
| `text_extraction_asr_audio_seconds_total` | counter | `backend` | ASR workers | Seconds of audio transcribed (as the speech recognition measured them; with `openai` the decoded audio sent, the cost basis). |
| `text_extraction_asr_seconds` | histogram | `backend` | ASR workers | Seconds of one transcription call (decoding and speech recognition; not the download), the wait for the child included. Buckets 5 s to 4 h. |
| `text_extraction_llm_calls_total` | counter | `kind` (`chat`, `vision`, `embeddings`), `outcome` (`ok`, `invalid`, `error`) | llm workers, API pods (with `LLM_ENABLED`) | Calls to the LLM endpoint, one per request that got an answer or a failure (the SDK's own retries are not counted). `invalid`: an answer that is no JSON, does not fit the schema or was cut off (it gets one repair call); `error`: the endpoint failed (status, timeout, connection). |
| `text_extraction_llm_tokens_total` | counter | `kind` (`prompt`, `completion`) | llm workers, API pods (with `LLM_ENABLED`) | Tokens the endpoint reported, over all models. Never a label of a prompt, text, value or key. |
| `text_extraction_vision_images_total` | counter | `outcome` (`described`, `skipped`, `failed`) | llm workers, and the API pods and other workers that answer `describe_images` without the LLM work or skip an image upload the model cannot take (with `LLM_ENABLED`) | Images of `describe_images`, counted once per written result: `described` (a description came back), `skipped` (not fetched: refused by the SSRF guard or the compliance checks, an HTTP error, busy host, not PNG, JPEG, GIF or WebP, over `LLM_VISION_MAX_IMAGE_BYTES`, an upload's input that was gone, still being fetched when the step's time ran out with no vision call outstanding) or `failed` (the endpoint refused it, its answer did not fit, it found nothing to describe, or the step stopped or never ran). Never a label of an image, URL, alt text or description. |
| `text_extraction_document_conversions_total` | counter | `converter` (`markitdown`, `docling`), `kind` (`pdf`, `docx`, ...), `outcome` (`success` or the error code) | API pods, workers (extract, browser, docling) | Document conversions, where they run: MarkItDown in API pods (URLs and uploads) and in the extract and browser workers, Docling in the Docling workers. |
| `text_extraction_document_conversion_seconds` | histogram | `converter` | API pods, workers | Seconds of one conversion, the wait for a free conversion process included. Buckets 0.1 s to 600 s. |
| `text_extraction_document_pages_total` | counter | `converter` | API pods, workers | Pages converted (after `DOCUMENT_MAX_PAGES` or `DOCLING_MAX_PAGES`). |
| `text_extraction_site_items_total` | counter | `role` (`page`, `robots`, `sitemap`, `unknown` for an item without a crawl record), `outcome` (`success` or the error code of the result) | extract workers | Items of site jobs the worker ran. A further part of a large sitemap (`sitemap_part`) is written with its sitemap and not counted. |
| `text_extraction_site_links_total` | counter | `crawl` (`listed`, `file`, `out_of_scope`, `nofollow`, `blocked`, `depth`, `queued`, `seen`, `max_pages`, `max_files`, `memory_guard`) | extract workers | Links of crawled pages by what the crawl did with them. Never by URL or host. |
| `text_extraction_site_caps_total` | counter | `cap` (`pages`, `files`, `urls`, `links`, `memory`) | extract workers | Site job items that ran into a cap: `max_pages`, `SITE_MAX_SITEMAP_FILES`, `SITE_MAX_SITEMAP_URLS`, `SITE_MAX_LINKS_PER_PAGE` (or the item size), or the memory guard. |
| `text_extraction_mcp_tool_calls_total` | counter | `tool`, `outcome` (`success`, `partial`, `failed`, `job` or the error code of a refusal or error result) | API pods (with `MCP_ENABLED`) | MCP tool calls. `tool` is the tool's name, or `unknown` for a name the server has no tool of (a caller's text is never a label). Every tool is exported with `success` at 0 from the start. Use `sum()` over pods. |
| `text_extraction_mcp_tool_seconds` | histogram | `tool` | API pods (with `MCP_ENABLED`) | Seconds of one MCP tool call, the rate limit check included. Buckets 0.05 s to 120 s. |
| `text_extraction_ui_image_previews_total` | counter | `outcome` (`shown`, `time`, `blocked`, `refused`, `too_large`, `unsupported`, `http_error`, `error`, `busy`) | API pods (with `UI_ENABLED`) | Preview images the UI fetched for "Load previews": `shown` (inlined) or why one was skipped. Every outcome is exported at 0 from the start. Never a label of a URL or an alt text. |
| `text_extraction_job_items_total` | counter | `outcome` | workers | Items handled: `success`, the error code of the result, `deferred` (given back while the host is busy or paused), `lost` (the job was gone) or `escalated` (an `auto` item the extract worker moved to the browser pool). |
| `text_extraction_job_item_seconds` | histogram | none | workers | Time one item took in the worker, from the claim to the stored result. |
| `text_extraction_worker_busy_slots` | gauge | none | workers | Slots of the worker that are processing an item. |

The browser worker adds these (its own registry; `driver` is `playwright` for the standard browser
and `patchright` for unblock mode):

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `text_extraction_browser_pages_in_use` | gauge | `driver` | Browser pages open now. |
| `text_extraction_browser_pages_total` | counter | `driver`, `outcome` | Pages by outcome: `ok`, `error` (the target or a refusal), `crash`, `timeout`. |
| `text_extraction_browser_render_seconds` | histogram | `driver` | Seconds a page was open. |
| `text_extraction_browser_launches_total` | counter | `driver`, `reason` | Browser launches: `start`, `recycle`, `crash`, `page` (unblock: one per page). |
| `text_extraction_browser_proxy_refused_total` | counter | `reason` | Requests the egress proxy refused: `blocked_address`, `bytes_cap`, `dns_error`, `connect_error`, `bad_request`, `auth`. |

The queue gauges come from the API pods and not from the workers, because workers may scale to
zero and the autoscaler (KEDA) must still see the backlog. Only API pods with `JOBS_ENABLED=true`
export them, so KEDA and the queue alerts of the Helm chart need such API pods (the chart rejects
KEDA with `jobs.enabled=false`). Every API pod reads the same queue, so
**query them with `max()` over the pods** (for example
`max(text_extraction_queue_pending_items{pool="extract"})`); `sum()` would multiply the backlog by
the number of API pods, and `avg()` is only right while all pods have read it at the same moment.
The same holds for `text_extraction_queue_leased_items` and `text_extraction_queue_clients`.
`text_extraction_jobs_created_total` is per pod: use `sum()` over pods for the total.

A pod reads the queue in the background every 5 seconds (collectors cannot wait for Valkey; the
read is three size lookups, whatever the queue holds); the gauges are up to 5 s old. A pod exports no queue gauge until its first
read has succeeded, so an empty series means "not known", never "empty queue". While Valkey fails
the pod keeps exporting the last values and logs `queue_metrics_unavailable` once; it logs
`queue_metrics_recovered` when the reads work again. To alert on stale data, use
`time() - text_extraction_queue_metrics_last_success_timestamp_seconds > 60` (per pod; with
`max()` over the series the alert fires only when no pod has read the queue lately). The Helm
alert `TextExtractionQueueMetricsStale` uses `max()` and 120 s. With
`JOBS_ENABLED=false` the pod does not
read the queue and exports no queue gauge.

## Resource baselines

The Helm chart's requests and limits come from measurement under load, not from estimates (ops
guide, section 5.3). The load test (`.github/workflows/load.yml`, `tests/load/`) starts the
compose stack with every profile (one container per service) plus a local site farm and a fake
OpenAI-compatible server, runs each scenario alone, and samples `docker stats` every 2 s. A
scenario's numbers come only from the samples inside its window (its first request to its last
answer plus a settle time). CPU is in % of one core. Memory is `MemUsage` in MiB: cgroup usage
without the page cache. Percentiles are nearest-rank.

Source runs, both on a GitHub `ubuntu-latest` runner (4 vCPU, 16 GB), on 2026-10-31:

- **Run 4:** [41190110782](https://github.com/janschachtschabel/text-extraction/actions/runs/41190110782),
  commit `ad80ae4`.
- **Run 6:** [41194228955](https://github.com/janschachtschabel/text-extraction/actions/runs/41194228955),
  commit `b7499cc`. It adds the site structure scenarios and the event-loop lag.

Both runs used the production defaults for the models: ASR `large-v3-turbo` (int8, `ASR_THREADS`
4) and PII `lg`. **ASR `small` and PII `md` were not measured.** Each run also swept
`STRUCTURED_SELECTOR_PROCESSES` (1, 2, 4) on fresh stacks with the selector scenario only.

**Load profile.** All targets are pages of the local farm; no request leaves the compose network.

| Scenario | Requests (at once) | What it sends | p50 / p95 ms, run 4 | p50 / p95 ms, run 6 | Requests/s, run 4 / 6 |
| --- | --- | --- | --- | --- | --- |
| `sync-fast-c10` | 300 (10) | `POST /v1/extract`, fast mode | 398 / 609 | 395 / 615 | 22.7 / 22.5 |
| `sync-fast-c30` | 900 (30) | the same at 30 clients | 436 / 646 | 531 / 778 | 68.3 / 54.5 |
| `batch-1000` | 1 batch of 1,000 URLs | `POST /v1/batch`, until done | 50,136 | 50,152 | - |
| `browser-100` | 100 (10) | `POST /v1/extract`, browser mode | 3,917 / 4,444 | 3,928 / 4,925 | 2.5 / 2.3 |
| `upload-docs-50` | 50 (5) | `POST /v1/extract/file`, MarkItDown documents | 1,541 / 2,436 | 2,092 / 2,813 | 3.0 / 2.2 |
| `upload-ocr-10` | 10 (2) | the same, a scanned PDF with `ocr: always` (Docling) | 8,959 / 9,963 | 11,974 / 13,984 | 0.21 / 0.17 |
| `pii-200` | 200 (10) | `POST /v1/extract` with `pii: mask` | 1,052 / 1,440 | 1,332 / 1,875 | 9.2 / 7.2 |
| `asr-10` | 10 (2) | audio uploads (a 30 s clip), each a job | 32,109 / 32,133 | 56,249 / 58,285 | - |
| `structured-selectors-200` | 200 (10) | `POST /v1/structured`, selectors | 422 / 606 | 370 / 609 | 24.1 / 24.7 |
| `structured-llm-50` | 50 (5) | `POST /v1/structured`, LLM (fake, 800 ms) | 1,739 / 2,232 | 1,831 / 2,428 | 2.8 / 2.6 |
| `chunk-semantic-50` | 50 (5) | `POST /v1/chunk`, `semantic` (fake embeddings) | 1,797 / 2,824 | 1,816 / 2,353 | 2.7 / 2.6 |
| `site-crawl` | 1 crawl | `POST /v1/site-structure` at the default caps | 2,010 | 6,024 | - |
| `site-crawl-max-pages` | 1 crawl | the same at `SITE_MAX_PAGES` (1,000) | - | 52,161 | - |
| `sitemap-200k` | 1 crawl | one sitemap of `SITE_MAX_SITEMAP_URLS` (200,000) entries | - | 10,042 | - |
| `sitemap-50m-1` | 1 crawl | one 50 MiB sitemap | - | 4,022 | - |
| `sitemaps-50m-3` | 1 crawl | three 50 MiB sitemaps in flight at once | - | 10,039 | - |
| `mcp-20` | 20 (5) | MCP tool calls | 493 / 860 | 436 / 938 | 9.4 / 9.4 |

Every request of both runs had its expected outcome (run 4's `site-crawl` was `partial`, as
expected while the farm had no linked pages; run 6's is `success`). Runner speed varies: run 6's ASR clips took
56 s against run 4's 32 s on the same code path.

**Sizing rule.** Per component, over every scenario of both runs:

- memory request = the largest memory p95, rounded up to the next 64Mi;
- CPU request = the largest CPU mean, rounded up to the next 50m (with 2 s samples the CPU p95 is
  a burst; the mean is the sustained demand);
- limit = 1.5 x the largest peak, rounded up to the next 256Mi or 250m;
- the memory limit is never below the value that backs a child process's address-space budget:
  `DOCUMENT_MEMORY_LIMIT_MB` (the chart derives it from the limit), `DOCLING_MEMORY_LIMIT_MB`,
  `ASR_MEMORY_LIMIT_MB`, `PII_MEMORY_LIMIT_MB` and `STRUCTURED_SELECTOR_MEMORY_MB`. The load's
  documents, audio and texts are small and cannot show the worst case those budgets allow. Where
  this floor applies, the table gives both numbers.

| Component (compose service) | Image | Scenario of the peak | CPU mean / p95 / max % | Memory p95 / max MiB | Model share (idle MiB) | Request | Limit | Source |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| API pods (`api`) | `text-extraction` | `upload-docs-50` (CPU), `pii-200` (memory) | 139.4 / 239.5 / 239.5 | 808 / 808 | none (584, 580) | 1400m / 832Mi | 3750m / 2560Mi (rule: 1280Mi; floor: 2 document children of 768 MiB after the 1024 MiB reserve) | CPU run 6, memory run 4 |
| Extract workers (`worker`) | `text-extraction` | `sitemap-200k` (CPU mean), `sitemap-50m-1` (CPU peak), `sitemaps-50m-3` (memory) | 43.7 / 113.0 / 113.0 | 766 / 766 | none (544, 550) | 450m / 768Mi | 1750m / 2560Mi (rule: 1280Mi; floor as for the API) | run 6 |
| Browser workers (`browser-worker`) | `text-extraction-browser` | `browser-100` | 72.7 / 122.3 / 148.5 | 1137 / 1147 | none: Chromium (674, 842) | 750m / 1152Mi | 2250m / 4608Mi (rule: 1792Mi; floor: 2 document children of 768 MiB after the 3072 MiB reserve) | run 6 |
| Docling workers (`docling-worker`) | `text-extraction-docling` | `upload-ocr-10` | 222.2 / 339.1 / 357.6 | 1540 / 1549 | 1139, 1243 (756 to 1061 on the fresh sweep stacks) | 2250m / 1600Mi | 5500m / 8Gi (rule: 2560Mi; floor: `DOCLING_MEMORY_LIMIT_MB` 6144 for one child) | CPU mean run 6, the rest run 4 |
| ASR workers (`asr-worker`), `large-v3-turbo` | `text-extraction-asr` | `asr-10` | 301.6 / 354.6 / 359.4 | 1350 / 1380 | 1192, 1146 | 3050m / 1408Mi | 5500m / 6Gi (rule: 2304Mi; floor: `ASR_MEMORY_LIMIT_MB` 4096 for one child) | CPU run 6, memory run 4 |
| ASR workers, `small` | `text-extraction-asr` | not measured | - | - | - | as above | as above | - |
| NLP workers (`nlp-worker`), `lg` | `text-extraction-nlp` | `pii-200` | 74.6 / 130.7 / 130.7 | 1704 / 1704 | 1675, 1690 | 750m / 1728Mi | 2 / 3Gi (rule: 2560Mi; floor: `PII_MEMORY_LIMIT_MB` 3072 for one child) | run 6 |
| NLP workers, `md` | `text-extraction-nlp` | not measured | - | - | - | as above | as above | - |
| llm workers (`llm-worker`) | `text-extraction` | `chunk-semantic-50` (CPU), idle with 4 selector processes (memory; 204 MiB at the default 1) | 10.4 / 44.1 / 44.1 | 253 / 253 | none (138, 138) | 150m / 256Mi | 750m / 1Gi (rule: 512Mi; floor: the selector child's 512 MiB plus `describe_images`, which the load did not send) | CPU run 6, memory run 4 |
| Bundled Valkey (`valkey`) | `valkey/valkey` | `site-crawl-max-pages` (CPU mean), `sitemap-200k` (CPU peak), `sitemaps-50m-3` (memory) | 3.6 / 12.7 / 12.7 | 53 / 53 | none (4, 4) | 500m / 1280Mi (rule: 50m / 64Mi) | 500m / 1280Mi (rule: 250m / 256Mi) | run 6 |

Model share: for a worker that loads a model (Docling, ASR, NLP) it is the memory in the `idle`
scenario of run 4 and run 6, which includes the worker's Python process. The components without
a model give their idle memory in brackets. Valkey keeps its sizes: one instance serves every replica of every
pool (the load ran one pod per pool), its memory request equals its limit (Guaranteed QoS) and its
limit is sized for `valkey.maxmemory` (60 %), that is for the results and documents held at once,
not for this load.

**Selector processes (P11-S5, issue #19).** The selector scenario on a fresh stack per value:

| `STRUCTURED_SELECTOR_PROCESSES` | Requests/s, run 4 / 6 | p95 ms, run 4 / 6 | API memory p95 MiB, run 4 / 6 | API memory idle MiB, run 4 / 6 |
| ---: | --- | --- | --- | --- |
| 1 | 23.2 / 22.7 | 605 / 660 | 574 / 584 | 567 / 567 |
| 2 | 24.0 / 21.5 | 622 / 650 | 610 / 611 | 604 / 604 |
| 4 | 22.9 / 22.7 | 623 / 681 | 681 / 679 | 674 / 672 |

More children buy no throughput at this load, and each extra child costs about 36 MiB. The
default stays 1, the smallest value within 10 % of the best throughput (run 4's 24.0 req/s).

**Site structure (P11-S7, run 6).**

| Scenario | Items | Job s | Valkey MiB of the job | Extract worker memory max MiB | Event-loop lag: total s, samples over 100 ms, max s |
| --- | ---: | ---: | ---: | ---: | --- |
| `site-crawl` | 102 | 6.0 | 0.4 | 563 | 0.0, 0, 0.005 |
| `site-crawl-max-pages` | 1,002 | 51.2 | 4.2 | 563 | 0.2, 0, 0.1 |
| `sitemap-200k` | 15 | 8.9 | 12.8 | 712 | 0.9, 1, 1 |
| `sitemap-50m-1` | 6 | 3.8 | 3.2 | 686 | 0.1, 0, 0.01 |
| `sitemaps-50m-3` | 14 | 8.9 | 9.5 | 766 | 1.1, 2, 0.5 |

A crawl at `SITE_MAX_PAGES` takes about a minute against `JOB_TTL_SECONDS` (24 h). The extract
worker's event loop is held for at most about 1 s by the largest sitemap. Three 50 MiB sitemaps
take 2.3 x one, because the pod's one sitemap thread parses them in turn: about 5 s in the worst
case. `SITE_PARSE_CONCURRENCY` is therefore not added (architecture.md, "Site structure").
