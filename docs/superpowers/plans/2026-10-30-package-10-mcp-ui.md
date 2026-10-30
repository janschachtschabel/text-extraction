# Package 10: MCP and UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An MCP client (Claude Code, Claude Desktop through a bridge) connects to `https://<host>/mcp` with its API key and gets one tool per public capability (extract a URL or a file, screenshot, structured output, chunks, batch, site structure, job status and paged results, the job's screenshot, cancel), and a person opens `https://<host>/ui`, enters a key and drives every public endpoint from an htmx playground; both are switched by `MCP_ENABLED` and `UI_ENABLED` (Helm `mcp.enabled`, `ui.enabled`).

**Architecture:**
- **Route cores, called twice (P10-S1).** Each public route's body moves unchanged into a function that takes the app's `State` instead of the `Request` (`respond`, `answer_upload`, `open_job` already almost are); the route becomes a one-line wrapper. MCP tools call these cores in-process, so a tool runs exactly the checks, limits, SSRF guard, compliance and envelope of its route, with no HTTP loopback.
- **MCP in the API pods (D1 a).** FastMCP's streamable HTTP app, stateless and with JSON answers (no session pinned to a pod, no SSE), served at exactly `/mcp` on port 8080 behind a gate that authenticates every HTTP request like REST and refuses foreign browser origins; a FastMCP middleware counts each `tools/call` against the caller's REST rate-limit bucket, logs it and counts it in Prometheus. Long work answers with a job id (`MCP_WAIT_SECONDS`), and `get_job` pages and waits.
- **UI as a REST client in-process (D5 a).** Jinja2 pages with vendored htmx (no CDN, no Node build); each form posts to `/ui/run/<op>`, which forwards the request through httpx's ASGI transport to the real `/v1/...` route with the browser's key headers and renders the answer as escaped HTML. The key lives in the tab's `sessionStorage` and travels only as a header (D6 a); a strict CSP, no cookies and an `HX-Request` + same-origin check close CSRF and script injection.

**Tech Stack:** FastMCP (Apache-2.0, with the MIT `mcp` SDK), Jinja2 (BSD-3-Clause), htmx 2.0.11 (0BSD, vendored), FastAPI/Starlette, httpx `ASGITransport`, the existing auth, rate limiter, route cores, `JobStore`, Prometheus client, structlog. Tests: pytest, fakeredis, pytest-httpserver, `fastmcp.Client` over real HTTP (uvicorn in a thread), FastAPI `TestClient` with raw JSON-RPC, Playwright Chromium (`-m browser`).

**Spec:**
- `docs/packages.md` row 10: FastMCP at `/mcp`, htmx playground at `/ui`, both toggled by env and Helm. Done when MCP tools are usable from Claude and the UI drives every endpoint.
- `docs/architecture.md`: the overview diagram (`8080: REST /v1, /mcp, /ui`), the API rows `/mcp` ("MCP tools (optional, `MCP_ENABLED`)", streamable HTTP) and `/ui` ("Playground (optional, `UI_ENABLED`), htmx, no Node build"), "Non-JSON content types … SSE (MCP)", "Auth", "Result envelope and errors", "Sync-to-job (P1)".
- `CLAUDE.md` rules 1, 2 (ports, stateless pods, env vars with defaults, logs and trace headers, jobs over ~30 s, Helm toggles `mcp` and `ui`), 3 (`AUTH_MODE`, the LLM key header wins), 4, 5, 7.
- Decisions D1-D6 below. Until Jan answers them, the plan uses the recommended options and is adjusted if he picks another.

## Global Constraints

- English only (code, templates, UI copy, tool descriptions). Python 3.13. No GPL or AGPL dependency; `uv run python scripts/check_licenses.py --lock uv.lock` stays green.
- New base dependencies: `fastmcp` and `jinja2` only, each pinned to its newest release at least 14 days old on the day it is added (ruling R16; on 2026-10-30 that is `fastmcp==4.1.0` and `jinja2==3.1.6`); transitive packages younger than 14 days are held back with `exclude-newer-package` in `pyproject.toml`. htmx 2.0.11 is vendored as a static file; no CDN, no Node build, no other JavaScript library.
- `/mcp` and `/ui` live on the public port 8080 only, never on 9090, and appear in no OpenAPI spec. The public spec changes only by `DELETE /v1/jobs/{id}` (D4 a).
- Stateless pods: no MCP session state, no UI session, nothing stored by MCP or UI beyond what the route cores store.
- Every new setting is an env var with a documented default in `docs/configuration.md`, `.env.example` and the Helm chart (`config`, or the toggle) in the task that adds it. No new secret.
- MCP and UI never skip a check of the REST route: same auth and roles (`AUTH_MODE` `none|api_key`, user and admin keys), the same rate-limit bucket, SSRF guard, compliance modes, body limits and result envelope.
- Secrets (the API key, `X-LLM-Api-Key`, `X-ASR-Api-Key`) arrive only as HTTP headers: never as tool arguments or form fields, never logged, never in a URL or cookie, never rendered into HTML.
- Logs and metric labels carry tool and operation names, outcomes, counts and job ids only: never tool arguments, URLs, content or keys.
- Off by default (`MCP_ENABLED=false`, `UI_ENABLED=false`); with both off the app is as before, and `fastmcp` and `jinja2` are not imported.
- Existing REST answers do not change: the whole existing suite passes unchanged after Task 1.
- Not in this package: OAuth for MCP (a B-API concern), MCP resources, prompts, sampling and the stdio transport, server-sent progress notifications, a UI login or session, a UI language other than English, a Node build.

## Decisions for Jan (open)

- **D1, where MCP runs.** (a, recommended) In the API pods: FastMCP at `/mcp` on port 8080, `MCP_ENABLED` (Helm `mcp.enabled`), stateless; one image, one ingress, the API's auth, limits, scaling and probes. (b) A Deployment of its own from the API image (an entry point serving only `/mcp`) with its own HPA, Service and ingress path; the same in-process cores. (c) A separate thin MCP server that calls the REST API over HTTP with the caller's key.
- **D2, tool arguments.** (a, recommended) Lean: each tool lists the few fields agents use (`url`, `format`, `mode`, `wait_seconds`, …) plus one `options` object for any other field of the REST body, validated by the REST model; `tools/list` stays a few KB instead of about 85 KB of JSON schema (`ExtractRequest`'s alone is 22 KB, and four tools share it), which every client puts into the model's context. (b) The full REST body schema as each tool's input. (c) Lean only: the other options are not reachable over MCP.
- **D3, files over MCP.** (a, recommended) `extract_file` takes the file as base64 (a `data:` URL prefix allowed) within the upload route's limit (`DOCUMENT_MAX_BYTES`, the media limit while uploads are transcribed), and `/mcp`'s body limit is 4/3 of it plus 64 KiB: programmatic clients send real files; a file at a URL goes through `extract_url` as today. (b) Base64 with a lower MCP-only cap (`MCP_FILE_MAX_BYTES`, 5 MB), since a model rarely writes more. (c) No upload tool: files by URL only.
- **D4, cancelling jobs.** (a, recommended) A new route `DELETE /v1/jobs/{id}` (the owner or an admin, else 404): deletes the job with all its keys and frees its open-job slot; workers drop its queued items as for an expired job, items in flight finish unstored; the MCP tool `cancel_job` and a UI button call it. (b) No cancel: jobs run out and expire after `JOB_TTL_SECONDS`. (c) A soft cancel: a new job state `cancelled` that workers check before each item, keeping the results so far (a Lua and model change).
- **D5, how the UI reaches the API.** (a, recommended) Server-rendered: htmx posts each form to `/ui/run/<op>`, which forwards it in-process (httpx `ASGITransport`, no network) to the real `/v1/...` route with the browser's key headers and renders the answer as escaped HTML, with the raw JSON and a curl line; every REST check (body limits, validation, auth, rate limit, 202) runs exactly as for any client. (b) The `/ui/run` routes call the route cores directly, as MCP does (the HTTP layer's body limits and validation answers are not exercised). (c) The browser calls `/v1/...` with `fetch` and renders the JSON in its own JavaScript (htmx mostly unused, more client code).
- **D6, where the UI keeps the API key.** (a, recommended) The tab's `sessionStorage`: it survives a reload and goes with the tab; the UI's own script sends it as the `Authorization` header, never as a cookie (no CSRF), and the CSP forbids any other script. (b) Memory only: retyped after every reload. (c) `localStorage` behind a "remember" box: survives browser restarts, readable later by any script on the origin.

## Rulings in this plan

- **P10-S1, route cores.** Shared code takes `state: starlette.datastructures.State` (the app's `request.app.state`) where it took `request` only to reach it: `respond`, `answer_upload`, `open_job`, `discard_job`. The bodies of the routes move unchanged into cores; each route calls its core:
  - `auth.principal_for(settings, authorization: str | None) -> Principal` (401 `ServiceError` as today; `authenticate` calls it); `ratelimit.check_rate(state, principal) -> None` (429 with `Retry-After` / 503 as today; `rate_limited` calls it); `jobs_api.check_jobs_enabled(settings) -> None` (503).
  - `screenshot_api.run_screenshot(state, principal, body: ScreenshotRequest) -> Envelope[ScreenshotResult] | JSONResponse`, `screenshot_api.read_artifact(state, principal, job_id: str, name: str) -> bytes` (404 as today); `structured_api.run_structured(state, principal, body: StructuredRequest, *, asr_key, llm_key)`, `structured_api.run_chunk(state, principal, body: ChunkRequest, *, asr_key, llm_key)`; `file_api.run_upload(state, principal, form: FileForm, data: bytes, name: str | None, content_type: str | None, *, asr_header: str | None, llm_header: str | None) -> Envelope[ExtractResult] | Response` (the route's checks, the size check of `upload_file_limit`, the ASR key check, then `answer_upload`; the route keeps checking before it reads the bytes); `jobs_api.start_batch(state, principal, body: BatchRequest, *, asr_key, llm_key) -> Envelope[JobAccepted]`, `jobs_api.read_job(state, principal, job_id: str, offset: int, limit: int) -> Envelope[JobView]`, `site_api.start_site(state, principal, body: SiteStructureRequest) -> Envelope[JobAccepted]`, `app.service_info(principal) -> Envelope[InfoData]`. `asr_key` and `llm_key` are the raw header values; the cores apply `asr_key_of` / `llm_key_of` as the routes did.
- **P10-S2, the MCP endpoint** (`mcp_server/server.py`). `FastMCP(name="text-extraction", version=__version__, instructions=SERVER_INSTRUCTIONS, mask_error_details=True)`; `http_app(path="/mcp", stateless_http=True, json_response=True, host_origin_protection=False)` (our gate checks origins, P10-S3). Added to the public app as a Starlette `Route("/mcp", endpoint=McpGate(...), methods=["GET", "POST", "DELETE"])`, so the endpoint is exactly `/mcp` (no redirect; `/mcp/` is 404) and FastAPI leaves it out of the spec. `GET /mcp` answers 405 (stateless: no stream). The public lifespan enters the MCP app's lifespan. `fastmcp` is imported only inside `build_mcp`, called only with `MCP_ENABLED`. Body limit of `/mcp`: `mcp_body_limit(settings) = ceil(upload_file_limit(settings) / 3) * 4 + 65_536` (D3 a). `SERVER_INSTRUCTIONS` says: use `extract_url` for pages and documents at a URL; a result whose `data` has a `status_url` is a job: poll `get_job` with `wait_seconds`; `options` takes any field of the REST body (`/docs`).
- **P10-S3, the gate** (`mcp_server/gate.py`, pure ASGI). For every HTTP request to `/mcp`: an `Origin` header whose value is not in `MCP_ALLOWED_ORIGINS` (exact match, scheme and host) → 403 envelope `forbidden` ("This origin may not call the MCP endpoint."); then `principal_for` (401 envelope with `WWW-Authenticate: Bearer`, the REST message); the principal is stored in `scope["state"]["mcp_principal"]` and the name in `scope["state"]["principal"]` (the access line, as `_record_principal`) and bound to the log context. `call_principal() -> Principal` reads it from FastMCP's `get_http_request()`; no principal there is `internal_error`.
- **P10-S4, each tool call** (`mcp_server/calls.py`, a FastMCP `Middleware.on_call_tool`). In order: `check_rate(state, principal)` (one hit per `tools/call`; `initialize`, `tools/list` and notifications are free), the tool, then the log line `mcp_tool_call` (`tool`, `outcome`, `duration_ms`, `job_id` when a job was answered) and the metrics (P10-S15). Errors become results, never exceptions to the client: `ServiceError` → its code, message and `retryable` (P10-S6); `RequestValidationError` and pydantic `ValidationError` → `invalid_request`, "The request is invalid.", `details` as REST (`loc`, `msg`, never the input); any other exception → `internal_error`, "Internal server error.", logged with its traceback.
- **P10-S5, the tools** (D2 a, `mcp_server/tools_extract.py`, `mcp_server/tools_jobs.py`). Every tool is a `ModelTool` (a `fastmcp.tools.Tool` subclass whose `parameters` is its args model's JSON schema): it validates the args, merges `options` under them (a key given in both → `invalid_request`, "<key> is given twice."), validates the merged body with the REST model (extra fields refused, as REST), and calls the core.

  | Tool | Route | Arguments besides `options` | Annotations |
  | --- | --- | --- | --- |
  | `extract_url` | `POST /v1/extract` | `url`, `format` (`markdown`), `mode` (`auto`), `wait_seconds`, `content_offset` (0), `max_chars` | open world |
  | `extract_file` | `POST /v1/extract/file` | `content_base64`, `file_name`, `content_type`, `format`, `wait_seconds`, `content_offset`, `max_chars` | open world |
  | `screenshot_url` | `POST /v1/screenshot` | `url`, `full_page` (true), `wait_seconds` | open world |
  | `extract_structured` | `POST /v1/structured` | `url`, `structured` (required object), `format`, `wait_seconds`, `content_offset`, `max_chars` | open world |
  | `chunk` | `POST /v1/chunk` | `url` or `text`, `chunking` (required object), `text_type`, `wait_seconds`, `content_offset`, `max_chars` | open world |
  | `batch_extract` | `POST /v1/batch` | `urls`, `format`, `mode` | open world |
  | `site_structure` | `POST /v1/site-structure` | `url`, `depth` (2), `max_pages` (100), `scope` (`host`) | open world |
  | `get_job` | `GET /v1/jobs/{id}` | `job_id`, `offset` (0), `limit` (5, 1-50), `wait_seconds` (0, 0-`MCP_WAIT_SECONDS`), `content_offset`, `max_chars` | read only, no `options` |
  | `get_job_screenshot` | `GET /v1/jobs/{id}/artifacts/screenshot.png` | `job_id` | read only, no `options` |
  | `cancel_job` | `DELETE /v1/jobs/{id}` (D4 a) | `job_id` | destructive, no `options` |
  | `service_info` | `GET /v1/info` | none | read only |

  `options`, `structured` and `chunking` are `{"type": "object"}` in the schema (the REST model validates them); `options` is described as "Any further field of the REST body of <route> (see /docs), e.g. {\"pii\": {\"mode\": \"mask\"}}". `max_chars` defaults to `MCP_MAX_CONTENT_CHARS` (1-`MCP_MAX_CONTENT_CHARS`). `extract_file` (D3 a): a leading `data:<type>;base64,` is stripped (its type used when `content_type` is unset), whitespace is ignored, anything else that is not base64 is `invalid_request` ("content_base64 is not valid base64."); decoded bytes over `upload_file_limit(settings)` are `invalid_request` ("The file is larger than <n> bytes."); the rest of its args and `options` form a `FileForm`; `file_name` passes `sanitize_file_name`.
- **P10-S6, tool results** (`mcp_server/results.py`). `structuredContent` is the route's envelope as JSON (a 202's `Envelope[JobAccepted]` too); no `outputSchema` (it would double `tools/list`). `isError` is true only where REST answers 4xx or 5xx; a target's `failed` (REST 200) is a normal result. `content` holds one `TextContent` with the same JSON, preceded by a one-line `TextContent` hint when there is something to do: a job ("Continues as job <id> (<kind>): call get_job with job_id=<id> and wait_seconds up to <MCP_WAIT_SECONDS>."), a cut content, an image left out. Content cut: `data.content` (each item's in `get_job`) becomes `content[content_offset : content_offset + share]`, `share = max_chars` for one result and `max_chars // len(items)` for a job page, and a cut one gets the warning `mcp_content_truncated` and the hint "Cut at <n> of <total> characters; call again with content_offset=<next>." No other field is cut. `screenshot_url` and `get_job_screenshot` add an `ImageContent` (PNG) when the artifact is at most `MCP_IMAGE_MAX_BYTES`, else the hint "The screenshot (<n> bytes) is over MCP_IMAGE_MAX_BYTES; download it from <status_url>."
- **P10-S7, long work.** `extract_url`, `extract_file`, `screenshot_url`, `extract_structured` and `chunk` pass `wait_seconds = MCP_WAIT_SECONDS` when the caller gives none, so a call ends within it (plus the route's own hand-over time) with a result or a job; `JOBS_ENABLED=false` ignores it as REST does. `batch_extract` and `site_structure` always answer a job. `get_job` with `wait_seconds` re-reads the job once a second until it is `completed` or the time is up, then answers the page (one rate-limit hit for the call).
- **P10-S8, headers of a tool call.** `X-LLM-Api-Key` and `X-ASR-Api-Key` are read from the HTTP request of the call (`get_http_request().headers`) and passed to the core as the routes do (the caller's LLM key wins over `LLM_API_KEY`, rule 3). `x-client-trace-id` and B3 are handled by `TraceMiddleware` around `/mcp`: the access line, every log line of the call and the envelope's `trace_id` carry them.
- **P10-S9, settings.** `MCP_ENABLED` (false), `MCP_ALLOWED_ORIGINS` (empty: no browser origin; a comma list of `scheme://host[:port]`), `MCP_WAIT_SECONDS` (25, 0-60), `MCP_MAX_CONTENT_CHARS` (50,000, 1,000-10,000,000), `MCP_IMAGE_MAX_BYTES` (5,000,000, 0-20,000,000; 0 never attaches), `UI_ENABLED` (false).
- **P10-S10, `DELETE /v1/jobs/{id}`** (D4 a, `jobs_api.delete_job(state, principal, job_id) -> Envelope[JobDeleted]`, `JobDeleted(id: str, kind: JobKind)`). User role, rate limited, `check_jobs_enabled`; `visible_job` (404 for unknown, expired, foreign); `JobStore.delete`, then the job is removed from its owner's open set (`_discard` with the record's owner, so an admin's delete frees the owner's slot); log `job_deleted` (`job_id`, `kind`); 200 `success`. Items of the job that a worker claims later are dropped (`job_item_dropped`, reason `job_gone`), and a P1 task or an item in flight that finishes later stores nothing (`JobLost`).
- **P10-S11, the UI shell** (`ui/`). `GET /ui` renders `index.html` (one tab per operation, a key box outside every form with no `name`, `autocomplete="off"`); `/ui/static` is a `StaticFiles` mount with `htmx-2.0.11.min.js`, `ui.js`, `ui.css`. Pages and static files need no key. Jinja2 with autoescape on; templates have no inline `<script>`, no `style` attribute and no `hx-on`. `<meta name="htmx-config">` sets `allowEval: false`, `allowScriptTags: false`, `includeIndicatorStyles: false`, `selfRequestsOnly: true`, `historyEnabled: false`, `historyCacheSize: 0`. Every `/ui` answer carries `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `Cross-Origin-Opener-Policy: same-origin`, `Cross-Origin-Resource-Policy: same-origin`, `Permissions-Policy: camera=(), microphone=(), geolocation=()`, and `Cache-Control: no-store` (static files `public, max-age=3600`). Routes have `include_in_schema=False`.
- **P10-S12, UI keys and CSRF** (D6 a). `ui.js` keeps the API key, the LLM key and the ASR key in `sessionStorage` (`te.apiKey`, `te.llmKey`, `te.asrKey`), a "Forget keys" button clears them, and on `htmx:configRequest` for paths under `/ui/run/` it sets `Authorization: Bearer <key>`, `X-LLM-Api-Key`, `X-ASR-Api-Key` when set. The service never sets a cookie under `/ui`. `/ui/run/*` answers 403 (plain text) without `HX-Request: true`, and for an `Origin` whose host and port differ from the `Host` header; the service has no CORS middleware, so a cross-site page cannot send that header.
- **P10-S13, UI operations** (D5 a, `ui/forms.py`, `ui/forward.py`). `OPERATIONS: Mapping[str, Operation]` maps each op (`extract`, `file`, `screenshot`, `structured`, `chunk`, `batch`, `site-structure`, `job`, `artifact`, `cancel`, `info`) to its REST method and path. A form's main fields plus an "Options (JSON)" textarea merged under them build the REST body (invalid JSON: an error fragment, nothing forwarded). `forward(request, method, path, *, json=None, data=None, files=None, params=None) -> Forwarded(status: int, body: dict[str, Any] | None, content: bytes, content_type: str)` sends it through `httpx.AsyncClient(transport=httpx.ASGITransport(app=request.app), base_url="http://ui.internal", timeout=None)` with only `Authorization`, `X-LLM-Api-Key`, `X-ASR-Api-Key`, the B3 headers and `x-client-trace-id` set to the outer request's trace id. `/ui/run/*` answers 200 with a fragment for every forwarded outcome: the HTTP status, the envelope status and error (with 422 `details`), the content in `<pre>`, metadata and warnings, links as `<a rel="noopener noreferrer">` only for http(s) URLs, the raw JSON in `<details>`, and the curl line with `$API_KEY` in place of the key. A 202 renders the job fragment (`GET /v1/jobs/{id}`, `limit` 10): progress, items (index, URL, status, error code, words, 300 characters of content), previous and next buttons, and, while not `completed`, `hx-trigger="load delay:2s"` to refresh itself. A screenshot answer and a `screenshot` job's item show the PNG from the artifact route as a `data:image/png;base64,` image. Body limits: `/ui/run/file` the upload limit, `/ui/run/chunk` the text limit, `/ui/run/batch` the batch limit, other ops the default. Default `wait_seconds` in the forms is 25.
- **P10-S14, Helm and compose.** Helm `mcp.enabled` (false) and `ui.enabled` (false) render `MCP_ENABLED` and `UI_ENABLED` on the API pods only (values under `config` are ignored, as `JOBS_ENABLED`); `config` gets `MCP_ALLOWED_ORIGINS`, `MCP_WAIT_SECONDS`, `MCP_MAX_CONTENT_CHARS`, `MCP_IMAGE_MAX_BYTES` with their defaults; the ingress already routes `/`; NOTES.txt prints the `/mcp` and `/ui` URLs when on. The compose `api` service sets `MCP_ENABLED: ${MCP_ENABLED:-true}` and `UI_ENABLED: ${UI_ENABLED:-true}` (local development).
- **P10-S15, metrics and logs.** `text_extraction_mcp_tool_calls_total{tool, outcome}` (`success`, `partial`, `failed`, `job`, or the refusal's error code; every tool exported with `success` at 0 from the start), `text_extraction_mcp_tool_seconds{tool}` (histogram, buckets 0.05 s to 120 s). Log lines `mcp_tool_call` (P10-S4) and `job_deleted`. The UI adds no metric: its forwarded calls are counted as REST requests.

## Review Focus

1. **Getting past auth or a role check through the new doors:** no key, a wrong key or a user key on `/mcp`; a user key asking `compliance: ignore`, an `evaluate` action or an XPath rule through `options`; a page on another origin posting to `/mcp` or `/ui/run/extract` under `AUTH_MODE=none`. Expected: 401 before any tool runs, the REST 403s as tool errors, 403 for the foreign origin. Owner: Task 3 (`test_missing_or_wrong_key_401_envelope`, `test_foreign_origin_403_allowed_origin_ok`), Task 4 (`test_user_options_checked_as_rest`), Task 6 (`test_run_requires_hx_request_and_same_origin`).
2. **Results too large for a model's context:** a 5 MB page, a job page of 50 items of 2 MB each, a 10 MB full-page screenshot. Expected: content cut to `max_chars` (per item an equal share) with `mcp_content_truncated` and a hint to page on; the image left out above `MCP_IMAGE_MAX_BYTES` with the download hint. Owner: Task 4 (`test_content_slicing`, `test_screenshot_image_attached_and_capped`), Task 5 (`test_get_job_slices_each_item`).
3. **Script injection in the playground:** a page whose content, title and metadata hold `<script>`, `"><img src=x onerror=…>`, a `javascript:` link, a job item URL with quotes. Expected: everything escaped, only http(s) links clickable, and no inline script could run anyway (CSP). Owner: Task 7 (`test_extract_renders_envelope_escaped`, `test_links_only_http`), Task 6 (`test_no_inline_script_or_style_or_hx_on`).
4. **Calls that would outlast a client's timeout:** a browser render of 60 s, an LLM step of 30 s, a 1,000-page crawl, `get_job` on a job that never ends. Expected: every tool call ends within `MCP_WAIT_SECONDS` plus the route's hand-over time with a result or a job id; `get_job`'s wait is bounded. Owner: Task 4 (`test_default_wait_becomes_job`), Task 5 (`test_get_job_wait_bounded`).
5. **Odd file input over MCP:** base64 with line breaks, a `data:application/pdf;base64,` prefix, broken base64, a file one byte over the limit, a body over `/mcp`'s limit. Expected: the first two convert, the next two are `invalid_request`, the last is 413 before it is parsed or authenticated. Owner: Task 4 (`test_extract_file_base64`), Task 3 (`test_body_limit_of_mcp_path`).

---

### Task 1: Route cores

**Files:**
- Modify: `src/text_extraction/auth.py`, `src/text_extraction/ratelimit.py`, `src/text_extraction/extract_api.py` (`respond(state, …)`), `src/text_extraction/screenshot_api.py`, `src/text_extraction/structured_api.py`, `src/text_extraction/file_api.py` (`answer_upload(state, …)`, `run_upload`), `src/text_extraction/jobs_api.py` (`open_job(state, …)`, `discard_job(state, …)`, `check_jobs_enabled`, `start_batch`, `read_job`), `src/text_extraction/site_api.py`, `src/text_extraction/app.py` (`service_info`)
- Test: `tests/test_cores.py`; the existing suite unchanged

**Interfaces:**
- Produces: every function of P10-S1 with the signatures given there.

- [ ] **Step 1: Failing tests** (fakeredis, the pipeline around `FakeFetcher` as in `tests/test_extract_api.py`): `test_principal_for` (none mode → `anonymous`/`admin`; a user key → its principal; no or a wrong header → `ServiceError` 401 with the REST message), `test_check_rate_shares_bucket_with_routes` (a key with `rate_limit_per_minute: 2`: `GET /v1/info`, then `check_rate`, then `GET /v1/info` → 429), `test_cores_answer_like_routes` (for `respond`, `run_structured`, `run_chunk` with a `text`, `run_upload`, `start_batch`, `start_site`, `read_job`: the core's envelope equals the route's JSON apart from `trace_id`, `id`, `status_url` and `timings`), `test_run_upload_refuses_large_before_converting`.
- [ ] **Step 2:** `uv run pytest tests/test_cores.py -q` → FAIL (`principal_for` missing). **Step 3:** move the route bodies, nothing else. **Step 4:** PASS; `uv run pytest` (every existing test unchanged), `uv run mypy`.
- [ ] **Step 5:** commit `refactor: move the public routes' bodies into cores that take the app state`.

### Task 2: `DELETE /v1/jobs/{id}` (D4 a)

**Files:**
- Modify: `src/text_extraction/jobs_api.py` (route, `delete_job`), `src/text_extraction/job_models.py` (`JobDeleted`), `docs/configuration.md` ("Job keys": what a delete removes)
- Test: `tests/test_jobs_api.py`, `tests/test_worker.py`, `tests/test_extract_api.py` (extend)

**Interfaces:**
- Consumes: Task 1's `check_jobs_enabled`; `visible_job`, `_discard`, `JobStore.delete`.
- Produces: `async delete_job(state: State, principal: Principal, job_id: str) -> Envelope[JobDeleted]`; `JobDeleted(id: str, kind: JobKind)`.

- [ ] **Step 1: Failing tests:** `test_delete_own_job_then_404`, `test_delete_foreign_job_404_admin_ok`, `test_delete_frees_open_slot` (`BATCH_MAX_OPEN_PER_CLIENT=1`: a batch, a second 429, delete, the second 202; an admin deleting the user's batch frees the user's slot), `test_delete_jobs_disabled_503`, `test_openapi_lists_delete_jobs`; `test_worker_drops_items_of_deleted_job` (a queued item of a deleted job: no key re-created, `job_items_total{outcome="lost"}` + 1); `test_deleted_p1_job_stores_nothing` (a slow extract with `wait_seconds: 0` → 202, delete, the task ends: no key of the job exists).
- [ ] **Step 2:** FAIL. **Step 3:** implement P10-S10. **Step 4:** PASS; `uv run pytest tests/test_jobs_api.py tests/test_worker.py`.
- [ ] **Step 5:** commit `feat: add DELETE /v1/jobs/{id} to cancel and remove a job`.

### Task 3: MCP endpoint, gate and call guard

**Files:**
- Create: `src/text_extraction/mcp_server/__init__.py`, `server.py` (P10-S2), `gate.py` (P10-S3), `calls.py` (P10-S4, P10-S15), `results.py` (P10-S6: the error results; Task 4 adds the rest), `tools_info.py` (`service_info`)
- Modify: `src/text_extraction/settings.py` (P10-S9, the `MCP_*` settings), `src/text_extraction/app.py` (route, lifespan, body limit), `pyproject.toml` and `uv.lock` (`fastmcp`), `NOTICE`, `docs/configuration.md` (section "MCP", the metrics rows), `.env.example`
- Test: `tests/mcp_server/__init__.py`, `tests/mcp_server/rpc.py` (helper `rpc(client, method, params=None, *, headers) -> dict` posting JSON-RPC with `Accept: application/json, text/event-stream`), `tests/mcp_server/test_server.py`, `tests/test_settings.py`

**Interfaces:**
- Consumes: Task 1's `principal_for`, `check_rate`, `service_info`.
- Produces: `build_mcp(state: State, settings: Settings, registry: CollectorRegistry) -> McpMount` (`McpMount(app: ASGIApp, lifespan: Callable[[], AbstractAsyncContextManager[None]], server: FastMCP)`), `mcp_body_limit(settings: Settings) -> int`, `McpGate(app: ASGIApp, settings: Settings)`, `call_principal() -> Principal`, `call_header(name: str) -> str | None`, `ModelTool` (base: `args_model: type[BaseModel]`, `async call(state: State, principal: Principal, args: BaseModel) -> object`), `register(server: FastMCP, state: State, tool: ModelTool) -> None`, `error_result(error: Exception) -> ToolResult`, `envelope_result(answer: Envelope[Any] | JSONResponse | Response, *, hints: Sequence[str] = ()) -> ToolResult`; settings `mcp_enabled`, `mcp_allowed_origins: list[str]`, `mcp_wait_seconds`, `mcp_max_content_chars`, `mcp_image_max_bytes`.

- [ ] **Step 1: Failing tests** (`TestClient` as a context manager, so the lifespan runs): `test_mcp_settings_defaults_and_bounds`, `test_disabled_has_no_mcp_route_and_no_import` (`POST /mcp` 404; a subprocess building the app with `MCP_ENABLED=false` has no `fastmcp` in `sys.modules`), `test_initialize_and_list_tools` (`serverInfo.name == "text-extraction"`, `service_info` listed), `test_service_info_call` (`structuredContent["data"]["principal"] == "alice"`, `isError` false), `test_not_in_openapi`, `test_exact_path_no_redirect` (`POST /mcp/` 404), `test_get_mcp_405`, `test_missing_or_wrong_key_401_envelope` (Review Focus 1: 401, `WWW-Authenticate: Bearer`, `error.code == "unauthorized"`), `test_auth_none_is_anonymous_admin`, `test_foreign_origin_403_allowed_origin_ok` (Review Focus 1), `test_rate_limit_shared_with_rest` (limit 2: `GET /v1/info`, one call, then a call → `isError`, `rate_limited`, retryable; `tools/list` not counted), `test_trace_id_in_envelope_and_logs` (`x-client-trace-id: t-1` → `trace_id == "t-1"`; a `mcp_tool_call` line with `tool`, `outcome`, `principal`, no argument values), `test_metrics_counted`, `test_unexpected_exception_is_internal_error` (no traceback in the result), `test_body_limit_of_mcp_path` (Review Focus 5: a `Content-Length` over `mcp_body_limit` → 413 `invalid_request` without a key).
- [ ] **Step 2:** `uv run pytest tests/mcp_server -q` → FAIL. **Step 3:** add `fastmcp` (P10-S2 pin rule; `uv lock`, `exclude-newer-package` for too-new transitive packages, licence gate), implement. **Step 4:** PASS; `uv run python scripts/check_licenses.py --lock uv.lock`, `uv run mypy`.
- [ ] **Step 5:** commit `feat: serve an MCP endpoint at /mcp with the API's auth and rate limit`.

### Task 4: MCP extraction tools

**Files:**
- Create: `src/text_extraction/mcp_server/tools_extract.py` (`extract_url`, `extract_file`, `screenshot_url`, `extract_structured`, `chunk`), `src/text_extraction/mcp_server/files.py`
- Modify: `src/text_extraction/mcp_server/results.py` (slicing, hints, images), `src/text_extraction/mcp_server/server.py` (register)
- Test: `tests/mcp_server/test_tools_extract.py`, `tests/mcp_server/test_files.py`

**Interfaces:**
- Consumes: Task 1's cores; Task 3's `ModelTool`, `register`, `call_header`, `envelope_result`.
- Produces: `decode_file(value: str, limit: int) -> tuple[bytes, str | None]` (raises `ServiceError` `invalid_request`); `merge_options(args: dict[str, Any], options: dict[str, Any] | None) -> dict[str, Any]`; `slice_content(envelope: dict[str, Any], *, offset: int, share: int) -> str | None` (the hint, or None).

- [ ] **Step 1: Failing tests:** `test_tools_list_lean_schemas` (the five tools, each input schema under 4,096 bytes of JSON, an `options` object, open-world annotation), `test_extract_url_matches_rest` (`status`, `data.content`, `data.title` equal the REST answer's), `test_options_merge_and_conflict` (`{"links": true}` → `data.links`; `url` in both → `invalid_request`; an unknown field → `invalid_request` with `details`), `test_user_options_checked_as_rest` (Review Focus 1: `compliance: ignore` → `forbidden`; an `evaluate` action → `forbidden`; `mode: browser` with browser off → `invalid_request`), `test_target_failure_is_not_tool_error` (404 page → `isError` false, `target_http_error`), `test_private_url_blocked` (`http://169.254.169.254/` → `failed`/`blocked_url`), `test_default_wait_becomes_job` (Review Focus 4: `MCP_WAIT_SECONDS=1`, a fetch of 3 s → a `JobAccepted` envelope, the hint names `get_job`), `test_content_slicing` (Review Focus 2: 60,000 characters, `max_chars` 50,000 → 50,000, `mcp_content_truncated`, hint `content_offset=50000`; with it → the last 10,000), `test_llm_key_from_header_only` (header → stored with the job; `{"llm_key": "x"}` in `options` → `invalid_request`), `test_extract_file_base64` (Review Focus 5: the corpus PDF → `document.kind == "pdf"`; with line breaks and with a `data:` prefix → success; broken → `invalid_request`; `DOCUMENT_MAX_BYTES + 1` → `invalid_request`), `test_structured_and_chunk_tools` (`metadata` strategy values; `chunk` of a `text` with `recursive`), `test_screenshot_browser_off_400`, `test_screenshot_image_attached_and_capped` (a stored artifact of 1 KB → one `ImageContent`; `MCP_IMAGE_MAX_BYTES=500` → no image, the download hint).
- [ ] **Step 2:** FAIL. **Step 3:** implement P10-S5 to P10-S8 for these tools. **Step 4:** PASS; mypy clean.
- [ ] **Step 5:** commit `feat: add the MCP extraction tools`.

### Task 5: MCP job tools

**Files:**
- Create: `src/text_extraction/mcp_server/tools_jobs.py` (`batch_extract`, `site_structure`, `get_job`, `get_job_screenshot`, `cancel_job`)
- Modify: `src/text_extraction/mcp_server/server.py` (register; `TOOL_ROUTES: Mapping[tuple[str, str], str]`, (method, path) → tool)
- Test: `tests/mcp_server/test_tools_jobs.py`

**Interfaces:**
- Consumes: Tasks 1-4.
- Produces: `TOOL_ROUTES`.

- [ ] **Step 1: Failing tests:** `test_batch_and_get_job_paging` (3 URLs, results written through `JobStore`; `limit: 2` → 2 items, `next_offset == 2`), `test_get_job_waits_until_completed` (last result written after 1 s, `wait_seconds: 5` → `completed` within 3 s), `test_get_job_wait_bounded` (Review Focus 4: never completes, `wait_seconds: 2` → answers after 2-3 s, `running`), `test_get_job_slices_each_item` (Review Focus 2: 3 items of 30,000 characters, `max_chars` 30,000 → 10,000 each with `mcp_content_truncated`), `test_foreign_job_not_found`, `test_site_structure_tool` (`kind == "site"`; `depth` over `SITE_MAX_DEPTH` → `invalid_request`), `test_get_job_screenshot` (`ImageContent`; missing → `not_found`), `test_cancel_job_then_not_found`, `test_jobs_disabled_tool_error` (`internal_error`, retryable), `test_every_public_route_has_a_tool` (every `APIRoute` of the public app in the spec is a key of `TOOL_ROUTES`).
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** PASS; `uv run pytest tests/mcp_server`.
- [ ] **Step 5:** commit `feat: add the MCP job tools (batch, site structure, status, screenshot, cancel)`.

### Task 6: UI shell and its security

**Files:**
- Create: `src/text_extraction/ui/__init__.py`, `ui/routes.py` (`GET /ui`, the `/ui/run` guard), `ui/security.py` (headers middleware for `/ui`, `require_htmx`), `ui/forward.py`, `ui/templates/base.html`, `ui/templates/index.html`, `ui/static/htmx-2.0.11.min.js` (`package/dist/htmx.min.js` of `https://registry.npmjs.org/htmx.org/-/htmx.org-2.0.11.tgz`), `ui/static/ui.js`, `ui/static/ui.css`
- Modify: `src/text_extraction/settings.py` (`UI_ENABLED`), `src/text_extraction/app.py` (router and static mount when on, body limits of `/ui/run/*`), `pyproject.toml`, `uv.lock` (`jinja2`), `NOTICE` (htmx, 0BSD), `docs/configuration.md` (section "UI"), `.env.example`
- Test: `tests/ui/__init__.py`, `tests/ui/test_shell.py`

**Interfaces:**
- Produces: `create_ui_router(settings: Settings) -> APIRouter`; `templates: Jinja2Templates`; `require_htmx(request: Request) -> None`; `forward(...) -> Forwarded` (P10-S13); `HTMX_SHA256: Final[str]` (in the test).

- [ ] **Step 1: Failing tests:** `test_disabled_404_and_no_jinja_import`, `test_page_and_static_served_without_key`, `test_security_headers_on_every_ui_answer` (the exact P10-S11 values on `/ui`, a static file and a `/ui/run` answer), `test_not_in_openapi`, `test_vendored_htmx_checksum` (SHA-256 of the file equals `HTMX_SHA256`, recorded when vendored), `test_no_inline_script_or_style_or_hx_on` (Review Focus 3: every template parsed: each `<script>` has `src`, no `style=`, no `hx-on`), `test_htmx_config_meta` (the six P10-S11 values), `test_run_requires_hx_request_and_same_origin` (Review Focus 1: without `HX-Request` 403; `Origin: https://evil.example` 403; the page's own origin passes), `test_forward_passes_only_listed_headers` (the inner request sees `Authorization`, `X-LLM-Api-Key` and the outer trace id as `x-client-trace-id`, not `Cookie`), `test_ui_sets_no_cookie`.
- [ ] **Step 2:** FAIL. **Step 3:** vendor htmx, add `jinja2`, implement. **Step 4:** PASS; licence gate, mypy.
- [ ] **Step 5:** commit `feat: add the UI shell with vendored htmx, CSP and the in-process forward`.

### Task 7: UI operations

**Files:**
- Create: `src/text_extraction/ui/forms.py` (`OPERATIONS`, request builders), `ui/templates/forms/*.html` (one per operation), `ui/templates/result.html`, `ui/templates/job.html`
- Modify: `src/text_extraction/ui/routes.py` (`/ui/run/{op}`), `ui/static/ui.js` (key storage and headers, P10-S12)
- Test: `tests/ui/test_operations.py`, `tests/ui/test_playground_browser.py` (`@pytest.mark.browser`)

**Interfaces:**
- Consumes: Task 6's `forward`, `templates`, `require_htmx`; Task 2's route.
- Produces: `Operation(method: Literal["GET", "POST", "DELETE"], path: str, form: str)`, `OPERATIONS: Mapping[str, Operation]`, `build_body(op: str, form: Mapping[str, str]) -> dict[str, Any]` (raises `OptionsError` with a message for bad JSON or a duplicate key).

- [ ] **Step 1: Failing tests** (`TestClient`, a user key and `HX-Request: true`, the `FakeFetcher` pipeline): `test_every_public_route_has_an_operation` (every spec `APIRoute` matches one `OPERATIONS` entry), `test_extract_renders_envelope_escaped` (Review Focus 3: a page with `<script>alert(1)</script>` and the title `"><img src=x onerror=alert(1)>` → `&lt;script&gt;` present, `<img src=x` absent), `test_links_only_http` (a `javascript:` link is text, an https link an `<a>` with `rel="noopener noreferrer"`), `test_options_json_merged_and_invalid_json_message` (invalid JSON → message, no inner request), `test_inner_status_shown` (no key → 401 `unauthorized` in the fragment; a 422 lists `details`), `test_file_upload_forwarded`, `test_chunk_text`, `test_structured`, `test_batch_and_job_paging` (next button `hx-get` with `offset=2&limit=2`; a running job's fragment has the refresh trigger, a completed one not), `test_site_structure`, `test_screenshot_embeds_png` (a stored artifact → `<img src="data:image/png;base64,`), `test_cancel_button`, `test_info`, `test_curl_line_hides_key` (`$API_KEY`, never the key). Browser: `test_playground_in_chromium` (uvicorn on a free port in a thread, the app's fakeredis and a worker thread; Chromium types the key, runs an extract (title shown), a batch (Next pages the job), reloads (key still in `sessionStorage`), and has no console CSP errors and `context.cookies() == []`).
- [ ] **Step 2:** FAIL. **Step 3:** implement P10-S13. **Step 4:** PASS; `uv run pytest -m browser tests/ui`.
- [ ] **Step 5:** commit `feat: let the UI drive every public endpoint`.

### Task 8: Helm, compose and docs

**Files:**
- Modify: `deploy/helm/text-extraction/values.yaml` (`mcp`, `ui`, `config` keys), `templates/_helpers.tpl` and `templates/deployment.yaml` (API pods only), `templates/NOTES.txt`, `deploy/helm/text-extraction/README.md` (values table; ingress timeouts of at least `MCP_WAIT_SECONDS` + 70 s for `/mcp` and `/ui`), `deploy/helm/test.sh`, `.github/workflows/ci.yml` (lint with `--set mcp.enabled=true --set ui.enabled=true`), `docker-compose.yml`, `.env.example`, `README.md` (sections "MCP": `claude mcp add --transport http text-extraction https://<host>/mcp --header "Authorization: Bearer <key>"`, Claude Desktop through the `mcp-remote` bridge in `claude_desktop_config.json`, the tool table; "Playground"), `docs/architecture.md` (section "MCP and UI" with D1-D6 as decided and P10-S1 to S15; the `/mcp` and `/ui` rows; "Non-JSON content types": MCP answers JSON, no SSE; the warning `mcp_content_truncated`; the `DELETE` row), `docs/configuration.md`
- Test: `deploy/helm/test.sh`

- [ ] **Step 1: Failing checks in `deploy/helm/test.sh`:** by default the API pod has `MCP_ENABLED` and `UI_ENABLED` `"false"`; `--set mcp.enabled=true --set ui.enabled=true` → `"true"` on the API pod and on no worker; `--set config.MCP_ENABLED=true` alone stays `"false"`. Run `deploy/helm/test.sh` (or the docker `alpine/helm:3.19.0` command of `CLAUDE.md`) → FAIL.
- [ ] **Step 2:** implement P10-S14 and the docs. **Step 3:** `deploy/helm/test.sh` PASS; `helm lint --strict` with both toggles; `docker compose config` shows both variables on `api`.
- [ ] **Step 4:** commit `feat: switch MCP and the UI in Helm and compose; document them`.

### Task 9: Done-when, live check

**Files:**
- Create: `tests/mcp_server/test_done_when.py`, `tests/ui/test_done_when.py`
- Modify: `docs/packages.md` (status row, package 10 note)

- [ ] **Step 1: MCP done-when** (default suite, no network): the public app with `MCP_ENABLED=true` served by uvicorn on `127.0.0.1` in a thread (the lifespan runs), the real extract `Worker` in another thread on the app's fakeredis (as `tests/site/test_done_when.py`), pages and a site on pytest-httpserver (`FETCH_ALLOW_PRIVATE_NETWORKS=true`, `OUTBOUND_MIN_DELAY_MS=0`), and `fastmcp.Client(StreamableHttpTransport(url, headers={"Authorization": f"Bearer {USER_KEY}"}))`, the transport Claude Code uses:
  ```python
  assert {t.name for t in await client.list_tools()} == EXPECTED_TOOLS  # the 11 of P10-S5
  page = await client.call_tool("extract_url", {"url": PAGE_URL})
  assert page.structured_content["status"] == "success"
  assert TITLE in page.structured_content["data"]["content"]
  batch = await client.call_tool("batch_extract", {"urls": THREE_URLS})
  job_id = batch.structured_content["data"]["id"]
  done = await client.call_tool("get_job", {"job_id": job_id, "wait_seconds": 20, "limit": 2})
  assert done.structured_content["data"]["state"] == "completed"
  assert done.structured_content["data"]["next_offset"] == 2
  pdf = await client.call_tool("extract_file", {"content_base64": PDF_B64, "file_name": "a.pdf"})
  assert pdf.structured_content["data"]["document"]["kind"] == "pdf"
  site = await client.call_tool("site_structure", {"url": SITE_URL, "depth": 1})
  assert site.structured_content["data"]["kind"] == "site"
  refused = await client.call_tool(
      "extract_url", {"url": PAGE_URL, "options": {"compliance": "ignore"}}, raise_on_error=False
  )
  assert refused.is_error and refused.structured_content["error"]["code"] == "forbidden"
  ```
  plus `extract_structured`, `chunk`, `cancel_job` (then `get_job` → `not_found`), `service_info`, and a client with a wrong key failing to connect (401). Run: `uv run pytest tests/mcp_server/test_done_when.py -v` → PASS.
- [ ] **Step 2: UI done-when:** `tests/ui/test_done_when.py` drives every entry of `OPERATIONS` through `/ui/run/<op>` against the same setup (the worker thread completes the batch, the site job and a P1 job; `screenshot` and `artifact` with the browser off show the REST 400 and a stored artifact) and asserts each fragment shows the REST route's status and envelope; with `-m browser` the Chromium test of Task 7 runs as the visible check. Run: `uv run pytest tests/ui -v` and `uv run pytest -m browser tests/ui -v` → PASS.
- [ ] **Step 3: Live check (done-when evidence for "usable from Claude"):** the service with Valkey (`docker compose up` with `MCP_ENABLED` and `UI_ENABLED` on; in the sandbox if Docker or a Valkey is available, else on Jan's PC); `claude mcp add --transport http text-extraction http://localhost:8080/mcp --header "Authorization: Bearer $API_KEY"`; in Claude Code: list the tools, extract a WLO page, start a batch of three URLs and read it with `get_job`, map a site at depth 1, cancel a job; open `http://localhost:8080/ui` and run each tab once. The transcript summary (tools used, answers, durations) goes into the PR.
- [ ] **Step 4:** label the PR `live`: `wlo_200` auto stays ≥ 80 % (Task 1 moved the route bodies).
- [ ] **Step 5:** `docs/packages.md`; commit `test: add the package 10 done-when tests and the live check`.

### Final: review and hand-over

- [ ] `requesting-code-review` over the whole branch (Review Focus above as the brief, plus: no secret in a log, tool argument or HTML; Task 1 changed no REST answer), fixes via `receiving-code-review`.
- [ ] `verification-before-completion`: `uv run pytest`, `uv run pytest -m browser tests/ui`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, the licence gate, `deploy/helm/test.sh` and helm lint, both done-when tests and the live summary; done-when met.
- [ ] `finishing-a-development-branch`: PR, squash merge once green (Jan's standing permission), progress memory updated.
