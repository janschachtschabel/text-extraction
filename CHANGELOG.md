# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version is the same in
`pyproject.toml`, in `deploy/helm/text-extraction/Chart.yaml` (`version` and `appVersion`) and
in the image tags; a release is the Git tag `vX.Y.Z` on `main`.

## [Unreleased]

Fixes from the audit of v0.1.0 (plans in
[docs/superpowers/plans/2026-11-01-api-audit-docs-release.md](docs/superpowers/plans/2026-11-01-api-audit-docs-release.md)
and
[docs/superpowers/plans/2026-11-01-remaining-audit-findings.md](docs/superpowers/plans/2026-11-01-remaining-audit-findings.md)).

### Added

- `docker-compose.release.yml`: runs the stack of `docker-compose.yml` with the published
  images of a release (`TEXT_EXTRACTION_VERSION`, default `0.1.0`) instead of building them.
- The `release` workflow: a pushed tag `vX.Y.Z`, or a manual run for an existing tag, puts the
  tag's section of this file on its GitHub release page, with the images and where to start
  (`scripts/release_notes.py`); a `0.x` release is marked as a pre-release.
- The OpenAPI document has request examples for `POST /v1/extract`, `/v1/extract/file`,
  `/v1/screenshot` and `/v1/batch`, and the description of `ErrorCode` explains every code.
- The OpenAPI document declares the bearer key (`AUTH_MODE=api_key`), the error envelope of
  every error answer (instead of FastAPI's `HTTPValidationError`), the error statuses each
  operation can answer, and a tag per area.

### Changed

- MCP tool answers are bounded as a whole by `MCP_MAX_ANSWER_CHARS` (new, 200,000 characters):
  a `get_job` page drops its trailing items, then a result's `chunks`, `links`,
  `structured.tables` and `site.links` are cut from the end, with the warning
  `mcp_answer_truncated`. Before, only `content` was cut and an answer could reach megabytes.
- JSON request bodies are read strictly: a value of the wrong JSON type (`"5"` for a number,
  `"yes"` for a boolean) answers 422. The form fields of `POST /v1/extract/file` are unchanged.
- A JSON route answers 415 `invalid_request` for a body sent with another `Content-Type`,
  instead of a 422 about a missing body.
- The service's own pause of a host (after errors or a 429) answers `rate_limited` instead of
  `connection_error`, still retryable; a refusal of the outbound gate carries the wait in
  `error.details` (`retry_after_seconds`).
- Under `AUTH_MODE=none` the public port answers only requests whose `Host` is in
  `ALLOWED_HOSTS` (new; `localhost,127.0.0.1,[::1],api` by default, `*` switches the check off),
  so a web page cannot reach the service through DNS rebinding. Add the host name your clients use.
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

### Fixed

- `POST /v1/screenshot` and a browser-mode `POST /v1/extract` with an invalid URL started a job
  (202); they fail at once with `invalid_url`, as fast mode does.
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
