# Implementation packages

Each package is one branch and one pull request. Order matters: a package starts only
when the one it builds on is merged.

Per package:

1. Implement with the **better-coding-workflow** skill.
2. Run the **better-coding-review** skill on the package.
3. Fix the findings with the better-coding-workflow skill.
4. Tests, ruff and mypy green; the "done when" criterion below is met; open the PR.

| # | Package | Contents | Done when |
| --- | --- | --- | --- |
| 0 | Repo foundation | uv project (Python 3.13), ruff, mypy, pytest, pre-commit, GitHub Actions (lint, test, image build with SemVer tags; `latest` only for staging), `.dockerignore`, commands in `CLAUDE.md` | CI green on an empty app |
| 1 | Service skeleton | pydantic-settings from env, public port 8080 and internal 9090 (`/health`, `/ready`, `/metrics`), structlog JSON logs with `x-client-trace-id` and B3, result envelope and error codes, API keys (user, admin), inbound rate limit in Valkey, OpenAPI display name and description, Dockerfile (multi-stage, non-root), docker-compose with Valkey, base Helm chart with toggles | `docker compose up` serves an authenticated stub endpoint; `helm lint` passes |
| 2 | Fast extraction | httpx fetcher with SSRF guard and limits, robots.txt and TDM checks, per-domain outbound limits, Trafilatura pipeline (link-only lists, boilerplate rules, MathML), quality gate, DOM metadata and licenses, fingerprints and result cache, `POST /v1/extract`, smoke regression set (`tests/live`) | Smoke set at or above the 68 % fast baseline |
| 3 | Jobs and batch | Taskiq on Valkey, job API (create, status, paged results), sync-to-job fallback, `POST /v1/batch` with fair per-domain scheduling, queue metrics, KEDA/HPA and alert rules in Helm | 1,000-URL batch completes across 2 workers with no domain over its limit |
| 4 | Browser worker | Playwright pool, auto escalation, `actions`, cookie consent, challenge detection, full-page screenshots at 1920×1080, `POST /v1/screenshot`, browser image, optional Patchright | Smoke set at or above the 80 % browser baseline |
| 5 | Documents and OCR | `POST /v1/extract/file`, MarkItDown, Docling worker with PDF routing, model PVC with init container | PDF, Office and image corpus converts; scans yield text |
| 6 | Audio and video | youtube-transcript-api, yt-dlp subtitles and metadata, HTML5 tracks, faster-whisper ASR worker, external ASR switch | YouTube and Mediathek samples return subtitles; an upload is transcribed |
| 7 | PII | Presidio de/en with `mask`, `redact`, `hash`, `annotate`; spaCy models from PVC | German and English test texts handled per mode |
| 8 | Structured output and chunking | Strategies `metadata`, `selectors`, `patterns`, `llm`, `auto`; learned selectors; LLM client with key order (header, then env); `POST /v1/structured`, `POST /v1/chunk` | Each strategy tested; LLM path tested against a mock endpoint |
| 9 | Site structure | Sitemap discovery (robots.txt, index, gzip), link graph with link types to a set depth, `POST /v1/site-structure` job | Sample sites return complete sitemaps and typed links |
| 10 | MCP and UI | FastMCP at `/mcp`, htmx playground at `/ui`, both toggled by env and Helm | MCP tools usable from Claude; UI drives every endpoint |
| 11 | Hand-over | Full WLO regression run, load test, resource baselines (`docker stats`), env reference, example `values.yaml`, secrets list, cost notes (LLM, ASR), final architecture doc | Ops-guide hand-over checklist complete |
