# Architecture

Status: planning closed on 2026-10-23. This file is the source of truth for the build;
update it in the same PR as any change to the design.

## Overview

Stateless API pods handle fast-mode requests inline and hand slow work to worker pools
through Valkey. The job queue is our own fair scheduler on Valkey (Lua scripts with leases, see
"Jobs and the fair queue"), not Taskiq: taskiq-redis queues are plain FIFO, and its stream broker
reclaims stale messages only when new ones arrive (Jan, 2026-10-24).

```
Clients / AI agents ──(8080: REST /v1, /mcp, /ui)──▶ API pods (FastAPI, stateless)
Prometheus ──(9090: /health /ready /metrics)──────▶ API pods
API pods ──fetch (SSRF guard)──▶ target websites and files
API pods, workers ──▶ document children (MarkItDown: PDF text layers, Office, EPUB, CSV)
API pods, workers ──▶ media children (youtube-transcript-api, yt-dlp: subtitles, metadata)
                      ──egress proxy (SSRF guard)──▶ video platforms
API pods ──▶ LLM endpoint (OpenAI-compatible, default model gpt-6-luna)
API pods ──jobs──▶ Valkey (queues, results, cache, rate limits, fingerprints)
Valkey ◀──▶ extract-worker  (fast pipeline: batch items from the fair queue)
Valkey ◀──▶ browser-worker  (Chromium behind an egress proxy: auto fallback, actions,
                             screenshots; Patchright + Chrome for unblock mode)
Valkey ◀──▶ docling-worker  (Docling and Tesseract: scans, broken text layers, images,
                             OpenDocument files, PDFs with `ocr: always`)
Valkey ◀──▶ nlp-worker      (Presidio)
Valkey ◀──▶ asr-worker      (faster-whisper on CPU, or an external OpenAI-compatible
                             endpoint; downloads media through its egress proxy)
workers ──▶ model volumes (one PVC per worker type, read-only: Docling and Tesseract,
            Whisper models; spaCy for the nlp-worker)
optional: S3-compatible store for screenshots and large results
```

Images: `api` (slim), `browser-worker` (image `text-extraction-browser`: Chromium headless
shell, Google Chrome stable, Xvfb), `docling-worker` (image `text-extraction-docling`: Docling,
CPU torch, the Tesseract CLI, no models), `nlp-worker`, `asr-worker` (image
`text-extraction-asr`: faster-whisper, CTranslate2, PyAV built against an LGPL FFmpeg, Deno, no
models). Each worker is a Helm toggle and scales on queue length (KEDA or HPA). The API image
holds no browser and no Playwright or Patchright, no Docling, torch or Tesseract, and no
faster-whisper, CTranslate2, PyAV or Deno; the details are in "Browser worker", "Documents and
OCR" and "Audio and video" below. Every image runs tini as PID 1, which forwards signals
and reaps orphaned processes (a tesseract whose conversion child was killed).

MarkItDown runs where the bytes are (package 5, D1): in child processes of the API pods, the
extract workers and the browser worker, never in their parent processes. So do
youtube-transcript-api and yt-dlp for subtitles (package 6), behind the pod's egress proxy. The
nlp-worker keeps Presidio (package 7).

## Extraction pipeline

1. **Validate and guard:** scheme, SSRF check (private, loopback, link-local and metadata
   IPs refused; re-checked after each redirect and for browser sub-requests), size and time limits.
2. **Compliance:** robots.txt, TDMRep (`/.well-known/tdmrep.json`, `tdm-reservation` header
   and meta), robots meta / `X-Robots-Tag`, ai.txt / llms.txt. Mode `enforce|report|ignore`
   per request; operator default and override permission via `COMPLIANCE_DEFAULT`,
   `COMPLIANCE_ALLOW_OVERRIDE`. Every result carries a `compliance` block. The origin files are
   cached in Valkey; files that are missing are fetched one after another inside the outbound
   turn of the page that needs them, before the page (R22), so they count against the host's
   limits like any request. The files and the page of one turn go back to back (R25).
3. **Fetch (fast, or render):** httpx, HTTP/2, byte cap, conditional requests (ETag, Last-Modified).
   Per-domain outbound limits in Valkey (parallelism, delay, `Crawl-delay`, backoff on
   429/503, circuit breaker). A turn has at most one request in flight, so the parallelism
   limit holds for every request a host sees, with one exception: a compliance file that
   redirects to another host goes out there without a lease or spacing at the target, at most
   3 times per origin per pod per cache period. The delay lies between the real starts of
   requests of different turns, back to back within a turn (R25); a request that was sent late
   delays the other turns' next request instead of letting it follow closer (R21). A browser
   render takes the same turn and holds it until its page is closed ("Browser worker", B3).
4. **Quality gate and escalation (auto mode):** `quality.py` reports `js_heavy` (fewer than 500
   visible characters while the page runs JavaScript), `bot_challenge`, `soft_404` and
   `login_wall`; `quality.browser_recommended` is `js_heavy or bot_challenge or soft_404` and is
   reported in both modes. A bot challenge, soft-404, login wall or empty content ends `failed`
   (`bot_protection`, `likely_error_page`, `login_required`, `empty_content`), fewer than
   `QUALITY_MIN_WORDS` words ends `partial` (`content_too_short`). In `auto` mode with
   `BROWSER_ENABLED`, a fast result escalates to the browser worker when it came from a 2xx
   response with an HTML content type (`text/html`, `application/xhtml+xml`) and
   `browser_recommended` is set (`escalation.needs_browser`). A page served without a
   `Content-Type` therefore never escalates, whatever its quality verdict (this includes a
   soft-404 page served without one), and neither does an error status or a non-HTML type.
   Under unblock mode (admin, `compliance: ignore`, `BROWSER_UNBLOCK_ENABLED`) it also escalates
   on `bot_protection` and on a 403, 429 or 503 answer whose body is a challenge. The render's
   result and the fast result are compared and the better one is kept (higher status, then more
   words, on a tie the render). `auto` with `actions`, `cookies`, `headers` or `screenshot`
   skips the fast phase and always renders, like `browser`. `mode` is part of the result cache
   key.
5. **Convert:** the body's bytes decide what it is (`documents/sniff.py`, F1). HTML →
   Trafilatura (keep link-only lists of linked files, domain boilerplate rules, MathML →
   LaTeX); `text/plain` → plain text. PDFs with a text layer, DOCX, PPTX, XLSX, XLS, EPUB and
   CSV → MarkItDown, in a child process of the pod that holds the bytes. Scans, broken text
   layers, images and OpenDocument files (and every PDF with `ocr: always`) → Docling with
   Tesseract OCR, in the Docling worker. A document never escalates to the browser. See
   "Documents and OCR". Vision LLM opt-in (package 8). Video and audio (package 6): a URL of a
   supported platform (YouTube, Vimeo, ARD, ZDF, arte, 3sat) is recognised before step 3,
   passes step 2 under its canonical URL and is not fetched: the media children read its
   subtitles and details in the host's turn. A body that sniffs as audio or video is a media
   file (transcribed with `transcribe: true`), and an HTML page's `<track>` text is appended
   after its conversion. Speech recognition is a job of the ASR worker. See "Audio and video".
6. **Post-process (optional):** DOM metadata, PII, links, structured output, chunking,
   screenshot.
7. **Fingerprint and classify:** see change detection; return the result envelope.

## Result envelope and errors

Fields: `status` (`success|partial|failed`), `error.code`, `error.message`,
`error.retryable`, `upstream_status`, `mode_used` (`fast|browser|upload`), `attempts`,
`final_url`, `content_type`, `language`, `word_count`, `quality`, `timings` (`render_ms` is the
time in the browser, 0 in fast mode), `compliance`, `metadata`, `fingerprint`, `warnings`, for
browser results `unblocked` (unblock mode rendered the page) and `screenshot` (the artifact
reference), for documents `document` (`kind`, `converter`, `pages`, `pages_converted`,
`ocr`, `file_name`; F7), and for audio and video `media` (`platform`, `transcript_source`,
`transcript_language`, `available_languages`, `duration_seconds`, `uploader`, `asr_model`,
`embeds`; M6). For a browser result `attempts` counts the main-frame document
responses of the render, redirect hops included.

`warnings` names what did not go as asked while the result still answers (the HTML converter may
add a note of its own):

- Browser mode: `browser_unavailable` (no browser worker took the page; the fast result),
  `action_<index>_failed`, `navigation_refused_non_get`, `consent_overlay_removed`,
  `screenshot_clipped`, `browser_bytes_capped`, `challenge_checkbox_clicked`,
  `challenge_not_cleared`, `browser_profile_left`.
- Documents: `ocr_unavailable` (a document that needed OCR answers its text layer, F12),
  `pages_truncated` (pages after `DOCUMENT_MAX_PAGES` or `DOCLING_MAX_PAGES` were cut),
  `pages_partial` (Docling stopped at its own time limit or skipped a page; the pages it read),
  `document_fetched_directly` (a render ended on a file, which was fetched and converted in fast
  mode, F18).
- Media: `subtitles_blocked` (the platform refused the subtitles; the metadata answers),
  `subtitles_failed` (the subtitles could not be fetched or read; the metadata answers),
  `asr_unavailable` (the transcription could not be queued or failed; the metadata answers),
  `no_speech` (the speech recognition heard no speech; final, also for the cache),
  `tracks_truncated` (more tracks than `MEDIA_MAX_TRACKS`), `tracks_failed` (every `<track>` of
  a page failed).
- Jobs: `content_not_stored_too_large` (an item stored without its content).

A licence in `metadata` names its `source`: `rel-license`, `json-ld`, `microdata`, `rdfa`,
`meta`, `opengraph`, `text`, `xmp` (a PDF's XMP packet, F8), or `platform` (the licence a video
platform states, e.g. YouTube's Creative Commons).

Error codes: `invalid_url`, `blocked_url`, `blocked_by_robots`, `tdm_reserved`,
`dns_error`, `connection_error`, `timeout`, `target_http_error`, `bot_protection`,
`login_required`, `likely_error_page`, `empty_content`, `content_too_short`,
`unsupported_content_type`, `too_large`, `conversion_failed`, `no_transcript_available`,
`media_too_long`, `transcription_failed`, `llm_failed`, `rate_limited`, `internal_error`.

Codes of the API's own 4xx answers (never an extraction outcome): `invalid_request`,
`unauthorized`, `forbidden`, `not_found`. `tests/test_errors.py` keeps both lists in step
with `ErrorCode`.

HTTP status of the API: 200 with `status: failed` when the target failed; 4xx for bad
requests, auth and rate limits; 5xx only for our own faults.

Request bodies of the public API are limited before they are read whole, parsed or
authenticated (`src/text_extraction/body_limit.py`): over the limit of its path a body answers
413 `invalid_request`, at once for a `Content-Length` over it and otherwise as soon as the bytes
read pass it. The limit is `REQUEST_MAX_BODY_BYTES` (64 KiB), the batch bound for
`POST /v1/batch`, and `DOCUMENT_MAX_BYTES` + 64 KiB for `POST /v1/extract/file` (the larger
`MEDIA_UPLOAD_MAX_BYTES` + 64 KiB while uploaded audio and video are transcribed: `MEDIA_ENABLED`,
`ASR_ENABLED` and `JOBS_ENABLED`).

`POST /v1/extract` (fast mode, `src/text_extraction/pipeline.py`) puts these fields into `data`,
also on failure, as far as the request got. `partial` keeps the content and names
`content_too_short`; a failed quality verdict (`bot_protection`, `likely_error_page`,
`login_required`, `empty_content`) keeps it too, a compliance block or a target error has none.
`data.cache` is `miss`, `hit` (fresh stored result, no fetch), `revalidated` (stale stored
result confirmed by a 304 or by identical bytes) or `bypass` (`cache: false`: no read, no write).
Only `success` and `partial` results are cached and fingerprinted (per URL and output format; a
document also per `ocr` option, F10). `attempts` counts fetches of the target in this request (0
for a cache hit or a refusal before the fetch). The outbound gate refusing a turn gives
`rate_limited` (busy) or `connection_error` (host paused), both retryable.

## Public API (port 8080)

| Endpoint | Purpose | Mode |
| --- | --- | --- |
| `POST /v1/extract` | URL → text/Markdown/HTML; options `mode` (`fast`, `auto`, `browser`), `format`, `actions`, `cookies`, `headers`, `pii`, `links`, `metadata`, `screenshot`, `full_page`, `chunking`, `compliance`, `ocr` (`auto`, `never`, `always`; F11), `transcribe` and `languages` (M9; `transcribe: true` answers 400 `invalid_request` while the service has no ASR, and without `wait_seconds` it waits 20 s), and the header `X-ASR-Api-Key` (the caller's key of an external transcription endpoint, a secret, M11; also on `/v1/extract/file` and `/v1/batch`). The browser options (`mode: browser`, `actions`, `cookies`, `headers`, `screenshot`) answer 400 `invalid_request` while `BROWSER_ENABLED` is false, and `mode: fast` refuses them (422 `invalid_request`) | sync; with `wait_seconds` (0-60, default none) a slower extraction becomes a job: 202 with the job (P1); media that needs a transcription is a job on the ASR pool, answered 202 at once (M9) |
| `POST /v1/extract/file` | Upload → text/Markdown/HTML (F17): `multipart/form-data` with one file part `file` (at most `DOCUMENT_MAX_BYTES`) and the fields `format`, `metadata`, `ocr`, `wait_seconds` (default 25); at most one file and 8 fields, unknown fields 422; a body over `DOCUMENT_MAX_BYTES` + 64 KiB is 413 before it is parsed, and the key and the rate limit are checked before a byte of it is read. The bytes decide the kind (the part's type only where they do not; a body that starts with `<!doctype html` or `<html` under a document type, `application/octet-stream` or none is HTML, for URLs too); HTML and plain text convert as pages with relative links kept, documents as for URLs. `mode_used: upload`, `url`/`final_url` null, `attempts` 0, `cache: bypass`, `compliance.decision: skipped`, a fingerprint that is never stored, `document.file_name` the part's base name (never logged). A part whose bytes are audio or video (decided by the bytes, no form field needed) is transcribed (`media.platform: upload`, a job on the ASR pool, M14) up to `MEDIA_UPLOAD_MAX_BYTES` (25 MB); over it `too_large`; while the service has no ASR `failed`/`unsupported_content_type` ("Transcription is not enabled on this service."). `ocr: always` while the service has no OCR is 400 | sync; `wait_seconds` (default 25): a slower upload becomes a job of kind `file` (202), whose item keeps the bytes as its input (F12) |
| `POST /v1/screenshot` | Full page (default) or first-viewport PNG, 1920 px wide; options `full_page`, `actions`, `cookies`, `headers`, `compliance`, `wait_seconds`. The answer is the extraction result without the text, with `screenshot` pointing at the artifact | sync (`wait_seconds`: 202 with a job of kind `screenshot`) |
| `POST /v1/structured` | Strategies `metadata`, `selectors`, `patterns`, `llm`, `auto` | sync, job fallback |
| `POST /v1/chunk` | Text/Markdown/URL → chunks (`fixed`, `recursive`, `markdown`, `sentence`, `semantic`) | sync |
| `POST /v1/site-structure` | Sitemaps + typed link graph to a set depth | job |
| `POST /v1/batch` | Many URLs (`urls`, at most `BATCH_MAX_URLS`; a body over `BATCH_MAX_URLS` × 2056 bytes plus 4 KiB is 413 before it is parsed; options `mode`, `format`, `metadata`, `links`, `compliance`, `cache`, `ocr`, `transcribe`, `languages` as for `/v1/extract`; header `X-ASR-Api-Key` as for it), fair per-domain scheduling. 202 with the job; 429 at `BATCH_MAX_OPEN_PER_CLIENT` unfinished batches | job |
| `GET /v1/jobs/{id}` | Status, progress, paged results (`offset`, `limit` 1-500). Own jobs only (admins: all); unknown, expired and foreign ids are 404. Envelope `success` while running or when every item succeeded, `partial` when a finished job has failed or partial items, `failed` when every item failed (then `error` carries the most frequent item error code among the first 500 items, "All N items failed.", retryable if any of them is). 503 when jobs are disabled (`JOBS_ENABLED=false`) or Valkey is down | — |
| `GET /v1/jobs/{id}/artifacts/{name}` | A job's screenshot (`screenshot.png`, `image/png`) or, later, a large result. Own jobs only (admins: all), otherwise 404; 404 once `ARTIFACT_TTL_SECONDS` have passed. It is a job route: it needs `JOBS_ENABLED=true`, so screenshots need jobs on | — |
| `/mcp` | MCP tools (optional, `MCP_ENABLED`) | streamable HTTP |
| `/ui` | Playground (optional, `UI_ENABLED`), htmx, no Node build | — |

Internal port 9090: `/health`, `/ready`, `/metrics` (not in OpenAPI).

Non-JSON content types to clear with operations: `multipart/form-data` (uploads),
`image/png` (artifact download), SSE (MCP).

## Auth

`AUTH_MODE=api_key|none`. Keys come from a secret as hashed entries with name, role and
optional rate limit.

- `user`: all extraction endpoints; own jobs only.
- `admin`: everything, plus all jobs, cache purge, `compliance: ignore`, JavaScript
  actions (`evaluate`), unblock mode, higher limits, effective config, key usage stats.
- **Unblock mode under `ignore` (D3):** a request gets the stealth browser only when all three
  hold: the caller is an admin, the resolved compliance mode is `ignore`, and the operator set
  `BROWSER_UNBLOCK_ENABLED`. A user key never gets it, not even where the operator's default is
  `ignore` (R9); a job's items run with their owner's role. Without the switch, `ignore`
  renders in the standard browser.

`INTERNAL_API_ENABLED` (off by default) serves the API without auth for cluster-internal callers.

## Feature notes

- **Browser actions:** `click`, `scroll`, `wait_for`, `type`, `press`, `select`,
  `expand_all`, presets `infinite_scroll`, `accept_cookies`; `cookies` and `headers`
  for logged-in pages (headers go only to the requested URL's host and port, also across
  redirects: requests that carry them are fetched without following redirects, and a redirect
  of the page becomes a checked navigation); max 20 actions; `evaluate` admin-only. Stealth
  (Patchright) is unblock mode, admin-only. The index in the warning `action_<index>_failed`
  counts from 0. A click on a link to a page that fails to load, or that the compliance checks
  refuse, fails the whole render with that page's error. A click that starts a download ends
  the render; the file is then fetched directly and converted when it is a document (F18;
  `unsupported_content_type` when the request carries cookies, headers or actions). Details in
  "Browser worker".
- **DOM metadata:** extruct (JSON-LD, microdata, RDFa, OpenGraph) + Trafilatura. License
  normalized (e.g. `CC-BY-SA-4.0`) with source and confidence; author, dates, canonical,
  language, LRMI educational fields, images with alt text.
- **Audio/video:** tier 1 subtitles (youtube-transcript-api and yt-dlp for YouTube, Vimeo and
  the Mediatheken of ARD, ZDF, arte and 3sat; HTML5 `<track>` on any page), tier 2 metadata
  (`metadata_only`: title, description, licence; also when YouTube refuses the service, with
  `subtitles_blocked`, D1), tier 3 speech recognition (faster-whisper on CPU in the ASR worker,
  or an external OpenAI-compatible endpoint, `ASR_BACKEND`) for `transcribe: true` and every
  uploaded audio or video file: always a job (202), never waited for. Players embedded in a page
  are listed in `data.media.embeds`, not transcribed (D4). `transcript_source`:
  `manual_subtitles|auto_subtitles|asr|metadata_only`. Off by default: ASR (`ASR_ENABLED`); on:
  subtitles (`MEDIA_ENABLED`). Details in "Audio and video".
- **Change detection:** before extraction ETag/Last-Modified + SHA-256 of raw bytes
  (unchanged → stored result). After extraction the text is normalized (no Markdown
  syntax, case, whitespace) and gets SHA-256 + 128-permutation MinHash.
  `similarity_to_previous`, `changed`; the MinHash is a small numpy implementation compatible
  with datasketch 2.x (`affine32`, seed 1), which would give optional LSH for near-duplicates
  without pulling scipy into the image by default.
- **Structured output:** LLM-free `metadata`, `selectors` (CSS/XPath schema),
  `patterns` (regex, tables); `llm` with JSON schema output validated by Pydantic;
  `learn_selectors` stores an LLM-written CSS schema per domain for LLM-free reuse.
  Env: `LLM_BASE_URL`, `LLM_MODEL` (default `gpt-6-luna`), `LLM_API_KEY` (secret),
  `LLM_TEMPERATURE`, `LLM_MAX_TOKENS`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_INPUT_TOKENS`.
- **Batch:** one sub-queue per client and domain, round-robin over clients, then over the
  client's domains with free capacity; see "Jobs and the fair queue".
- **Sync-to-job (P1):** `POST /v1/extract` without `wait_seconds` always answers when the
  extraction ends (at most `EXTRACT_TIMEOUT_SECONDS`, 25 s). With `wait_seconds` the extraction
  runs as a task in the API pod and the route waits that long. If it is still running, the route
  creates an `extract` job with one URL (state `running`, lease of `EXTRACT_TIMEOUT_SECONDS` + 10 s)
  and answers 202; the same task goes on and a done-callback stores its envelope as item 0.
  The job only stores the result: nothing is routed through a worker, which would add a hop to
  every request and make workers mandatory. Browser work is the exception (package 4): a sync
  request that needs a browser puts a one-item job on the `browser` pool and waits for the
  browser worker (B1, B13), and so is OCR (package 5, F12): a document that needs it goes as a
  one-item job (kind `extract` or `file`, its bytes as the item's input) to the `docling` pool,
  and the request waits `DOCLING_SYNC_WAIT_SECONDS` for it, or until its 202. `POST
  /v1/extract/file` hands over the same way, as a job of kind `file` whose item has no URL and
  keeps the upload's bytes in Valkey for the job's run (up to `DOCUMENT_MAX_BYTES`,
  `DOCLING_INPUT_TTL_SECONDS` at most, deleted once the result is stored). They count against
  `DOCLING_INPUT_BUDGET_BYTES` like a queued document's; while that is full the request stays
  synchronous.
  A pod that dies mid-run leaves the lease to expire, and `GET /v1/jobs/{id}` then shows a job
  that is still `running` past its `lease_until` as completed, with item 0 `failed` /
  `internal_error`, retryable ("The pod running this extraction stopped."); this view is
  computed, not stored, and a completed job never gets it. If Valkey fails when the job is
  created, the request keeps waiting and answers 200 with the envelope, not 503; if it fails when
  the result is stored, the lease expires and the lost view takes over. The shutdown waits up to
  `EXTRACT_TIMEOUT_SECONDS` + 7 s for such tasks (after the graceful phase). Extract jobs do not
  count toward `BATCH_MAX_OPEN_PER_CLIENT`. `JOBS_ENABLED=false` ignores `wait_seconds`. A pod
  holds at most `EXTRACT_MAX_BACKGROUND` (32) such tasks; beyond it a request keeps waiting inline
  and answers 200.
- **Storage:** Valkey (`VALKEY_URL`, any Redis-protocol server) with TTLs; `S3_ENABLED`
  moves screenshots and large results to an S3-compatible store.

## Jobs and the fair queue

Package 3, `jobs.py`, `scheduler.py`, `worker.py`, `jobs_api.py`. Keys and settings are listed in
[configuration.md](configuration.md) ("Job keys", "Queue keys").

**Job records.** A job is four keys under one hash tag, `job:{<id>}:…`: meta (owner, options,
state, counters, lease), URLs, results and a done bitmap, all with `JOB_TTL_SECONDS`. One Lua
script stores an item's result: it sets the done bit and counts the item only when the bit was 0,
so a re-run after a lost lease counts once. The script answers `first`, `last` (this write
completed the job; only then is `job_duration_seconds` observed) or `repeat` (R7).

**The fair queue.** One queue per worker pool (`extract`, `browser`, `docling`, `asr`; the nlp
pool will reuse it), all keys under one hash tag `q:{<pool>}:…`; no script touches a job key and
a queue key. Items are `<job>:<index>:<attempts>` in lists per client and domain.

- Client = API key name (P3); under `AUTH_MODE=none` everyone is `anonymous` and batches share
  by domain only. Domain = `normalize_host(hostname)`, the key the outbound gate uses; a URL
  without a valid host goes to `-` (P2).
- A claim is one script: it takes back up to 20 expired leases (front of their domain, attempts
  + 1), then rotates clients and, within the client, its domains. It skips a domain that is
  deferred (`notbefore`) or holds `OUTBOUND_MAX_CONCURRENCY_PER_HOST` leases (P4: the scheduler
  only avoids pointless waits; the gate stays the authority on limits). The next item is leased
  with a random token, so a worker whose lease was taken back cannot complete or defer the next
  worker's lease.
- Scan budget (R2, R5, R6): a claim visits at most `SCHEDULER_CLAIM_SCAN_BUDGET` (200)
  client-domain pairs with read-only checks and rotates only what it visited, so an idle claim
  stays well under a millisecond on a large blocked queue. When the budget runs out inside a
  client, that client moves to the end too, so one client with many blocked domains cannot pin
  the scan. Such an answer is marked `scan_incomplete`; the worker backs off 0.05 s doubling to
  its 1 s idle sleep on consecutive ones and resets on a claimed item. No scan state is kept.
- Exhausted items (R3, R9): an item past `JOB_ITEM_MAX_ATTEMPTS` (lost leases plus deferrals) is
  handed out once more, marked exhausted and still leased. The worker records `failed` /
  `internal_error` (retryable), then completes it. It also completes it when the job is gone; on
  a Valkey error it leaves it leased, so it comes back after the lease instead of vanishing
  without a result.
- A gate refusal (busy, paused) defers the item: back to the front of its domain, the domain not
  claimed before the gate's retry time. At the attempt limit the pipeline's own `rate_limited` or
  `connection_error` envelope is stored instead.

**Workers.** `text-extraction-worker` runs `WORKER_CONCURRENCY` slot loops on the internal port
only (`/health`, `/ready`, `/metrics`). An item is leased for `EXTRACT_TIMEOUT_SECONDS` × 2 + 10 s;
a killed worker's items return after the lease. Valkey errors back off 1, 2, 4 … 30 s.

**Sync-to-job (P1).** See the feature note above. The lost view (R13): a job still `running` past
its `lease_until` is shown as `completed`, with item 0 `failed` / `internal_error`, retryable, so
pollers stop; the stored record stays as it is until its TTL. A pod keeps at most
`EXTRACT_MAX_BACKGROUND` (32) such extractions (R14); beyond it a request keeps waiting inline.

**Results (P6).** An item result whose JSON exceeds `JOB_ITEM_MAX_BYTES` is stored without
`content`, with the warning `content_not_stored_too_large`; a later `POST /v1/extract` of the URL
is a cache hit for `RESULT_CACHE_TTL_SECONDS`.

**Valkey memory (P5, R18).** Valkey runs with `--maxmemory 768mb --maxmemory-policy noeviction`:
eviction would drop gate, lease or job keys silently, while noeviction fails writes loudly (503).
Its memory request equals its limit, 1280Mi (Guaranteed QoS), so maxmemory is 60 % of the limit
and leaves room for fragmentation, buffers and the AOF fork. Package 11 measures real sizes.

**Memory guard (R28).** Under noeviction a full Valkey fails every write, and with it the rate
limiter, the outbound gate and the job reads, for as long as the results stay (up to
`JOB_TTL_SECONDS`). A few ordinary batches can get there: 5 × 1,000 items of ~80 KB is ~400 MB of
job results, stored a second time by the result cache. So each API pod and each worker reads `INFO memory`
every 5 s (`memory_guard.py`, its own task; on API pods also with `JOBS_ENABLED=false`), and from
`VALKEY_MEMORY_GUARD_RATIO` (0.8) of `maxmemory` on: `POST /v1/batch` answers 503
`internal_error` (retryable), the sync-to-job hand-over does not happen (the request waits inline
and answers 200), and the result cache skips its writes on API pods and workers alike (R31). The
workers' cache writes matter most: they are the second copy of every result of the batches that
were queued before the guard tripped. The job store's result writes stay unguarded, so jobs that
were accepted complete. Without `maxmemory` there is no guard; a failing read keeps the last
verdict. The gauges `text_extraction_valkey_used_memory_bytes` and `…_max_memory_bytes` come from
API pods and workers and feed the alert `TextExtractionValkeyMemoryHigh` (`max()` over the pods). The guard bounds new work, not the worst case:
batches accepted below the ratio still add their results. Quotas per client and a separate cache
Valkey are left to package 11.

**Outbound limits under load (R21, R22, R25).** The end-to-end run (`tests/e2e`) found two gaps
in the package-2 gate and fixed them. Spacing counts from real request starts, and the fetcher
reports each request once its headers are on the wire, so a request held up by a busy event loop
delays the next one instead of letting it follow closer (R21). The compliance files ride in the
page's gate turn, fetched one after another before the page, so the concurrency limit holds for
every request a host sees (R22). Fix round 1 (R24) gave the three files one 10 s budget.

R25 sets where the spacing lies: between the requests of different turns, not inside a turn.
`out:starts:{host}` holds one entry per turn token, scored by that turn's latest real start
(`ZADD GT`, so a late report never moves it back; old entries are trimmed). Before every request
of a turn, each compliance file and the page, the fetcher asks the gate, which waits out what is
left of the delay since the latest start of the other turns (the two highest scores, skipping
the turn's own token) and records the request under the token, atomically per host and bounded by
`OUTBOUND_MAX_WAIT_SECONDS`. The token travels in a context variable set while the turn is held,
so a shared compliance load runs under the turn that started it. First contact therefore costs
no more than R22 did: the files and the page go back to back. Two cold turns in one pod that
share one file load still send their pages at least the delay apart. Two pods may each load a
cold origin's files once; that is accepted (no cross-pod lock), and those loads keep the spacing
to each other's requests too.

Taking the turn before the files changes two answers on a cold origin (one whose files are not
cached). A busy host (no lease or no slot within `OUTBOUND_MAX_WAIT_SECONDS`, an open circuit or a
pause) now answers `rate_limited` or `connection_error` before robots.txt is read, where package
2 could answer `blocked_by_robots` first; a page that robots.txt disallows is still answered
`blocked_by_robots` without its own request. A cross-host redirect to a cold origin takes a turn
at the target for its files, so even a target that robots.txt then blocks has used one booked
spacing slot there.

**Done when (package 3):** `tests/e2e/run_batch.py` (workflow `e2e`) runs 1,000 URLs (400 on one
host, 600 over nine) plus a second client's 20 URLs on the busiest host, across two workers,
against a local site farm. Both jobs complete with no failed item. Every host sees at most 2
requests at once and page starts at least 180 ms apart (200 ms minus 10 %). Each compliance file
is loaded at most once per worker, and two loads of one file start at least 180 ms apart. The
second client finishes first.

## Browser worker

Package 4: `browser.py`, `browser_render.py`, `browser_actions.py`, `browser_proxy.py`,
`browser_unblock.py`, `browser_worker.py`, `browser_dispatch.py`, `clearance.py`,
`escalation.py`, `render.py`. Settings are in [configuration.md](configuration.md) ("Browser mode").

**Where Chromium runs.** Only in the browser worker (`text-extraction-browser-worker`, pool
`browser`, image `text-extraction-browser`). API pods and extract workers never start a browser
and never import Playwright or Patchright. The browser worker is the extract worker's `Worker`
(slot loops, leases, deferrals, retries, result writes) with another runner. It serves only the
internal port.

**One pipeline, plus a renderer.** The browser worker runs the same `Pipeline` as fast mode with a
`Renderer` in place of the fetch: the rendered DOM goes through the same conversion, quality gate,
metadata, fingerprint and cache. The fast pipeline's answers for `mode: fast`, and for `auto`
while `BROWSER_ENABLED` is false, are unchanged.

**Decisions of Jan (2026-10-24), with his reasons.**

- **D1, hybrid page loading.** Chromium loads the page, document included, so the target sees a
  real browser. Our checks wrap it (B2). Reason: Jan prefers the higher extraction quality of a
  browser that fetches the page itself over a stricter guarantee on redirects; the one gap this
  leaves, the first request of an HTTP redirect hop to another domain, which goes out before the
  domain limits are asked, is not critical to him.
- **D2, done-when.** `wlo_200` in `auto` mode at or above the 80 % browser baseline (4,000 URLs,
  2026-09-28), and the smoke set reported. The live workflow gates on that and also reports the
  smoke set and `wlo_200` with `mode: browser`. A miss is debugged per domain, fast against
  browser, before any setting is tuned. So that the rig does not count against the product, the
  workflow lets a sync request wait 30 s for a browser page (`BROWSER_QUEUE_WAIT_SECONDS`), runs
  the browser report with as many requests at once as the worker renders pages, and the summary
  counts `browser_unavailable` answers and the error codes per domain.
- **D3, unblock mode.** Patchright with real Chrome, tied to `compliance: ignore` (admin) and
  `BROWSER_UNBLOCK_ENABLED`. Reason: partners allowed extraction by contract but keep their bot
  protection, so once getting past it is allowed we should reach as much as we can. Captchas:
  nothing paid, only stealth plus a free Turnstile or hCaptcha checkbox click. Partners are
  advised to add an allowlist rule (a secret header we send, or our egress IP); there is no IP or
  proxy rotation.

**Rulings.**

- **B1, where the browser runs.** A sync request that needs a browser creates a job of kind
  `extract` or `screenshot` with one URL and queues item 0 on pool `browser` under the client
  `<key name>#live`, so the key's own batches do not queue it. It polls the job (0.2 s, rising
  to 0.5 s), waits `BROWSER_QUEUE_WAIT_SECONDS` for a worker to claim the item, then the render
  deadline + 5 s for the result. A request that gives up deletes the job, so a worker that claims
  it later drops it. With `wait_seconds` the request becomes a 202 with that job instead
  (`Handover` decides which of the route and its task turns the request into a job). A request
  that answers with the job's result deletes the job too (nobody can read it); one whose result
  has a screenshot is kept `ARTIFACT_TTL_SECONDS` at most, for the artifact route. A job answered
  with 202 keeps `JOB_TTL_SECONDS`. The browser worker deletes an item's draft once the item has
  its outcome.
- **B2, hybrid loading (D1).** A render runs these steps:
  1. Before the navigation: robots, TDM and `noai` for the request URL (missing compliance files
     are fetched by our own fetcher inside the turn). A block ends the item without any browser
     request. Then the host's gate turn is taken and held for the render.
  2. `page.goto(url)`: Chromium fetches the document itself, through the egress proxy.
  3. Later main-frame navigations (a JavaScript redirect, a meta refresh, a clicked link) are
     sent only after the same checks; the turn moves when the host differs. A refused one is
     aborted and ends the render with the block's code (during an action: the action fails). A
     non-GET navigation is allowed only in unblock mode; otherwise it is answered with 204 and
     the warning `navigation_refused_non_get`.
  4. HTTP redirect hops, which Playwright cannot intercept, and the final document are checked
     when the answer arrives: robots, TDM and `noai` of every URL in the chain, and the headers
     and DOM of the final response. A block discards the render. The first request of a hop to
     another host went out before its check (the accepted gap of D1); the hop gets its turn from
     then on.
  5. The final response is judged as in fast mode: a non-2xx status is `target_http_error`, a
     download or non-HTML type is `unsupported_content_type`, except that a download or a
     document type without caller secrets is fetched directly (package 5, F18).
  A document that cannot be loaded because the egress proxy refused its host (a private address)
  shows in Chromium as a failed tunnel; the render maps it to `blocked_url` by matching the
  document host against the proxy's refusals of that page (R6).
- **B3, outbound limits.** A render holds the host's gate turn from the first navigation until
  the page is closed. Sub-resources take no turn, like a visitor's browser (Chromium caps them
  at 6 connections per host). Extraction renders in standard mode do not load images, media and
  fonts (`BROWSER_BLOCKED_RESOURCES`); screenshot renders and unblock renders block nothing.
- **B4, SSRF.** Every byte a browser sends goes through the in-process egress proxy
  (`browser_proxy.py`, 127.0.0.1, random port), in both modes: it resolves each host once with
  the fast mode's address check, dials the first allowed address, refuses non-public addresses
  with 403 and counts bytes per page (cap `BROWSER_MAX_BYTES`). It is a plain `CONNECT` tunnel
  and relays plain `http://` requests, so TLS stays end to end and Chromium's own TLS
  fingerprint reaches the site. A connection lives at most the longer of `BROWSER_TIMEOUT_SECONDS`
  and `BROWSER_UNBLOCK_TIMEOUT_SECONDS`, plus 5 s. Launch flags: `--proxy-server` with a bypass list that sends even
  loopback through the proxy, `--disable-quic`, WebRTC limited to proxied UDP,
  `--disable-dev-shm-usage` and `--dns-prefetch-disable`. Each page has its own proxy
  credentials. Standard mode blocks service workers and refuses downloads. Speculation rules
  (prefetch, prerender) are removed by an init script, files named by a `Speculation-Rules`
  header are refused, and prefetch requests are aborted, because a document they fetch would be
  used without passing the navigation check.
- **B5, auto escalation.** See pipeline step 4 for the rule. A result that will escalate is
  neither cached nor fingerprinted by the fast run. Its envelope is stored as the item's draft
  and the item is queued on `browser`; the browser worker renders and keeps the better of both.
  The extract worker moves an escalating batch item to the `browser` pool (same job and index)
  and completes it without a result. An `auto` item without a draft runs the fast step itself
  first. If no worker claims a sync `auto` request in time, or the render does not answer, the
  request returns its fast result with the warning `browser_unavailable` (its fingerprint is
  dropped); a sync `browser` request fails with `timeout`, retryable.
  An `auto` request with browser options always renders (R8); while browser mode is off it
  answers 400 (R19). A stored result is final: a cache `hit` or `revalidated` result never
  escalates, since only the better of both was stored. Accepted consequence: a fast result
  cached while browser mode was off escalates only once its entry has expired.
- **B6, caller secrets.** `actions`, `cookies` and `headers` are accepted by `/v1/extract` and
  `/v1/screenshot` only, not by `/v1/batch`. They are stored in `job:{<id>}:secrets` with a short
  TTL (`BROWSER_QUEUE_WAIT_SECONDS + lease + 60` s, where the browser lease is
  `2 × max(BROWSER_TIMEOUT_SECONDS, BROWSER_UNBLOCK_TIMEOUT_SECONDS) + 10`: an item whose worker
  died at the claim runs again with them once the lease has ended), never appear in
  `GET /v1/jobs` or the logs, and are deleted once the item has its outcome (R18). An item that
  is given back to run again (a refusal by the outbound gate, a Valkey error, a cancelled
  shutdown) keeps them for that run. A job whose secrets expired before the claim fails with
  `timeout`, retryable, instead of rendering without them. Requests carrying any of them bypass
  the result cache and fingerprints. `cookies` may only name the URL's host or a parent domain.
  `evaluate` is admin-only (403 `forbidden`).
- **B7, screenshots.** PNG, 1920 px wide, viewport 1920×1080. `full_page` (default true) is
  clipped at `SCREENSHOT_MAX_HEIGHT_PX` with the warning `screenshot_clipped`; a PNG over
  `SCREENSHOT_MAX_BYTES` fails with `too_large`. The browser worker stores it as
  `job:{<id>}:artifact:screenshot.png` for `ARTIFACT_TTL_SECONDS`; the API serves it at
  `GET /v1/jobs/{id}/artifacts/screenshot.png`. While Valkey's memory guard is high, or the
  write fails, the item fails `internal_error`, retryable. A screenshot is `success` once stored;
  its `quality` is reported but does not fail it. Screenshot requests bypass the result cache.
- **B8, user agent.** Standard mode sends Chromium's own user agent plus
  `text-extraction/<version> (+https://github.com/janschachtschabel/text-extraction)`; the robots
  token is unchanged. Unblock mode sends Chrome's unmodified user agent (see the exception to
  CLAUDE.md rule 6).
- **B9, switches.** `BROWSER_ENABLED` (default false) turns browser mode on; compose and Helm set
  it from the browser worker toggle. Off, the browser options answer 400 and `auto` never
  escalates. `BROWSER_UNBLOCK_ENABLED` (default false): off, `compliance: ignore` renders in the
  standard browser.
- **B10, browser results.** `mode_used: "browser"` and `timings.render_ms`. Cached while fresh
  but never revalidated (no validators are stored; `raw_sha256` is the hash of the rendered HTML).
  `unblocked` is true when unblock mode rendered the result.
- **B11, consent.** Every render runs autoconsent with the opt-out (`BROWSER_AUTOCONSENT`,
  default true) with custom rules for Government Site Builder banners and for MediaWiki's
  CookieConsent dialog (Klexikon and other wikis: manage, then save with the optional cookies
  unchecked; the page reloads, which the render waits for). A dialog that no rule could close is
  removed before the text is read when it is a visible, fixed-position `role=dialog` or
  `aria-modal` overlay whose text is about cookies or consent (warning
  `consent_overlay_removed`, R22). The `accept_cookies` action opts in instead; when there is no
  banner it is a no-op success (R14).
- **B12, challenges in standard mode.** Nothing waits for or solves a challenge. The rendered page
  goes through the quality gate. In addition `bot_challenge` is set on a thin page (under 100
  words) whose render contacted a challenge host: `challenges.cloudflare.com`, `hcaptcha.com` or
  a subdomain, `geo.captcha-delivery.com`. reCAPTCHA is left out: it sits on too many ordinary
  forms. Chrome's own background hosts never count.
- **B13, sync bound and shutdown.** A sync request without `wait_seconds` can take the fast phase
  plus `BROWSER_QUEUE_WAIT_SECONDS`, the render deadline and 5 s. The API's graceful shutdown
  covers that while `BROWSER_ENABLED` (75 s by default, 105 s with unblock mode, R20); the Helm
  chart and docker compose derive their grace periods from it (R21). Callers who need less use
  `wait_seconds` and get a 202 with the job.
- **B14, unblock mode (D3).** It applies when the resolved compliance mode is `ignore`, the caller
  is an admin and `BROWSER_UNBLOCK_ENABLED` is true; batches with `compliance: ignore` too.
  - Driver: Patchright with Google Chrome (`BROWSER_UNBLOCK_CHANNEL`; `chromium` uses
    Patchright's own build), headful under Xvfb unless `BROWSER_UNBLOCK_HEADLESS`, one persistent
    context in a fresh temporary profile per page, a 1920×1080 window, Chrome's own user agent
    and no extra headers. The profile is removed when the page ends. After a crash Chrome's
    remaining processes may write to it for a while, even after its removal, so one background
    sweep removes every released profile again for 30 s, the pause doubling from 1 s (warning
    `browser_profile_left` for one still there then); the worker's shutdown removes any left.
    Patchright is pinned to the newest release at least 14 days old
    (1.63.0; supply-chain caution, R16). Google Chrome is not pinned: each image build installs
    the stable of that day.
  - What changes: robots and TDM are not checked (that is `ignore`); nothing is blocked; service
    workers and non-GET navigations are allowed. The proxy, the navigation checks, the byte cap
    and the outbound gate stay: unblock mode changes how we look, never how often we come, and
    the route handler stays in place (R17).
  - Challenges: after the load, while the DOM is a challenge and time is left, the render waits up
    to `BROWSER_UNBLOCK_CHALLENGE_WAIT_SECONDS`, looks again every second and follows the
    challenge's own navigations. Non-interactive challenges clear this way (Cloudflare managed
    and Turnstile, Anubis proof-of-work, DataDome and Akamai JavaScript checks). A visible
    Turnstile or hCaptcha checkbox still there after 3 s is clicked once, mouse move first, at
    most twice per render (warning `challenge_checkbox_clicked`). Image or puzzle challenges are
    not attempted; the result is `bot_protection` with the warning `challenge_not_cleared`.
  - Clearance reuse: after a render whose last document was 2xx and not a challenge, the cookies
    of the host and its parent domain are stored in `unblock:cookies:{<host>}` (TTL at most
    `BROWSER_UNBLOCK_COOKIE_TTL_SECONDS`) and added by the next unblock render of that host. Only
    unblock renders read them, and they are never shown. A render with caller secrets (actions,
    cookies or headers) stores none: its cookies may be the caller's own or a session they opened
    (B6); it still reads the stored ones. A render that got past a 403, 429 or 503
    challenge keeps that answer out of the host's backoff.
  - Escalation: under unblock `auto` also escalates per B5. The result carries `unblocked: true`.
- **B15, what unblock does not do.** No image or puzzle captcha solving and no paid or
  third-party solver services (Jan: nothing paid). Partners are advised to add an allowlist rule
  in their bot protection instead: a secret header (sent with `headers`) or our egress IP. No IP
  or proxy rotation; CLAUDE.md rule 6 keeps that. If a partner blocks by IP, the fix is that they
  allow our egress IP; a later package can add an upstream proxy option if Jan wants it.

**Caller headers (R10, R13).** Headers go only to requests on the requested URL's host and port.
Playwright would re-apply them to the hops of a redirect, so while a render carries headers every
request on that host is fetched by the route handler without following redirects. A redirect of
the document is answered with a tiny page that navigates to the target; the hop is then a later,
checked navigation, and the headers go only to it when its host is the request's. Residuals: those
requests are buffered and carry Node's TLS fingerprint rather than Chromium's.

**Known limits.** A click that starts a download discards the page (the render ends without
its content; the file is fetched directly, F18). The first request of an HTTP redirect hop to
another host leaves before its check (D1). Screenshots need `JOBS_ENABLED=true`. The first
browser launch of a worker happens before `/ready`, and a browser that does not start ends the
process. HTML served without a `Content-Type` does not escalate (step 4).

## Documents and OCR

Package 5: `documents/` (`sniff.py`, `pool.py`, `pool_child.py`, `pdf.py`, `routing.py`,
`markitdown_convert.py`, `docling_convert.py`, `images.py`, `meta.py`, `formats.py`,
`models.toml`), `model_store.py` (shared with the ASR worker since package 6),
`pipeline_documents.py`, `file_api.py`, `pool_dispatch.py`, `docling_dispatch.py`,
`docling_worker.py`. Settings are in [configuration.md](configuration.md)
("Documents and OCR").

**Decisions of Jan (2026-10-25: the recommended options).**

- **D1, where MarkItDown runs: in the pod that has the bytes.** API pods, extract workers and the
  browser worker each keep `DOCUMENT_CONCURRENCY` child processes for it. No extra hop and no
  mandatory worker for PDFs; the API image grows by about 120 MB (pandas, pdfminer, onnxruntime
  via magika). The nlp-worker stays for Presidio.
- **D2, a sync request that needs OCR: wait, then answer the text layer.** `POST /v1/extract`
  without `wait_seconds` waits up to `DOCLING_SYNC_WAIT_SECONDS` (20) for the Docling worker, then
  answers the text layer with the warning `ocr_unavailable` (an image, which has none, fails
  `timeout`, retryable). With `wait_seconds` it becomes a 202 job. `POST /v1/extract/file`
  defaults to `wait_seconds: 25`, so an upload that needs longer answers 202 with its job. The
  sync contract of `/v1/extract` holds, but not the ~30 s job rule: a sync request that needs OCR
  can take `EXTRACT_TIMEOUT_SECONDS` + `DOCLING_SYNC_WAIT_SECONDS` (25 + 20 = 45 s by default),
  the fast phase and then the wait for the worker. Callers that need a shorter bound send
  `wait_seconds` or lower `DOCLING_SYNC_WAIT_SECONDS` (at 0 a sync request does not wait).
- **D3, the Docling worker is off by default.** Helm `workers.docling.enabled: false`, compose
  profile `docling`; it needs an image of about 2 GB and about 335 MiB of models (9 files). Without it a
  scan keeps its text layer (`ocr_unavailable`), and images and OpenDocument files answer
  `unsupported_content_type` ("OCR is not enabled on this service.").

**Rulings, as built.**

- **F1, formats and detection** (`documents/sniff.py`). Magic bytes win; the declared type
  decides only where the bytes do not; the extension (URL path or upload name) only breaks the
  CSV tie.

  | Kind | Detected by | Converter |
  | --- | --- | --- |
  | `pdf` | `%PDF-` in the first 1024 bytes | MarkItDown, or Docling per F4 and F11 |
  | `docx`, `pptx`, `xlsx` | ZIP with `[Content_Types].xml` naming the main part | MarkItDown |
  | `xls` | OLE2 signature with a `Workbook` or `Book` stream | MarkItDown |
  | `epub` | ZIP whose first entry `mimetype` is `application/epub+zip` | MarkItDown |
  | `csv` | `text/csv` or `.csv`, decodable text | MarkItDown |
  | `odt`, `ods`, `odp` | ZIP `mimetype` `application/vnd.oasis.opendocument.*` | Docling |
  | `png`, `jpeg`, `tiff`, `webp`, `bmp` | image signatures | Docling (OCR) |
  | anything else (`doc`, `ppt`, `rtf`, `msg`, `zip`, `gif`, ...) | none | `unsupported_content_type` |

  A body declared HTML or XHTML keeps its package-2 path. A body declared `text/plain` is a
  document only when a signature starts it (offset 0 after an optional byte order mark and at
  most 1 KiB of whitespace); otherwise it stays plain text, also when its URL ends in `.csv` (the extension
  tie-break does not apply to a declared `text/plain`). Untyped bodies, `application/octet-stream`
  and other declared types keep the 1024-byte window for `%PDF-`. Where no document signature
  matches and the body (after a byte order mark and whitespace) starts with `<!doctype html` or
  `<html`, under a document type, `application/octet-stream` or no type, it is HTML: an "upload
  named `report.pdf`" or a PDF URL that serves an error page converts as that page, for URLs and
  uploads alike. `content_type` in the result is the sniffed media type.
- **F2, where conversions run (D1).** MarkItDown kinds convert in the pod that holds the bytes,
  in a `DocumentPool` child; Docling kinds and OCR convert only in the Docling worker. No module
  that the API, the extract worker or the browser worker imports pulls in Docling or torch, and
  their parent processes never import MarkItDown, pdfminer, pdfplumber or pandas.
- **F3, process isolation** (`documents/pool.py`). A child is a plain interpreter (`python -m
  text_extraction.documents.pool_child`) in a session of its own, not a `multiprocessing` child
  (which would re-import the parent's `__main__` before its limit is set). It gets an
  allowlisted environment (no keys, no Valkey password), talks over a socket pair, and imports
  only its target module (`documents/child.py` for MarkItDown, `documents/docling_child.py` for
  Docling), whose `FUNCTIONS` the parent calls by name. It raises its `oom_score_adj` to 1000
  (so the container's OOM killer, which counts the parent and all children, ends a child before
  the server) and sets `RLIMIT_AS` (`DOCUMENT_MEMORY_LIMIT_MB`, 2048) before its first import.
  The Helm chart renders `DOCUMENT_MEMORY_LIMIT_MB` from each pod's memory limit: floor((limit -
  `documentParentReserveMiB`) / `DOCUMENT_CONCURRENCY`), 768 MiB with the defaults (2Gi with a
  512 MiB reserve for the API and extract pods, 4Gi with a 2560 MiB reserve for Chromium in the
  browser pods), so the children's address space fits the limit and a large document ends as
  `too_large` in its child rather than as an OOM kill of the pod; a share below 256 fails the
  render (measured: an idle MarkItDown child maps about 390 MB, about 500 MB after a small
  PDF). A child runs one conversion at a
  time and is replaced after 50 conversions, when it dies, runs out of memory or is killed. The
  caller passes its deadline; waiting for a free child counts against it; when it passes, the
  child's process group gets SIGKILL (a tesseract it started too) and the caller gets
  `timeout`. Replies are unpickled with builtin types only and at most 64 MiB of them; a larger
  one, like a `MemoryError`, is `too_large`. A dead child or a malformed reply is our fault,
  logged by its type only (a converter's message may quote the document). The children start
  before `/ready`.
- **F4, PDF probe and OCR routing** (`documents/pdf.py`). In the child, before MarkItDown,
  pypdfium2 opens the PDF (one that needs a password fails `conversion_failed`, not retryable,
  "The PDF is password-protected."), counts its pages and the characters of each of the first
  50. The PDF needs OCR when at least half of the probed pages have fewer than 50 characters, or
  when more than 5 % of MarkItDown's tokens are `(cid:N)` or U+FFFD. A PDF without pages is
  `empty_content` without a conversion. Pages after `DOCUMENT_MAX_PAGES` (100) are cut before
  the conversion (a new pdfium document of the first pages; warning `pages_truncated`).
  "Table-heavy" is not guessed: MarkItDown's pdfplumber pass keeps simple tables, and
  `ocr: always` sends a PDF to Docling for its layout and tables.
- **F5, container bombs.** Before a ZIP-based kind (OOXML, EPUB, OpenDocument) converts, its
  central directory alone decides: at most 10,000 entries, at most
  `DOCUMENT_MAX_UNCOMPRESSED_BYTES` (200,000,000) unpacked in total, and no entry over 1 MiB
  unpacked whose ratio to its compressed size is over 100, else `too_large`; nothing is
  decompressed. Small entries are exempt from the ratio (a sparse sheet of zeros is no bomb; the
  total still bounds them), and `RLIMIT_AS` is the backstop. An archive that cannot be read is
  `conversion_failed`.
- **F6, output formats** (`documents/formats.py`). `markdown` is the converter's Markdown,
  whitespace normalised; `html` is markdown-it-py (CommonMark plus tables, raw HTML escaped)
  rendering it; `text` is the text of that HTML with a newline after each block. For Docling the
  Markdown is its `export_to_markdown` and the text its `export_to_text`, but the HTML is built
  from its Markdown with markdown-it too, not with `export_to_html`: Docling's page carries a
  stylesheet whose words the quality gate would count, and our renderer escapes the raw HTML of
  a hostile document. Merged table cells are lost in that HTML.
- **F7, the result.** `document` (`DocumentInfo`): `kind`, `converter` (`markitdown` or
  `docling`), `pages` (None for kinds without pages), `pages_converted`, `ocr` (the text comes
  from OCR; false for OpenDocument files, which Docling reads without it) and `file_name`
  (uploads only). URL documents have `mode_used: "fast"`, also after a render that ended on a
  file (F18); uploads `mode_used: "upload"`, `url` and `final_url` null, `attempts` 0. The new
  fields have defaults, so cached results stay valid; `JobItem.url` is null for uploads.
- **F8, document metadata** (`metadata: true`, `documents/meta.py`). From the PDF info
  dictionary and its XMP packet (`dc:title`, `dc:creator`, `dc:description`, `dc:rights`,
  `xmpRights:WebStatement`, `cc:license`, dates; the packet the catalog's `/Metadata` names,
  else a stream typed `/Metadata`, never the XMP of an embedded image), OOXML
  `docProps/core.xml`, ODF `meta.xml` and the EPUB package document (`dc:*`), parsed with
  defusedxml. Licences go through
  `LicenseCollector`: a CC licence in a PDF's XMP becomes `CC-BY-SA-4.0` with the source `xmp`
  and confidence `high`. The language is `dc:language`, then `Content-Language`, then the text;
  the child always reads the metadata for it, the result carries it only when asked.
- **F9, quality.** A document is judged like `text/plain`: `assess(None, content)` and
  `QUALITY_MIN_WORDS`. It never escalates to the browser. A scan without OCR ends `empty_content`
  or `content_too_short` with the warning `ocr_unavailable`.
- **F10, URL documents.** Pipeline step 7 hands document kinds to
  `pipeline_documents.DocumentSteps`. The byte limit is `DOCUMENT_MAX_BYTES` (20,000,000; at most
  `FETCH_MAX_BYTES`, checked at start-up) instead of `CONVERT_MAX_BYTES`. Compliance after the
  fetch checks the headers only (`X-Robots-Tag`, `tdm-reservation`), as for `text/plain`. Cache,
  revalidation and fingerprints work as for pages. The cache key gains `ocr` when it is not
  `auto` (so the keys of pages and of default requests stay those of package 4 and a deploy keeps
  the cache), and the fingerprint variant of a document is `<format>|ocr=<ocr>` (pages keep
  `<format>`, so their stored fingerprints stay valid): `auto`, `never` and `always` may read one
  file with different converters, and alternating them must not report a change. A cached document result is final,
  also one cached with `ocr_unavailable`. The MarkItDown child gets what the fetch left of
  `EXTRACT_TIMEOUT_SECONDS`, so its timeout is `timeout`, retryable.
- **F11, the `ocr` option.** `ExtractRequest`, `BatchRequest` and the upload form take `ocr:
  "auto" | "never" | "always"` (default `auto`; a job stores it only when it is not `auto`, so
  pods of package 4 still read the jobs of new pods during a rolling upgrade). `auto`: Docling
  for scans and broken text layers
  (F4), images and OpenDocument files; `never`: MarkItDown only (images and OpenDocument files
  answer `unsupported_content_type`); `always`: Docling for every PDF too. `routing.needs_ocr` is
  the one rule; Docling is used only while `DOCLING_ENABLED` and `JOBS_ENABLED`. `ocr: always`
  while it is not answers 400 `invalid_request` ("OCR is not enabled on this service.").
- **F12, sync dispatch (D2)** (`pool_dispatch.py`, `docling_dispatch.py`). `PoolDispatcher` and
  `Handover` are what the `browser` and `docling` pools share; `BrowserDispatcher` and its metric
  are unchanged. A sync request that needs OCR creates a one-item job (kind `extract` for a URL,
  `file` for an upload) and queues it on pool `docling` under the client `<key name>#live`. The
  bytes go to `job:{<id>}:input:<index>` (a hash with `body` and `meta`: kind, sniffed type, file
  name, validators, `Content-Language`, `raw_sha256`, the probe's verdict, the compliance report),
  with TTL `DOCLING_INPUT_TTL_SECONDS` (3600, never longer than the job); the worker deletes it
  once the item has its outcome. An item whose input expired before the claim answers its draft
  with `ocr_unavailable`, neither fingerprinted nor cached; without a draft it fails `timeout`,
  retryable. MarkItDown's result, when there is one, is the item's draft. Without
  `wait_seconds` the request waits `DOCLING_SYNC_WAIT_SECONDS` in all, then answers the draft with
  `ocr_unavailable` (no draft: `failed`/`timeout`, retryable) and deletes the job; with
  `wait_seconds` the `Handover` turns it into a 202 with the Docling job. A request whose route
  had already handed it over to a P1 job puts the document on that job instead. Back-pressure:
  while Valkey's memory guard is high, the caller already has `DOCLING_MAX_OPEN_PER_CLIENT` (5)
  unfinished Docling jobs, or the input would take the inputs of all jobs over
  `DOCLING_INPUT_BUDGET_BYTES` (200,000,000), nothing is stored or queued: the draft answers with
  `ocr_unavailable` (no draft: `failed`/`rate_limited`, retryable). The memory guard is read
  every 5 s and is no bound on its own; the input budget is: `JobStore.put_input` reserves the
  input's bytes in one Lua script (check and reservation are one step, so concurrent writers on
  every pod never pass it together) before the input is written, and gives them back when the
  input is deleted. The reservations live in keys of their own hash tag (`inputs:{budget}:*`; one
  script on keys of two hash tags would not run on a Valkey cluster), scored by the input's
  expiry, and each reservation pruned when that has passed, so an input that expires by its TTL
  leaves the budget too and a pod that dies between reservation and write leaks nothing for
  longer than the TTL. Every input writer goes through it: this sync dispatch, the batch move
  (F13) and a slow upload's P1 job (F17). A fallback answer carries no fingerprint, so
  it is neither compared nor cached and the next request may get the OCR. The API's graceful
  shutdown covers the fast phase plus `DOCLING_SYNC_WAIT_SECONDS` + 5 s while `DOCLING_ENABLED`.
- **F13, batches.** The extract worker moves an item that needs OCR to pool `docling` (same job,
  index and client): input, then draft, then the queue, then its own completion without a
  result, as B5 moves items to `browser`. While the memory guard is high, or the input budget
  (F12) is full, it keeps the draft with `ocr_unavailable` instead, unstored (no draft:
  `rate_limited`, retryable), and counts the refusal in `text_extraction_docling_dispatch_total`
  (`memory_guard`, `input_budget`).
- **F14, the Docling worker** (`text-extraction-docling-worker`, pool `docling`, internal port
  only). The package-3 `Worker` with a `DoclingRunner`; leases of `2 × DOCLING_TIMEOUT_SECONDS +
  10` seconds. `DOCLING_CONCURRENCY` (1) slot loops, each converting in a child of its own (F3,
  with `DOCLING_MEMORY_LIMIT_MB`, 6144), killed at `DOCLING_TIMEOUT_SECONDS` (300) from the start
  of its conversion. Docling's options: the PDF pipeline with OCR through Tesseract's command line
  (`TesseractCliOcrOptions`, languages `DOCLING_OCR_LANGUAGES`, `deu,eng`), full-page OCR when the
  probe said "scan" (always for an image), table structure in fast mode, `artifacts_path` =
  `DOCLING_MODELS_PATH/docling` with Tesseract's data beside it in `DOCLING_MODELS_PATH/tessdata`
  (`TESSDATA_PREFIX`), CPU only with `DOCLING_THREADS` (4) threads, Hugging Face offline, and
  `document_timeout` = 0.9 × `DOCLING_TIMEOUT_SECONDS`, so Docling stops between pages and answers
  what it read (warning `pages_partial`) before the child is killed. OpenDocument files go through
  Docling's simple pipeline (no OCR, no models). An image with a frame over Pillow's bomb
  limit (89,478,485 pixels, in each of the first `DOCLING_MAX_PAGES` frames) is `too_large`
  before it is decoded (`documents/images.py`). Pages after `DOCLING_MAX_PAGES` (100) are cut
  (`pages_truncated`). The runner calls `Pipeline.convert_queued_document`, which builds the result
  like the inline path (F7-F9) and keeps the OCR result unless it failed when the request said
  `ocr: always` or the probe found a scan or a broken text layer (`needs_ocr`): a garbage layer
  of many `(cid:N)` words, or a good one with the headers and footers Docling leaves out, must
  not beat it. Otherwise it keeps the better of draft and OCR result (`escalation.better`; a tie
  goes to OCR). For a URL it compares and stores the kept one in the result cache and
  fingerprints (with the input's validators; none for a file a render ended on, whose warning
  `document_fetched_directly` is kept). A draft kept because the OCR failed gets `ocr_unavailable`;
  when that failure is retryable (models missing, the conversion pool unavailable) the draft also
  loses its fingerprint and is neither compared nor stored. A Docling timeout is not retryable,
  unlike MarkItDown's: the child had all of `DOCLING_TIMEOUT_SECONDS`, and the same file would time
  out again. Models missing on the volume are `internal_error`, retryable: before a PDF or an
  image the child checks the manifest's files on the volume (a stat of each, `require_models`),
  because Docling raises a plain `RuntimeError` for a missing layout `config.json`, which would
  otherwise read as `conversion_failed`. An item lost too often
  (`JOB_ITEM_MAX_ATTEMPTS`) answers its draft with `ocr_unavailable`, unstored (no draft:
  `internal_error`, retryable). Before `/ready` the
  worker checks the manifest's files, starts its children and converts a built-in one-page scan
  (`documents/scan_check.png`) in each, which loads the models; a missing file or a failed check
  ends the process with exit code 1 and a log line naming `DOCLING_MODELS_PATH`. On SIGTERM it
  stops claiming, gives the items in flight 30 s, then kills its children; their leases expire and
  the items run again.
- **F15, models on the PVC** (`model_store.py`, `documents/models.toml`).
  `--download-models` fills `DOCLING_MODELS_PATH` from our own manifest instead of Docling's
  downloader, which pins its layout model to a branch (`main`), not a commit: every file has a
  URL at a fixed commit (https only, redirects too), its byte size and its SHA-256: Docling's
  layout and TableFormer files under `docling/`, and `deu`, `eng` and `osd.traineddata` from
  `tesseract-ocr/tessdata_best` under `tessdata/`. It downloads into a staging directory on the
  volume, checks size and hash while writing, renames each directory into place and then
  writes `.complete-<manifest hash>`; with that marker present it does nothing, and a new
  manifest has a new marker. Each file has a total deadline (120 s plus 256 KiB/s). `--check`
  converts the built-in scan and exits 0 or 1 (the image build and CI run it). Helm: an init
  container runs `--download-models` with the PVC mounted writable, the worker mounts it
  read-only; `models.download: false` with `models.persistence.existingClaim` serves an
  air-gapped, pre-filled volume. Compose: the one-shot service `docling-models` on the named
  volume `docling-models`. Init containers of several replicas may share one ReadWriteMany
  volume: one that finds the marker after its downloads keeps the other's files, and a failed
  rename is a failure only while the marker is still missing.
- **F16, switches (D3).** `DOCLING_ENABLED` (default false). Helm renders it into every pod from
  `workers.docling.enabled` (default false); compose reads it from `.env` (`docker compose
  --profile docling up` with `DOCLING_ENABLED=true`).
- **F17, `POST /v1/extract/file`** (`file_api.py`). `multipart/form-data` with one file part
  `file` and the fields `format`, `metadata`, `ocr`, `wait_seconds` (default 25, D2); at most one
  file and 8 fields. The body limit is `DOCUMENT_MAX_BYTES` + 64 KiB (`MEDIA_UPLOAD_MAX_BYTES`
  + 64 KiB where it is larger and audio and video uploads are transcribed). Auth (role `user`) and the
  inbound rate limit are checked before a byte of the body is read, so a caller without a key or
  over its limit never makes the pod receive or spool the upload. The part is read into memory
  and its spooled file in `/tmp` deleted at once. The file name is reduced to its base name,
  control characters removed, at most 255 characters, reported as `document.file_name` and never
  logged. HTML and plain-text uploads use the package-2 converters (relative links stay as they
  are). No compliance check (`compliance.decision: "skipped"`), no result cache (`cache:
  "bypass"`), a fingerprint that is computed but never compared or stored (`changed: null`).
  With `wait_seconds` a slower conversion becomes a job of kind `file` (202) whose item keeps
  the upload's bytes as its input (the same input hash as F12, up to `DOCUMENT_MAX_BYTES`, for
  the job's run and deleted once the result is stored, `DOCLING_INPUT_TTL_SECONDS` at most). The
  input counts against `DOCLING_INPUT_BUDGET_BYTES`: while the budget is full the upload is not
  handed over and its request stays synchronous. An upload that needs OCR becomes its Docling
  job.
- **F18, a render that ends on a file.** When a render's main frame ends on a download (also one
  a clicked link started; `Rendered.download_url`) or on a 2xx answer of a document type, and the
  request carries no caller secrets (cookies, headers, actions) and is no screenshot, the
  pipeline fetches that URL with the fast fetcher in a turn of its own, with the compliance
  checks of fast mode and the SSRF checks of a navigation, and converts it per F10, with the
  warning `document_fetched_directly` and `mode_used: "fast"`. Its validators are not stored (they
  fit the request URL only). With secrets it stays `unsupported_content_type`: the fetcher must
  not send the caller's cookies or headers. Built in the fix round of Task 6, which the plan had
  left without an owner. The render ends on a download only when the first navigation became one
  or the after-load hook or an action started one: then it reads no HTML. A download that an
  iframe started (its URL was an iframe's navigation only), or that the page started while it
  went idle or its consent banner was handled, leaves the page in the main frame: the page's
  HTML is the result, `download_url` is None and the download's answer is not among the
  render's document answers.
- **F19, metrics.** `text_extraction_document_conversions_total{converter, kind, outcome}`,
  `text_extraction_document_conversion_seconds{converter}` and
  `text_extraction_document_pages_total{converter}`, recorded where the conversions run (the
  MarkItDown ones in API pods and workers, uploads included; the Docling ones in the Docling
  worker); `text_extraction_docling_dispatch_total{outcome}` on API pods; the queue gauges for
  pool `docling`. Helm alert rules: `TextExtractionQueueBacklog` for pool `docling` (its own
  threshold, `alerts.doclingQueueBacklogItems`, 100) and `TextExtractionWorkersDown` for the
  Docling workers. The conversion metrics joined Task 6, the dispatch metric Task 8, the Docling
  conversions Task 9 and the alert rules Task 10.

**Images and processes.** The Docling image (target `docling` of the one Dockerfile, linux/amd64)
holds Docling (`docling-slim`), CPU torch and the Tesseract CLI, no models. Every image runs tini
as PID 1. A CI licence gate (`scripts/check_licenses.py`) fails on any GPL or AGPL distribution in
the installed environments (CLAUDE.md rule 7).

**Done when (package 5):** `tests/documents/test_corpus.py` converts the corpus that
`tests/documents/make_corpus.py` builds (`tests/documents/corpus/`, described in `expected.json`)
through the real pipeline and document children: a German PDF with a table, a 120-page PDF (cut
to 100), a PDF with a CC BY-SA licence in its XMP, DOCX, PPTX, XLSX, XLS, EPUB and a Latin-1
CSV, each with its expected phrases, kind and pages; a DOCX container bomb answers `too_large`, a
password-protected PDF `conversion_failed` and a PDF without pages `empty_content`. Under the
marker `docling` (CI job `documents`, with the models) the scan, a PNG, a JPEG and an ODT convert
through the Docling path, and the two-page scan yields its text with a word recall of at least
0.9.

**Known limits.**

- An HTML page with a UTF-16 byte order mark, an `<?xml` prolog or a leading comment, served under
  a document type, `application/octet-stream` or no type, is not recognised as HTML and answers
  `unsupported_content_type`.
- A CSV served as `text/plain` stays plain text.
- A crashed Docling child is final (`conversion_failed`, not retryable): a scan keeps its draft
  with `ocr_unavailable`, which is cached until its entry expires, and an image fails.
- Docling backlog: with no Docling worker (scaled to zero, crashed, models missing) sync requests
  answer without OCR after `DOCLING_SYNC_WAIT_SECONDS`, and batch items wait in the queue (the
  alerts `TextExtractionQueueBacklog` and `TextExtractionWorkersDown` fire), but their inputs
  only `DOCLING_INPUT_TTL_SECONDS` (3600): an item claimed later answers its draft with
  `ocr_unavailable`, unstored, and an image or OpenDocument file fails `timeout`, retryable. A
  crashed API pod's sync Docling job counts as open for its key until `JOB_TTL_SECONDS`.
- The browser worker hands no document to OCR: an image a render ended on fails
  `unsupported_content_type` ("... Extract the document's own URL with mode fast."), and a scan
  answers its text layer with `ocr_unavailable`.
- Not in this package: vision-LLM descriptions of images (package 8), PII (package 7), S3 offload
  of inputs (package 11), legacy binary `.doc` and `.ppt`, RTF, e-mail files and archives
  (`unsupported_content_type`).

## Audio and video

Package 6: `media/` (`sites.py`, `sniff.py`, `select.py`, `captions.py`, `transcript.py`,
`html5.py`, `types.py`, `pool.py`, `child.py`, `asr_child.py`, `asr_windows.py`,
`asr_external.py`, `models.toml`, `asr_check.ogg`), `pipeline_media.py`, `asr_dispatch.py`,
`asr_worker.py`, `model_store.py` (made manifest-generic). Settings are in
[configuration.md](configuration.md) ("Audio and video"); the plan with all rulings is
`docs/superpowers/plans/2026-10-25-package-6-audio-and-video.md`, the spike's measurements
`docs/superpowers/plans/2026-10-25-package-6-spike.md`.

Three tiers. **Tier 1, subtitles:** a URL of a supported platform answers with the video's
subtitles (manual first, then the platform's automatic captions in the spoken language), read by
youtube-transcript-api and yt-dlp in child processes of the pod that holds the request; an HTML
page gains the text of its `<track>` elements. **Tier 2, metadata:** without subtitles the answer
is the video's title, description, uploader, date, duration, chapters, tags and licence
(`transcript_source: metadata_only`, `partial`/`no_transcript_available`). **Tier 3, speech
recognition:** with `transcribe: true` a video without subtitles or a media file, and every
uploaded audio or video file, goes to the ASR worker (faster-whisper on CPU, or an external
OpenAI-compatible endpoint): always a job, answered 202.

**Decisions of Jan (2026-10-25).**

- **D1, YouTube refuses the service: answer honestly (a).** YouTube blocks many data-centre IPs
  ("confirm you're not a bot") and asks for a proof-of-origin token on a growing share of caption
  tracks. Such an answer is the video's details (tier 2) with `transcript_source: metadata_only`,
  the warning `subtitles_blocked`, `partial`/`no_transcript_available`, neither fingerprinted
  nor cached (the block may lift); an HTTP 429 goes to the outbound gate's backoff. No proxy, no
  PO-token plugin (`bgutil-ytdlp-pot-provider` is GPL-3.0) and no browser fallback in this
  package. Not taken: (b) one fixed upstream proxy for media requests, (c) a browser fallback
  that opens the watch page's transcript panel.
- **D2, default Whisper model: `large-v3-turbo` (a).** CPU only, `int8`; about 1.6 GB on the
  volume. The spike measured a real-time factor of 0.88 on 4 threads (`small`: 0.43) and a peak
  RSS of 1.9 GB; Jan re-confirmed it after the spike. `small` stays selectable (`ASR_MODEL`).
- **D3, uploads of audio and video: through Valkey up to `MEDIA_UPLOAD_MAX_BYTES` (a).**
  25,000,000 bytes by default (about 25 minutes of MP3 at 128 kbit/s, hours of Opus), adjustable
  by env and Helm (`workers.asr.uploadMaxBytes`), bounded only by the input budget
  (`DOCLING_INPUT_BUDGET_BYTES`, checked at start-up and by the chart). Larger media go by URL;
  S3 lifts the limit in package 11. Not taken: (b) extracting the audio track in the API pod.
- **D4, players embedded in a page: listed, not transcribed (a).** `data.media.embeds` names
  them (platform URL or file URL, at most 10), so the caller can extract each with a request of
  its own; only `<track>` text is added inline.
- **D5, PyAV and FFmpeg: an LGPL build (a).** The PyPI wheel of PyAV bundles GPL `libx264` and
  `libx265`, which its metadata does not show. The ASR image builds PyAV from its sdist against
  a pinned BtbN `lgpl-shared` FFmpeg (9.0, `--enable-version3`, no `--enable-gpl` or
  `--enable-nonfree`, with `libopus`), checked by SHA-256. How it is enforced: see "Images and
  processes".
- **D6, length limits: 2 h media, 3 h per item, 8 h inputs (a).** `MEDIA_MAX_DURATION_SECONDS`
  7200, `ASR_TIMEOUT_SECONDS` 10800 (the budget of one whole item), `ASR_INPUT_TTL_SECONDS`
  28800 (longer than the item's lease of 2 × 10800 + 10 s, checked at start-up and by the chart),
  all adjustable. Not taken: 1 h media with a 90 min timeout; 3 h media with `small` past 1 h.

**Rulings, as built.**

- **M1, media targets** (`media/sites.py`, `media/sniff.py`). A platform URL is recognised by its
  parsed host and path before any fetch, only in a `fast` or `auto` request without browser
  options, a screenshot or a draft (`mode: browser`, `actions`, `cookies`, `headers` and
  `screenshot` render the page as before):

  | Platform | URLs | yt-dlp extractor (`IE_NAME`) |
  | --- | --- | --- |
  | `youtube` | `youtube.com/watch?v=`, `/shorts/`, `/embed/`, `/live/`, `m.` and `music.` hosts, `youtu.be/<id>`, `youtube-nocookie.com/embed/` | `youtube` |
  | `vimeo` | `vimeo.com/<digits>`, `player.vimeo.com/video/<digits>` | `vimeo` |
  | `ard` | `ardmediathek.de/video/...` | `ardmediathek` |
  | `zdf` | `zdf.de/...html` and `zdf.de/video/<section>/<show>/...` | `zdf` |
  | `arte` | `arte.tv/<lang>/videos/...` | `artetv` |
  | `3sat` | `3sat.de/...html` | `3sat` |

  A ZDF or 3sat `.html` URL may be a news article without a video: when the probe finds none
  there (no track, no title, no description, and the extractor says so: ZDF's fallback
  document answers 404 or has no stream, 3sat's page has no player, or the video is
  unavailable) the request goes on as a page, in the same request and under the
  page's turn, compliance and cache rules, so it answers what the page path answers (`media`
  null or the page's embeds; the probe's fetch counts in `attempts`); a fresh page entry of such
  a URL answers without a probe. yt-dlp runs without retries, so its 5xx, timeouts and broken
  connections are failures that may pass (`target_http_error` or `connection_error`,
  retryable), never "no video". A refusal (`blocked`, `rate_limited`, `blocked_url`), any other
  extractor error or a
  failure that may pass is answered as for any video. YouTube, Vimeo, ARD, arte and ZDF's
  `/video/` URLs name a video and have no such fallback.

  The extractor is yt-dlp's only `allowed_extractors` entry, so the generic extractor (which
  follows anything) never runs; a unit test pins that each name matches exactly one extractor of
  the pinned yt-dlp. The URL is normalised to its canonical form (`https://www.youtube.com/watch?v=<id>`;
  Vimeo's player URL, which needs no login) for the cache key, the fingerprint, the compliance
  check and every fetch; user information and a port are dropped, `url` in the result stays the
  request's and `final_url` is the canonical URL. A playlist (`/playlist?list=`, `/watch?list=`
  without `v`, `/embed/videoseries?list=`) and a live stream that is still running answer
  `unsupported_content_type`. A media file is a fetched or uploaded body whose bytes say MP3,
  MP4/M4A/MOV (`ftyp` with a media brand; HEIC and AVIF are images), WebM/Matroska, Ogg, WAV,
  FLAC or ADTS AAC; the bytes decide, never the declared type or the file name. A response
  declared `audio/*`, `video/*` or `application/ogg` is read only to its first 64 KiB, so a large
  file is sniffed, not refused as `too_large`.
- **M2, tiers and selection** (`media/select.py`). YouTube asks youtube-transcript-api first
  (list, then fetch the chosen track) and yt-dlp for the metadata, and for the track only when
  youtube-transcript-api failed; the other platforms ask yt-dlp. Each tier runs at most once per
  request, and after a YouTube 429 yt-dlp is not asked at all. The track: manual tracks in the
  order of `languages`, automatic captions in that order, a manual track in the video's original
  language, any manual track, automatic captions in the original language. Languages compare by
  primary subtag after normalisation (ISO 639-2 `deu` is `de`; YouTube's named keys such as
  `de-XwLwiJMB_Xs` count as `de`); of YouTube's automatic captions only `<lang>-orig` counts
  (every other automatic language is a machine translation); arte's `-forced` tracks (only the
  foreign-language passages) and `und` never count. When both libraries fail on YouTube without
  a refusal and give no metadata, the watch page's `ytInitialPlayerResponse` (fast fetcher, same
  turn) gives the details. When yt-dlp fails on Vimeo with an error it does not name, the player
  page's `og:title` and `og:description` do; the player's 403, 404 or 410 (a removed, private or
  domain-restricted video, which yt-dlp words as a TLS fingerprint block) answers
  `no_transcript_available`, not retryable, as does yt-dlp's "embed-only video". Of the chosen track's formats (best first: VTT, json3, TTML, SRT), the
  first whose body has a cue with text is taken; a format that fails, is too large or has no
  text gives way to the next, but a refusal of the egress proxy ends the track.
- **M3 and M4, children and network** (`media/pool.py`, `media/child.py`). youtube-transcript-api
  and yt-dlp run in `DocumentPool` children (`MEDIA_CONCURRENCY` 2, `MEDIA_MEMORY_LIMIT_MB` 1024)
  in API pods, extract workers and the browser worker (not the Docling worker), started before
  `/ready` and killed at the run's deadline (`timeout`, retryable). The parent never imports
  them. Their only way out is the pod's own `EgressProxy` (the package-4 SSRF proxy): each call
  gets a `ProxyContext` with its own credentials and `MEDIA_EXTRACT_MAX_BYTES` (30 MB), so every
  connection, redirect and player sub-request is address-checked and byte-capped; yt-dlp's
  request handlers take http and https only, so no `ftp:` or `file:` URL leaves past the proxy.
  yt-dlp runs with one allowed extractor, no playlist, no download, no cache, no cookies, no
  retries, no JavaScript runtime and no remote components. Page requests carry our honest user
  agent; no impersonation (`curl_cffi`). A track is at most `MEDIA_SUBTITLE_MAX_BYTES` (5 MB);
  arte and Vimeo deliver subtitles as HLS, whose VTT segments the child concatenates within that
  cap. yt-dlp is installed without its `default` extra (it pulls in GPL `mutagen`).
- **M5, compliance and outbound.** The canonical URL passes robots.txt, TDMRep and ai.txt like a
  page, and the run holds the host's outbound turn while the children work (as a render does).
  The libraries' own requests (player APIs, caption files) are the player's sub-requests:
  SSRF-checked and byte-capped by the proxy, not robots-checked or gated. A media file fetched by
  URL gets the header checks of a document (F10).
- **M6, the result.** `data.media` (`MediaInfo`): `platform` (M1's, `file`, `upload` or
  `html5`), `transcript_source` (`manual_subtitles`, `auto_subtitles`, `asr`, `metadata_only`;
  null where there is no transcript at all: a page with embeds only, a file not transcribed),
  `transcript_language`, `available_languages` (the manual tracks), `duration_seconds`,
  `uploader`, `asr_model` and `embeds`; null for every result without media. `content` is the
  transcript as paragraphs (a new one after a pause of 2 s, after 6 sentences, or after 120
  words of unpunctuated captions) under `# <title>`, with `## <chapter>` headings where the
  platform has chapters; everything a speaker says is escaped, so it cannot become markup.
  `metadata_only` content is the title and the description. `content_type` is `text/vtt` for a
  subtitle result, `text/html` for `metadata_only`, the media type for a transcription;
  `mode_used` is `fast` (`upload` for uploads). The description, uploader, date, keywords and
  licence go to `metadata` (licence source `platform`, confidence `high`; YouTube's "Creative
  Commons Attribution license" is `CC-BY-3.0`). Quality is judged like `text/plain`. Only a
  probe that succeeded and found no usable track says the video "has no subtitles" (not
  retryable, cached, served from the cache with the same error); a refusal (D1) or any other
  failure of the probe (children, network, track, a limit) says what failed, mostly with the
  warning `subtitles_failed`, is retryable where it may pass, and is neither compared nor cached.
  A proxy refusal (a private address) is `failed`/`blocked_url`. Warnings: `subtitles_blocked`,
  `subtitles_failed`, `asr_unavailable`, `no_speech`, `tracks_truncated`, `tracks_failed`.
- **M7, captions** (`media/captions.py`, `media/transcript.py`). WebVTT (cue settings, `NOTE`,
  `STYLE`, `REGION`, voice and class spans), SRT, TTML (defusedxml, any namespace prefix,
  absolute times) and YouTube's `json3` become cues; tags and entities go, a byte order mark and
  a non-UTF-8 charset are handled, and the rolling duplicates of automatic captions are dropped.
  A track that is not what its format says is skipped to the next tier.
- **M8, HTML5 tracks and embeds** (`media/html5.py`). After an HTML page's conversion (fast or
  rendered; not on a challenge, error page or login wall), each `<video>` or `<audio>` with a
  `<track>` of kind `subtitles`, `captions` or none gives one track (by `languages`, the page's
  language, `default`, the first), up to `MEDIA_MAX_TRACKS` (3; more: `tracks_truncated`). Each
  is fetched by the fast fetcher in a turn of its host, SSRF-checked and gated, without
  compliance files (a sub-resource of a page that passed); a redirect to another host, a track
  over `MEDIA_SUBTITLE_MAX_BYTES` or a turn that does not come within the run's deadline skips it
  (`tracks_failed` only when every one failed). The text follows the page's as `## Transcript`
  (with the track's label). The page's cache key and fingerprint variant gain `languages` when
  the request sets them. A page with neither tracks nor embeds keeps `media: null` and its
  package-5 answer.
- **M9, `transcribe` and the ASR dispatch** (`asr_dispatch.py`). `POST /v1/extract`,
  `POST /v1/batch` and jobs take `transcribe` (default false) and `languages` (at most 5 ISO 639
  codes such as `de` or `en-GB`). `transcribe: true` while the service has no ASR (`ASR_ENABLED`
  and `JOBS_ENABLED`) is 400 `invalid_request`, and so is a request that needs a transcription on
  the external backend with neither a caller key nor `ASR_EXTERNAL_API_KEY`. Without
  `wait_seconds`, `transcribe: true` sets it to 20, so a video with subtitles still answers 200.
  When a transcription is needed (no subtitles, a media file, an upload), a one-item job (kind
  `extract` or `file`) goes to pool `asr` with the details result as its draft and the
  `MediaInputMeta` (plus an upload's bytes) as its input (`ASR_INPUT_TTL_SECONDS`, in the shared
  input budget), and the route answers 202 at once: nothing waits for a transcription, so the API
  pods' grace period is unchanged. Back-pressure as F12: the memory guard high,
  `ASR_MAX_OPEN_PER_CLIENT` (3) unfinished ASR jobs, or the input budget full answer the draft
  with `asr_unavailable`, unstored (without a draft `failed`/`rate_limited`, retryable). A media
  file without `transcribe` is `failed`/`no_transcript_available` ("Send transcribe: true to
  transcribe it."). Batches: the extract worker moves an item that needs ASR to pool `asr`
  (input, draft, queue, completion), as F13. The result cache key of a platform video gains
  `media` (so results cached before the deploy are not served) and `languages`; a cached
  `metadata_only` answer is no hit for `transcribe: true`, except a final one: with `no_speech`,
  or with `asr_unavailable` (stored only after a transcription that failed for a reason of the
  media's own). With
  `transcribe: true`, a video without a usable track that the platform says is longer than
  `MEDIA_MAX_DURATION_SECONDS` answers `media_too_long` at once with its details (final, stored),
  without a job; a media file's duration is known only after its download, so the ASR worker
  checks it. The fingerprint variant of a platform or file result is `<format>|media`, so the
  details-only draft and a later transcript of a video share it and have different
  fingerprints (the first transcript reports `changed: true`); a transcribed media file's result
  is cached under the plain URL's key (`transcribe` is not part of it) and served to later
  requests without `transcribe` too.
- **M10, the ASR worker** (`asr_worker.py`, `media/asr_child.py`, `media/asr_windows.py`).
  `text-extraction-asr-worker`, pool `asr`, internal port only: the package-3 `Worker` with an
  `AsrRunner`, `ASR_CONCURRENCY` (1) slots, each with a child (`ASR_MEMORY_LIMIT_MB` 4096) that
  loads the model once. Leases are `2 × ASR_TIMEOUT_SECONDS + 10` s (about 6 h), and
  `ASR_TIMEOUT_SECONDS` is the budget of the whole item from the claim. Per item: an upload's
  bytes are written to a directory of its own under `ASR_SCRATCH_PATH`; a platform video is
  `platform_info` (yt-dlp's info, format `bestaudio/best[height<=360]` over plain HTTP(S),
  native HLS or native DASH only) in the host's outbound turn, then its `download` outside the
  turn; a media file URL is one download in its host's turn; each through the worker's
  `EgressProxy`, capped at `MEDIA_MAX_BYTES` (500 MB) within `MEDIA_DOWNLOAD_TIMEOUT_SECONDS`
  (600). The duration (the platform's, yt-dlp's, PyAV's probe, and the decoded audio itself) over
  `MEDIA_MAX_DURATION_SECONDS` is `media_too_long` before any transcription. PyAV (files only,
  `protocol_whitelist=file`) decodes the first audio stream to 16 kHz mono float32, and
  faster-whisper (`device="cpu"`, `ASR_COMPUTE_TYPE` int8, `ASR_THREADS` 4, `vad_filter=True`,
  `condition_on_previous_text=False`, the first requested language or the detected one) reads
  that array in windows of at most 10 minutes cut in pauses: faster-whisper computes the
  features of a whole call at once (3.4 GiB for an hour), and a cut in a pause needs no overlap
  or dedupe. `Pipeline.transcribed_result` builds the result (the draft's details,
  `transcript_source: asr`, `asr_model`) and, for a URL, compares and caches it under the
  canonical URL, so the next request is a hit; an upload's is never stored. Media without speech
  answers the draft (final, warning `no_speech`) or fails `no_transcript_available`. The scratch
  directory is deleted in a `finally`. Failures: the item's budget spent is `timeout`, not
  retryable; a download that ran out of its own time is `timeout`, retryable; a decoder error
  `transcription_failed`; a missing model `internal_error`, retryable. With a draft the draft
  answers with the failure's error and `asr_unavailable`, and keeps its fingerprint only when the
  failure is final, which is when it belongs to the media (too long, too large, not media,
  undecodable, the item's own budget, out of memory, a refused address, the site's own error
  answer): then it is cached; served from the cache it drops `asr_unavailable` and says the video
  could not be transcribed, without the failure's code. A failure of the caller or of the
  endpoint (a refused or missing key, another 4xx of the endpoint) is never final, retryable or
  not: the result cache is shared by every caller, so nothing is stored and the next
  `transcribe: true` is handed on again. An input that expired before
  the claim answers the draft with `asr_unavailable` (an upload: `timeout`, retryable).
- **M11, external ASR** (`media/asr_external.py`). `ASR_BACKEND=openai`: the child decodes as
  above, encodes chunks of at most 20 minutes (cut in pauses) as 16 kHz mono Opus at 32 kbit/s
  in Ogg (about 6 MB each) and posts them to `ASR_EXTERNAL_BASE_URL` `/audio/transcriptions`
  (OpenAI SDK, `ASR_EXTERNAL_MODEL` `whisper-1`, `verbose_json`, falling back to `json`),
  directly, not through the egress proxy (operator configuration, https only), without
  redirects or SDK retries. Key order (CLAUDE.md rule 3): the caller's `X-ASR-Api-Key` (on
  `POST /v1/extract`, `/v1/extract/file` and `/v1/batch`; stored as the job's secret, deleted
  with the item's outcome or the batch's last result, never in a job view or a log) wins over
  `ASR_EXTERNAL_API_KEY` (a secret, also in the API pods, only to know whether there is one).
  401/403 is `transcription_failed`, not retryable; 429, 5xx, a chunk's timeout
  (`ASR_EXTERNAL_TIMEOUT_SECONDS`, 300, for its whole request) or an unreachable endpoint
  retryable. No models are needed.
- **M12, switches.** `MEDIA_ENABLED` (default true: platform URLs, media files, tracks, embeds).
  `ASR_ENABLED` (default false): Helm renders it into every pod from `workers.asr.enabled`;
  compose has the profile `asr` with `ASR_ENABLED=true` in `.env`.
- **M13 and M17, models and volumes** (`model_store.py`, `media/models.toml`). `--download-models`
  fills `ASR_MODELS_PATH/<model>` with only the configured `ASR_MODEL` from Hugging Face at a
  pinned commit per file, size- and SHA-256-checked, staged and renamed, with a
  `.complete-<manifest hash>` marker. No model or downloaded media in an image, on the root
  filesystem or on a shared data volume: the ASR workers have their own models PVC (`asr-models`,
  4Gi: `large-v3-turbo` plus a staged copy), mounted read-only, filled by an init container
  (`models.downloadMode: init`, the default) or a post-install/post-upgrade hook Job (`job`;
  install without `helm --wait`, as the workers wait for the models); `download: false` with
  `existingClaim` for an air-gapped volume. Downloaded media live on a per-pod emptyDir with a
  `sizeLimit` (2Gi; the render fails unless it holds `ASR_CONCURRENCY` × (`MEDIA_MAX_BYTES` +
  5 MB)). Compose: the one-shot `asr-models` on the volume `asr-models`, the worker's scratch on
  the volume `asr-scratch`.
- **M14, uploads** (`file_api.py`). A part whose bytes are audio or video is transcribed, no form
  field needed: up to `MEDIA_UPLOAD_MAX_BYTES` as the item's input, a job of kind `file` on pool
  `asr`, 202 at once (the form's `wait_seconds` does not apply). The path's body limit becomes the
  larger of `DOCUMENT_MAX_BYTES` and `MEDIA_UPLOAD_MAX_BYTES`, plus 64 KiB, while media and ASR are
  on; a document over `DOCUMENT_MAX_BYTES` still answers `too_large`. Without ASR an audio upload
  answers `failed`/`unsupported_content_type` ("Transcription is not enabled on this service.").
- **M15, metrics and alerts.** `text_extraction_media_extractions_total{platform, source,
  outcome}` and `text_extraction_media_seconds{platform}` (API pods and workers);
  `text_extraction_asr_dispatch_total{outcome}` (API pods, extract workers);
  `text_extraction_asr_transcriptions_total{backend, outcome}`,
  `text_extraction_asr_audio_seconds_total{backend}` (the external backend's cost basis) and
  `text_extraction_asr_seconds{backend}` (ASR workers); the queue gauges for pool `asr`. The
  media children's proxy refusals count in `text_extraction_browser_proxy_refused_total` (the
  same `EgressProxy`). Helm alerts: `TextExtractionQueueBacklog` for pool `asr`
  (`alerts.asrQueueBacklogItems`, 50) and `TextExtractionWorkersDown` for the ASR workers.
- **M16, yt-dlp versions.** The 14-day rule holds (yt-dlp 2026.8.19, youtube-transcript-api
  1.2.4). A pinned yt-dlp that a platform broke is replaced by the newest stable release in its
  own small PR (CONTRIBUTING.md), since the platforms change their players without notice.
- **M18, spike findings** (Task 1, from Jan's line). YouTube answered subtitles for every row
  that has them, with and without Deno; Vimeo needs a login for `vimeo.com/<id>` but not for the
  player URL, and an embed-restricted video (player 403) can only answer its details; the
  extractor names are `IE_NAME`s; faster-whisper 1.2.1's own `decode_audio` breaks on PyAV 19,
  so the child always decodes itself and passes an array.

**Scaling and shutdown.** A transcription runs for minutes to hours, and its lease lasts
`2 × ASR_TIMEOUT_SECONDS + 10` s (about 6 h with D6). On SIGTERM the worker stops claiming,
gives the items in flight 30 s, then cancels them and kills its children; a cancelled item goes
back to the front of its domain at once (`attempts` + 1), not when its lease expires, and runs
again from the start on another worker (an item given back `JOB_ITEM_MAX_ATTEMPTS` times fails as
lost). Only a worker killed without SIGTERM (an OOM kill, a node failure) leaves its item to the
lease, so that item waits up to about 6 h. The Helm chart scales the ASR workers on pending plus
leased items (`keda.countLeased`, a replica per `concurrency`) with a scale-down stabilisation of
3600 s, the Kubernetes maximum; Kubernetes still picks the pod to remove without knowing which is
idle, so a scale-down, a rolling update or a node drain interrupts transcriptions in flight.
Prefer fixed replicas for steady load.

**Images and processes.** The API image gets yt-dlp and youtube-transcript-api (MIT, Unlicense),
no faster-whisper, CTranslate2, PyAV, Deno or models (image.yml checks it), and its parent
processes never import yt-dlp or youtube-transcript-api. The ASR image (target `asr`, image
`text-extraction-asr`, linux/amd64, about 1.4 GB uncompressed) holds the venv with the extra `asr`
(faster-whisper 1.2.1, CTranslate2 4.8.2, PyAV 19.0.1 built against FFmpeg, `yt-dlp[deno]` with
`yt-dlp-ejs` for YouTube's player challenges, the OpenAI SDK), the FFmpeg shared libraries and
their licence (`/opt/ffmpeg`, registered with `ldconfig`), no `ffmpeg`, `ffprobe` or `rtmpdump`
program and no models; non-root, tini. The LGPL build (D5) is enforced four times: the build
checks the FFmpeg archive's SHA-256 and fails unless `ffmpeg_gpl_findings` over PyAV's libraries
and codecs is empty, the `libopus` encoder exists and no such program is on `PATH`; every ASR
child runs the same check in the worker's self-check before `/ready` (no `--enable-gpl` or
`--enable-nonfree`, no GPL or non-free external library or encoder such as libx264, every
library's licence LGPL, neither `ffmpeg` nor `rtmpdump` on `PATH`, which yt-dlp could hand
protocols it does not download itself), so a worker on the PyPI wheel never becomes ready;
image.yml runs `--check` in the built image; and the licence gate covers the extra `asr` in the
CI job `media` (metadata only, which cannot see inside the wheel). Before `/ready` the worker
also checks the model's files and transcribes the built-in clip (`media/asr_check.ogg`) in each
child (the external backend encodes it as Opus instead); a failure exits 1 naming
`ASR_MODELS_PATH`.

**Done when (package 6):** `tests/media/test_corpus.py` sends the caption fixtures
(`tests/media/captions`: YouTube's json3 and rolling automatic captions, ARD's TTML, ZDF's and a
lecture's WebVTT, a Latin-1 SRT) through `POST /v1/extract` of the public app as the tracks of
YouTube, ARD, ZDF, Vimeo and arte videos (a fake media fetcher, as the sandbox reaches no
platform) and gets each transcript in order without tags, entities or repeated lines; an empty
track answers the details, and a page's `<track>` is appended as `## Transcript`. Under the
marker `asr` (CI job `media`, with the `small` model; weekly also `large-v3-turbo`) the German
clip `tests/media/corpus/speech_de.ogg` is uploaded to `POST /v1/extract/file`, answered 202,
transcribed by the ASR worker's runner on the real children and model, and read back from
`GET /v1/jobs/{id}` with a word recall of at least 0.8. Live (from Jan's line, compose profile
`asr`): every YouTube and Mediathek row of `tests/live/urls/media.csv` that has subtitles answers
`manual_subtitles` or `auto_subtitles`, and an upload is transcribed; the live workflow reports
the media set without gating it (D1).

**Known limits.**

- **YouTube from data-centre IPs (D1).** From a cluster or a CI runner YouTube may refuse most
  requests (bot check, PO token, 429): the answer is then the video's details with
  `subtitles_blocked`, and `transcribe: true` may fail too, as the ASR worker downloads from the
  same IPs. There is no proxy, no PO token and no browser fallback.
- **Interrupted transcriptions.** A scale-down, rolling update or node drain restarts the
  transcriptions in flight elsewhere from the beginning; with the external backend the chunks
  already sent are billed again, and so is every retry of a retryable failure. A worker killed
  without SIGTERM leaves its item to its lease of about 6 h.
- **Lengths and sizes.** Media over 2 h (`media_too_long`), files over `MEDIA_MAX_BYTES` and
  uploads over `MEDIA_UPLOAD_MAX_BYTES` (`too_large`; larger media by URL) are not transcribed. At
  RTF 0.88 a 2 h video takes about 1 h 45 min of one worker slot. An audio upload larger than
  `DOCUMENT_MAX_BYTES` while ASR is off is a 413 (the path limit widens only with ASR).
- **What the ASR child needs in memory** (`ASR_MEMORY_LIMIT_MB`, 4096, an address-space limit)
  is first measured in CI's `media` job (the long clip prints VmPeak and VmHWM) and in the live
  run; package 11 sets the final resources.
- **Cached final failures.** A transcription that failed for a reason of the media's own
  (`media_too_long`, undecodable media, the item's budget) leaves the video's details cached (a
  refused key does not); a later request without
  `transcribe` is served "could not be transcribed", without the failure's own code, and so is a
  later `transcribe: true`, which does not try again until the entry expires.
- **Model switch.** Switching `ASR_MODEL` downloads the new model beside the old one, which stays
  on the volume until removed by hand.
- **Batch keys.** A batch's `X-ASR-Api-Key` lives as long as the job (`JOB_TTL_SECONDS`); a batch
  that runs longer loses it, and its later items fail `transcription_failed`.
- **Tracks.** A `<track>` that redirects to another host (a CDN) is skipped. An `auto` request
  whose browser worker did not answer in time (`browser_unavailable`) gets the fast result
  without its tracks and embeds.
- **Platforms.** Vimeo videos restricted to their uploader's domains, private or removed answer
  `failed` without details (the player refuses them, `vimeo.com` shows a bot check, oEmbed 404s);
  arte videos whose only German track is `-forced` answer another language's manual track (with
  its `transcript_language`), or their details when there is none; Mediathek items expire.
  Tier 1 runs without a JavaScript runtime in the API image (Deno is in the ASR image only).
- Not in this package: transcripts of players embedded in a page (D4), speaker diarisation,
  translation, timestamps in the content, HLS or DASH URLs outside the platform extractors, S3
  offload of inputs (package 11), PII in transcripts (package 7 applies to them like to any
  content).

## Observability

Prometheus via prometheus-fastapi-instrumentator plus: queue length per pool, job
duration, extraction outcome by error code and mode, browser pool usage (pages in use, pages by
outcome, render seconds, launches, proxy refusals), sync browser dispatches by outcome (API pods:
rendered, not claimed, no result, handed over, backend error), document conversions by converter,
kind and outcome with their seconds and pages, sync Docling dispatches by outcome, media
extractions by platform, transcript source and outcome with their seconds, ASR dispatches by
outcome, transcriptions by backend and outcome with their seconds and audio seconds, LLM calls,
tokens and failures, rate-limit rejections. Alert rules ship in the Helm chart
(queue filling, error-rate spike, workers down). structlog JSON logs; query strings
are never logged.

## Lessons from the previous services

| Topic | Source | Taken over |
| --- | --- | --- |
| Auto mode: < 500 visible chars + JS → browser; bot challenges qualify | Selenium service 3.2 | Yes |
| YouTube text from embedded page data; MathML → LaTeX; linked files kept | Selenium service | Yes (YouTube: subtitles first; the watch page's `ytInitialPlayerResponse` is the last tier-2 fallback, M2) |
| Domain boilerplate lists, cookie auto-consent, soft-404 detection | Playwright service (kidra) | Yes (opt-out by default, own rules for GSB and MediaWiki dialogs) |
| WLO URL sets (25 / 200 / 4,000) and live result buckets | Playwright service | Regression harness |
| Baseline 2026-09-28 on 4,000 URLs: fast 68 %, browser 80 %; medians 0.5 s / 9-10 s | Playwright service | Bar to beat: the live gate is `wlo_200` in `auto` mode at 80 % (D2) |
| diskcache state on local disk | Selenium service | Replaced by Valkey |
