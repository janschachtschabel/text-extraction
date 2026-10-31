# CLAUDE.md

Guidance for Claude (and humans) working in this repository.

## What this is

A FastAPI service that turns web pages, files, audio and video into clean text or
Markdown. It has a fast HTTP mode and a browser mode (Playwright), plus screenshots,
site structure, DOM metadata (licenses), PII handling, structured output (with and
without an LLM), chunking, an optional MCP server and an optional web UI.

- Architecture: [docs/architecture.md](docs/architecture.md)
- Implementation packages and their definition of done: [docs/packages.md](docs/packages.md)

## Non-negotiable rules

1. **English only** in code, comments, docs, commit messages and API texts.
2. **Ops guide compliance** (the operations hand-over guide). In particular:
   - Two ports: public API on `8080`, internal `9090` with `/health`, `/ready`, `/metrics`.
     Internal endpoints never appear in the OpenAPI spec.
   - Stateless pods. All state (jobs, results, cache, rate limits, fingerprints) lives in
     Valkey (Redis protocol). No SQLite, no local files as state.
   - Every setting is an env var with a documented default; secrets are marked separately.
   - Logs are JSON on stdout (plain text switchable for local dev); `LOG_LEVEL` env;
     `x-client-trace-id` and B3 headers are passed through and logged.
   - Work longer than ~30 s runs as a job (create, status, results).
   - Images: slim base, multi-stage, non-root, no models or data inside; models come from a PVC.
   - Optional components are switchable in Helm (`valkey`, workers, `s3`, `mcp`, `ui`).
3. **B-API ready, not integrated:** `AUTH_MODE=none|api_key`; an LLM key from a request
   header (future B-API virtual key) wins over the env key. Do not build B-API specifics.
4. **Result envelope everywhere:** `status` (`success|partial|failed`), `error.code`,
   `error.retryable`, `upstream_status`, `mode_used`, timings. The API answers 200 with
   `status: failed` when the *target* failed; 5xx only for our own faults.
5. **Security:** SSRF guard on every fetch (incl. redirects and browser sub-requests),
   byte and time limits, no evaluation of caller JavaScript unless the caller is admin.
6. **Respect sites:** robots.txt and EU TDM opt-out checks (`enforce|report|ignore`),
   per-domain outbound limits, honest user agent. No IP/proxy rotation to evade limits.
   Exception: unblock mode (admin, `compliance: ignore`, `BROWSER_UNBLOCK_ENABLED`) for sites that allowed extraction by contract may present a stealth browser with an unmodified Chrome user agent; outbound limits still apply and IP/proxy rotation stays excluded.
7. **Licences:** no AGPL/GPL dependencies (e.g. PyMuPDF, Marker, MinerU are excluded).

## Workflow

Coding follows the **Superpowers** skills, vendored in `.claude/skills/`
(see `.claude/skills/SUPERPOWERS.md`). At the start of every session read
`.claude/skills/using-superpowers/SKILL.md` and its `references/claude-code-tools.md`.
`superpowers:<name>` in a skill means `.claude/skills/<name>`.

- Build package by package as listed in `docs/packages.md`; one branch and one PR per package.
- Design is settled in `docs/architecture.md`; use `brainstorming` only for an open design
  question inside a package, and record the answer in that file.
- Per package:
  1. `writing-plans` → plan in `docs/superpowers/plans/<date>-package-<n>-<name>.md`.
  2. `subagent-driven-development` (or `executing-plans` when working inline), with
     `test-driven-development` for every feature and fix.
  3. `requesting-code-review` on the whole package; handle findings with
     `receiving-code-review`, bugs and failing tests with `systematic-debugging`.
  4. `verification-before-completion`, then `finishing-a-development-branch`: open the PR.
- A package is done only when its "done when" criterion in `docs/packages.md` is met,
  tests and linters are green, and the review findings are fixed. Jan allows merging a
  done package PR without waiting for him (squash merge).

## Stack

Python 3.13, uv, FastAPI, pydantic-settings, httpx, Playwright (Chromium), Trafilatura,
extruct, MarkItDown, Docling, Presidio + spaCy, youtube-transcript-api, yt-dlp,
faster-whisper, own fair job queue on Valkey (Lua, leases), OpenAI SDK
(OpenAI-compatible endpoints), FastMCP,
structlog, prometheus-fastapi-instrumentator, OpenTelemetry (B3 propagator).
Quality: ruff, mypy, pytest.

## Commands

```bash
uv sync                      # install dependencies into .venv
uv run pytest                # run the tests
uv run ruff check .          # lint
uv run ruff format .         # format
uv run mypy                  # type-check (strict)
uv run pre-commit install    # install the git hooks
docker compose up --build    # API, worker, Valkey; needs .env with API_KEYS, VALKEY_PASSWORD
docker run --rm -v "$PWD/deploy/helm:/apps" -w /apps alpine/helm:3.19.0 lint text-extraction  # helm lint
```

## Reference material

The previous Selenium and Playwright services are not part of this repo. Their lessons
(auto-mode heuristic, YouTube handling, MathML, boilerplate lists, WLO URL sets and
baselines: fast 68 %, browser 80 % on 4,000 URLs, 2026-09-28) are summarised in
`docs/architecture.md`.
