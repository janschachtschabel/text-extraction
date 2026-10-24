# Architecture

Status: planning closed on 2026-10-23. This file is the source of truth for the build;
update it in the same PR as any change to the design.

## Overview

Stateless API pods handle fast-mode requests inline and hand slow work to worker pools
through Valkey.

```
Clients / AI agents ──(8080: REST /v1, /mcp, /ui)──▶ API pods (FastAPI, stateless)
Prometheus ──(9090: /health /ready /metrics)──────▶ API pods
API pods ──fetch (SSRF guard)──▶ target websites and files
API pods ──▶ LLM endpoint (OpenAI-compatible, default model gpt-6-luna)
API pods ──jobs──▶ Valkey (queues, results, cache, rate limits, fingerprints)
Valkey ◀──▶ browser-worker  (Playwright Chromium: auto fallback, actions, screenshots)
Valkey ◀──▶ docling-worker  (OCR, scanned and table-heavy PDFs, images)
Valkey ◀──▶ nlp-worker      (MarkItDown, Presidio)
Valkey ◀──▶ asr-worker      (faster-whisper transcription)
workers ──▶ model volume (PVC, read-only: spaCy, Docling, Whisper models)
optional: S3-compatible store for screenshots and large results
```

Images: `api` (slim), `browser-worker` (Chromium), `docling-worker`, `nlp-worker`,
`asr-worker`. Each worker is a Helm toggle and scales on queue length (KEDA or HPA).

## Extraction pipeline

1. **Validate and guard:** scheme, SSRF check (private, loopback, link-local and metadata
   IPs refused; re-checked after each redirect and for browser sub-requests), size and time limits.
2. **Compliance:** robots.txt, TDMRep (`/.well-known/tdmrep.json`, `tdm-reservation` header
   and meta), robots meta / `X-Robots-Tag`, ai.txt / llms.txt. Mode `enforce|report|ignore`
   per request; operator default and override permission via `COMPLIANCE_DEFAULT`,
   `COMPLIANCE_ALLOW_OVERRIDE`. Every result carries a `compliance` block.
3. **Fetch (fast):** httpx, HTTP/2, byte cap, conditional requests (ETag, Last-Modified).
   Per-domain outbound limits in Valkey (parallelism, delay, `Crawl-delay`, backoff on
   429/503, circuit breaker).
4. **Quality gate (auto mode):** fewer than 500 visible characters while the page runs
   JavaScript, a bot challenge, or a soft-404 → browser worker.
   `quality.py` reports `js_heavy`, `bot_challenge`, `soft_404` and `login_wall`; a bot
   challenge, soft-404, login wall or empty content ends `failed` (`bot_protection`,
   `likely_error_page`, `login_required`, `empty_content`), fewer than `QUALITY_MIN_WORDS`
   words ends `partial` (`content_too_short`). `quality.browser_recommended` is reported in
   both modes; only `auto` will act on it (escalation arrives with the browser package), which
   is why `mode` is already part of the result cache key.
5. **Convert:** HTML → Trafilatura (keep link-only lists of linked files, domain
   boilerplate rules, MathML → LaTeX). Office/EPUB and PDFs with a good text layer →
   MarkItDown. Scans, table-heavy PDFs, images → Docling. Vision LLM opt-in.
   Video/audio → see below.
6. **Post-process (optional):** DOM metadata, PII, links, structured output, chunking,
   screenshot.
7. **Fingerprint and classify:** see change detection; return the result envelope.

## Result envelope and errors

Fields: `status` (`success|partial|failed`), `error.code`, `error.message`,
`error.retryable`, `upstream_status`, `mode_used` (`fast|browser`), `attempts`,
`final_url`, `content_type`, `language`, `word_count`, `quality`, `timings`,
`compliance`, `metadata`, `fingerprint`.

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

`POST /v1/extract` (fast mode, `src/text_extraction/pipeline.py`) puts these fields into `data`,
also on failure, as far as the request got. `partial` keeps the content and names
`content_too_short`; a failed quality verdict (`bot_protection`, `likely_error_page`,
`login_required`, `empty_content`) keeps it too, a compliance block or a target error has none.
`data.cache` is `miss`, `hit` (fresh stored result, no fetch), `revalidated` (stale stored
result confirmed by a 304 or by identical bytes) or `bypass` (`cache: false`: no read, no write).
Only `success` and `partial` results are cached and fingerprinted (per URL and output format). `attempts` counts fetches of
the target in this request (0 for a cache hit or a refusal before the fetch). The outbound gate
refusing a turn gives `rate_limited` (busy) or `connection_error` (host paused), both retryable.

## Public API (port 8080)

| Endpoint | Purpose | Mode |
| --- | --- | --- |
| `POST /v1/extract` | URL → text/Markdown/HTML; options `mode`, `format`, `actions`, `pii`, `links`, `metadata`, `screenshot`, `chunking`, `compliance` | sync; with `wait_seconds` (0-60, default none) a slower extraction becomes a job: 202 with the job (P1) |
| `POST /v1/extract/file` | Upload (multipart) → text/Markdown | sync, job for large files |
| `POST /v1/screenshot` | Full page or viewport PNG, 1920×1080 | sync, job fallback |
| `POST /v1/structured` | Strategies `metadata`, `selectors`, `patterns`, `llm`, `auto` | sync, job fallback |
| `POST /v1/chunk` | Text/Markdown/URL → chunks (`fixed`, `recursive`, `markdown`, `sentence`, `semantic`) | sync |
| `POST /v1/site-structure` | Sitemaps + typed link graph to a set depth | job |
| `POST /v1/batch` | Many URLs (`urls`, at most `BATCH_MAX_URLS`; a body over `BATCH_MAX_URLS` × 2056 bytes plus 4 KiB is 422 before validation; options `mode`, `format`, `metadata`, `links`, `compliance`, `cache` as for `/v1/extract`), fair per-domain scheduling. 202 with the job; 429 at `BATCH_MAX_OPEN_PER_CLIENT` unfinished batches | job |
| `GET /v1/jobs/{id}` | Status, progress, paged results (`offset`, `limit` 1-500). Own jobs only (admins: all); unknown, expired and foreign ids are 404. Envelope `success` while running or when every item succeeded, `partial` when a finished job has failed or partial items, `failed` when every item failed (then `error` carries the most frequent item error code among the first 500 items, "All N items failed.", retryable if any of them is). 503 when jobs are disabled (`JOBS_ENABLED=false`) or Valkey is down | — |
| `GET /v1/jobs/{id}/artifacts/{name}` | Screenshot or large result | — |
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
  actions, stealth browser, higher limits, effective config, key usage stats.

`INTERNAL_API_ENABLED` (off by default) serves the API without auth for cluster-internal callers.

## Feature notes

- **Browser actions:** `click`, `scroll`, `wait_for`, `type`, `press`, `select`,
  `expand_all`, presets `infinite_scroll`, `accept_cookies`; `cookies` and `headers`
  for logged-in pages; max 20 actions; `evaluate` admin-only. Stealth (Patchright) opt-in, admin-only.
- **DOM metadata:** extruct (JSON-LD, microdata, RDFa, OpenGraph) + Trafilatura. License
  normalized (e.g. `CC-BY-SA-4.0`) with source and confidence; author, dates, canonical,
  language, LRMI educational fields, images with alt text.
- **Audio/video:** tier 1 subtitles (youtube-transcript-api, yt-dlp, HTML5 `<track>`),
  tier 2 metadata, tier 3 ASR (faster-whisper local, external OpenAI-compatible
  transcription switchable), opt-in `transcribe: true`, always a job.
  `transcript_source`: `manual_subtitles|auto_subtitles|asr|metadata_only`.
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
- **Batch:** one sub-queue per domain, round-robin over domains with free capacity;
  fair shares between clients.
- **Sync-to-job (P1):** `POST /v1/extract` without `wait_seconds` always answers when the
  extraction ends (at most `EXTRACT_TIMEOUT_SECONDS`, 25 s). With `wait_seconds` the extraction
  runs as a task in the API pod and the route waits that long. If it is still running, the route
  creates an `extract` job with one URL (state `running`, lease of `EXTRACT_TIMEOUT_SECONDS` + 10 s)
  and answers 202; the same task goes on and a done-callback stores its envelope as item 0.
  The job only stores the result: nothing is routed through a worker, which would add a hop to
  every request and make workers mandatory (package 4 sends browser work to its own pool).
  A pod that dies mid-run leaves the lease to expire, and `GET /v1/jobs/{id}` then shows a job
  that is still `running` past its `lease_until` as completed, with item 0 `failed` /
  `internal_error`, retryable ("The pod running this extraction stopped."); this view is
  computed, not stored, and a completed job never gets it. If Valkey fails when the job is
  created, the request keeps waiting and answers 200 with the envelope, not 503; if it fails when
  the result is stored, the lease expires and the lost view takes over. The shutdown waits up to
  `EXTRACT_TIMEOUT_SECONDS` + 5 s for such tasks. Extract jobs do not count toward
  `BATCH_MAX_OPEN_PER_CLIENT`. `JOBS_ENABLED=false` ignores `wait_seconds`.
- **Storage:** Valkey (`VALKEY_URL`, any Redis-protocol server) with TTLs; `S3_ENABLED`
  moves screenshots and large results to an S3-compatible store.

## Observability

Prometheus via prometheus-fastapi-instrumentator plus: queue length per pool, job
duration, extraction outcome by error code and mode, browser pool usage, LLM calls,
tokens and failures, rate-limit rejections. Alert rules ship in the Helm chart
(queue filling, error-rate spike, workers down). structlog JSON logs; query strings
are never logged.

## Lessons from the previous services

| Topic | Source | Taken over |
| --- | --- | --- |
| Auto mode: < 500 visible chars + JS → browser; bot challenges qualify | Selenium service 3.2 | Yes |
| YouTube text from embedded page data; MathML → LaTeX; linked files kept | Selenium service | Yes |
| Domain boilerplate lists, cookie auto-consent, soft-404 detection | Playwright service (kidra) | Yes |
| WLO URL sets (25 / 200 / 4,000) and live result buckets | Playwright service | Regression harness |
| Baseline 2026-09-28 on 4,000 URLs: fast 68 %, browser 80 %; medians 0.5 s / 9-10 s | Playwright service | Bar to beat |
| diskcache state on local disk | Selenium service | Replaced by Valkey |
