# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version is the same in
`pyproject.toml`, in `deploy/helm/text-extraction/Chart.yaml` (`version` and `appVersion`) and
in the image tags; a release is the Git tag `vX.Y.Z` on `main`.

## [Unreleased]

## [0.1.2] - 2026-11-01

Dependency updates that v0.1.1 left out. Jan waived the 14-day rule for new dependency versions
(CONTRIBUTING.md) for these on 2026-11-01.

### Changed

- fastmcp 4.1.2 (was 4.1.0): fixes advisories in code paths the MCP endpoint does not use.
- Pillow 13.0.0 (was 12.3.0), with the Docling releases that allow it: docling-slim 2.142.1 (was
  2.137.0), docling-core 2.104.0, docling-parse 7.23.0 and docling-ibm-models 4.0.4. Pillow 13
  fixes the TIFF out-of-bounds reads of 12.3.0; the image check of uploads already opened only
  the formats it sniffs.

### Fixed

- CHANGELOG: the entry for `docker-compose.release.yml` in 0.1.1 named the default version
  `0.1.0`; the default is the version of the release the file comes with.

## [0.1.1] - 2026-11-01

Fixes from the audit of v0.1.0 (plans in
[docs/superpowers/plans/2026-11-01-api-audit-docs-release.md](docs/superpowers/plans/2026-11-01-api-audit-docs-release.md)
and
[docs/superpowers/plans/2026-11-01-remaining-audit-findings.md](docs/superpowers/plans/2026-11-01-remaining-audit-findings.md)).

### Added

- `tests/e2e/audit_probes.py`: hostile probes against a running service, run by hand before a
  release (a redirect loop, a 30 MB body, a gzip bomb, a slow drip, binary and mis-declared
  pages; with `--ssrf`, private addresses in their various spellings and forbidden schemes).
- `docker-compose.release.yml`: runs the stack of `docker-compose.yml` with the published
  images of a release (`TEXT_EXTRACTION_VERSION`, default: that release's version) instead of
  building them.
- The `release` workflow: a pushed tag `vX.Y.Z`, or a manual run for an existing tag, puts the
  tag's section of this file on its GitHub release page, with the images and where to start
  (`scripts/release_notes.py`); a `0.x` release is marked as a pre-release.
- The OpenAPI document has request examples for `POST /v1/extract`, `/v1/extract/file`,
  `/v1/screenshot` and `/v1/batch`, and the description of `ErrorCode` explains every code.
- The OpenAPI document declares the bearer key (`AUTH_MODE=api_key`), the error envelope of
  every error answer (instead of FastAPI's `HTTPValidationError`), the error statuses each
  operation can answer, and a tag per area.

### Changed

- `/ready` no longer fails while Valkey is down: the pods stay in the Service and answer 503
  `internal_error` (retryable, with the trace id) instead of dropping out behind the ingress's
  bare 503. The new gauge `text_extraction_valkey_up` and the Helm alert
  `TextExtractionValkeyDown` (critical, 2 minutes) report the outage.
- MCP tool answers are bounded as a whole by `MCP_MAX_ANSWER_CHARS` (new, 200,000 characters):
  a `get_job` page drops its trailing items, then a result's `chunks`, `links`,
  `structured.tables` and `site.links` are cut from the end, with the warning
  `mcp_answer_truncated`. Before, only `content` was cut and an answer could reach megabytes.
  The budget counts the envelope JSON, which an answer carries twice; with MCP on, the service
  refuses to start when it is not larger than `MCP_MAX_CONTENT_CHARS`.
- JSON request bodies are read strictly: a value of the wrong JSON type (`"5"` for a number,
  `"yes"` for a boolean) answers 422. The form fields of `POST /v1/extract/file` are unchanged.
- A JSON route answers 415 `invalid_request` for a body sent with another `Content-Type`,
  instead of a 422 about a missing body. A body without a `Content-Type` is read as JSON, as
  before.
- The service's own pause of a host (after errors or a 429) answers `rate_limited` instead of
  `connection_error`, still retryable; a refusal of the outbound gate carries the wait in
  `error.details` (`retry_after_seconds`), in the extract and the ASR workers alike.
- Under `AUTH_MODE=none` the public port answers only requests whose `Host` is in
  `ALLOWED_HOSTS` (new; `localhost,127.0.0.1,[::1],api` by default, `*` switches the check off),
  so a web page cannot reach the service through DNS rebinding. Add the host name your clients use.
  An empty list under `AUTH_MODE=none` stops the start (exit code 2); the Helm notes name the
  setting.
- One counted call does about one unit of work: MCP `initialize` and `tools/list` count one
  rate-limit hit each, a waiting `get_job` one more per further 10 s, the UI's image previews
  one per image and its site tree one per 500 items; the UI reads a screenshot's file without a
  second hit.
- `JOBS_ENABLED=false` makes the job routes answer 400 `invalid_request` instead of 503
  `internal_error`, like every other switch that is off. A retryable 503 carries
  `Retry-After: 5`.
- OpenAPI operation ids are plain names (`extract`, `extract_file`, `get_job`, `delete_job`,
  ...) instead of FastAPI's path-derived ones (`extract_v1_extract_post`); a client generated
  from the document gets new method names.
- A job item that failed through a fault of the service before any step ran (a lost lease, an
  expired input) had `data: null`; it has `data` now, with `mode_used` and `upstream_status`
  null, `attempts` 0, no content and zero timings, so every item carries the envelope's fields.
  `mode_used` may therefore be null.
- A host of digits that is no IPv4 literal (`0177.0.0.1`, `127.1`, `2130706433`) is
  `invalid_url` now. `0177.0.0.1` failed its robots.txt as `blocked_by_robots` (retryable),
  since the HTTP client refuses it; no such URL was ever fetched.
- A gzip bomb in browser mode kept Chromium decompressing until the render timed out (`timeout`,
  retryable, after about 27 s): the egress proxy counts the bytes on the wire. The renderer now
  counts the decoded bytes too and ends the render with `too_large` once they pass twice
  `BROWSER_MAX_BYTES`. A request with the caller's `headers` was decoded whole in the browser
  driver's memory first; the service fetches it itself now, through the page's proxy, and
  counts as it reads.
- A page or upload declared UTF-8 whose bytes are not (a Latin-1 page behind a default UTF-8
  header) came out with every umlaut as U+FFFD; it is read as windows-1252 (or the legacy
  encoding its bytes fit) now, with the new warning `charset_mismatch`.
- A body declared `text/html` that starts with a document's or image's signature (a PDF behind
  a `download.php`) answered `partial` with a page of U+FFFD characters; it is converted as that
  document now, as a declared `text/plain` one is. A body declared `text/html` that is binary
  data (more than 2 % of its first KiB are bytes no text has; UTF-16 and UTF-32 text excepted)
  is `unsupported_content_type`, with a message that says so.

### Fixed

- A batch item moving to the Docling or ASR workers whose job was deleted between storing its
  bytes and its draft kept the bytes in Valkey and in `DOCLING_INPUT_BUDGET_BYTES` until their
  TTL; the move now gives them back at once. An enqueue whose reply is lost keeps them, since
  the item may be queued.
- Valkey: the client retried a command up to 10 times on a connection error, so a Lua script, a
  MULTI or a job claim lost in flight could run twice. Commands are no longer retried; instead a
  pooled connection idle for more than 5 s is checked with PING and opened again once.
- B3 trace headers: a job keeps them with its trace id and its workers log them; without
  `x-client-trace-id` the B3 trace id is the `trace_id`; the LLM and the external ASR endpoint
  receive `x-client-trace-id` and the B3 headers. CLAUDE.md no longer names OpenTelemetry, which
  the service does not use.
- `POST /v1/screenshot` and a browser-mode `POST /v1/extract` with an invalid URL started a job
  (202); they fail at once with `invalid_url`, as fast mode does, with `mode_used` null.
- A host name without a DNS form (a label over 63 characters, an empty label) answered 500;
  it is `invalid_url` now.
- Under `compliance: enforce`, a robots.txt that could not be fetched because of the host (DNS,
  connection, timeout, a blocked address) answered `blocked_by_robots`, not retryable; the page
  now fails with that error and its own retryable flag.
- More than five stacked `Content-Encoding`s answered 500; they are `unsupported_content_type`.
- Tables nested more than 16 deep held a conversion thread for half a minute; deeper tables are
  flattened before Trafilatura.
- The image check of uploads opens only the formats it sniffs (PNG, JPEG, TIFF, WebP, BMP), so
  Pillow's other decoders are not reachable.
- MCP refused request bodies over the SDK's 4 MiB with a plain-text 413, below the advertised
  upload limit; it takes bodies up to its own limit now.
- A page of job results read up to 500 stored items whole (about 1 GB); it stops at 20 MB and
  `next_offset` continues from there.
- A full Valkey (`noeviction`) made the rate limit answer 503 to every route, `DELETE` of a job
  included; the limit lets calls through uncounted then and logs `rate_limit_backend_full`.
- `partial` job items counted as failures in `TextExtractionJobItemFailures`.
- A worker pool scaled to zero by KEDA with `countLeased: false` never took back the items of
  expired leases; at `minReplicaCount: 0` the scaler counts leased items too.
- The 429 for too many unfinished batches and site jobs carries `Retry-After: 60`.
- `.env.example` lacked `LLM_GRACEFUL_SECONDS`, `UI_DEFAULT_LANGUAGE` and `UI_DOCS_BASE_URL`; a
  test pins it to the settings now.
- Documentation: an XPath selector of a user answers 422, not 403; the extraction metrics, the
  kinds of `text_extraction_job_duration_seconds` and every pool of `escalated`; the bounds of
  twelve settings; that `JOB_TTL_SECONDS` is not renewed while a job runs; the DNS-rebinding risk
  of `AUTH_MODE=none`.
- User guide and Helm README: the local uv recipe gives each process `AUTH_MODE=none` and its own
  `INTERNAL_PORT`; the quick start runs `docker compose up` detached; `auto` with a schema needs
  an LLM key once the service has an LLM; the 400s of `describe_images`, `ocr: always` and the
  browser options while their switch is off; the grace period of the API pods with browser
  workers; and that a floating `0.1` tag needs a `pull`.

### Not in this release

- fastmcp 4.1.2 and Pillow 13 (with docling-slim 2.142): they came in v0.1.2.

## [0.1.0] - 2026-11-01

The first release, a prototype: packages 0 to 13 of [docs/packages.md](docs/packages.md), tagged
`v0.1.0` on the merge of the package 13 pull request; packages 11 and 12 are merged without a
tag.

### Added

- Package 0, repo foundation: a uv project on Python 3.13, ruff, mypy, pytest, pre-commit and
  the GitHub Actions workflows for lint and tests.
- Package 1, service skeleton: settings from environment variables, the public port 8080 and
  the internal port 9090 (`/health`, `/ready`, `/metrics`), JSON logs with `x-client-trace-id`
  and B3 headers, the result envelope with its error codes, API keys with user and admin roles,
  an inbound rate limit in Valkey, a multi-stage non-root Dockerfile, SemVer image tags, a
  compose stack and the base Helm chart with its toggles.
- Package 2, fast extraction: an HTTP fetcher with an SSRF guard and limits, robots.txt and EU
  TDM checks, per-domain outbound limits, the Trafilatura pipeline with its quality gate, DOM
  metadata and licences, fingerprints, the result cache and `POST /v1/extract`.
- Package 3, jobs and batch: a fair job queue on Valkey with leases, the job API (create,
  status, paged results), the fallback from a sync request to a job, `POST /v1/batch` with fair
  per-domain scheduling, queue metrics, and KEDA, HPA and alert rules in the Helm chart.
- Package 4, browser worker: a Playwright pool, escalation from fast to browser mode in `auto`,
  `actions`, cookie-consent handling, challenge detection, full-page screenshots and
  `POST /v1/screenshot`.
- Package 5, documents and OCR: `POST /v1/extract/file`, MarkItDown, a Docling worker with PDF
  routing and a model volume filled by an init container.
- Package 6, audio and video: subtitles through youtube-transcript-api and yt-dlp, HTML5 tracks,
  a faster-whisper ASR worker and a switch for an external ASR backend.
- Package 7, PII: Presidio and spaCy for German and English with the modes `mask`, `redact`,
  `hash` and `annotate`, and the spaCy models from a volume.
- Package 8, structured output and chunking: the strategies `metadata`, `selectors`, `patterns`,
  `llm` and `auto`, learned selectors, an OpenAI-compatible LLM client that prefers the key of the
  request header, `POST /v1/structured`, `POST /v1/chunk` and five chunkers.
- Package 9, site structure: sitemap discovery through robots.txt, indexes and gzip, a link graph
  with link types to a set depth, and `POST /v1/site-structure` as a growing job.
- Package 10, MCP and UI: a FastMCP server at `/mcp` with 11 tools and the htmx playground at
  `/ui`, both off by default and switchable in Helm.
- Package 11, hand-over: a load test (`tests/load/`, run by hand in the `load.yml` GitHub Actions
  workflow against a local site farm) with the resource baselines from `docker stats`, the Helm
  sizing and an example production values file, the environment reference, the cost notes for the
  LLM and ASR, the hand-over document, Dependabot, this changelog and a `mode` input for the
  `live.yml` regression.
- Package 12, UI languages, unblock option and bilingual docs: maximum extraction, which falls
  back to the browser on a plain 403, 429 or 503 for an admin with `compliance: ignore` and
  `BROWSER_UNBLOCK_ENABLED`; the playground in German and English with a language switch and no
  cookie, a "Site rules" choice and a "Browser fallback" select, help texts and status messages,
  and checks for WCAG 2.2 A and AA with a vendored axe-core 4.14.0 and keyboard tests; and the
  READMEs and a user guide in German and English, kept in sync by tests and a pull request
  check.
- Package 13, YouTube transcript panel: a YouTube video whose subtitles could not be read gets a
  second try in the browser in `auto` mode, where YouTube serves its new transcript view
  (`transcript_source: transcript_panel`, `mode_used: browser`); otherwise its details answer
  with the warning `transcript_panel_unavailable`. The standard browser only, no request of its
  own to YouTube; the switch `MEDIA_TRANSCRIPT_PANEL_ENABLED` (default true, needs
  `BROWSER_ENABLED`) and the metric `text_extraction_media_transcript_panel_total`.

### Not in this release

- A full 4,000-URL regression run against the WLO set. The `live` workflow takes a `mode` input
  for it, and the run is started by hand later.
- A decision on permanent operation. This release is a prototype, and Jan Schachtschabel decides.
