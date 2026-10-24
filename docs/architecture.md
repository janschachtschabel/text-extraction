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
Valkey ◀──▶ browser-worker  (Chromium behind an egress proxy: auto fallback, actions,
                             screenshots; Patchright + Chrome for unblock mode)
Valkey ◀──▶ docling-worker  (OCR, scanned and table-heavy PDFs, images)
Valkey ◀──▶ nlp-worker      (MarkItDown, Presidio)
Valkey ◀──▶ asr-worker      (faster-whisper transcription)
workers ──▶ model volume (PVC, read-only: spaCy, Docling, Whisper models)
optional: S3-compatible store for screenshots and large results
```

Images: `api` (slim), `browser-worker` (image `text-extraction-browser`: Chromium headless
shell, Google Chrome stable, Xvfb), `docling-worker`, `nlp-worker`, `asr-worker`. Each worker is a
Helm toggle and scales on queue length (KEDA or HPA). The API image holds no browser and no
Playwright or Patchright; the details are in "Browser worker" below.

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
`final_url`, `content_type`, `language`, `word_count`, `quality`, `timings` (`render_ms` is the
time in the browser, 0 in fast mode), `compliance`, `metadata`, `fingerprint`, and for browser
results `unblocked` (unblock mode rendered the page) and `screenshot` (the artifact reference).
For a browser result `attempts` counts the main-frame document responses of the render, redirect
hops included.

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
| `POST /v1/extract` | URL → text/Markdown/HTML; options `mode` (`fast`, `auto`, `browser`), `format`, `actions`, `cookies`, `headers`, `pii`, `links`, `metadata`, `screenshot`, `full_page`, `chunking`, `compliance`. The browser options (`mode: browser`, `actions`, `cookies`, `headers`, `screenshot`) answer 400 `invalid_request` while `BROWSER_ENABLED` is false, and `mode: fast` refuses them (422 `invalid_request`) | sync; with `wait_seconds` (0-60, default none) a slower extraction becomes a job: 202 with the job (P1) |
| `POST /v1/extract/file` | Upload (multipart) → text/Markdown | sync, job for large files |
| `POST /v1/screenshot` | Full page (default) or first-viewport PNG, 1920 px wide; options `full_page`, `actions`, `cookies`, `headers`, `compliance`, `wait_seconds`. The answer is the extraction result without the text, with `screenshot` pointing at the artifact | sync (`wait_seconds`: 202 with a job of kind `screenshot`) |
| `POST /v1/structured` | Strategies `metadata`, `selectors`, `patterns`, `llm`, `auto` | sync, job fallback |
| `POST /v1/chunk` | Text/Markdown/URL → chunks (`fixed`, `recursive`, `markdown`, `sentence`, `semantic`) | sync |
| `POST /v1/site-structure` | Sitemaps + typed link graph to a set depth | job |
| `POST /v1/batch` | Many URLs (`urls`, at most `BATCH_MAX_URLS`; a body over `BATCH_MAX_URLS` × 2056 bytes plus 4 KiB is 413 before it is parsed; options `mode`, `format`, `metadata`, `links`, `compliance`, `cache` as for `/v1/extract`), fair per-domain scheduling. 202 with the job; 429 at `BATCH_MAX_OPEN_PER_CLIENT` unfinished batches | job |
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
  refuse, fails the whole render with that page's error. A click that starts a download
  discards the page: the render ends without content, as a download is `unsupported_content_type`
  until package 5 routes files. Details in "Browser worker".
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
  every request and make workers mandatory. Browser work is the exception (package 4): a sync
  request that needs a browser puts a one-item job on the `browser` pool and waits for the
  browser worker (B1, B13).
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
  browser, before any setting is tuned.
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
     download or non-HTML type is `unsupported_content_type`.
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
  fingerprint reaches the site. Launch flags: `--proxy-server` with a bypass list that sends even
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
  TTL (`2 × BROWSER_TIMEOUT_SECONDS + BROWSER_QUEUE_WAIT_SECONDS + 60` s), never appear in
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
    and no extra headers. Patchright is pinned to the newest release at least 14 days old
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
content). The first request of an HTTP redirect hop to another host leaves before its check
(D1). Screenshots need `JOBS_ENABLED=true`. The first browser launch of a worker happens before
`/ready`, and a browser that does not start ends the process. HTML served without a
`Content-Type` does not escalate (step 4).

## Observability

Prometheus via prometheus-fastapi-instrumentator plus: queue length per pool, job
duration, extraction outcome by error code and mode, browser pool usage (pages in use, pages by
outcome, render seconds, launches, proxy refusals), LLM calls,
tokens and failures, rate-limit rejections. Alert rules ship in the Helm chart
(queue filling, error-rate spike, workers down). structlog JSON logs; query strings
are never logged.

## Lessons from the previous services

| Topic | Source | Taken over |
| --- | --- | --- |
| Auto mode: < 500 visible chars + JS → browser; bot challenges qualify | Selenium service 3.2 | Yes |
| YouTube text from embedded page data; MathML → LaTeX; linked files kept | Selenium service | Yes |
| Domain boilerplate lists, cookie auto-consent, soft-404 detection | Playwright service (kidra) | Yes (opt-out by default, own rules for GSB and MediaWiki dialogs) |
| WLO URL sets (25 / 200 / 4,000) and live result buckets | Playwright service | Regression harness |
| Baseline 2026-09-28 on 4,000 URLs: fast 68 %, browser 80 %; medians 0.5 s / 9-10 s | Playwright service | Bar to beat: the live gate is `wlo_200` in `auto` mode at 80 % (D2) |
| diskcache state on local disk | Selenium service | Replaced by Valkey |
