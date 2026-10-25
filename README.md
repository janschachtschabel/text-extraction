# text-extraction

A FastAPI service that turns web pages, files, audio and video into clean text or
Markdown, with a fast HTTP mode and a browser mode.

## Status

Under construction. See [docs/packages.md](docs/packages.md) for the implementation
packages and their progress.

## Quick start

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                      # install dependencies into .venv
uv run pytest                # run the tests
uv run ruff check .          # lint
uv run ruff format .         # format
uv run mypy                  # type-check (strict)
uv run pre-commit install    # install the git hooks
```

## Quick start with Docker

Requires Docker with Compose. Compose starts the API, an extract worker, a browser worker and a Valkey (with a password) for the shared state.

```bash
cp .env.example .env         # set API_KEYS (the sha256 of your key) and VALKEY_PASSWORD (openssl rand -hex 24)
docker compose up --build    # public API on http://localhost:8080
curl -H "Authorization: Bearer $KEY" http://localhost:8080/v1/info
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/"}' http://localhost:8080/v1/extract
```

The internal port 9090 (`/health`, `/ready`, `/metrics`) is not published. For Kubernetes use the
[Helm chart](deploy/helm/text-extraction/README.md).

OCR for scans and images runs in the Docling worker, which is off by default (an image of about
2 GB and about 335 MiB of models). Set `DOCLING_ENABLED=true` in `.env` and start the profile
`docling`: `docker compose --profile docling up --build`. The first start downloads the models
into a volume.

### Documents and uploads

PDFs, Word, PowerPoint and Excel files (DOCX, PPTX, XLSX, XLS), EPUBs and CSVs convert like pages:
`POST /v1/extract` with their URL, or `POST /v1/batch`. The bytes decide what a file is before its
`Content-Type` does (a PDF served as `application/octet-stream` is still a PDF). To convert a
file you have, upload it to `POST /v1/extract/file` as `multipart/form-data` (at most
`DOCUMENT_MAX_BYTES`, 20 MB):

```bash
curl -H "Authorization: Bearer $KEY" -F file=@report.pdf -F format=markdown \
  http://localhost:8080/v1/extract/file
# {"status": "success", "data": {"mode_used": "upload", "url": null, "content": "# ...",
#  "document": {"kind": "pdf", "converter": "markitdown", "pages": 12, "pages_converted": 12,
#  "ocr": false, "file_name": "report.pdf"}, ...}, ...}
```

The form also takes `metadata`, `ocr` (`auto`, `never` or `always`) and `wait_seconds` (25 by
default: an upload that takes longer answers 202 with a job to poll). PDFs keep their first
`DOCUMENT_MAX_PAGES` (100) pages, with the warning `pages_truncated`. With the Docling worker
(above), scans and images (PNG, JPEG, TIFF, WebP, BMP) are read with OCR and OpenDocument files
(ODT, ODS, ODP) are converted too; without it a scan answers its text layer with the warning
`ocr_unavailable`, and images and OpenDocument files answer `unsupported_content_type`.

### Batch

`POST /v1/batch` queues many URLs as one job (at most `BATCH_MAX_URLS`, 1,000 by default) and
answers 202 with its id. The extract worker processes the items, fairly between API keys and
between hosts, within the per-host outbound limits. Poll the job for progress and paged results
(`offset`, `limit` up to 500):

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"urls": ["https://example.org/", "https://example.com/"], "format": "markdown"}' \
  http://localhost:8080/v1/batch
# {"status": "success", "data": {"id": "<job id>", "kind": "batch", "state": "queued", "total": 2,
#  "status_url": "/v1/jobs/<job id>"}, ...}
curl -H "Authorization: Bearer $KEY" "http://localhost:8080/v1/jobs/<job id>?offset=0&limit=100"
```

The job's `state` goes from `queued` to `running` to `completed`. Every URL gets its own result
envelope in `items`, also a failed one, and `progress` counts them. A job is kept for
`JOB_TTL_SECONDS` (24 h), and only its owner (or an admin) can read it. `POST /v1/extract` with
`wait_seconds` answers 202 with a job when the extraction takes longer; that extraction keeps
running in the API pod that took the request (not on a worker), and the job stores its result.

### Browser mode

Pages that need JavaScript are rendered in Chromium by the browser worker. `mode` is `fast` (HTTP
only), `auto` (the default: HTTP first, the browser when the page needs it) or `browser` (always).
The compose file turns it on (`BROWSER_ENABLED=true`); in your own deployment set that on the API
and all workers. `auto` keeps the HTTP result, with the warning `browser_unavailable`, when no
browser worker takes the page within `BROWSER_QUEUE_WAIT_SECONDS` (10 s).

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/app", "mode": "browser", "format": "markdown",
       "actions": [{"type": "accept_cookies"}, {"type": "click", "selector": "button.more"}]}' \
  http://localhost:8080/v1/extract
```

`data.mode_used` says which mode produced the answer. `actions` (at most 20), `cookies` and
`headers` (sent only to the requested site) work in `browser` and `auto` mode, not in `fast` mode
or batches; see the OpenAPI spec for the actions.

A screenshot is a PNG, 1920 px wide, of the whole page (`full_page: false`: the first 1920×1080).
`POST /v1/screenshot` answers with the page's data and a reference to the file, which you download
from the job that holds it (screenshots need `JOBS_ENABLED=true`, the default):

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/"}' http://localhost:8080/v1/screenshot
# {"status": "success", "data": {"screenshot": {"url": "/v1/jobs/<job id>/artifacts/screenshot.png",
#  "content_type": "image/png", "width": 1920, "height": 2758, ...}, ...}, ...}
curl -H "Authorization: Bearer $KEY" -o page.png \
  http://localhost:8080/v1/jobs/<job id>/artifacts/screenshot.png
```

`POST /v1/extract` takes `"screenshot": true` as well. The file is kept for `ARTIFACT_TTL_SECONDS`
(one hour) and only its owner (or an admin) can fetch it.

**Unblock mode** is for sites that allowed extraction by contract but keep their bot protection.
It is off by default. The operator switches it on with `BROWSER_UNBLOCK_ENABLED=true`, and it then
applies only to an admin's request with `"compliance": "ignore"` (robots.txt and TDM checks are
off for it): the page is loaded in a stealth Chrome with its own user agent that waits for
non-interactive bot challenges to clear and clicks a Turnstile or hCaptcha checkbox. It solves no
image captchas, uses no paid service and rotates no IP address, and the per-domain limits still
apply. Ask the site owner for an allowlist rule instead where you can: a secret header you send
with `headers`, or an exemption for the service's egress IP.

## Documentation

- [Architecture](docs/architecture.md)
- [Configuration](docs/configuration.md): environment variables, secrets, ports, headers
- [Helm chart](deploy/helm/text-extraction/README.md)
- [CLAUDE.md](CLAUDE.md): rules and workflow for contributors

## Licence

Apache-2.0. See [LICENSE](LICENSE).
