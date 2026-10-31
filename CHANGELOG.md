# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version is the same in
`pyproject.toml`, in `deploy/helm/text-extraction/Chart.yaml` (`version` and `appVersion`) and
in the image tags; a release is the Git tag `vX.Y.Z` on `main`.

## [0.1.0] - unreleased

The first release, a prototype: packages 0 to 12 of [docs/packages.md](docs/packages.md). The
release is tagged `v0.1.0` after the package 12 pull request is merged; package 11 is merged
without a tag.

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
- Package 11, hand-over: the load test and the resource baselines (`docker stats`), the Helm
  sizing and an example production values file, the environment reference, the cost notes for the
  LLM and ASR, the hand-over document, Dependabot and this changelog.

### Not in this release

- A full 4,000-URL regression run against the WLO set. The `live` workflow takes a `mode` input
  for it, and the run is started by hand later.
- A decision on permanent operation. This release is a prototype, and Jan Schachtschabel decides.
