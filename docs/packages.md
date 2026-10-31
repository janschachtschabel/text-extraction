# Implementation packages

Each package is one branch and one pull request. Order matters: a package starts only
when the one it builds on is merged.

Per package:

1. Plan with the Superpowers `writing-plans` skill (`docs/superpowers/plans/`).
2. Implement with `subagent-driven-development` (or `executing-plans`) and `test-driven-development`.
3. Review with `requesting-code-review`; fix findings via `receiving-code-review` and `systematic-debugging`.
4. `verification-before-completion`: tests, ruff and mypy green and the "done when"
   criterion below met; then `finishing-a-development-branch` opens and merges the PR.

## Status

| # | Package | State | Pull request | Done-when evidence |
| --- | --- | --- | --- | --- |
| 0 | Repo foundation | merged 2026-10-23 | #3 | CI green |
| 1 | Service skeleton | merged 2026-10-23 | #4 | compose stub endpoint, `helm lint` |
| 2 | Fast extraction | merged 2026-10-24 | #5, #6 | `wlo_200` fast 78.5 % (baseline 68 %) |
| 3 | Jobs and batch | merged 2026-10-24 | #7 | 1,000-URL batch across 2 workers (`batch` job in CI) |
| 4 | Browser worker | merged 2026-10-25 | #8 | `wlo_200` auto 83.0 % (baseline 80 %) |
| 5 | Documents and OCR | merged 2026-10-25 | #9, #10 | corpus converts, scans yield text (CI `documents`); `wlo_200` auto 84.0 % in an A/B run against package 4 (83.0 %) |
| 6 | Audio and video | merged 2026-10-26 | #12 | subtitles from YouTube and Mediathek samples, an upload transcribed (CI `asr`, live run on Jan's PC) |
| 7 | PII | merged 2026-10-29 | #13 | corpus test `tests/pii/test_corpus.py` (CI job `nlp`: `md` on every push, `lg` weekly): every mode through `POST /v1/extract/file` as Markdown and HTML with the real nlp worker loop; pattern types found 100 %, `PERSON` recall pinned per model size (`lg` German 0.96, English 1.00; `md` 1.00 and 1.00); no keep word touched except the listed known false positives (`Pythagoras`, `Calvin`: historical names used as concepts); live `wlo_200` auto 83.0 % unchanged; Docker check with `lg` on Jan's PC (2026-10-29) |
| 8 | Structured output and chunking | merged 2026-10-30 | #18 | done-when test `tests/structured/test_done_when.py` (default suite, no real LLM): on a German teaching page, `metadata`, `selectors` (CSS for a user key, XPath for an admin key), `patterns`, `llm`, `auto` (the LLM asked only for the missing field), `tables`, `learn_selectors` and its reuse without an LLM call (`sources: learned`, per key), every chunker, `POST /v1/structured`, `POST /v1/chunk` (URL and text) and a batch, through the real llm worker loop against a mock OpenAI-compatible endpoint (pytest-httpserver: `/chat/completions`, `/embeddings`); live `wlo_200` auto 82.5 % |
| 9 | Site structure | merged 2026-10-30 | #23 | done-when test `tests/site/test_done_when.py` (default suite, no network): two sample sites (`tests/site/sites/`) on pytest-httpserver, crawled through `POST /v1/site-structure` by the real extract worker loop and read back through `GET /v1/jobs/{id}` in pages of 50: every entry of robots.txt's sitemaps (an index with a plain and a gzipped urlset, a text sitemap) and of a guessed `/sitemap.xml` of 300 entries across parts, every page to depth 2 as one item, typed links (`region`, `source`, `crawl`) with targets at the right items, a robots.txt-disallowed page never requested, the 404 the only failure; live site run on five WLO-type sites reported by the live workflow (first run 2026-10-30, `enforce`, depth 1, 25 pages: all five jobs completed in 11-47 s; de.serlo.org 13,531 sitemap entries, www.leifiphysik.de answered 2 HTTP errors); live `wlo_200` auto 84.0 % |
| 10 | MCP and UI | merged 2026-10-31 | #24 | done-when tests (default suite, no network) against the app under uvicorn with the real extract worker loop and a sample site on pytest-httpserver: `tests/mcp_server/test_done_when.py` calls all 11 tools through `fastmcp.Client` over streamable HTTP (the transport Claude Code uses; a wrong or missing key cannot connect, 401) and `tests/ui/test_done_when.py` drives every entry of `OPERATIONS` and the views `progress`, `site-tree`, `batch-file`, `batch-urls` and `images` through `/ui/run`, proven from the REST calls the views forward, with each call's REST status and envelope checked (in its fragment for the single operations and the job item, at the forward for the job views, whose fragments show the jobs, the image or the "cancelled" line); five Chromium tests (`pytest -m browser tests/ui`); Helm toggles in `deploy/helm/test.sh`; live check 2026-10-31 in the build sandbox with Claude Code 2.1.314 (`claude mcp add`, `claude mcp list` connected, all MCP steps through `claude -p`) and the playground in Chromium against a local Valkey, API and worker (see the package 10 note); the WLO pages and Claude Desktop move to Jan's PC (no egress to WLO in the sandbox); live `wlo_200` auto 82.5 % (165 of 200; package 9: 84.0 %), smoke 83.3 %, browser 79.0 %, media 46.4 %, and all five site jobs completed (2026-10-31) |
| 11 | | open | | |

| # | Package | Contents | Done when |
| --- | --- | --- | --- |
| 0 | Repo foundation | uv project (Python 3.13), ruff, mypy, pytest, pre-commit, GitHub Actions (lint, test), `.dockerignore`, commands in `CLAUDE.md` | CI green on an empty app |
| 1 | Service skeleton | pydantic-settings from env, public port 8080 and internal 9090 (`/health`, `/ready`, `/metrics`), structlog JSON logs with `x-client-trace-id` and B3, result envelope and error codes, API keys (user, admin), inbound rate limit in Valkey, OpenAPI display name and description, Dockerfile (multi-stage, non-root), image build in CI with SemVer tags (`latest` only for staging), docker-compose with Valkey, base Helm chart with toggles | `docker compose up` serves an authenticated stub endpoint; `helm lint` passes |
| 2 | Fast extraction | httpx fetcher with SSRF guard and limits, robots.txt and TDM checks, per-domain outbound limits, Trafilatura pipeline (link-only lists, boilerplate rules, MathML), quality gate, DOM metadata and licenses, fingerprints and result cache, `POST /v1/extract`, smoke regression set (`tests/live`) | `wlo_200` at or above the 68 % fast baseline; smoke set reported |
| 3 | Jobs and batch | fair job queue on Valkey (own scheduler with leases), job API (create, status, paged results), sync-to-job fallback, `POST /v1/batch` with fair per-domain scheduling, queue metrics, KEDA/HPA and alert rules in Helm | 1,000-URL batch completes across 2 workers with no domain over its limit |
| 4 | Browser worker | Playwright pool, auto escalation, `actions`, cookie consent, challenge detection, full-page screenshots at 1920×1080, `POST /v1/screenshot`, browser image, optional Patchright | `wlo_200` in auto mode at or above the 80 % browser baseline; smoke set reported |
| 5 | Documents and OCR | `POST /v1/extract/file`, MarkItDown, Docling worker with PDF routing, model PVC with init container | PDF, Office and image corpus converts; scans yield text |
| 6 | Audio and video | youtube-transcript-api, yt-dlp subtitles and metadata, HTML5 tracks, faster-whisper ASR worker, external ASR switch | YouTube and Mediathek samples return subtitles; an upload is transcribed |
| 7 | PII | Presidio de/en with `mask`, `redact`, `hash`, `annotate`; spaCy models from PVC | German and English test texts handled per mode |
| 8 | Structured output and chunking | Strategies `metadata`, `selectors`, `patterns`, `llm`, `auto`; learned selectors; LLM client with key order (header, then env); `POST /v1/structured`, `POST /v1/chunk` | Each strategy tested; LLM path tested against a mock endpoint |
| 9 | Site structure | Sitemap discovery (robots.txt, index, gzip), link graph with link types to a set depth, `POST /v1/site-structure` job | Sample sites return complete sitemaps and typed links |
| 10 | MCP and UI | FastMCP at `/mcp`, htmx playground at `/ui`, both toggled by env and Helm | MCP tools usable from Claude; UI drives every endpoint |
| 11 | Hand-over | Full WLO regression run, load test (it also sizes the default `STRUCTURED_SELECTOR_PROCESSES`, 1 today, issue #19), resource baselines (`docker stats`), env reference, example `values.yaml`, secrets list, cost notes (LLM, ASR), final architecture doc | Ops-guide hand-over checklist complete |

Package 5 note (Jan, D2): a sync request that needs OCR waits up to `DOCLING_SYNC_WAIT_SECONDS`
(20 s) for the Docling worker and then answers its text layer with the warning `ocr_unavailable`
(an image fails `timeout`, retryable); with `wait_seconds` it becomes a 202 job.
`POST /v1/extract/file` defaults to `wait_seconds: 25`, so a slower upload answers 202 with its
job. The sync contract of `POST /v1/extract` stays; a sync request that needs OCR can take up to
`EXTRACT_TIMEOUT_SECONDS` + `DOCLING_SYNC_WAIT_SECONDS` (25 + 20 = 45 s by default), more than the
~30 s of the job rule, because it waits for the worker only when the document turns out to need
OCR. Details in [architecture.md](architecture.md), "Documents and OCR".

Package 6 note (Jan, D1): YouTube blocks many data-centre IPs and asks for a proof-of-origin token
on a growing share of caption tracks. The service answers such a refusal honestly: the video's
details (`transcript_source: metadata_only`, `partial`/`no_transcript_available`) with the warning
`subtitles_blocked`, and a 429 goes to the outbound gate's backoff; no proxy, no PO-token plugin,
no browser fallback. The done-when check therefore runs from Jan's line (`tests/live/urls/media.csv`
with the compose profile `asr`); the live workflow reports the media set from GitHub's runners
without gating it. Transcription (`transcribe: true`, uploads) is always a job, and ASR is off by
default (`ASR_ENABLED`). Details in [architecture.md](architecture.md), "Audio and video".

Package 7 note (Jan, D1-D4): PII runs in the nlp worker, as the last hop of every result that asked
for it (`pii: {"mode": "mask|redact|hash|annotate"}` on `POST /v1/extract`, `/v1/extract/file` and
`/v1/batch`), with Presidio and the spaCy models `de_core_news_<size>` and `en_core_web_<size>`
(`lg` by default, `md` switchable) from a PVC; the API image and the other workers have neither.
By default persons and contact or payment data are found (`PERSON`, `EMAIL_ADDRESS`,
`PHONE_NUMBER`, `IBAN_CODE`, `CREDIT_CARD`, `IP_ADDRESS`); places, organisations and URLs only when
a request asks. `hash` is a keyed HMAC (`PII_HASH_KEY`, else a random key per result). A sync
request with `pii` waits `PII_SYNC_WAIT_SECONDS` (20 s) and then answers 202 with its job. A result
that asked for PII is never answered unprocessed: it is `failed`/`pii_failed` without text. Not in
this package: PII in screenshots, `/v1/structured` and `/v1/chunk` (package 8), other languages,
custom lists. The done-when evidence runs in CI (`nlp` job) and locally with
`PII_TEST_MODEL_SIZE=lg uv run pytest -m nlp tests/pii/test_corpus.py`. Details in
[architecture.md](architecture.md), "PII".

Package 8 note (Jan, D1-D5): `structured` (strategies `metadata`, `selectors`, `patterns`, `llm`,
`auto`; `tables`; `learn_selectors`) and `chunking` (`fixed`, `recursive`, `markdown`, `sentence`,
`semantic`) on `POST /v1/extract`, `/v1/extract/file`, `/v1/batch` and the new routes
`POST /v1/structured` and `POST /v1/chunk` (a URL or a text). The LLM is an OpenAI-compatible
endpoint of the operator (`LLM_BASE_URL`, default model `gpt-6-luna`), off by default
(`LLM_ENABLED`, Helm `workers.llm.enabled`, compose profile `llm`); its calls run only in the llm
worker (API image, no model), as the last hop after PII, so the LLM only sees masked content, and a
sync request that needs it waits `LLM_SYNC_WAIT_SECONDS` (30 s), then answers 202 with its job. The
caller's `X-LLM-Api-Key` wins over `LLM_API_KEY`. `semantic` embeds with the same endpoint's
`/embeddings` (`LLM_EMBEDDING_MODEL`). Learned selectors are kept per API key and host, 30 days.
When the LLM step cannot run the answer keeps its content and is `partial`/`llm_failed`. Not in
this package: vision-LLM image descriptions, MCP tools, a model per request, tokenisers. The
done-when evidence is `uv run pytest tests/structured/test_done_when.py`; the compose check against
a local mock endpoint and a real `gpt-6-luna` call (`text-extraction-llm-worker --check`) were not
run in the build sandbox (no Docker daemon, no endpoint key). Details in
[architecture.md](architecture.md), "Structured output and chunking".

Package 9 note (Jan, D1-D6): `POST /v1/site-structure` maps a site as a growing job of kind
`site` on the extract pool: every page, robots.txt and sitemap file is an item, and the items an
item finds are appended to the running job (`total` grows; results paged by `GET /v1/jobs/{id}`).
Pages are fetched in fast mode only (no browser), with the compliance checks of an extraction for
every fetch, sitemaps included, within the start page's host (a leading `www.` ignored; `scope:
path` for one directory). Page items carry their typed links (`kind`, `source`, `rel`, `hreflang`,
`region`, `crawl`, `target`) and no content; sitemap items their entries, split into
`sitemap_part` items when large. A crawl counts against `BATCH_MAX_OPEN_PER_CLIENT` with the
batches. Upgrade the extract workers, then the API pods, and send site jobs only after that:
older pods cannot parse a site job (their workers re-lease its items, their API answers 500 to
its reads).
The done-when evidence is `uv run pytest tests/site/test_done_when.py`; the live site run
(`tests/live/run_site.py`) could not run in the build sandbox (no egress to the sites) and is a
report-only step of the live workflow. Details in [architecture.md](architecture.md), "Site
structure".

Package 10 note (Jan, D1-D8): FastMCP at `/mcp` and the htmx playground at `/ui`, both on the
public port 8080, off by default (`MCP_ENABLED`, `UI_ENABLED`; Helm `mcp.enabled`, `ui.enabled`;
compose switches both on). Both run every check of the REST route (auth, roles, rate limit, SSRF
guard, compliance, body limits, envelope). The done-when evidence is
`uv run pytest tests/mcp_server/test_done_when.py tests/ui/test_done_when.py` and
`uv run pytest -m browser tests/ui`.

Live check (2026-10-31, build sandbox; Valkey as `redis-server` on 127.0.0.1:6390, the API
`text-extraction` with `AUTH_MODE=api_key`, `MCP_ENABLED=true`, `UI_ENABLED=true`,
`FETCH_ALLOW_PRIVATE_NETWORKS=true` on 8080/9090 and one `text-extraction-worker` with
`INTERNAL_PORT=9091`; Docker is not available in the sandbox, and the sandbox has no egress to
WLO: `https://wirlernenonline.de/` answered `blocked_by_robots` because its robots.txt could not be
resolved, `dns_error`, so the pages came from a local sample site on 127.0.0.1:8765):

- `claude mcp add --transport http text-extraction http://localhost:8080/mcp --header
  "Authorization: Bearer $KEY"` (Claude Code 2.1.314, a config directory of its own): added;
  `claude mcp list`: "text-extraction: http://localhost:8080/mcp (HTTP) - Connected" (0.8 s).
- `claude -p` with only `mcp__text-extraction` allowed: `service_info` answered principal `live`,
  role `user` (5.2 s in all). A second run (10 turns, 22.7 s) listed the 11 tools and called, in
  the server's log (`mcp_tool_call`): `extract_url` of the start page, `success` (136 ms; title and
  59 words); `batch_extract` of three pages, `job` (7 ms), then `get_job` with `wait_seconds` 20,
  `completed` (1.0 s; each item `success`, `run.pool` `extract`); `site_structure` at depth 1,
  `job` (6 ms), then `get_job`, `completed` (6.0 s; 6 items: 4 pages, robots.txt, sitemap.xml);
  `batch_extract` of one page, `cancel_job` `success` (4 ms, `job_deleted`), and `get_job` on it
  `not_found` (2 ms). All 12 `/mcp` requests 200 for principal `live`; no error line; the key in
  no log.
- The playground in Chromium (Playwright, headless): login; the start page as Text (0.15 s) and as
  Markdown with structured metadata (0.18 s; `license` from `rel-license`), image preview loaded
  through the service; site at depth 1 (3.5 s, 6 items), tree with 4 pages, two selected and
  extracted (3.4 s, 2/2 `success`); a CSV of ten URLs previewed and started (3.5 s, 10/10
  `success`). No cookie, no console message. Screenshots of the tree and the progress view go into
  the PR.

Open on Jan's PC: the same steps against WLO pages and a WLO-type site with `docker compose up`,
and Claude Desktop through `mcp-remote`.

Package 11 load-test items from package 9 (final review): the event-loop time of packing a large
sitemap into parts and encoding the part results at `SITE_MAX_SITEMAP_URLS` (200,000 entries,
about 20 MB of JSON, done on the event loop after the parse), with the ~3 s lease margin left
after a sitemap item that got its slot late; the duration of a crawl at `SITE_MAX_PAGES` against
`JOB_TTL_SECONDS` (nothing renews the TTL mid-crawl); the Valkey size per open crawl (worst case
at default caps: 1,000 page items of up to `JOB_ITEM_MAX_BYTES` each); the extract worker's memory
with several 50 MiB sitemaps in flight; the split of `site/runner.py` into `runner.py` (dispatch,
pages, extend, metrics) and `sitemap_items.py` (robots, sitemaps, parsing), a pure move; and,
if the measurements call for it, a setting `SITE_PARSE_CONCURRENCY` for the sitemap thread pool
(one per pod today).
