# Installation

Deutsch: [Installation](../de/installation.md)

The service runs in three ways: locally with uv (for development), with Docker Compose (one
machine) and on Kubernetes with the Helm chart. Every way needs a Valkey (or Redis 7) for the
shared state: jobs, results, the cache and the rate limits. The service itself keeps no state.

## Local with uv

Requires Python 3.13, [uv](https://docs.astral.sh/uv/) and a Valkey on `localhost:6379`.

```bash
uv sync                                                     # install the dependencies into .venv
docker run -d --name valkey -p 6379:6379 valkey/valkey:8.1-alpine
export AUTH_MODE=none                                       # in each terminal below
UI_ENABLED=true uv run text-extraction                      # terminal 1: API on http://localhost:8080
INTERNAL_PORT=9091 uv run text-extraction-worker            # terminal 2: extract worker (jobs)
```

`AUTH_MODE=none` lets anyone who reaches the port use the service: use it on your own machine
only, and only while you need it. It answers only the host names of `ALLOWED_HOSTS` (`localhost`,
`127.0.0.1`, `[::1]` and `api`), so a web page that points its own domain at your machine (DNS
rebinding) is refused. Every process reads the same settings, so each terminal needs
`AUTH_MODE=none` (or the same `API_KEYS`), and each serves its internal port: give every further
process its own `INTERNAL_PORT`. The browser worker needs the extra `browser` and Chromium
(`uv sync --extra browser`, then `uv run playwright install chromium`); start it with
`INTERNAL_PORT=9092 uv run text-extraction-browser-worker` and set `BROWSER_ENABLED=true` on the
API, the extract worker and the browser worker.

For development:

```bash
uv run pytest                # run the tests
uv run ruff check .          # lint
uv run ruff format .         # format
uv run mypy                  # type-check (strict)
uv run pre-commit install    # install the git hooks
```

## Docker Compose

Requires Docker with Compose. Compose starts the API, an extract worker, a browser worker, the
optional workers (OCR, transcription, personal data, LLM) and a Valkey with a password. It switches
on browser mode, OCR, transcription, personal data handling, the LLM, the MCP server and the
playground. There are two compose files:

- `docker-compose.yml` runs the published images from `ghcr.io/janschachtschabel/text-extraction`
  (linux/amd64) and starts without a single variable: for a server, and for Docker panels such as
  Hostinger's Docker Manager.
- `docker-compose.build.yml` builds the images from your checkout: for development.

### Published images

```bash
cp .env.example .env                           # optional: the file starts without it
docker compose up -d --wait                    # public API on http://localhost:8080
docker compose logs init                       # the generated admin key (admin_key), shown once
KEY=$(docker compose run --rm -T init text-extraction-init --show-admin-key)
curl -H "Authorization: Bearer $KEY" http://localhost:8080/v1/info
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/"}' http://localhost:8080/v1/extract
```

On its first start the one-shot service `init` creates the secrets in the volume `secrets` and
keeps them afterwards: the Valkey password and an admin API key. The key appears once in the log
of `init`; the `--show-admin-key` command above prints it at any time. Your own keys go into
`API_KEYS` in `.env` (see below); they count together with the generated one. The internal port
9090 (`/health`, `/ready`, `/metrics`) is not published. The playground is at
http://localhost:8080/ui and the MCP endpoint at http://localhost:8080/mcp.

The file follows the newest `0.1.x` release on its own: its default image tag is `0.1`, and every
`docker compose up` (a panel's redeploy too) pulls it again, so it needs ghcr.io; a container that
Docker merely restarts keeps its image. `TEXT_EXTRACTION_VERSION` in `.env` or the shell pins one
release, for example `0.1.10`; set it only when you want that. This file needs `0.1.8` or later:
older images have no `init` service. The releases and their notes are on the
[releases page](https://github.com/janschachtschabel/text-extraction/releases).

For HTTPS, the `api` service carries labels for a Traefik in front of it (Hostinger's VPS runs
one): set `DOMAIN` in `.env` to the bare host name whose DNS points at the server, for example
`api.example.org`, without `https://`. `API_PORT=127.0.0.1:8080` then keeps port 8080 to the
server itself. The [configuration reference](../../configuration.md#root-compose-file-published-images)
(English) describes the secrets, the Traefik settings and the network.

### Hostinger VPS

The Docker Manager of a Hostinger VPS deploys the root `docker-compose.yml` straight from the
repository. Plan a VPS with 8 GB of memory or more: with every worker the stack takes about
6 GiB when idle.

1. Docker Manager, Compose, "Compose from URL": enter
   `https://github.com/janschachtschabel/text-extraction`, keep the suggested name
   (`text-extraction`) and deploy. The first deploy downloads the images and the models (about
   3 GB of models) and takes several minutes.
2. Open the project's logs, or the web console, and take the admin key from the service `init`
   (event `init_admin_key_created`).
3. Edit the project: set `DOMAIN` in `.env` (for example the VPS host name
   `srvNNNNNN.hstgr.cloud` or your own domain with an A record pointing at the VPS), optionally
   `API_PORT=127.0.0.1:8080`, then deploy again. Traefik gets the certificate itself. Until
   `DOMAIN` is set, Traefik's log shows an error about an empty `Host()` rule: that is harmless.
4. Open `https://<DOMAIN>/ui` and log in with the key.

A failed deploy shows only "Deployment failed" or "Project not found"; the web console (a root
shell) has the real message:

```bash
cat /docker/text-extraction/.build.log                 # the real error of a failed deploy
cd /docker/text-extraction && docker compose logs init  # the admin key
docker compose run --rm -T init text-extraction-init --show-admin-key
```

### Build from the checkout

```bash
cp .env.example .env                           # then set API_KEYS and VALKEY_PASSWORD in it
KEY=$(openssl rand -hex 32)                    # your API key: keep it
printf %s "$KEY" | sha256sum | cut -d' ' -f1   # its sha256: replace the zeros in API_KEYS with it
openssl rand -hex 24                           # a VALKEY_PASSWORD
docker compose -f docker-compose.build.yml up --build -d --wait
```

`API_KEYS` holds the SHA-256 of each key, never the key itself. `.env.example` has one admin key
named `local`, whose SHA-256 you replace; [API keys](usage.md#api-keys) explains the keys and
their roles. This file has no `init` service: it needs `API_KEYS` and `VALKEY_PASSWORD` in `.env`.

### Optional workers

The heavier parts run in workers of their own. Compose starts all of them, and each has a switch
in `.env` that is `true` by default:

| Worker | Switch in `.env` | Services | What it adds |
| --- | --- | --- | --- |
| Docling | `DOCLING_ENABLED` | `docling-models`, `docling-worker` | OCR for scans and images, OpenDocument files (an image of about 2 GB, models of about 335 MiB) |
| ASR | `ASR_ENABLED` | `asr-models`, `asr-worker` | Speech recognition for audio and video (the Whisper model, about 1.6 GB; `ASR_MODEL=small`, about 0.5 GB) |
| NLP | `PII_ENABLED` | `nlp-models`, `nlp-worker` | Personal data handling with Presidio and spaCy (models of about 1 GB for `lg`, 115 MB for `md`) |
| LLM | `LLM_ENABLED` | `llm-worker` | Structured output with an LLM, semantic chunking and image descriptions |

The first start downloads the models into volumes (about 3 GB in all); with every worker the stack
takes about 6 GiB of memory when idle. To run without a worker, set its switch to `false` in `.env`
and leave its services out, for example
`docker compose up -d --wait --scale docling-models=0 --scale docling-worker=0` (with
`-f docker-compose.build.yml` for the build file). An `.env` copied
from an older `.env.example` still has the switches at `false`: set them to `true`.

The LLM worker needs `LLM_BASE_URL` and `LLM_MODEL` of an OpenAI-compatible endpoint and a key:
`LLM_API_KEY` in `.env`, or the caller's `X-LLM-Api-Key` (the playground's key field); without
either, a request that names LLM work answers 400, and the strategy `auto` runs without the LLM. `LLM_EMBEDDING_MODEL` is for semantic chunking;
`docker compose run --rm llm-worker text-extraction-llm-worker --check` sends one tiny
extraction to the endpoint. For `pii: hash` with the same hash in every result, set
`PII_HASH_KEY` (`openssl rand -hex 32`).

While a switch is off, a request that needs its worker answers 400: `pii` without
`PII_ENABLED`, the strategy `llm`, `learn_selectors` and `semantic` chunking without
`LLM_ENABLED`, `describe_images` without `LLM_ENABLED`, `transcribe` without `ASR_ENABLED`, and
`ocr: always` without `DOCLING_ENABLED`. With `ocr: auto` (the default) a scan without
`DOCLING_ENABLED` answers its text layer with the warning `ocr_unavailable`.

To keep a feature for admin keys, set its second switch to `false`: `OCR_ALLOW_USERS`,
`ASR_ALLOW_USERS`, `PII_ALLOW_USERS` or `LLM_ALLOW_USERS` (all `true` by default). A user key
that asks for the feature then gets 403 `forbidden`: `ocr: always`, `transcribe` or an audio or
video upload, `pii`, and the strategy `llm`, `learn_selectors`, `semantic` chunking or
`describe_images`. Where a user key does not ask by name, the request runs without the feature:
a scan gets no OCR (`ocr_unavailable`), and the strategy `auto` asks no LLM for the fields still missing
(warning `llm_unavailable`). The playground shows such a choice as disabled, with a hint. Admin
keys may always use every feature the service has.

Maximum extraction (for sites that allowed extraction by contract) needs
`BROWSER_UNBLOCK_ENABLED=true`, which docker compose and `.env.example` set since 0.1.9 (an
admin still has to choose `compliance: ignore`); `false` in `.env` switches it off. See
[Site rules](site-rules.md#maximum-extraction).

## Kubernetes with Helm

The [Helm chart](../../../deploy/helm/text-extraction/README.md) deploys the API and the workers
with a Valkey of its own or yours; each worker, the MCP server and the playground are switches
in its values. Its README lists the values and the secrets.

## Key settings

Every setting is an environment variable with a default. These are the ones a user or an
operator touches most; the [configuration reference](../../configuration.md#environment-variables)
(English) lists all of them, with the [secrets](../../configuration.md#secrets).

| Setting | Default | What it does |
| --- | --- | --- |
| `AUTH_MODE` | `api_key` | `api_key`: every request needs a key. `none`: no keys, every caller is an admin. |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1],api` | With `AUTH_MODE=none`: the host names the service answers; `*` answers every one. |
| `API_KEYS` | empty | Secret. The keys as a JSON list of `name`, `role` (`user` or `admin`) and `sha256`. |
| `API_KEYS_FILE` | unset | A JSON file of the same shape, merged with `API_KEYS`. |
| `VALKEY_URL` | `redis://localhost:6379/0` | The Valkey for the shared state; a secret when it holds a password. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. |
| `LOG_FORMAT` | `json` | `json` for production, `console` for local development. |
| `RATE_LIMIT_PER_MINUTE` | `60` | Requests per minute and key; over it a request answers 429. |
| `COMPLIANCE_DEFAULT` | `enforce` | The site rules when a request names none: `enforce`, `report` or `ignore`. |
| `FETCH_TIMEOUT_SECONDS` | `20` | Time for one fetch of a page. |
| `FETCH_MAX_BYTES` | `20000000` | Most bytes of one page or file. |
| `OUTBOUND_MAX_CONCURRENCY_PER_HOST` | `2` | Most requests at the same time to one host. |
| `OUTBOUND_MIN_DELAY_MS` | `500` | Least time between two requests to one host. |
| `OUTBOUND_BACKOFF_SECONDS` | `30` | The pause after a 429 or 503 of a host without `Retry-After`. |
| `JOBS_ENABLED` | `true` | Jobs: batches, site structures, `wait_seconds` and screenshots. |
| `JOB_TTL_SECONDS` | `86400` | How long a job and its results are kept. |
| `BATCH_MAX_URLS` | `1000` | Most URLs of one batch. |
| `BATCH_MAX_OPEN_PER_CLIENT` | `5` | Most unfinished batches and site structures of one key. |
| `BROWSER_ENABLED` | `false` | Browser mode: pages that need JavaScript are rendered in Chromium. |
| `BROWSER_QUEUE_WAIT_SECONDS` | `10` | How long a request waits for a browser worker to take its page. |
| `BROWSER_UNBLOCK_ENABLED` | `false` | Allows maximum extraction (unblock mode) for admins. |
| `DOCUMENT_MAX_BYTES` | `20000000` | Most bytes of an uploaded document. |
| `DOCLING_ENABLED` | `false` | OCR and OpenDocument files in the Docling worker. |
| `ASR_ENABLED` | `false` | Speech recognition in the ASR worker. |
| `PII_ENABLED` | `false` | Personal data handling in the NLP worker. |
| `LLM_ENABLED` | `false` | LLM work in the LLM worker. |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | The OpenAI-compatible endpoint. |
| `LLM_API_KEY` | unset | Secret. The operator's key; a caller's `X-LLM-Api-Key` wins over it. |
| `SITE_MAX_PAGES` | `1000` | Most pages one site structure may ask for. |
| `MCP_ENABLED` | `false` | The MCP server at `/mcp`. |
| `UI_ENABLED` | `false` | The playground at `/ui`. |
| `UI_DEFAULT_LANGUAGE` | `en` | The playground's language when the browser asks for neither `en` nor `de`. |
| `UI_DOCS_BASE_URL` | `https://github.com/janschachtschabel/text-extraction/blob/main/docs/user-guide` | The user guide the playground's help links open; point it at your own copy for a fork or an air-gapped deployment. |
