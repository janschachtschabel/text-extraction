# text-extraction

A FastAPI service that turns web pages, files, audio and video into clean text or
Markdown, with a fast HTTP mode and a browser mode.

## Status

Under construction: packages 0-8 are merged (foundation, service skeleton, fast extraction,
jobs and batch, browser worker, documents and OCR, audio and video, PII, structured output and
chunking). See
[docs/packages.md](docs/packages.md#status) for the implementation packages and their status.

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

Personal-data handling (`pii`, below) runs in the NLP worker, which is off by default too (Presidio
and spaCy; models of about 1 GB for `lg`, 115 MB for `md`, downloaded into a volume on the first
start). Set `PII_ENABLED=true` in `.env` (optionally `PII_MODEL_SIZE=md` and `PII_HASH_KEY`, from
`openssl rand -hex 32`) and start the profile `nlp`:
`docker compose --profile nlp up --build`. Without it a request with `pii` answers 400.

Structured output with an LLM and semantic chunking (below) call an OpenAI-compatible endpoint
from the LLM worker, which is off by default as well (it runs from the API image, no models). Set
`LLM_ENABLED=true`, `LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY` (and `LLM_EMBEDDING_MODEL` for
`semantic`) in `.env` and start the profile `llm`: `docker compose --profile llm up --build`.
`docker compose run --rm llm-worker text-extraction-llm-worker --check` sends one tiny extraction to
the endpoint. Without it, the strategy `llm`, `learn_selectors` and `semantic` chunking answer 400;
`auto` leaves the fields it would ask the LLM for missing, and everything else works without an
LLM.

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

### Audio and video

A video URL of YouTube, Vimeo or the Mediatheken of ARD, ZDF, arte and 3sat answers with the
video's subtitles (manual ones first, then the platform's automatic captions), its title,
description, duration and licence. The page itself is not fetched:

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=<id>", "languages": ["de"]}' \
  http://localhost:8080/v1/extract
# {"status": "success", "data": {"content_type": "text/vtt", "content": "# <title>\n\n...",
#  "media": {"platform": "youtube", "transcript_source": "manual_subtitles",
#  "transcript_language": "de", "duration_seconds": 754.0, "embeds": [], ...}, ...}, ...}
```

A video without subtitles answers its details only (`"transcript_source": "metadata_only"`,
`status: partial`, `error.code: no_transcript_available`); so does a video whose platform refused
the service (warning `subtitles_blocked`: YouTube blocks many data-centre IPs). An HTML page whose
`<video>` or `<audio>` has a `<track>` gets that text after its own, under `## Transcript`, and
`data.media.embeds` lists the players a page embeds, for you to extract one by one.

Speech recognition is off by default. Start the ASR worker with the profile `asr` and
`ASR_ENABLED=true` in `.env` (the first start downloads the Whisper model `large-v3-turbo`, about
1.6 GB, into a volume): `docker compose --profile asr up --build`. Then `"transcribe": true`
transcribes a video without subtitles or an audio or video file URL. A video with subtitles still
answers at once; a transcription is always a job, answered 202 at once:

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/podcast/episode-12.mp3", "transcribe": true}' \
  http://localhost:8080/v1/extract
# 202 {"status": "success", "data": {"id": "<job id>", "kind": "extract", "state": "running",
#  "total": 1, "status_url": "/v1/jobs/<job id>"}, ...}
curl -H "Authorization: Bearer $KEY" http://localhost:8080/v1/jobs/<job id>
# once "state" is "completed": items[0].result is the extraction result, with
#  "media": {"transcript_source": "asr", "asr_model": "large-v3-turbo", ...}
```

An uploaded audio or video file is always transcribed (no form field needed; the bytes decide),
up to `MEDIA_UPLOAD_MAX_BYTES` (25 MB; larger media by URL), and answers 202 with its job the same
way:

```bash
curl -H "Authorization: Bearer $KEY" -F file=@lecture.mp3 http://localhost:8080/v1/extract/file
# 202 {"status": "success", "data": {"id": "<job id>", "kind": "file", ...,
#  "status_url": "/v1/jobs/<job id>"}, ...}
```

A transcription runs at about real time on 4 CPU threads (media up to 2 hours). With
`ASR_BACKEND=openai` the worker sends the audio to an OpenAI-compatible endpoint instead
(`ASR_EXTERNAL_BASE_URL`); a caller's own key in the header `X-ASR-Api-Key` wins over the
operator's `ASR_EXTERNAL_API_KEY`.

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

### Personal data (PII)

`pii` finds personal data in the result and handles it, in German and English text: the content
and the free-text fields (title, authors, description, link texts, ...). `pii` is a mode, or an
object with `entities`, `language` and `score_threshold`. By default persons and contact or payment
data are found (`PERSON`, `EMAIL_ADDRESS`, `PHONE_NUMBER`, `IBAN_CODE`, `CREDIT_CARD`,
`IP_ADDRESS`); `LOCATION`, `ORGANIZATION` and `URL` only when you name them.

| Mode | An entity becomes |
| --- | --- |
| `mask` | `[PERSON]`, `[EMAIL_ADDRESS]`, ... |
| `redact` | `[REDACTED]` |
| `hash` | `[PERSON:3f9a1c7d02be]`: a keyed hash, equal for equal values |
| `annotate` | the text stays; `data.pii.items` lists type, field, offsets, score and text (Markdown and text only, not `format: html`) |

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/team", "pii": "mask"}' http://localhost:8080/v1/extract
# {"status": "success", "data": {"content": "Contact [PERSON], [EMAIL_ADDRESS] ...",
#  "pii": {"mode": "mask", "language": "en", "entities": {"PERSON": 3, "EMAIL_ADDRESS": 1},
#  "items": null, "hash_scope": null}, ...}, ...}

curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/team", "pii": {"mode": "annotate", "entities": ["PERSON", "ORGANIZATION"]}}' \
  http://localhost:8080/v1/extract
# "pii": {"mode": "annotate", "entities": {"PERSON": 3}, "items": [{"field": "content",
#  "type": "PERSON", "start": 8, "end": 20, "score": 0.85, "text": "Anna Schmidt"}, ...], ...}
```

A batch and an upload take it the same way; the results of a batch carry the processed text:

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"urls": ["https://example.org/a", "https://example.org/b"], "pii": {"mode": "hash"}}' \
  http://localhost:8080/v1/batch
curl -H "Authorization: Bearer $KEY" -F file=@letter.docx -F pii=redact \
  http://localhost:8080/v1/extract/file
```

The service answers a request with `pii` when the NLP worker is done; after `PII_SYNC_WAIT_SECONDS`
(20 s) a slower one answers 202 with a job to poll. A result is never answered unprocessed: when
PII handling cannot run, the answer is `failed` with `error.code: pii_failed` (retryable) and none
of the text. `hash` is keyed with `PII_HASH_KEY`: set, the same value has the same hash in every
result (`hash_scope: service`), unset, only within one result (`result`); with nothing hashed
`hash_scope` is null. A language other than
German and English uses the English model for names, with the warning `pii_language_unsupported`.
`pii` cannot be combined with `screenshot`. Names are found by a statistical model, so a few are
missed and a few words are taken for names; see "PII" in the [architecture](docs/architecture.md)
for the limits.

### Structured output and chunking

`structured` takes a JSON schema and fills its fields from the page: `metadata` (JSON-LD,
microdata, RDFa, OpenGraph and meta tags), `selectors` (your CSS rules; XPath with an admin key),
`patterns` (your regular expressions on the content), `llm`, or `auto` (the default): per field the
first of your selectors, the page's metadata and your patterns that finds a value, and the LLM only
for the fields still missing. `POST /v1/structured` is `POST /v1/extract` with `structured`
required:

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/mathe/brueche", "structured": {
        "schema": {"type": "object", "properties": {"name": {"type": "string"},
          "bearbeitungszeit": {"type": "integer"}, "merksatz": {"type": "string"}}},
        "patterns": {"bearbeitungszeit": {"regex": "Bearbeitungszeit: (\\d+)", "group": 1}}}}' \
  http://localhost:8080/v1/structured
# {"status": "success", "data": {"content": "# Brüche addieren ...", "structured": {
#  "values": {"name": "Brüche addieren und subtrahieren", "bearbeitungszeit": 45,
#   "merksatz": "Brüche mit gleichem Nenner ..."},
#  "sources": {"name": "metadata", "bearbeitungszeit": "patterns", "merksatz": "llm"},
#  "missing": [], "tables": null, "learned": null,
#  "llm": {"model": "gpt-6-luna", "calls": 1, ...}, "stage": "done", ...}, ...}, ...}
```

`"tables": true` adds the page's tables; `"learn_selectors": true` (with `llm` or `auto`) asks the
LLM for CSS selectors that reproduce the values it found and keeps them for your API key and the
site, so that the next pages of that site need no LLM for those fields (`"sources": {...:
"learned"}`). Your own `X-LLM-Api-Key` header wins over the operator's `LLM_API_KEY`. A request
that needs the LLM waits `LLM_SYNC_WAIT_SECONDS` (30 s), then answers 202 with a job to poll; when
the LLM step cannot run, the answer keeps its content and is `partial` with `error.code:
llm_failed` (retryable).

`chunking` splits the content into chunks of at most `size` characters (default 1,000, `overlap`
100): `fixed`, `recursive` (the default), `markdown` (at headings; each chunk carries its heading
path), `sentence` or `semantic` (cut where the meaning shifts, with the endpoint's embeddings).
`POST /v1/chunk` takes a `url` or your own `text` (Markdown, or `"text_type": "plain"`):

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"text": "# Brüche\n\nEin Bruch beschreibt einen Anteil.\n\n## Addieren\n\nGleiche Nenner bleiben.",
       "chunking": {"strategy": "markdown", "size": 500, "overlap": 0}}' \
  http://localhost:8080/v1/chunk
# {"status": "success", "data": {"mode_used": "upload", "chunks": [
#  {"index": 0, "text": "# Brüche\n\nEin Bruch beschreibt einen Anteil.", "start": 0, "end": 44,
#   "headings": ["Brüche"]},
#  {"index": 1, "text": "## Addieren\n\nGleiche Nenner bleiben.", "start": 46, "end": 82,
#   "headings": ["Brüche", "Addieren"]}], ...}, ...}
```

`POST /v1/extract`, `/v1/extract/file` (as form fields with a JSON object each) and `/v1/batch`
take `structured` and `chunking` too; every result of a batch carries its own chunks:

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"urls": ["https://example.org/a", "https://example.org/b"],
       "chunking": {"strategy": "sentence", "size": 800}}' \
  http://localhost:8080/v1/batch
```

### Image descriptions

`describe_images` asks the LLM (the operator's vision model, `LLM_VISION_MODEL`, else `LLM_MODEL`)
for a short description of each image of the page, for a search index and for people who cannot
see it. It needs `metadata` (on by default; with `"metadata": false` it is 422): the descriptions
go on the entries of `metadata.images` (cover images first, then the page's `<img>`), at most
`max_images` of them (default 5, at most 20). `content` stays the page text:

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/natur/wasserkreislauf", "describe_images": {"max_images": 3}}' \
  http://localhost:8080/v1/extract
# {"status": "success", "data": {"metadata": {"images": [
#   {"url": "https://example.org/bilder/titel.png", "alt": "Wolken über einem See",
#    "description": "Wolken ziehen über einen See, an dessen Ufer Bäume stehen."},
#   {"url": "https://example.org/bilder/tafel.jpg", "alt": "", "description": "Ein Tafelbild ..."},
#   {"url": "https://cdn.example.net/x.png", "alt": "", "description": null}], ...},
#  "image_descriptions": {"asked": 3, "described": 2, "skipped": 1, "failed": 0,
#   "llm": {"model": "gpt-6-luna", "calls": 2, ...}},
#  "warnings": ["image_not_described"], "timings": {"llm_ms": 2140, ...}, ...}, ...}
```

The images are fetched the way pages are: through the SSRF guard, robots.txt and the TDM checks of
the request's `compliance`, and the per-domain limits, with the service's user agent and never
your cookies or headers. An image that is not fetched, not a PNG, JPEG, GIF or WebP, larger than
`LLM_VISION_MAX_IMAGE_BYTES` (5 MB) or that the model cannot describe keeps `description: null`,
with the warning `image_not_described`; the status stays. `timings.llm_ms` covers the descriptions,
their image fetches included. An uploaded PNG, JPEG or WebP is described too: its description is
also appended to `content` after the OCR text (`> Image description: ...` in Markdown), on an image
entry with `url: null`:

```bash
curl -H "Authorization: Bearer $KEY" -F file=@arbeitsblatt.png -F 'describe_images={}' \
  http://localhost:8080/v1/extract/file
# {"status": "success", "data": {"content": "Der Wasserkreislauf ...\n\n> Image description:
#  Ein Arbeitsblatt mit einer Skizze des Wasserkreislaufs.", "metadata": {"images": [
#  {"url": null, "alt": "", "description": "Ein Arbeitsblatt mit einer Skizze ..."}], ...}, ...}, ...}
```

`describe_images` cannot be combined with `pii` (422): no unmasked image goes to the LLM. As for
`structured`, your `X-LLM-Api-Key` wins over the operator's key, a request waits
`LLM_SYNC_WAIT_SECONDS` (30 s) before it answers 202 with a job, and descriptions are never cached:
a repeated request describes its images again. Images inside PDFs and other documents are not
described.

The descriptions have their own time budget (1.5 × `LLM_TIMEOUT_SECONDS`), on top of the time of
`structured` and `semantic` chunking; descriptions already made stay when a later LLM step fails.
Images still being fetched when the budget ends, for example on a host whose robots.txt sets a
long `Crawl-delay`, are skipped (`image_not_described`), and the status stays. A page whose result
is close to `JOB_ITEM_MAX_BYTES` (2 MB) can come back without its `content`, with the warning
`content_not_stored_too_large`, once `describe_images` sends it through the LLM workers, as with
`structured`: the descriptions do not need it.

### Site structure

`POST /v1/site-structure` maps a site: its sitemaps (robots.txt's `Sitemap:` lines, else
`/sitemap.xml`; indexes, gzip, text, RSS and Atom) with their URLs, and its link graph from the
start page, `depth` clicks deep (default 2), for at most `max_pages` pages (default 100). It
answers 202 with a job of kind `site`; the extract workers crawl, and every page, robots.txt and
sitemap file they find becomes an item of the job, so its `total` grows while it runs:

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/", "depth": 2, "max_pages": 100}' \
  http://localhost:8080/v1/site-structure
# 202 {"status": "success", "data": {"id": "<job id>", "kind": "site", "state": "queued",
#  "total": 1, "status_url": "/v1/jobs/<job id>"}, ...}
curl -H "Authorization: Bearer $KEY" "http://localhost:8080/v1/jobs/<job id>?offset=0&limit=500"
# {"status": "success", "data": {"kind": "site", "state": "completed", "progress": {"total": 17,
#  ...}, "items": [
#  {"index": 0, "url": "https://example.org/", "result": {"status": "success", "data": {
#   "title": "Start", "content": null, "site": {"role": "page", "depth": 0, "parent": null,
#   "links": [
#    {"url": "https://example.org/mathe/", "text": "Mathe", "kind": "internal", "source": "a",
#     "rel": [], "hreflang": null, "region": "nav", "crawl": "queued", "target": 2},
#    {"url": "https://example.org/intern/", "text": "Intern", "kind": "internal", "source": "a",
#     "rel": [], "hreflang": null, "region": "main", "crawl": "blocked", "target": null}, ...]}}}},
#  {"index": 1, "url": "https://example.org/robots.txt", "result": {"data": {"site": {
#   "role": "robots", "depth": 0, "parent": 0,
#   "sitemaps": ["https://example.org/sitemap.xml"]}, ...}, ...}},
#  {"index": 2, "url": "https://example.org/mathe/", "result": {"data": {"site": {
#   "role": "page", "depth": 1, "parent": 0, "links": [...]}, ...}, ...}},
#  {"index": 3, "url": "https://example.org/sitemap.xml", "result": {"data": {"site": {
#   "role": "sitemap", "depth": 0, "parent": 1, "sitemap": {"format": "urlset", "guessed": false,
#   "entries": [{"loc": "https://example.org/mathe/", "lastmod": "2026-10-01"}, ...],
#   "children": [], "entry_count": 2400, "part": 1, "parts": 3}}, ...}, ...}}, ...]}, ...}
```

Read the graph from the items: each item's `data.site.role` is `page`, `robots`, `sitemap` or
`sitemap_part`, and `parent` is the index of the item that found it. A page's `links` are its
outgoing links in document order: `kind` (`internal`, `external`, `file`), `source` (`a`, `area`,
a head `link` such as `canonical` or `alternate`, `iframe`), `rel`, `hreflang`, `region` (`nav`,
`header`, `footer`, `aside`, `main`, `breadcrumb`, `pagination`, or null) and `crawl`, what the
crawl did with it. `queued` and `seen` lead to a page item, whose index is `target`: an edge of
the graph (a link deeper than `depth` to a page that is an item anyway says `seen` too). The
others are not followed: `depth` (deeper than asked), `listed` (head links and iframes), `file`,
`out_of_scope` (another host, or outside the start directory with `scope: "path"`), `nofollow`,
`blocked` (robots.txt or a TDM opt-out under `compliance: enforce`), `max_pages` and
`memory_guard`. A sitemap's entries are in
`sitemap.entries`; a large one continues in `sitemap_part` items with the same URL (`part` of
`parts`), and an index lists its sitemaps in `children`, each with the item it became. An item
whose `final_url` is another item's URL is a redirect to that page (its links repeat the page's):
dedupe the graph by `final_url`.

Pages are fetched in fast mode only, with the service's user agent, the per-host limits and the
checks of `compliance`; the items keep title, status, metadata and quality but no text
(`content: null`): send the URLs you want as text to `POST /v1/batch`. The crawl stays on the start
page's host (a leading `www.` ignored); `scope: "path"` keeps it under the start URL's directory,
`sitemaps: false` skips the sitemaps, and `depth: 0` reads the start page and the sitemaps only. A
crawl counts against `BATCH_MAX_OPEN_PER_CLIENT` with your batches.

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
