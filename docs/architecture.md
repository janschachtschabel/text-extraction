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
API pods ──▶ LLM endpoint (OpenAI-compatible, default model gpt-6-luna)
API pods ──jobs──▶ Valkey (queues, results, cache, rate limits, fingerprints)
Valkey ◀──▶ extract-worker  (fast pipeline: batch items from the fair queue)
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
   `COMPLIANCE_ALLOW_OVERRIDE`. Every result carries a `compliance` block. The origin files are
   cached in Valkey; files that are missing are fetched one after another inside the outbound
   turn of the page that needs them, before the page (R22), so they count against the host's
   limits like any request. The files and the page of one turn go back to back (R25).
3. **Fetch (fast):** httpx, HTTP/2, byte cap, conditional requests (ETag, Last-Modified).
   Per-domain outbound limits in Valkey (parallelism, delay, `Crawl-delay`, backoff on
   429/503, circuit breaker). A turn has at most one request in flight, so the parallelism
   limit holds for every request a host sees, with one exception: a compliance file that
   redirects to another host goes out there without a lease or spacing at the target, at most
   3 times per origin per pod per cache period. The delay lies between the real starts of
   requests of different turns, back to back within a turn (R25); a request that was sent late
   delays the other turns' next request instead of letting it follow closer (R21).
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

Request bodies of the public API are limited before they are read whole, parsed or
authenticated (`src/text_extraction/body_limit.py`): over the limit of its path a body answers
413 `invalid_request`, at once for a `Content-Length` over it and otherwise as soon as the bytes
read pass it. The limit is `REQUEST_MAX_BODY_BYTES` (64 KiB), and the batch bound for
`POST /v1/batch`; a later upload route brings its own bound.

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
| `POST /v1/batch` | Many URLs (`urls`, at most `BATCH_MAX_URLS`; a body over `BATCH_MAX_URLS` × 2056 bytes plus 4 KiB is 413 before it is parsed; options `mode`, `format`, `metadata`, `links`, `compliance`, `cache` as for `/v1/extract`), fair per-domain scheduling. 202 with the job; 429 at `BATCH_MAX_OPEN_PER_CLIENT` unfinished batches | job |
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
- **Batch:** one sub-queue per client and domain, round-robin over clients, then over the
  client's domains with free capacity; see "Jobs and the fair queue".
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

**The fair queue.** One queue per worker pool (`extract` now; browser, docling, nlp and asr pools
reuse it), all keys under one hash tag `q:{<pool>}:…`; no script touches a job key and a queue
key. Items are `<job>:<index>:<attempts>` in lists per client and domain.

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
job results, stored a second time by the result cache. So each API pod reads `INFO memory` every
5 s (`memory_guard.py`, its own task, also with `JOBS_ENABLED=false`), and from
`VALKEY_MEMORY_GUARD_RATIO` (0.8) of `maxmemory` on it takes on no new bulk: `POST /v1/batch`
answers 503 `internal_error` (retryable), the sync-to-job hand-over does not happen (the request
waits inline and answers 200), and the API pod's result cache skips its writes. Workers still
store the results of accepted items, so jobs that were accepted complete, and their own result
cache writes go on (they have no guard). Without `maxmemory` there is no guard; a failing read keeps
the last verdict. The gauges `text_extraction_valkey_used_memory_bytes` and `…_max_memory_bytes`
feed the alert `TextExtractionValkeyMemoryHigh`. The guard bounds new work, not the worst case:
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
