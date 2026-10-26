# Implementation packages

Each package is one branch and one pull request. Order matters: a package starts only
when the one it builds on is merged.

Per package:

1. Plan with the Superpowers `writing-plans` skill (`docs/superpowers/plans/`).
2. Implement with `subagent-driven-development` (or `executing-plans`) and `test-driven-development`.
3. Review with `requesting-code-review`; fix findings via `receiving-code-review` and `systematic-debugging`.
4. `verification-before-completion`: tests, ruff and mypy green and the "done when"
   criterion below met; then `finishing-a-development-branch` opens and merges the PR.

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
| 11 | Hand-over | Full WLO regression run, load test, resource baselines (`docker stats`), env reference, example `values.yaml`, secrets list, cost notes (LLM, ASR), final architecture doc | Ops-guide hand-over checklist complete |

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
