# Usage

Deutsch: [Nutzung](../de/usage.md)

This page explains what every request has in common: the API key, the answer (the result
envelope), the choice between waiting for the answer and a job, and the browser fallback. Then it
covers files, audio and video, personal data and image descriptions, and the playground.
[Parameters](parameters.md) lists every field of each request body. The examples use
`http://localhost:8080` (Docker Compose) and a key in the shell variable `KEY`.

## API keys

Every request sends an API key of the service as `Authorization: Bearer <key>`. The operator
lists the keys in `API_KEYS` (or `API_KEYS_FILE`), each with a name, a role and the SHA-256 of the
key, never the key itself:

```bash
KEY=$(openssl rand -hex 32)                       # the key you send
printf %s "$KEY" | sha256sum | cut -d' ' -f1      # the sha256 for API_KEYS
```

A key has the role `user` or `admin`. Admins may also choose `compliance: ignore` (and with it
maximum extraction), use XPath selectors and the `evaluate` action, and read every job; a user reads
only their own jobs. A missing or wrong key answers 401 `unauthorized`, and a request that the role
may not make 403 `forbidden`. An XPath selector with a user key is the exception: like any other
invalid field it answers 422 `invalid_request`. Each key has a rate limit (`RATE_LIMIT_PER_MINUTE`); over it a request
answers 429 `rate_limited` with `Retry-After`.

With `AUTH_MODE=none` the service needs no key and every caller is an admin. Use it on your own
machine only.

Your own keys for an LLM or a speech recognition endpoint go into the headers `X-LLM-Api-Key` and
`X-ASR-Api-Key`. They win over the operator's `LLM_API_KEY` and `ASR_EXTERNAL_API_KEY` and are
never stored with a result.

## The result envelope

Every answer has the same shape, the result envelope:

```json
{
  "status": "success",
  "data": {
    "url": "https://example.org/",
    "final_url": "https://example.org/",
    "upstream_status": 200,
    "mode_used": "fast",
    "title": "Example Domain",
    "content": "# Example Domain\n\nThis domain is for use in illustrative examples ...",
    "timings": {"total_ms": 412, "compliance_ms": 35, "fetch_ms": 210, "convert_ms": 18, "render_ms": 0},
    "warnings": []
  },
  "error": null,
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"
}
```

- `status` is `success`, `partial` (the content is there, but something is missing, such as
  `content_too_short`) or `failed`.
- `error` explains a `partial` or `failed` answer: `error.code` is a fixed code such as
  `target_http_error` or `blocked_by_robots`, `error.message` is an English text, and
  `error.retryable` says whether the same request may succeed later.
- `data.upstream_status` is the HTTP status of the page, `data.mode_used` is `fast`, `browser` or
  `upload`, and `data.timings` gives the time of each step in milliseconds.
- `data.warnings` names what did not go as asked, although a result is returned, such as
  `browser_unavailable`.

When the target fails (the site answers 404, refuses the service or is down), the API still
answers HTTP 200, with `status: failed`. A 4xx is a problem of your request (a bad field, the key,
the rate limit); a 5xx is a fault of the service.

## Sync and jobs

By default a request answers when its work is done. Work that takes longer becomes a job: send
`wait_seconds`, and when the work still runs after that many seconds, the answer is 202 with the
job's id and `status_url`.

<!-- request: POST /v1/extract -->
```json
{"url": "https://example.org/", "wait_seconds": 10}
```

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/", "wait_seconds": 10}' http://localhost:8080/v1/extract
# 202 {"status": "success", "data": {"id": "<job id>", "kind": "extract", "state": "running",
#  "total": 1, "status_url": "/v1/jobs/<job id>"}, ...}
curl -H "Authorization: Bearer $KEY" http://localhost:8080/v1/jobs/<job id>
```

Some work is always a job: a batch, a site structure, a transcription, and a request that waits
longer for the NLP or LLM worker than `PII_SYNC_WAIT_SECONDS` or `LLM_SYNC_WAIT_SECONDS`. An
upload waits 25 s by default. A job's `state` goes from `queued` to `running` to `completed`, and
`progress` counts its items by status. A job is kept for `JOB_TTL_SECONDS` (24 h), and only its
owner (or an admin) can read it. With `JOBS_ENABLED=false` there are no jobs: `wait_seconds` is
ignored, and batches and site structures answer 503.

### Batch

`POST /v1/batch` queues many URLs as one job (at most `BATCH_MAX_URLS`, 1,000 by default) and
answers 202 with its id. The extract workers process the items fairly between API keys and
between hosts, within the per-host outbound limits. Read the results page by page (`offset`,
`limit` up to 500):

<!-- request: POST /v1/batch -->
```json
{"urls": ["https://example.org/", "https://example.com/"], "format": "markdown"}
```

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"urls": ["https://example.org/", "https://example.com/"], "format": "markdown"}' \
  http://localhost:8080/v1/batch
# {"status": "success", "data": {"id": "<job id>", "kind": "batch", "state": "queued", "total": 2,
#  "status_url": "/v1/jobs/<job id>"}, ...}
curl -H "Authorization: Bearer $KEY" "http://localhost:8080/v1/jobs/<job id>?offset=0&limit=100"
```

Every URL gets its own result envelope in `items`, a failed one too. Each item also shows `run`,
its last claim by a worker (`pool`, `attempts`, `claimed_at`; null while it waits): `attempts`
above 1 means that the item runs again after a lost lease or a deferral.

### Cancel a job

`DELETE /v1/jobs/{id}` cancels a job (its owner or an admin). It deletes the job with its
results and files at once and frees its place in `BATCH_MAX_OPEN_PER_CLIENT`. Items that still
wait are dropped; items in flight finish without being stored.

## Browser fallback

Some pages need JavaScript. With browser mode on (`BROWSER_ENABLED=true`; Docker Compose switches
it on), the browser worker renders them in Chromium. `mode` chooses the browser fallback:

| `mode` | In the playground | What happens |
| --- | --- | --- |
| `auto` | when the page needs it | HTTP first; the browser when the page needs it (a JavaScript shell, a challenge, a soft 404). The default. |
| `fast` | never | HTTP only. |
| `browser` | always | Always the browser. |

`data.mode_used` says which mode produced the answer. When no browser worker takes the page
within `BROWSER_QUEUE_WAIT_SECONDS` (10 s), `auto` keeps the HTTP result with the warning
`browser_unavailable`, and `browser` fails with `timeout` (retryable). While browser mode is off,
`browser`, `POST /v1/screenshot` and `auto` with `actions`, `cookies`, `headers` or `screenshot`
answer 400, and `auto` without them never uses the browser.

`actions` (at most 20), `cookies` and `headers` (sent only to the requested site) work with `auto`
and `browser`, not with `fast` and not in batches; the OpenAPI spec at `/docs` lists the actions.

<!-- request: POST /v1/extract -->
```json
{"url": "https://example.org/app", "mode": "browser", "actions": [{"type": "accept_cookies"}, {"type": "click", "selector": "button.more"}]}
```

### Screenshots

A screenshot is a PNG, 1920 px wide, of the whole page (`full_page: false`: the first
1920×1080). `POST /v1/screenshot` answers with the page's data and a reference to the file, which
you download from the job that holds it:

<!-- request: POST /v1/screenshot -->
```json
{"url": "https://example.org/", "full_page": true}
```

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/"}' http://localhost:8080/v1/screenshot
# {"status": "success", "data": {"screenshot": {"url": "/v1/jobs/<job id>/artifacts/screenshot.png",
#  "content_type": "image/png", "width": 1920, "height": 2758, ...}, ...}, ...}
curl -H "Authorization: Bearer $KEY" -o page.png \
  http://localhost:8080/v1/jobs/<job id>/artifacts/screenshot.png
```

`POST /v1/extract` takes `"screenshot": true` as well. The file is kept for `ARTIFACT_TTL_SECONDS`
(one hour), and only its owner (or an admin) can fetch it.

### Maximum extraction

By default the service respects a site's rules: robots.txt and the EU TDM opt-out are checked as
the operator set them up (`COMPLIANCE_DEFAULT`). Maximum extraction is for sites that allowed
extraction by contract. An admin sends `"compliance": "ignore"`, and where the operator switched
on `BROWSER_UNBLOCK_ENABLED`, the page loads in a stealth Chrome that waits for bot challenges to
clear. With `mode: auto` it also falls back to the browser when the page refuses (HTTP 403, 429
or 503) or gives too little text, and `data.unblocked` is true. It solves no image captchas, uses
no paid service and rotates no IP address; the per-host limits and a host's pause after a 429 or
503 still apply. Where you can, ask the site owner for an allowlist rule instead: a secret header
that you send with `headers`, or an exemption for the service's egress IP.
[Site rules](site-rules.md) has the details.

<!-- request: POST /v1/extract -->
```json
{"url": "https://example.org/partner-page", "compliance": "ignore"}
```

## Files, media and personal data

### Documents and uploads

PDFs, Word, PowerPoint and Excel files (DOCX, PPTX, XLSX, XLS), EPUBs and CSVs convert like pages:
`POST /v1/extract` with their URL, or `POST /v1/batch`. The bytes decide what a file is before its
`Content-Type` does (a PDF served as `application/octet-stream` is still a PDF). To convert a file
you have, upload it to `POST /v1/extract/file` as `multipart/form-data` (at most
`DOCUMENT_MAX_BYTES`, 20 MB):

```bash
curl -H "Authorization: Bearer $KEY" -F file=@report.pdf -F format=markdown \
  http://localhost:8080/v1/extract/file
# {"status": "success", "data": {"mode_used": "upload", "url": null, "content": "# ...",
#  "document": {"kind": "pdf", "converter": "markitdown", "pages": 12, "pages_converted": 12,
#  "ocr": false, "file_name": "report.pdf"}, ...}, ...}
```

The form also takes `metadata`, `ocr` (`auto`, `never` or `always`) and `wait_seconds` (25 by
default: an upload that takes longer answers 202 with a job). PDFs keep their first
`DOCUMENT_MAX_PAGES` (100) pages, with the warning `pages_truncated`. With the Docling worker,
scans and images (PNG, JPEG, TIFF, WebP, BMP) are read with OCR and OpenDocument files (ODT, ODS,
ODP) are converted too; without it a scan answers its text layer with the warning
`ocr_unavailable`, and images and OpenDocument files answer `unsupported_content_type`.

### Audio and video

A video URL of YouTube, Vimeo or the Mediatheken of ARD, ZDF, arte and 3sat answers with the
video's subtitles (manual ones first, then the platform's automatic captions), its title,
description, duration and licence. The page itself is not fetched:

<!-- request: POST /v1/extract -->
```json
{"url": "https://www.youtube.com/watch?v=<id>", "languages": ["de"]}
```

A video without subtitles answers its details only (`"transcript_source": "metadata_only"`,
`status: partial`, `error.code: no_transcript_available`); so does a video whose platform refused
the service (warning `subtitles_blocked`: YouTube blocks many data-centre IPs). With browser mode
on, a YouTube video whose subtitles the service could not read (warning `subtitles_blocked` or
`subtitles_failed`) gets a second try in the browser in `auto` mode without `transcribe`: where
YouTube offers its new transcript view, the answer is that transcript
(`"transcript_source": "transcript_panel"`, `"mode_used": "browser"`); otherwise it is the
details with the warning `transcript_panel_unavailable`. An HTML page whose `<video>` or `<audio>`
has a `<track>` gets that text after its own, under `## Transcript`, and `data.media.embeds` lists
the players a page embeds, for you to extract one by one.

With the ASR worker, `"transcribe": true` transcribes a video without subtitles or an audio or
video file URL. A video with subtitles still answers at once; a transcription is always a job,
answered 202 at once. An uploaded audio or video file is always transcribed (the bytes decide), up
to `MEDIA_UPLOAD_MAX_BYTES` (25 MB; larger media by URL):

```bash
curl -H "Authorization: Bearer $KEY" -F file=@lecture.mp3 http://localhost:8080/v1/extract/file
# 202 {"status": "success", "data": {"id": "<job id>", "kind": "file", ...,
#  "status_url": "/v1/jobs/<job id>"}, ...}
```

A transcription runs at about real time on 4 CPU threads (media up to 2 hours). With
`ASR_BACKEND=openai` the worker sends the audio to an OpenAI-compatible endpoint instead
(`ASR_EXTERNAL_BASE_URL`).

### Personal data (PII)

`pii` finds personal data in the result and handles it, in German and English text: the content
and the free-text fields (title, authors, description, link texts, ...). `pii` is a mode, or an
object with `entities`, `language` and `score_threshold`. By default persons and contact or
payment data are found (`PERSON`, `EMAIL_ADDRESS`, `PHONE_NUMBER`, `IBAN_CODE`, `CREDIT_CARD`,
`IP_ADDRESS`); `LOCATION`, `ORGANIZATION` and `URL` only when you name them.

| Mode | An entity becomes |
| --- | --- |
| `mask` | `[PERSON]`, `[EMAIL_ADDRESS]`, ... |
| `redact` | `[REDACTED]` |
| `hash` | `[PERSON:3f9a1c7d02be]`: a keyed hash, equal for equal values |
| `annotate` | the text stays; `data.pii.items` lists type, field, offsets, score and text (Markdown and text only, not `format: html`) |

<!-- request: POST /v1/extract -->
```json
{"url": "https://example.org/team", "pii": {"mode": "annotate", "entities": ["PERSON", "ORGANIZATION"]}}
```

A batch and an upload take `pii` the same way (an upload as the form field `-F pii=redact`). A
result is never answered unprocessed: when PII handling cannot run, the answer is `failed` with
`error.code: pii_failed` (retryable) and none of the text. `hash` is keyed with `PII_HASH_KEY`:
set, the same value has the same hash in every result; unset, only within one result. `pii`
cannot be combined with `screenshot`. Names are found by a statistical model, so a few are missed
and a few words are taken for names.

### Image descriptions

`describe_images` asks the LLM for a short description of each image of the page, for a search
index and for people who cannot see it. It needs `metadata` (on by default): the descriptions go
on the entries of `metadata.images` (cover images first), at most `max_images` of them (default 5,
at most 20). `content` stays the page text. An uploaded PNG, JPEG or WebP is described too, and
its description is appended to `content`.

<!-- request: POST /v1/extract -->
```json
{"url": "https://example.org/natur/wasserkreislauf", "describe_images": {"max_images": 3}}
```

The images are fetched the way pages are: through the service's checks and per-host limits, with
the service's user agent and never your cookies or headers. An image that is not fetched, too
large (`LLM_VISION_MAX_IMAGE_BYTES`) or not described keeps `description: null`, with the warning
`image_not_described`. `describe_images` cannot be combined with `pii` (422): no unmasked image
goes to the LLM.

## The playground

With `UI_ENABLED=true` (Docker Compose switches it on) the service serves a playground at
`/ui` (local: http://localhost:8080/ui). Log in with an API key of the service, and optionally
your LLM and ASR keys. The page keeps the keys in the browser tab only (`sessionStorage`, gone
when the tab closes; "Log out" clears them) and sends them as request headers, never as a cookie.
Under `AUTH_MODE=none` the playground opens at once, under a banner: anyone who can reach the
service can use it. For a shared deployment keep `UI_ENABLED=false` or `AUTH_MODE=api_key`.

The playground speaks English and German. The language switch ("Deutsch", "English") is at the
top of every page; the choice stays in the browser. Each tab starts with a short intro and a
"Help (user guide)" link to this guide, which opens in a new tab, and each field has a hint.

Every form runs one REST call, with the same checks and rate limit as any client, and shows the
HTTP status, the envelope, the raw JSON and the request as a `curl` command (with `$API_KEY` in
place of your key). The tabs:

- **Extract**: a URL as Markdown or text, with metadata, structured metadata (schema.org and
  LRMI fields such as licence, author and educational level) and image descriptions. The result
  shows the content, the metadata, the images, the links and a timeline of what happened.
- **File**, **Screenshot**, **Structured**, **Chunk** and **Info**: the other single calls, with
  the same result view; the "Options (JSON)" box takes any further field of the REST body.
- **Batch**: URLs, one per line. **Batch file**: a CSV or JSON file of URLs, checked on the
  server first, with a preview before "Start batch".
- **Site structure**: starts a crawl; once it is completed, "Show as tree" lists its pages by
  host and path (while it runs, "Show tree now" shows a snapshot), with a checkbox per page and
  per section, and "Extract selected" starts batches of the checked URLs.

Every job started from the playground shows its state, a progress bar and its items, refreshes
every 3 s until it is completed, and has a "Cancel" button.

### Site rules in the playground

The forms of a URL have the choice "Site rules":

- "Respect the site's rules (default)" sends no `compliance`: the operator's checks apply.
- "Maximum extraction (contract partners)" sends `compliance: ignore`. It is for sites that
  allowed extraction by contract (see [Site rules](site-rules.md)).

Only admins see the choice, and it is disabled with a hint while `BROWSER_ENABLED` or
`BROWSER_UNBLOCK_ENABLED` is off. With `AUTH_MODE=none` every visitor counts as an admin, so
every visitor sees the choice. The "Browser fallback" field sets `mode`: "when the page needs it"
(`auto`), "never" (`fast`) or "always" (`browser`). With maximum extraction, "never" stays with
HTTP: the page never reaches the browser.
