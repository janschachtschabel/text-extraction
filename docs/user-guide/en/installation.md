# Installation

Deutsch: [Installation](../de/installation.md)

The service runs in three ways: with Docker Compose (one machine, the easiest way), locally with
uv (without Docker images, for development) and on Kubernetes with the Helm chart. Every way needs
a Valkey (or Redis 7) for the shared state: jobs, results, the cache and the rate limits. The
service itself keeps no state.

Every request needs an API key of the service (unless you switch keys off with `AUTH_MODE=none`).
Docker Compose creates the first one for you; [API keys: show, create, enter](#api-keys-show-create-enter)
explains how to see it, how to make more and where they go, for every way and on Windows.

On Windows, Docker Compose runs in [Docker Desktop](https://docs.docker.com/desktop/), in
PowerShell or the command prompt. The local way without Docker images needs Linux or macOS; on
Windows run it in [WSL 2](https://learn.microsoft.com/windows/wsl/install) (Ubuntu), because the
document workers use Linux process limits.

## Local with uv

Requires Python 3.13, [uv](https://docs.astral.sh/uv/) and a Valkey on `localhost:6379`.

```bash
uv sync                                                     # install the dependencies into .venv
docker run -d --name valkey -p 6379:6379 valkey/valkey:9.1-alpine
export AUTH_MODE=none                                       # in each terminal below
UI_ENABLED=true uv run text-extraction                      # terminal 1: API on http://localhost:8080
INTERNAL_PORT=9091 uv run text-extraction-worker            # terminal 2: extract worker (jobs)
```

`uv run` does not read `.env`: each terminal takes its settings from the shell, so `export` them
there (or put them in a file and run `set -a; . ./my.env; set +a` in each terminal).
`AUTH_MODE=none` lets anyone who reaches the port use the service: use it on your own machine
only, and only while you need it. It answers only the host names of `ALLOWED_HOSTS` (`localhost`,
`127.0.0.1`, `[::1]` and `api`), so a web page that points its own domain at your machine (DNS
rebinding) is refused. Every process reads the same settings, so each terminal needs
`AUTH_MODE=none` (or the same `API_KEYS`), and each serves its internal port: give every further
process its own `INTERNAL_PORT`. The browser worker needs the extra `browser` and Chromium
(`uv sync --extra browser`, then `uv run playwright install chromium`); start it with
`INTERNAL_PORT=9092 uv run text-extraction-browser-worker` and set `BROWSER_ENABLED=true` on the
API, the extract worker and the browser worker.

With an API key instead of `AUTH_MODE=none` (for example when other machines reach the port),
first take `AUTH_MODE=none` away again, or the keys are ignored:

```bash
uv run text-extraction-init --new-key me --role admin   # prints a key and an "export API_KEYS=..." line
unset AUTH_MODE                                         # in each terminal: keys are on again
export API_KEYS='[{"name":"me","role":"admin","sha256":"..."}]'  # the printed line, in each terminal
UI_ENABLED=true uv run text-extraction                  # log in to http://localhost:8080/ui with the key
```

Keep the single quotes around the value: they keep the JSON's double quotes for the shell.

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
of `init`; the `--show-admin-key` command above prints it at any time. More keys (for other
people, or with the role `user`) go into `API_KEYS` in `.env`; they count together with the
generated one: see [API keys: show, create, enter](#api-keys-show-create-enter).

In Windows PowerShell the same steps read:

```powershell
Copy-Item .env.example .env
docker compose up -d --wait
$KEY = docker compose run --rm -T init text-extraction-init --show-admin-key
Invoke-RestMethod -Headers @{ Authorization = "Bearer $KEY" } http://localhost:8080/v1/info
Invoke-RestMethod -Method Post -Headers @{ Authorization = "Bearer $KEY" } `
  -ContentType "application/json" -Body '{"url": "https://example.org/"}' `
  http://localhost:8080/v1/extract
```

The internal port 9090 (`/health`, `/ready`, `/metrics`) is not published. The playground is at
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
2. Take the admin key: in the web console (see below) run
   `cd /docker/text-extraction && docker compose run --rm -T init text-extraction-init --show-admin-key`,
   or find it in the project's logs of the service `init` (event `init_admin_key_created`, field
   `admin_key`).
3. Edit the project: set `DOMAIN` in `.env` (for example the VPS host name
   `srvNNNNNN.hstgr.cloud` or your own domain with an A record pointing at the VPS), optionally
   `API_PORT=127.0.0.1:8080`, then deploy again. Traefik gets the certificate itself. Until
   `DOMAIN` is set, Traefik's log shows an error about an empty `Host()` rule: that is harmless.
4. Open `https://<DOMAIN>/ui` and log in with the key.
5. Optional, more keys: create them in the web console with `--new-key` and enter them in the
   project's environment variables, as [Hostinger and other Docker panels](#hostinger-and-other-docker-panels)
   shows.

A failed deploy shows only "Deployment failed" or "Project not found"; the web console (a root
shell) has the real message:

```bash
cat /docker/text-extraction/.build.log                 # the real error of a failed deploy
cd /docker/text-extraction && docker compose logs init  # the admin key
docker compose run --rm -T init text-extraction-init --show-admin-key
docker compose logs api | tail -n 20                   # why the API does not start
```

### Build from the checkout

```bash
cp .env.example .env                           # then set VALKEY_PASSWORD and API_KEYS in it
openssl rand -hex 24                           # a VALKEY_PASSWORD
uv run text-extraction-init --new-key me --role admin   # a key and its API_KEYS line for .env
docker compose -f docker-compose.build.yml up --build -d --wait
```

This file has no `init` service: it needs `API_KEYS` and `VALKEY_PASSWORD` in `.env`. Replace the
`API_KEYS` line of `.env` with the line that `--new-key` printed and keep the key itself for your
requests and the playground. Without uv, run the command in the image once it is built and
`VALKEY_PASSWORD` is set (the file refuses to load without it):
`docker compose -f docker-compose.build.yml run --rm --no-deps api text-extraction-init --new-key me --role admin`.
A `VALKEY_PASSWORD` may hold only letters and digits. Without `openssl` (on Windows, Git Bash has it),
`python -c "import secrets; print(secrets.token_hex(24))"` makes one too.

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

## API keys: show, create, enter

The service checks every request against a list of keys. For each key the list holds a name, a
role and the key's SHA-256, never the key itself: so there are always two parts.

- The key: 64 letters and digits. You keep it, send it with every request as
  `Authorization: Bearer <key>` and log in to the playground with it. Nobody can recover it from
  the list: lost is lost, so make a new one.
- Its entry in `API_KEYS`: `[{"name":"jan","role":"user","sha256":"…"}]`, a JSON list in the
  settings. Several keys share one list: `[{…},{…}]`.

The role is `user` or `admin`: an admin may also switch the site rules off (and with it use
maximum extraction), use XPath selectors and the `evaluate` action, and read every job.
[API keys](usage.md#api-keys) in the usage guide describes the roles and the error answers.

### Show the generated admin key

The root `docker-compose.yml` creates an admin key named `admin` on its first start and keeps it
in the volume `secrets`. It prints it at any time, the same in bash, PowerShell and Hostinger's web
console (there, first `cd /docker/text-extraction`):

```bash
docker compose run --rm -T init text-extraction-init --show-admin-key
```

For most single-person setups this key is enough. `docker compose down -v` deletes the volumes and
with them this key (and the downloaded models); the next start creates a new one.

### Create another key

`--new-key` prints a new key and its entry, ready to paste; it changes nothing on its own. Give
each key a name of its own (letters, digits, `.`, `_` or `-`; `admin` is taken by the generated
key) and the role it needs, `user` by default:

```bash
docker compose run --rm -T init text-extraction-init --new-key anna                 # a user key
docker compose run --rm -T init text-extraction-init --new-key jan --role admin     # an admin key
uv run text-extraction-init --new-key anna                                          # without Docker
```

The answer has four parts (shortened here):

```text
Key (send it as 'Authorization: Bearer <key>' or enter it in the playground; it is shown only now):
  9f2c…
.env line (docker compose; replace the API_KEYS line, then run docker compose up -d):
  API_KEYS=[{"name":"anna","role":"user","sha256":"4be1…"}]
Shell (uv run without Docker; run it in each terminal):
  export API_KEYS='[{"name":"anna","role":"user","sha256":"4be1…"}]'
Docker panel variable (such as Hostinger): name API_KEYS, value:
  [{"name":"anna","role":"user","sha256":"4be1…"}]
More than one key: put every entry, comma-separated, into one list: [{...},{...}]
```

Copy the key somewhere safe now: it is not stored anywhere. `--new-key` needs version `0.1.13` or
later; without it (older images) [API keys](usage.md#api-keys) shows how to make a key with
`openssl`.

### Enter the key

| Where the service runs | What to do |
| --- | --- |
| `docker compose` (either file) | Open `.env` in a text editor, replace the whole line that starts with `API_KEYS=` with the printed `.env` line, save, run `docker compose up -d --wait` (with `-f docker-compose.build.yml` for the build file). |
| Hostinger and other Docker panels | See [below](#hostinger-and-other-docker-panels). |
| `uv run` without Docker | Run the printed `export API_KEYS=…` line in every terminal, then start the processes again. |
| Kubernetes | The value goes into `secrets.apiKeys` (or your own Secret), see the [Helm chart](../../../deploy/helm/text-extraction/README.md). |

For several keys, the line holds one list with every entry, for example
`API_KEYS=[{"name":"anna","role":"user","sha256":"…"},{"name":"jan","role":"admin","sha256":"…"}]`.
Keep it on one line and without quotes around the list. The generated `admin` key is not in this
list (it comes from the volume) and keeps working. To take a key away, remove its entry and run
`docker compose up -d --wait` again.

Compose passes `.env` to the containers only when it creates them: `docker compose up -d` does
that after a change, while `docker compose restart` keeps the old values.

### Hostinger and other Docker panels

1. Create the key in the VPS's web console (a root shell):
   `cd /docker/text-extraction && docker compose run --rm -T init text-extraction-init --new-key anna`.
2. Docker Manager, the project, "Edit" (environment variables): add a variable.
   - Name: `API_KEYS`
   - Value: the line under "Docker panel variable", the one that starts with `[`. Not the
     `.env` line: `API_KEYS=` in the value field makes the name appear twice, and the API and
     the workers stop with "API_KEYS must be a JSON list".
3. Deploy again. The variables go into `.env`, and the containers start with the new list.

If a variable `API_KEYS` already exists (from `.env.example`, with zeros as the hash), replace its
value instead of adding a second one.

### Check the key

```bash
curl -H "Authorization: Bearer <key>" http://localhost:8080/v1/info
```

```powershell
Invoke-RestMethod -Headers @{ Authorization = "Bearer <key>" } http://localhost:8080/v1/info
```

The answer names the key's `role`. In Windows PowerShell 5, `curl` is a different command: use
`curl.exe` or `Invoke-RestMethod`. Behind a domain, use `https://<DOMAIN>` instead of
`http://localhost:8080`.

### When it does not work

| What you see | Cause and fix |
| --- | --- |
| 401 `unauthorized` | The key does not match an entry: copy it again, without spaces. After a change of `.env`, run `docker compose up -d --wait` (a restart keeps the old list). |
| The API does not start, its log says `API_KEYS must be a JSON list` | The value is not a bare list: it starts with `API_KEYS=` (panel value field), is wrapped in quotes in a panel field, or spans several lines. |
| The log says `duplicate API key name` | Two entries share a name, or one is named `admin` next to the generated key: rename it (for `--new-key`, choose another name). |
| The log says `AUTH_MODE=api_key requires API_KEYS or API_KEYS_FILE` | There is no key at all: enter one, or use the root `docker-compose.yml`, which creates one. |
| The key is lost | A key of your own cannot be recovered: create a new one and replace its entry. The generated admin key: `--show-admin-key`. |

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
