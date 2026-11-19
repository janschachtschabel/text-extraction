# Package 25: Free web search through SearXNG Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Search the web without a paid API: an optional SearXNG service queried through
`POST /v1/search`, an MCP tool `web_search` and a "Search" tab in the playground whose hits can be
extracted as a batch.

**Architecture:** SearXNG runs as a separate, unmodified container (`searxng/searxng`, AGPL-3.0,
never imported). Our API talks to its JSON API over plain httpx (`src/text_extraction/search/`),
caches answers in Valkey and answers our envelope. The `init` command writes SearXNG's
`settings.yml` (from `src/text_extraction/data/searxng-settings.yml`, with a generated
`secret_key`) into a volume, because the Hostinger stack allows no bind mounts. Only engines with an
official, free API are on; scraping engines stay off.

**Tech Stack:** Python 3.13, FastAPI, httpx, pydantic-settings, Valkey, FastMCP, Jinja2/htmx UI,
docker compose, Helm.

**Spec:** `/mnt/project-files/package-25/plan-paket-25.md` (German, approved by Jan on 2026-11-18,
D1-D5 = a) and `/mnt/project-files/mcp-websuche/suchoptionen-kostenfrei.md`.

## Global Constraints

- D1 a: SearXNG only as a separate unmodified container; CLAUDE.md rule 7 gets the exception
  "an unmodified AGPL service in its own container (SearXNG) is allowed; never an AGPL/GPL import".
- D2 a: engines on by default: `wikipedia` (display_type `list`), `wikibooks`, `wikiversity`,
  `wikinews`, `mwmbl`, `openalex`, `arxiv`, `crossref`; every other engine off
  (`use_default_settings.engines.keep_only`).
- D3 a: root `docker-compose.yml` and `docker-compose.build.yml` run `searxng` and set
  `SEARCH_ENABLED=true`; Helm `searxng.enabled: false`, `config.SEARCH_ENABLED: "false"`.
- D4 a: a "Search" tab in the UI (de/en, keyboard and screen-reader usable like the other tabs).
- D5 a: no WLO source in this package.
- Settings (env, documented in `docs/configuration.md` and `.env.example`): `SEARCH_ENABLED`
  (false in code), `SEARCH_URL` (`http://searxng:8080`; http allowed: an internal service),
  `SEARCH_TIMEOUT_SECONDS` (10), `SEARCH_MAX_RESULTS` (50), `SEARCH_CACHE_SECONDS` (3600, 0 = off),
  `SEARCH_SOURCES` (the D2 list, comma-separated), `SEARCH_ALLOW_USERS` (true).
- Envelope: SearXNG down or timing out → 200 `failed`, `upstream_unavailable`, retryable;
  some sources failed → `partial` with `data.sources_failed`; our own fault → 5xx.
- Hostinger rules (`/mnt/project-files/hostinger/hostinger-docker-SKILL.md`): no bind mounts, no
  build, image pinned by tag, secrets created by `init`, `.env.example` works as `.env`.
- The `searxng` container: internal network only, no published port, limiter off, no Valkey of
  its own, `read_only` where the image allows it, `no-new-privileges`.

## Review Focus

1. A query that SearXNG answers with zero results and every engine in `unresponsive_engines`:
   `failed` with `upstream_unavailable`, not an empty `success`.
2. `sources` naming an engine outside `SEARCH_SOURCES`: 422 with the allowed names.
3. A result URL that is not http(s) (SearXNG can return `magnet:` or relative links): dropped.
4. A cached answer for one `language`/`page`/`sources`/`time_range` never answers another (the
   cache key covers every parameter).
5. The UI's "extract selected" with no hit ticked: the form error, not an empty batch.

---

### Task 1: Search client and models

**Files:** Create `src/text_extraction/search/__init__.py`, `search/types.py`, `search/client.py`;
modify `src/text_extraction/settings.py`; test `tests/search/test_client.py`.

**Interfaces (produces):**
- `SearchRequest` (pydantic, `extra="forbid"`): `query: str` (1-500 chars), `max_results: int = 10`
  (1..`SEARCH_MAX_RESULTS`, checked in the route), `language: str | None` (ISO 639-1),
  `page: int = 1` (1-10), `time_range: Literal["day","month","year"] | None`,
  `sources: list[str] | None`.
- `SearchHit`: `url`, `title`, `snippet: str | None`, `sources: list[str]`,
  `published: str | None`, `score: float | None`.
- `SourceFailure`: `source`, `reason`.
- `SearchResult`: `query`, `results: list[SearchHit]`, `suggestions: list[str]`,
  `sources_failed: list[SourceFailure]`, `cache: Literal["hit","miss","bypass"]`.
- `async def search(request, settings, http: httpx.AsyncClient, valkey) -> Envelope[SearchResult]`.

- [ ] Failing tests against `httpx.MockTransport` answering SearXNG JSON (fixtures recorded from
  the SearXNG docs' shape): mapping of `results`/`engines`/`publishedDate`/`unresponsive_engines`;
  non-http(s) URLs dropped; `max_results` cut; timeout and connection error → `failed`
  `upstream_unavailable` retryable; HTTP 403 from SearXNG (JSON format off) → `failed`
  `upstream_unavailable` with a message naming `search.formats`; all sources failed and no hits →
  `failed`; some failed → `partial`; cache hit on the same parameters, miss on another page.
- [ ] Implement; the request is `GET {SEARCH_URL}/search?q=…&format=json&pageno=…&language=…&engines=…&time_range=…`.
- [ ] `uv run pytest tests/search -q`, ruff, mypy; commit.

### Task 2: `POST /v1/search` and `/v1/info`

**Files:** Create `src/text_extraction/search_api.py`; modify `app.py` (router, operation id
`search`), `info.py` (`features.search`), `openapi.py` (tag), `feature_access.py` (feature
`search`, `SEARCH_ALLOW_USERS`); tests `tests/test_search_api.py`, `tests/test_info.py`,
`tests/test_app.py` (operation ids).

- [ ] Failing tests: 400 `invalid_request` "Web search is not enabled on this service." while off;
  403 for a user key with `SEARCH_ALLOW_USERS=false`; 422 for `max_results` over the setting and
  for an unknown source; rate limit counts one hit; `features.search` per role.
- [ ] Implement `run_search(state, principal, request) -> Envelope[SearchResult]` (the route core,
  ruling P10-S1) and the route; the app keeps one `httpx.AsyncClient` for search in its lifespan.
- [ ] Tests, ruff, mypy; commit.

### Task 3: MCP tool `web_search`

**Files:** Create `src/text_extraction/mcp_server/tools_search.py`; modify `server.py` (register,
`TOOL_ROUTES`), `option_hints.py` if `options` carries fields; tests
`tests/mcp_server/test_tools_search.py`, `test_tools_jobs.py::test_every_public_route_has_a_tool`.

- [ ] Failing tests: tool listed with `query` required, `max_results`, `language`, `sources`;
  answer equals REST's; read-only and open-world annotations; description says to read hits with
  `extract_url` or `batch_extract`.
- [ ] Implement; commit.

### Task 4: Deployment

**Files:** Create `src/text_extraction/data/searxng-settings.yml`; modify
`src/text_extraction/init_secrets.py` (write `searxng/settings.yml` with a kept 32-byte
`secret_key`, readable by the searxng user), `docker-compose.yml`, `docker-compose.build.yml`,
`.env.example`, `deploy/helm/text-extraction/values.yaml` and new templates
`searxng-deployment.yaml`, `searxng-service.yaml`, `searxng-configmap.yaml` (secret key from a
Secret), `networkpolicy.yaml`; tests `tests/test_compose_files.py`, `tests/test_init_secrets.py`
(or the existing init test file), helm lint.

- [ ] Failing tests: the settings file keeps only the D2 engines, enables `json`, limiter off,
  binds no public port; init writes it once and keeps the key; both compose files have `searxng`
  on the backend network only, with a pinned tag and `SEARCH_ENABLED=true` for the API;
  `docker compose config -q` passes with an empty `.env`.
- [ ] Implement; `docker run --rm -v "$PWD/deploy/helm:/apps" -w /apps alpine/helm:3.19.0 lint
  text-extraction` (CI runs it); commit.

### Task 5: UI "Search" tab

**Files:** modify `src/text_extraction/ui/routes.py` (tab), `ui/forms.py` (form and operation),
templates (a search form and a results list with checkboxes and "Extract selected"),
`ui/locales/en.json`, `de.json`; tests `tests/ui/test_search_tab.py`.

- [ ] Failing tests: tab shown only with `features.search`; results render escaped with links
  (`rel="noopener noreferrer"`); "Extract selected" posts the ticked URLs to the batch flow and
  shows the batch progress; nothing ticked → form error; de/en keys present
  (`tests/ui/test_i18n.py`).
- [ ] Implement following the site tree's selection pattern; commit.

### Task 6: Docs, rules, live check

**Files:** `CLAUDE.md` (rule 7 exception), `docs/architecture.md` (section "Web search",
endpoint row, rulings), `docs/configuration.md`, `docs/user-guide/en|de/search.md` (new) and the
index pages, `README.md`/`README.de.md` (feature and endpoint rows), `docs/packages.md` (row 25),
`CHANGELOG.md`, `scripts/check_licenses.py` notes if it lists images.

- [ ] Write; `uv run pytest tests/test_docs_i18n.py -q`,
  `uv run python scripts/check_translation_pairs.py --base origin/main`.
- [ ] Live check: run a real SearXNG with `data/searxng-settings.yml` (sandbox install or CI
  service container) and query "Photosynthese" through `POST /v1/search`; record hits per source.
- [ ] Whole-branch review, fixes, full suite, PR, merge when green (Jan, 2026-11-18 23:23).
