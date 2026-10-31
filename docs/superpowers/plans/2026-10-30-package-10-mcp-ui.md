# Package 10: MCP and UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An MCP client (Claude Code, Claude Desktop through a bridge) connects to `https://<host>/mcp` with its API key and gets one tool per public capability (extract a URL or a file, screenshot, structured output, chunks, batch, site structure, job status and paged results, the job's screenshot, cancel). A person opens `https://<host>/ui`, logs in with an API key, and uses a playground for every public endpoint: a single page as text or Markdown with its metadata (also in structured form), image previews and their descriptions; a site's structure as a tree whose sections or pages are selected for a batch; batches from a CSV or JSON file; and live progress, with what happened to each item. Both are switched by `MCP_ENABLED` and `UI_ENABLED` (Helm `mcp.enabled`, `ui.enabled`).

**Architecture:**
- **Route cores, called from several doors (P10-S1).** Each public route's body moves unchanged into a function that takes the app's `State` instead of the `Request`; the route becomes a one-line wrapper. MCP tools, and the UI views that combine many reads, call these cores in-process, so they run exactly the checks, limits, SSRF guard, compliance and envelope of the route, with no HTTP loopback.
- **MCP in the API pods (D1 a).** FastMCP's streamable HTTP app, stateless and with JSON answers (no session pinned to a pod, no SSE), served at exactly `/mcp` on port 8080 behind a gate that authenticates every HTTP request as REST does and refuses foreign browser origins. A FastMCP middleware counts each `tools/call` against the caller's REST rate-limit bucket, logs it and counts it in Prometheus. Long work answers with a job id (`MCP_WAIT_SECONDS`), and `get_job` pages and waits.
- **UI as a server-rendered REST client (D5 a).** Jinja2 pages with vendored htmx (no CDN, no Node build). A form that is one REST call posts to `/ui/run/<op>`, which forwards it through httpx's ASGI transport to the real `/v1/...` route with the browser's key headers and renders the answer as escaped HTML. Views that combine many reads (progress, the site tree, image previews) call the route cores with one rate-limit hit per refresh. The login is the API key itself, kept in the tab's `sessionStorage` and sent only as a header (D6 a). A strict CSP, no cookies and an `HX-Request` and same-origin check close CSRF and script injection. Client JavaScript is one small file: key handling and the tree's checkboxes.
- **What happened to an item (D8 a).** Workers record each claim of an item (pool, claim number, time) with its job, and `GET /v1/jobs/{id}` shows it as `items[].run`. The UI's timeline joins that with the result's `attempts`, `mode_used`, `timings`, `upstream_status`, `cache`, `compliance` and `warnings`.

**Tech Stack:** FastMCP (Apache-2.0, with the MIT `mcp` SDK), Jinja2 (BSD-3-Clause), htmx 2.0.11 (0BSD, vendored), markdown-it-py (already a dependency), FastAPI/Starlette, httpx `ASGITransport`, the existing auth, rate limiter, route cores, `JobStore`, the worker loop, the vision `ImageFetcher`, the Prometheus client and structlog. Tests: pytest, fakeredis, pytest-httpserver, `fastmcp.Client` over real HTTP (uvicorn in a thread), FastAPI `TestClient` with raw JSON-RPC, Playwright Chromium (`-m browser`).

**Spec:**
- `docs/packages.md` row 10: FastMCP at `/mcp`, htmx playground at `/ui`, both toggled by env and Helm. Done when MCP tools are usable from Claude and the UI drives every endpoint.
- Jan's UI requirements of 2026-10-30, in the Decisions section below.
- `docs/architecture.md`: the overview diagram (`8080: REST /v1, /mcp, /ui`), the API rows `/mcp` ("MCP tools (optional, `MCP_ENABLED`)", streamable HTTP) and `/ui` ("Playground (optional, `UI_ENABLED`), htmx, no Node build"), "Non-JSON content types … SSE (MCP)", "Auth", "Result envelope and errors", "Sync-to-job (P1)", "Jobs and the fair queue", "Site structure", "Image descriptions (issue #20)".
- `CLAUDE.md` rules 1, 2 (ports, stateless pods, env vars with defaults, logs and trace headers, jobs over ~30 s, Helm toggles `mcp` and `ui`), 3 (`AUTH_MODE`, the LLM key header wins), 4, 5 (SSRF guard on every fetch, byte and time limits), 6, 7.
- Decisions D1-D8 below: Jan approved the plan with the recommended option (a) for each on 2026-10-30, 18:15 UTC.

## Global Constraints

- English only (code, templates, UI copy, tool descriptions). Python 3.13. No GPL or AGPL dependency; `uv run python scripts/check_licenses.py --lock uv.lock` stays green.
- New base dependencies: `fastmcp` and `jinja2` only, each pinned to its newest release at least 14 days old on the day it is added (ruling R16; on 2026-10-30 that is `fastmcp==4.1.0` and `jinja2==3.1.6`). Transitive packages younger than 14 days are held back with `exclude-newer-package` in `pyproject.toml`. htmx 2.0.11 is vendored as a static file. No CDN, no Node build, no other JavaScript library; the UI's own JavaScript is one file, `ui.js`.
- `/mcp` and `/ui` live on the public port 8080 only, never on 9090, and appear in no OpenAPI spec. The public API changes only by `DELETE /v1/jobs/{id}` (D4 a) and the additive field `items[].run` of `GET /v1/jobs/{id}` (D8 a).
- Stateless pods: no MCP session state, no UI session; MCP and the UI store nothing beyond what the route cores store (and D8's claim records with the job).
- Every new setting is an env var with a documented default in `docs/configuration.md`, `.env.example` and the Helm chart (`config`, or the toggle), added in the task that adds the setting. No new secret.
- MCP and the UI never skip a check of the REST route. They use the same auth and roles (`AUTH_MODE` `none|api_key`, user and admin keys), the same rate-limit bucket, SSRF guard, compliance modes, body limits and result envelope. Every fetch the UI makes itself (image previews) goes through the SSRF guard, the compliance checks and the outbound gate.
- Secrets (the API key, `X-LLM-Api-Key`, `X-ASR-Api-Key`) arrive only as HTTP headers. They are never tool arguments or form fields, never logged, never put in a URL or cookie, and never rendered into HTML.
- Logs and metric labels carry tool and operation names, outcomes, counts and job ids only: never tool arguments, URLs, file contents, content or keys.
- Off by default (`MCP_ENABLED=false`, `UI_ENABLED=false`). With both off the app is as before, and `fastmcp` and `jinja2` are not imported.
- Existing REST answers do not change: the whole existing suite passes unchanged after Task 1.
- Not in this package: OAuth for MCP (a B-API concern), MCP resources, prompts, sampling and the stdio transport, server-sent progress notifications, a UI password apart from the API keys (P10-S13), a UI session or cookie, a UI language other than English, a Node build, CSV export of results, resizing of preview images.

## Decisions of Jan (2026-10-30, 18:15 UTC: plan approved, recommended options confirmed)

**Jan's UI requirements (2026-10-30, translated from German).** "For the UI, please note: UI can be switched on and off, or protected with a password. Perhaps the same password can be used that can be set for using the API. Besides extracting single pages and showing the result as txt or Markdown by choice, there should be an option to read site structures based on sitemaps and links, and to select or deselect site sections or pages in a tree for a batch extraction. Batch files such as CSV or JSON for processing URLs would be good. Progress displays for batch jobs. The option to also extract metadata in structured form. Plan for showing preview images and their description (vision). One should see what is happening, e.g. retries and process progress, if these are returned." Where each is met: switch and password P10-S12, P10-S13 (the password is an API key; D6); text or Markdown and structured metadata P10-S15; site tree P10-S20; batch files P10-S19; progress P10-S18; previews and descriptions P10-S16 (D7); what is happening P10-S11, P10-S17 (D8).

D1-D6 are open as proposed with the first plan; D7 and D8 are new with this revision.

- **D1, where MCP runs.** (a, recommended) In the API pods: FastMCP at `/mcp` on port 8080, `MCP_ENABLED` (Helm `mcp.enabled`), stateless; one image, one ingress, the API's auth, limits, scaling and probes. (b) A Deployment of its own from the API image (an entry point serving only `/mcp`) with its own HPA, Service and ingress path; the same in-process cores. (c) A separate thin MCP server that calls the REST API over HTTP with the caller's key.
- **D2, tool arguments.** (a, recommended) Lean: each tool lists the few fields agents use (`url`, `format`, `mode`, `wait_seconds`, …) plus one `options` object for any other field of the REST body, validated by the REST model. `tools/list` then stays a few KB instead of about 85 KB of JSON schema (`ExtractRequest`'s alone is 22 KB, and four tools share it); every client puts that list into the model's context. (b) The full REST body schema as each tool's input. (c) Lean only: the other options cannot be reached over MCP.
- **D3, files over MCP.** (a, recommended) `extract_file` takes the file as base64 (a `data:` URL prefix allowed) within the upload route's limit (`DOCUMENT_MAX_BYTES`, the media limit while uploads are transcribed), and `/mcp`'s body limit is 4/3 of it plus 64 KiB. Programmatic clients can send real files; a file at a URL goes through `extract_url` as today. (b) Base64 with a lower MCP-only cap (`MCP_FILE_MAX_BYTES`, 5 MB), since a model rarely writes more. (c) No upload tool: files by URL only.
- **D4, cancelling jobs.** (a, recommended) A new route `DELETE /v1/jobs/{id}` (the owner or an admin, else 404). It deletes the job with all its keys and frees its open-job slot. Workers drop the job's queued items as for an expired job, and items in flight finish without being stored. The MCP tool `cancel_job` and a UI button call it. (b) No cancel: jobs run out and expire after `JOB_TTL_SECONDS`. (c) A soft cancel: a new job state `cancelled` that workers check before each item, keeping the results so far (a Lua and model change).
- **D5, how the UI reaches the API.** (a, recommended) Server-rendered. A form that is one REST call posts to `/ui/run/<op>`, which forwards it in-process (httpx `ASGITransport`, no network) to the real `/v1/...` route with the browser's key headers. Every REST check (body limits, validation, auth, rate limit, 202) runs exactly as for any client. Views that combine many reads (progress refresh, site tree, previews) call the route cores with one rate-limit hit per view. (b) Every `/ui/run` route calls the cores directly, as MCP does; the HTTP layer's body limits and validation answers are not exercised. (c) The browser calls `/v1/...` with `fetch` and renders the JSON in its own JavaScript (much more client code, htmx mostly unused).
- **D6, where the UI keeps the API key.** Jan: the UI's password may be the API's own (requirement 1); all options keep it so. (a, recommended) The tab's `sessionStorage`. It survives a reload and goes away with the tab. The UI's own script sends it as the `Authorization` header and never as a cookie (so there is no CSRF), and the CSP forbids any other script. (b) Memory only: the key is typed again after every reload. (c) `localStorage` behind a "remember" box: it survives browser restarts, and any script on the origin can read it later.
- **D7, how preview images reach the browser (new).** (a, recommended) A server-side preview. On a "Load previews" click, the server fetches the result's images through the vision `ImageFetcher`: SSRF guard on the URL and every redirect, the caller's compliance mode, the host's outbound turn, the service's user agent, types sniffed from the bytes (PNG, JPEG, GIF, WebP) and at most `UI_IMAGE_MAX_BYTES` each. It inlines them as `data:` URIs. Third-party sites never see the viewer, and the CSP stays `img-src 'self' data:`. (b) The browser loads the images directly behind a "Show remote images" switch that is off by default (CSP `img-src 'self' data: https:`, `referrerpolicy="no-referrer"`). This uses no server fetch, but each image's host sees the viewer's IP address. (c) No previews: alt texts and descriptions only.
- **D8, showing retries of job items (new).** Today `GET /v1/jobs/{id}` shows an item as `pending` or `done` with its result. The result's `attempts` counts fetches within one run. Claims, lost leases, deferrals and moves to another pool live only in the queue's keys. (a, recommended) Each claim is recorded with the job. The worker loop that every pool shares writes the item's pool, claim number and time into a new job hash `job:{<id>}:runs` (one Lua call on the job's hash tag per claim, about 40 bytes per item, expiring with the job), and `JobItem` gains `run: ItemRun | None`. The UI and `get_job` then show "running in browser, claim 2". This costs one Valkey round trip per claimed item. (b) Show only what exists: the item state, the result's `attempts`, `error.retryable` and the warnings; a retry is not visible until it ends. (c) Read the leases from the queue's keys when a job is viewed. That needs no worker change but means reads across hash tags and a scan per page.

## Rulings in this plan

- **P10-S1, route cores.** Shared code takes `state: starlette.datastructures.State` (the app's `request.app.state`) wherever it took `request` only to reach the state: `respond`, `answer_upload`, `open_job`, `discard_job`. The bodies of the routes move unchanged into cores, and each route calls its core:
  - `auth.principal_for(settings, authorization: str | None) -> Principal` (401 `ServiceError` as today; `authenticate` calls it); `ratelimit.check_rate(state, principal) -> None` (429 with `Retry-After` / 503 as today; `rate_limited` calls it); `jobs_api.check_jobs_enabled(settings) -> None` (503).
  - `screenshot_api.run_screenshot(state, principal, body: ScreenshotRequest) -> Envelope[ScreenshotResult] | JSONResponse` and `screenshot_api.read_artifact(state, principal, job_id: str, name: str) -> bytes` (404 as today).
  - `structured_api.run_structured(state, principal, body: StructuredRequest, *, asr_key, llm_key)` and `structured_api.run_chunk(state, principal, body: ChunkRequest, *, asr_key, llm_key)`.
  - `file_api.run_upload(state, principal, form: FileForm, data: bytes, name: str | None, content_type: str | None, *, asr_header: str | None, llm_header: str | None) -> Envelope[ExtractResult] | Response`: the route's checks, the size check of `upload_file_limit`, the ASR key check, then `answer_upload`. The route keeps checking before it reads the bytes.
  - `jobs_api.start_batch(state, principal, body: BatchRequest, *, asr_key, llm_key) -> Envelope[JobAccepted]` and `jobs_api.read_job(state, principal, job_id: str, offset: int, limit: int) -> Envelope[JobView]`.
  - `site_api.start_site(state, principal, body: SiteStructureRequest) -> Envelope[JobAccepted]` and `app.service_info(principal) -> Envelope[InfoData]`.

  `asr_key` and `llm_key` are the raw header values; the cores apply `asr_key_of` / `llm_key_of` as the routes did.
- **P10-S2, the MCP endpoint** (`mcp_server/server.py`). `FastMCP(name="text-extraction", version=__version__, instructions=SERVER_INSTRUCTIONS, mask_error_details=True)`, served by `http_app(path="/mcp", stateless_http=True, json_response=True, host_origin_protection=False)`; our gate checks origins instead (P10-S3).
  - It is added to the public app as a Starlette `Route("/mcp", endpoint=McpGate(...), methods=["GET", "POST", "DELETE"])`. The endpoint is exactly `/mcp` (no redirect; `/mcp/` is 404), and FastAPI leaves it out of the spec. `GET /mcp` answers 405 (stateless: no stream).
  - The public lifespan enters the MCP app's lifespan. `fastmcp` is imported only inside `build_mcp`, which runs only with `MCP_ENABLED`.
  - Body limit of `/mcp`: `mcp_body_limit(settings) = ceil(upload_file_limit(settings) / 3) * 4 + 65_536` (D3 a).
  - `SERVER_INSTRUCTIONS` says: use `extract_url` for pages and documents at a URL; a result whose `data` has a `status_url` is a job, so poll `get_job` with `wait_seconds`; `options` takes any field of the REST body (`/docs`).
- **P10-S3, the gate** (`mcp_server/gate.py`, pure ASGI). For every HTTP request to `/mcp`, in order:
  1. An `Origin` header whose value is not in `MCP_ALLOWED_ORIGINS` (exact match on scheme and host) gets a 403 envelope `forbidden` ("This origin may not call the MCP endpoint.").
  2. `principal_for` runs; on failure the answer is a 401 envelope with `WWW-Authenticate: Bearer` and the REST message.
  3. The principal is stored in `scope["state"]["mcp_principal"]`, its name in `scope["state"]["principal"]` (for the access line, as `_record_principal` does), and the name is bound to the log context.

  `call_principal() -> Principal` reads it through FastMCP's `get_http_request()`; a missing principal is `internal_error`.
- **P10-S4, each tool call** (`mcp_server/calls.py`, a FastMCP `Middleware.on_call_tool`). In order: `check_rate(state, principal)` (one hit per `tools/call`; `initialize`, `tools/list` and notifications are free), then the tool, then the log line `mcp_tool_call` (`tool`, `outcome`, `duration_ms`, and `job_id` when a job was answered) and the metrics (P10-S22). Errors become results and never reach the client as exceptions:
  - `ServiceError` → its code, message and `retryable` (P10-S6).
  - `RequestValidationError` and pydantic `ValidationError` → `invalid_request`, "The request is invalid.", with `details` as REST gives them (`loc`, `msg`, never the input).
  - Any other exception → `internal_error`, "Internal server error.", logged with its traceback.
- **P10-S5, the tools** (D2 a, `mcp_server/tools_extract.py`, `mcp_server/tools_jobs.py`). Every tool is a `ModelTool`, a `fastmcp.tools.Tool` subclass whose `parameters` is its args model's JSON schema. A call validates the args, merges `options` under them (a key given in both is `invalid_request`, "<key> is given twice."), validates the merged body with the REST model (extra fields are refused, as in REST), and calls the core.

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

  - `options`, `structured` and `chunking` are `{"type": "object"}` in the schema; the REST model validates them. `options` is described as "Any further field of the REST body of <route> (see /docs), e.g. {\"pii\": {\"mode\": \"mask\"}}".
  - `max_chars` defaults to `MCP_MAX_CONTENT_CHARS` (1-`MCP_MAX_CONTENT_CHARS`).
  - `extract_file` (D3 a): a leading `data:<type>;base64,` is stripped (its type is used when `content_type` is unset) and whitespace is ignored. Anything else that is not base64 is `invalid_request` ("content_base64 is not valid base64."), and decoded bytes over `upload_file_limit(settings)` are `invalid_request` ("The file is larger than <n> bytes."). The rest of its args and `options` form a `FileForm`; `file_name` passes `sanitize_file_name`.
- **P10-S6, tool results** (`mcp_server/results.py`).
  - `structuredContent` is the route's envelope as JSON, a 202's `Envelope[JobAccepted]` included. There is no `outputSchema`; it would double the size of `tools/list`.
  - `isError` is true only where REST answers 4xx or 5xx. A target's `failed` (REST 200) is a normal result.
  - `content` holds one `TextContent` with the same JSON. A one-line `TextContent` hint comes first when the caller has something to do:
    - a job: "Continues as job <id> (<kind>): call get_job with job_id=<id> and wait_seconds up to <MCP_WAIT_SECONDS>.";
    - a cut content (below);
    - an image left out (below).
  - Content cut: `data.content` (each item's in `get_job`) becomes `content[content_offset : content_offset + share]`, where `share = max_chars` for one result and `max_chars // len(items)` for a job page. A cut content gets the warning `mcp_content_truncated` and the hint "Cut at <n> of <total> characters; call again with content_offset=<next>." No other field is cut.
  - `screenshot_url` and `get_job_screenshot` add an `ImageContent` (PNG) when the artifact is at most `MCP_IMAGE_MAX_BYTES`, else the hint "The screenshot (<n> bytes) is over MCP_IMAGE_MAX_BYTES; download it from <status_url>."
- **P10-S7, long work.** `extract_url`, `extract_file`, `screenshot_url`, `extract_structured` and `chunk` pass `wait_seconds = MCP_WAIT_SECONDS` when the caller gives none, so a call ends within that time (plus the route's own hand-over time) with a result or a job. `JOBS_ENABLED=false` ignores it, as REST does. `batch_extract` and `site_structure` always answer a job. `get_job` with `wait_seconds` reads the job again once a second until it is `completed` or the time is up, then answers the page; the call counts as one rate-limit hit.
- **P10-S8, headers of a tool call.** `X-LLM-Api-Key` and `X-ASR-Api-Key` are read from the HTTP request of the call (`get_http_request().headers`) and passed to the core as the routes pass them, so the caller's LLM key wins over `LLM_API_KEY` (rule 3). `TraceMiddleware` handles `x-client-trace-id` and B3 around `/mcp`: the access line, every log line of the call and the envelope's `trace_id` carry them.
- **P10-S9, settings.** MCP: `MCP_ENABLED` (false), `MCP_ALLOWED_ORIGINS` (empty: no browser origin; a comma list of `scheme://host[:port]`), `MCP_WAIT_SECONDS` (25, 0-60), `MCP_MAX_CONTENT_CHARS` (50,000, 1,000-10,000,000), `MCP_IMAGE_MAX_BYTES` (5,000,000, 0-20,000,000; 0 never attaches). UI: `UI_ENABLED` (false), `UI_TREE_MAX_URLS` (5,000, 100-50,000), `UI_BATCH_FILE_MAX_BYTES` (10,485,760, 1,024-104,857,600), `UI_BATCH_FILE_MAX_ROWS` (100,000, 10-1,000,000), `UI_IMAGE_MAX_BYTES` (1,000,000, 10,000-20,000,000).
- **P10-S10, `DELETE /v1/jobs/{id}`** (D4 a; `jobs_api.delete_job(state, principal, job_id) -> Envelope[JobDeleted]`, `JobDeleted(id: str, kind: JobKind)`).
  - The route needs the user role, is rate limited and calls `check_jobs_enabled`. `visible_job` answers 404 for an unknown, expired or foreign job.
  - It runs `JobStore.delete`, then removes the job from its owner's open set (`_discard` with the record's owner, so an admin's delete frees the owner's slot). It logs `job_deleted` (`job_id`, `kind`) and answers 200 `success`.
  - Items of the job that a worker claims later are dropped (`job_item_dropped`, reason `job_gone`). A P1 task or an item in flight that finishes later stores nothing (`JobLost`).
- **P10-S11, item runs** (D8 a; `jobs.py`, `worker.py`, `job_models.py`).
  - `JobStore.record_claim(job_id: str, index: int, pool: str, attempts: int) -> None` is one Lua script on `meta` and `runs`. When `meta` exists, it sets `HSET runs <index> "<attempts>|<pool>|<epoch ms>"` and `PEXPIRE runs` to `PTTL meta`; otherwise it does nothing.
  - `Worker` calls it after every claim, before the item runs. `attempts` is the claim number of the queue item (1 on the first claim, more after a lost lease or a deferral), and `pool` is the worker's pool. A Valkey error is logged and ignored, so the item still runs.
  - `JobStore.items` reads the page's fields of `runs` in the same pipeline. `JobItem.run: ItemRun | None = None` with `ItemRun(pool: str, attempts: int, claimed_at: datetime)`; it is None before the first claim, for a P1 job's item (it runs in the API pod) and while older workers run.
  - `_KINDS` gains `runs`, so `delete` and `shorten` remove it. Cost: one round trip per claimed item and about 40 bytes per item while the job lives.
- **P10-S12, the UI shell** (`ui/`).
  - `GET /ui` renders `index.html`. `/ui/static` is a `StaticFiles` mount with `htmx-2.0.11.min.js`, `ui.js` and `ui.css`. Pages and static files need no key: they hold nothing but forms.
  - Jinja2 runs with autoescape on. Templates have no inline `<script>`, no `style` attribute and no `hx-on`.
  - `<meta name="htmx-config">` sets `allowEval: false`, `allowScriptTags: false`, `includeIndicatorStyles: false`, `selfRequestsOnly: true`, `historyEnabled: false` and `historyCacheSize: 0`.
  - Every `/ui` answer carries `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'`, plus `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `Cross-Origin-Opener-Policy: same-origin`, `Cross-Origin-Resource-Policy: same-origin` and `Permissions-Policy: camera=(), microphone=(), geolocation=()`. `Cache-Control` is `no-store`, except `public, max-age=3600` for static files.
  - Routes have `include_in_schema=False`. `UI_ENABLED=false` leaves `/ui` 404 and `jinja2` unimported.
- **P10-S13, access** (Jan's requirement 1; D6 a).
  - Under `AUTH_MODE=api_key`, `/ui` shows only a login box (API key, and optionally the LLM and ASR keys). `ui.js` keeps the keys in `sessionStorage` (`te.apiKey`, `te.llmKey`, `te.asrKey`) and posts to `/ui/run/login`. That route forwards `GET /v1/info`: a 200 swaps in the application (tabs, the principal's name and role, a "Log out" button that clears the keys and reloads), and a 401 shows "This key is not valid." with nothing else rendered.
  - On a reload, `ui.js` repeats the login with the stored key. The login gate is only a convenience: every forwarded call still carries and checks the key.
  - Under `AUTH_MODE=none` the application renders at once, under a banner: "This service runs without authentication: anyone who can reach it can use the API and this UI." There is no UI-only password; an open UI goes with an open API. `docs/configuration.md` and the Helm README recommend `ui.enabled=false` or `AUTH_MODE=api_key` for shared deployments, and NOTES.txt warns when `ui.enabled` is true while `config.AUTH_MODE` is `none`.
  - On `htmx:configRequest` for paths under `/ui/run/`, `ui.js` sets `Authorization: Bearer <key>`, and `X-LLM-Api-Key` and `X-ASR-Api-Key` when they are set. The key inputs have no `name` and `autocomplete="off"`.
  - The service never sets a cookie under `/ui`. `/ui/run/*` answers 403 (plain text) without `HX-Request: true`, and for an `Origin` whose host and port differ from the `Host` header. The service has no CORS middleware, so a cross-site page cannot send that header.
- **P10-S14, forwarding and reading** (D5 a; `ui/forward.py`, `ui/forms.py`).
  - `OPERATIONS: Mapping[str, Operation]` maps each forwarded op to its REST method and path: `login` and `info`, `extract`, `file`, `screenshot`, `structured`, `chunk`, `batch`, `site-structure`, `job`, `artifact` and `cancel`.
  - A form's fields, plus an "Options (JSON)" textarea merged under them, build the REST body. Invalid JSON or a key given twice gives an error fragment, and nothing is forwarded.
  - `forward(request, method, path, *, json=None, data=None, files=None, params=None) -> Forwarded(status: int, body: dict[str, Any] | None, content: bytes, content_type: str)` sends the request through `httpx.AsyncClient(transport=httpx.ASGITransport(app=request.app), base_url="http://ui.internal", timeout=None)`. It passes only `Authorization`, `X-LLM-Api-Key`, `X-ASR-Api-Key`, the B3 headers, and `x-client-trace-id` set to the outer request's trace id.
  - `/ui/run/*` answers 200 with a fragment for every forwarded outcome: the HTTP status, the envelope's status and error (with the 422 `details`), the raw JSON in `<details>`, and the curl line with `$API_KEY` in place of the key.
  - The views that combine reads (`progress`, `site-tree`, `images`, `batch-file`) authenticate with `principal_for`, count one `check_rate` hit per request, and call the cores (`read_job`) or the `ImageFetcher` directly.
  - Body limits: `/ui/run/file` the upload limit, `/ui/run/chunk` the text limit, `/ui/run/batch-file` `UI_BATCH_FILE_MAX_BYTES` + 64 KiB, `/ui/run/batch-urls` `BATCH_MAX_OPEN_PER_CLIENT` × the batch limit, every other op the default. The forms' default `wait_seconds` is 25.
- **P10-S15, single extraction** (Jan's requirements 2 and 5). The extract form has:
  - `url`;
  - the format as a radio choice of Markdown (default) and Text (REST `markdown` and `text`; `html` only through Options);
  - `mode`;
  - "Metadata" (REST `metadata`, on by default);
  - "Metadata, structured" (off). It sends `structured: {"strategy": "metadata", "schema": UI_METADATA_SCHEMA}`. The `metadata` strategy reads JSON-LD, microdata, RDFa, OpenGraph and meta tags by field name and needs a schema, so `UI_METADATA_SCHEMA` (`ui/forms.py`) is an object of `name`, `headline`, `description`, `author` (array), `datePublished`, `dateModified`, `license`, `inLanguage`, `keywords` (array), `publisher`, `educationalLevel` (array), `learningResourceType` (array) and `isAccessibleForFree` (boolean). The answer goes to `POST /v1/extract` with that `structured`;
  - "Describe images" (off): `describe_images: {"max_images": <1-20, default 5>}`. The REST answers apply: 400 without the LLM, 422 with `pii` or without metadata.

  The result shows:
  - the content as source in `<pre>`. Markdown can also be shown rendered, through markdown-it-py with `html: False`, links only for http(s) and images rendered as their alt text;
  - a "Download .md" or "Download .txt" link (`data:` URI with `download`);
  - the title, language and word count;
  - the `metadata` fields (licences, LRMI, authors, dates, canonical);
  - `structured.values`, `sources` and `missing` as a table;
  - the images (P10-S16), the timeline (P10-S17) and the links (as `<a rel="noopener noreferrer">` for http(s) only).

  The file, screenshot, structured, chunk and info forms render the same result view. A screenshot answer, and a `screenshot` job's item, shows the PNG from the artifact route as a `data:image/png;base64,` image.
- **P10-S16, images and descriptions** (Jan's requirement 6; D7 a).
  - The result lists `metadata.images` (page images and `og:image` / `twitter:image`, at most 20) with each image's alt text and, when `describe_images` ran, its `description`, plus the `image_descriptions` report (asked, described, skipped, failed).
  - "Load previews" posts the image URLs of the result to `/ui/run/images`. That view authenticates, counts one rate-limit hit, and fetches each URL (at most 20, at most 4 at once) through `ImageFetcher.get(url, mode, timeout=10, max_bytes=UI_IMAGE_MAX_BYTES)`. `mode` is the caller's resolved compliance mode, and `ImageFetcher.get` gains the keyword `max_bytes`, which defaults to `LLM_VISION_MAX_IMAGE_BYTES` (the vision step's behaviour is unchanged).
  - A fetched image becomes `<img src="data:<sniffed type>;base64,…" alt="<escaped alt>">`. A skipped one shows its reason (`blocked`, `refused`, `too_large`, `unsupported`, `http_error`, `error`, `busy`) instead.
- **P10-S17, what is happening** (Jan's requirement 7). Each item, and each single result, has a timeline built from fields only, in this order:
  1. the claims (D8: the pool, claim *n*, the time);
  2. compliance (decision, `compliance_ms`);
  3. fetch (`fetch_ms`, `attempts`, `upstream_status`, `final_url` when it differs, `cache`);
  4. render (`render_ms`, `unblocked`) when `mode_used` is `browser`, with the note "escalated from fast" for an `auto` request;
  5. convert (`convert_ms`, `content_type`, the document's converter, pages and OCR);
  6. PII (`pii_ms`, the entity count);
  7. LLM (`llm_ms`);
  8. the outcome (status, `error.code`, "retryable" when `error.retryable`, `total_ms`).

  Warnings are listed with a one-line text from `WARNING_TEXTS` (`ui/texts.py`, the warnings of `docs/architecture.md`); an unknown warning shows its name.
- **P10-S18, progress** (Jan's requirement 4). The job view (`/ui/run/progress?id=…[&id=…]`, one or more jobs, reading through `read_job`) shows per job:
  - the kind and state;
  - a `<progress value=done max=total>` with done/total and the success, partial and failed counts. For a `site` job the note says the total grows while it crawls;
  - the elapsed time;
  - a page of 20 items (index, URL, "queued", "running in <pool>, claim <n>" or the status badge, error code and a link that opens the item's timeline and result), with previous and next buttons;
  - for a `site` job, a "Show as tree" button.

  While any job shown is not `completed`, the fragment refreshes itself with `hx-trigger="load delay:3s"`. Each refresh is one rate-limit hit, whatever the number of jobs. A "Cancel" button calls `cancel` (D4 a).
- **P10-S19, batches from lists and files** (Jan's requirement 3; `ui/url_lists.py`, parsed on the server).
  - `parse_url_file(data: bytes, *, max_rows: int) -> UrlList`. The content must be UTF-8 (a BOM is dropped; anything else is "The file is not UTF-8.").
    - A first non-blank character of `[` means JSON: an array of strings or of objects with a string `url` (other keys ignored); anything else is "Expected a JSON array of URLs or of objects with url.".
    - Otherwise it is CSV, with the dialect sniffed among `,`, `;` and tab. A header row with a `url` column (any case) selects that column; else the first column, and a first row whose value is not http(s) is treated as a header.
    - Each value is stripped. It must be `http` or `https` with a host and at most `MAX_URL_LENGTH` characters, else it is reported as `(row, reason)`. Exact duplicates after stripping are dropped and counted.
    - More than `max_rows` rows (`UI_BATCH_FILE_MAX_ROWS`) is refused before anything else ("The file has more than <n> rows."). JSON nested too deep or invalid gives "The file is not valid JSON." The CSV field size is limited to `MAX_URL_LENGTH` + 1, larger is "A field is too long.".
  - `UrlList(urls: list[str], invalid: list[tuple[int, str]], duplicates: int, rows: int)`.
  - `/ui/run/batch-file` (multipart, at most `UI_BATCH_FILE_MAX_BYTES`, else 413) shows a preview: the counts, the first 50 URLs, the first 20 problems (escaped), and the URL list in a read-only textarea, with the format and options of the batch form and a "Start batch" button.
  - `/ui/run/batch-urls` (also used by the batch form's textarea and by the tree) takes `urls` (lines or repeated fields), checks them again with `parse_url_lines`, and splits them into `ceil(n / BATCH_MAX_URLS)` batches. When that is more than `BATCH_MAX_OPEN_PER_CLIENT`, nothing is sent: "Select at most <BATCH_MAX_OPEN_PER_CLIENT × BATCH_MAX_URLS> URLs at once.". Otherwise the batches are forwarded one after another (`POST /v1/batch`). A refusal of a later part (429 when other jobs are open) is shown beside the jobs already started, which are not undone. The answer is the progress view of the started jobs.
  - Values such as `=HYPERLINK(…)` or `@cmd` are not URLs and are reported as invalid. The UI writes no CSV, so nothing a cell holds can reach a spreadsheet through it.
- **P10-S20, the site tree** (Jan's requirement 2; `ui/site_tree.py`, `ui.js`).
  - **Hierarchy by URL path segments.** Jan asked for site sections, which are URL directories to editors. Sitemap entries have no parent page but slot into a path tree. The link graph's `parent` is the first page that happened to find a URL (discovery order depends on the crawl and its timing), so a parent tree would move between runs.
  - Each page node shows its link `depth`, its crawl status and how it was found (crawled, sitemap, linked only).
  - `build_tree(items: Iterable[JobItem], *, max_urls: int) -> SiteTree` is pure. It merges:
    1. page items (`site.role == "page"`: URL, title, status, depth);
    2. the entries of `sitemap` and `sitemap_part` items (`loc`, `lastmod`);
    3. links of crawled pages with `crawl` `depth` or `max_pages` ("linked only").

    URLs are merged by `seen_key` (package 9); pages come first, and each source counts toward `max_urls` (`UI_TREE_MAX_URLS`). Over the cap, the tree is cut with "<n> more URLs not shown (UI_TREE_MAX_URLS)".
  - Roots are hosts (`www.` dropped as in package 9). Children are path segments in name order; a query string makes its own leaf.
  - `/ui/run/site-tree?id=` reads the job through `read_job` in pages of 20 items (so at most 20 items of up to `JOB_ITEM_MAX_BYTES` each are in memory at once) until `next_offset` is null. A site job holds at most `SITE_MAX_PAGES` + `SITE_MAX_SITEMAP_FILES` + its sitemap parts items. Only the tree's fields are kept. It is offered once the job is `completed`, and as "Show tree now" (a snapshot) while it runs.
  - The tree renders as nested `<ul>` with one checkbox per node. Only page and entry checkboxes carry `name="url"` and `value`; a section's checkbox carries none.
  - `ui.js` (about 60 lines) does three things: checking a section checks its subtree, a parent shows `indeterminate` when part of its subtree is checked, and a counter shows "<n> selected". "Select all" and "Select none" work the same way.
  - "Extract selected" posts the checked URLs with the chosen format and options to `/ui/run/batch-urls` (P10-S19's split rule and messages).
- **P10-S21, Helm and compose.**
  - Helm `mcp.enabled` (false) and `ui.enabled` (false) render `MCP_ENABLED` and `UI_ENABLED` on the API pods only; values under `config` are ignored, as for `JOBS_ENABLED`.
  - `config` gets `MCP_ALLOWED_ORIGINS`, `MCP_WAIT_SECONDS`, `MCP_MAX_CONTENT_CHARS`, `MCP_IMAGE_MAX_BYTES`, `UI_TREE_MAX_URLS`, `UI_BATCH_FILE_MAX_BYTES`, `UI_BATCH_FILE_MAX_ROWS` and `UI_IMAGE_MAX_BYTES` with their defaults.
  - The ingress already routes `/`. NOTES.txt prints the `/mcp` and `/ui` URLs when they are on, and the P10-S13 warning.
  - The compose `api` service sets `MCP_ENABLED: ${MCP_ENABLED:-true}` and `UI_ENABLED: ${UI_ENABLED:-true}` (local development).
- **P10-S22, metrics and logs.**
  - `text_extraction_mcp_tool_calls_total{tool, outcome}`: `outcome` is `success`, `partial`, `failed`, `job`, or the refusal's error code. Every tool is exported with `success` at 0 from the start.
  - `text_extraction_mcp_tool_seconds{tool}`: a histogram with buckets from 0.05 s to 120 s.
  - `text_extraction_ui_image_previews_total{outcome}`: `shown` or the skip reason.
  - Log lines: `mcp_tool_call` (P10-S4), `job_deleted`, `ui_batch_split` (`parts`, `urls`) and `ui_batch_file` (`rows`, `urls`, `invalid`, `duplicates`). Other UI calls are counted as REST requests.

## Review Focus

1. **Getting past auth or a role check through the new doors:** no key, a wrong key or a user key on `/mcp` and at the UI login; a user key asking `compliance: ignore`, an `evaluate` action or an XPath rule through `options`; a page on another origin posting to `/mcp` or `/ui/run/extract` under `AUTH_MODE=none`. Expected: 401 before any tool or form runs; the REST 403s appear as tool errors or fragments; 403 for the foreign origin. Owner: Task 4 (`test_missing_or_wrong_key_401_envelope`, `test_foreign_origin_403_allowed_origin_ok`), Task 5 (`test_user_options_checked_as_rest`), Task 7 (`test_login_wrong_key_renders_nothing_else`, `test_run_requires_hx_request_and_same_origin`).
2. **Oversized or malformed input and output:** a CSV of 100,000 lines and one of 100,001; a JSON file nested 100,000 levels deep; malformed JSON; a site job with 200,000 sitemap entries for the tree; 5,000 selected URLs at `BATCH_MAX_URLS=1000`; a 5 MB page or a 50-item job page over MCP; base64 with line breaks, a `data:` prefix, broken base64, or one byte over the limit. Expected:
   - every bound holds with its message;
   - the tree shows `UI_TREE_MAX_URLS` and counts the rest;
   - the split runs or refuses as P10-S19 says;
   - MCP content is cut with `mcp_content_truncated`;
   - the API pod's memory stays bounded.

   Owner: Task 9 (`test_parse_url_file_bounds_and_errors`, `test_tree_caps_and_reads_in_pages`, `test_batch_split_and_refusal`), Task 5 (`test_content_slicing`, `test_extract_file_base64`), Task 6 (`test_get_job_slices_each_item`).
3. **Script injection in the playground:** a page whose content, title, metadata, image alt text, vision description or structured values hold `<script>`, `"><img src=x onerror=…>` or a `javascript:` link; rendered Markdown with raw HTML; CSV cells such as `=HYPERLINK("http://evil")` or `<svg onload=…>`; a job item URL with quotes. Expected: everything is escaped; only http(s) links are clickable; Markdown raw HTML is not rendered; the CSP would block any inline script anyway. Owner: Task 8 (`test_result_escapes_every_field`, `test_rendered_markdown_is_safe`), Task 9 (`test_parse_url_file_bounds_and_errors`), Task 7 (`test_no_inline_script_or_style_or_hx_on`).
4. **Previews that reach private addresses:** image URLs at `http://127.0.0.1:6379/`, `http://169.254.169.254/latest`, a public image that redirects to a private address, an SVG with script, a 50 MB image. Expected: no request reaches them; they show `refused`, `unsupported` or `too_large`; nothing but PNG, JPEG, GIF or WebP is inlined. Owner: Task 8 (`test_previews_guarded`).
5. **Calls and views that outlast a timeout or a rate limit:** a browser render of 60 s, an LLM step of 30 s, a 1,000-page crawl, `get_job` on a job that never ends, a progress view of five jobs left open for an hour at 60 calls a minute. Expected: every tool call ends within `MCP_WAIT_SECONDS` plus the route's hand-over time with a result or a job id; `get_job`'s wait is bounded; a progress refresh costs one rate-limit hit whatever the number of jobs, and refreshing stops once all are completed. Owner: Task 5 (`test_default_wait_becomes_job`), Task 6 (`test_get_job_wait_bounded`), Task 9 (`test_progress_refresh_one_hit_and_stops`).

---

### Task 1: Route cores

**Files:**
- Modify: `src/text_extraction/auth.py`, `src/text_extraction/ratelimit.py`, `src/text_extraction/extract_api.py` (`respond(state, …)`), `src/text_extraction/screenshot_api.py`, `src/text_extraction/structured_api.py`, `src/text_extraction/file_api.py` (`answer_upload(state, …)`, `run_upload`), `src/text_extraction/jobs_api.py` (`open_job(state, …)`, `discard_job(state, …)`, `check_jobs_enabled`, `start_batch`, `read_job`), `src/text_extraction/site_api.py`, `src/text_extraction/app.py` (`service_info`)
- Test: `tests/test_cores.py`; the existing suite unchanged

**Interfaces:**
- Produces: every function of P10-S1 with the signatures given there.

- [ ] **Step 1: Failing tests** (fakeredis, the pipeline around `FakeFetcher` as in `tests/test_extract_api.py`):
  - `test_principal_for`: none mode → `anonymous`/`admin`; a user key → its principal; no header or a wrong one → `ServiceError` 401 with the REST message.
  - `test_check_rate_shares_bucket_with_routes`: a key with `rate_limit_per_minute: 2`; `GET /v1/info`, then `check_rate`, then `GET /v1/info` → 429.
  - `test_cores_answer_like_routes`: for `respond`, `run_structured`, `run_chunk` with a `text`, `run_upload`, `start_batch`, `start_site` and `read_job`, the core's envelope equals the route's JSON apart from `trace_id`, `id`, `status_url` and `timings`.
  - `test_run_upload_refuses_large_before_converting`.
- [ ] **Step 2:** `uv run pytest tests/test_cores.py -q` → FAIL (`principal_for` missing).
- [ ] **Step 3:** move the route bodies, nothing else.
- [ ] **Step 4:** PASS; `uv run pytest` (every existing test unchanged), `uv run mypy`.
- [ ] **Step 5:** commit `refactor: move the public routes' bodies into cores that take the app state`.

### Task 2: `DELETE /v1/jobs/{id}` (D4 a)

**Files:**
- Modify: `src/text_extraction/jobs_api.py` (route, `delete_job`), `src/text_extraction/job_models.py` (`JobDeleted`), `docs/configuration.md` ("Job keys": what a delete removes)
- Test: `tests/test_jobs_api.py`, `tests/test_worker.py`, `tests/test_extract_api.py` (extend)

**Interfaces:**
- Consumes: Task 1's `check_jobs_enabled`; `visible_job`, `_discard`, `JobStore.delete`.
- Produces: `async delete_job(state: State, principal: Principal, job_id: str) -> Envelope[JobDeleted]`; `JobDeleted(id: str, kind: JobKind)`.

- [ ] **Step 1: Failing tests:**
  - `test_delete_own_job_then_404`, `test_delete_foreign_job_404_admin_ok`.
  - `test_delete_frees_open_slot`: `BATCH_MAX_OPEN_PER_CLIENT=1`; a batch, a second one is 429, delete, the second is 202. An admin deleting the user's batch frees the user's slot.
  - `test_delete_jobs_disabled_503`, `test_openapi_lists_delete_jobs`.
  - `test_worker_drops_items_of_deleted_job`: a queued item of a deleted job re-creates no key; `job_items_total{outcome="lost"}` + 1.
  - `test_deleted_p1_job_stores_nothing`: a slow extract with `wait_seconds: 0` → 202, delete, the task ends; no key of the job exists.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** implement P10-S10.
- [ ] **Step 4:** PASS; `uv run pytest tests/test_jobs_api.py tests/test_worker.py`.
- [ ] **Step 5:** commit `feat: add DELETE /v1/jobs/{id} to cancel and remove a job`.

### Task 3: Item runs (D8 a)

**Files:**
- Modify: `src/text_extraction/jobs.py` (`record_claim`, `_KINDS` gains `runs`, `items` reads it; module docstring), `src/text_extraction/worker.py` (call after a claim), `src/text_extraction/job_models.py` (`ItemRun`, `JobItem.run`), `src/text_extraction/jobs_api.py` (the `GET /v1/jobs/{id}` description), `docs/configuration.md` ("Job keys")
- Test: `tests/test_jobs.py`, `tests/test_worker.py`, `tests/test_jobs_api.py` (extend)

**Interfaces:**
- Produces: `async JobStore.record_claim(job_id: str, index: int, pool: str, attempts: int) -> None`; `ItemRun(pool: str, attempts: int, claimed_at: datetime)`; `JobItem.run: ItemRun | None = None`.

- [ ] **Step 1: Failing tests:**
  - `test_record_claim_sets_run_with_job_ttl`: the TTL of `runs` is at most the TTL of `meta`.
  - `test_record_claim_on_gone_job_creates_nothing`, `test_delete_and_shorten_cover_runs`.
  - `test_items_show_run`: claim 2 in `browser` → `run == ItemRun(pool="browser", attempts=2, …)`; an unclaimed item → None.
  - `test_worker_records_each_claim`: a lost lease → the second claim records `attempts == 2`; an escalation to `browser` records that pool.
  - `test_worker_runs_item_when_record_fails`: the Valkey error is logged and the item still runs.
  - `test_job_json_has_run_field`.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** implement P10-S11.
- [ ] **Step 4:** PASS; `uv run pytest tests/test_jobs.py tests/test_worker.py tests/test_jobs_api.py`.
- [ ] **Step 5:** commit `feat: show each job item's last claim (pool, claim number, time)`.

### Task 4: MCP endpoint, gate and call guard

**Files:**
- Create: `src/text_extraction/mcp_server/__init__.py`, `server.py` (P10-S2), `gate.py` (P10-S3), `calls.py` (P10-S4, P10-S22), `results.py` (P10-S6: the error results; Task 5 adds the rest), `tools_info.py` (`service_info`)
- Modify: `src/text_extraction/settings.py` (the `MCP_*` settings of P10-S9), `src/text_extraction/app.py` (route, lifespan, body limit), `pyproject.toml` and `uv.lock` (`fastmcp`), `NOTICE`, `docs/configuration.md` (section "MCP", the metrics rows), `.env.example`
- Test: `tests/mcp_server/__init__.py`, `tests/mcp_server/rpc.py` (helper `rpc(client, method, params=None, *, headers) -> dict`, posting JSON-RPC with `Accept: application/json, text/event-stream`), `tests/mcp_server/test_server.py`, `tests/test_settings.py`

**Interfaces:**
- Consumes: Task 1's `principal_for`, `check_rate`, `service_info`.
- Produces:
  - `build_mcp(state: State, settings: Settings, registry: CollectorRegistry) -> McpMount`, where `McpMount(app: ASGIApp, lifespan: Callable[[], AbstractAsyncContextManager[None]], server: FastMCP)`; `mcp_body_limit(settings: Settings) -> int`; `McpGate(app: ASGIApp, settings: Settings)`.
  - `call_principal() -> Principal`; `call_header(name: str) -> str | None`.
  - `ModelTool` (base: `args_model: type[BaseModel]`, `async call(state: State, principal: Principal, args: BaseModel) -> object`); `register(server: FastMCP, state: State, tool: ModelTool) -> None`.
  - `error_result(error: Exception) -> ToolResult`; `envelope_result(answer: Envelope[Any] | JSONResponse | Response, *, hints: Sequence[str] = ()) -> ToolResult`.
  - Settings `mcp_enabled`, `mcp_allowed_origins: list[str]`, `mcp_wait_seconds`, `mcp_max_content_chars`, `mcp_image_max_bytes`.

- [ ] **Step 1: Failing tests** (`TestClient` as a context manager, so the lifespan runs):
  - `test_mcp_settings_defaults_and_bounds`.
  - `test_disabled_has_no_mcp_route_and_no_import`: `POST /mcp` 404; a subprocess that builds the app with `MCP_ENABLED=false` has no `fastmcp` in `sys.modules`.
  - `test_initialize_and_list_tools`: `serverInfo.name == "text-extraction"`, `service_info` listed.
  - `test_service_info_call`: `structuredContent["data"]["principal"] == "alice"`, `isError` false.
  - `test_not_in_openapi`, `test_exact_path_no_redirect` (`POST /mcp/` 404), `test_get_mcp_405`.
  - `test_missing_or_wrong_key_401_envelope` (Review Focus 1): 401, `WWW-Authenticate: Bearer`, `error.code == "unauthorized"`.
  - `test_auth_none_is_anonymous_admin`, `test_foreign_origin_403_allowed_origin_ok` (Review Focus 1).
  - `test_rate_limit_shared_with_rest`: limit 2; `GET /v1/info`, one call, then a call → `isError`, `rate_limited`, retryable; `tools/list` is not counted.
  - `test_trace_id_in_envelope_and_logs`: `x-client-trace-id: t-1` → `trace_id == "t-1"`; a `mcp_tool_call` line with `tool`, `outcome` and `principal`, and no argument values.
  - `test_metrics_counted`, `test_unexpected_exception_is_internal_error` (no traceback in the result).
  - `test_body_limit_of_mcp_path` (Review Focus 2): a `Content-Length` over `mcp_body_limit` → 413 `invalid_request`, without a key.
- [ ] **Step 2:** `uv run pytest tests/mcp_server -q` → FAIL.
- [ ] **Step 3:** add `fastmcp` (P10-S2 pin rule; `uv lock`, `exclude-newer-package` for transitive packages that are too new, the licence gate), then implement.
- [ ] **Step 4:** PASS; `uv run python scripts/check_licenses.py --lock uv.lock`, `uv run mypy`.
- [ ] **Step 5:** commit `feat: serve an MCP endpoint at /mcp with the API's auth and rate limit`.

### Task 5: MCP extraction tools

**Files:**
- Create: `src/text_extraction/mcp_server/tools_extract.py` (`extract_url`, `extract_file`, `screenshot_url`, `extract_structured`, `chunk`), `src/text_extraction/mcp_server/files.py`
- Modify: `src/text_extraction/mcp_server/results.py` (slicing, hints, images), `src/text_extraction/mcp_server/server.py` (register)
- Test: `tests/mcp_server/test_tools_extract.py`, `tests/mcp_server/test_files.py`

**Interfaces:**
- Consumes: Task 1's cores; Task 4's `ModelTool`, `register`, `call_header`, `envelope_result`.
- Produces:
  - `decode_file(value: str, limit: int) -> tuple[bytes, str | None]` (raises `ServiceError` `invalid_request`).
  - `merge_options(args: dict[str, Any], options: dict[str, Any] | None) -> dict[str, Any]`.
  - `slice_content(envelope: dict[str, Any], *, offset: int, share: int) -> str | None` (the hint, or None).

- [ ] **Step 1: Failing tests:**
  - `test_tools_list_lean_schemas`: the five tools, each input schema under 4,096 bytes of JSON, an `options` object, the open-world annotation.
  - `test_extract_url_matches_rest`: `status`, `data.content` and `data.title` equal the REST answer's.
  - `test_options_merge_and_conflict`: `{"links": true}` → `data.links`; `url` in both → `invalid_request`; an unknown field → `invalid_request` with `details`.
  - `test_user_options_checked_as_rest` (Review Focus 1): `compliance: ignore` → `forbidden`; an `evaluate` action → `forbidden`; `mode: browser` with browser mode off → `invalid_request`.
  - `test_target_failure_is_not_tool_error`: a 404 page → `isError` false, `target_http_error`.
  - `test_private_url_blocked`: `http://169.254.169.254/` → `failed`/`blocked_url`.
  - `test_default_wait_becomes_job` (Review Focus 5): `MCP_WAIT_SECONDS=1` and a fetch of 3 s → a `JobAccepted` envelope; the hint names `get_job`.
  - `test_content_slicing` (Review Focus 2): 60,000 characters with `max_chars` 50,000 → 50,000, `mcp_content_truncated`, hint `content_offset=50000`; with that offset → the last 10,000.
  - `test_llm_key_from_header_only`: the header → stored with the job; `{"llm_key": "x"}` in `options` → `invalid_request`.
  - `test_extract_file_base64` (Review Focus 2): the corpus PDF → `document.kind == "pdf"`; with line breaks and with a `data:` prefix → success; broken → `invalid_request`; `DOCUMENT_MAX_BYTES + 1` → `invalid_request`.
  - `test_structured_and_chunk_tools`: the values of the `metadata` strategy; `chunk` of a `text` with `recursive`.
  - `test_screenshot_browser_off_400`.
  - `test_screenshot_image_attached_and_capped`: a stored artifact of 1 KB → one `ImageContent`; `MCP_IMAGE_MAX_BYTES=500` → no image, the download hint.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** implement P10-S5 to P10-S8 for these tools.
- [ ] **Step 4:** PASS; mypy clean.
- [ ] **Step 5:** commit `feat: add the MCP extraction tools`.

### Task 6: MCP job tools

**Files:**
- Create: `src/text_extraction/mcp_server/tools_jobs.py` (`batch_extract`, `site_structure`, `get_job`, `get_job_screenshot`, `cancel_job`)
- Modify: `src/text_extraction/mcp_server/server.py` (register; `TOOL_ROUTES: Mapping[tuple[str, str], str]`, mapping (method, path) to a tool)
- Test: `tests/mcp_server/test_tools_jobs.py`

**Interfaces:**
- Consumes: Tasks 1-5.
- Produces: `TOOL_ROUTES`.

- [ ] **Step 1: Failing tests:**
  - `test_batch_and_get_job_paging`: 3 URLs, results written through `JobStore`; `limit: 2` → 2 items, `next_offset == 2`, and each item's `run` as stored.
  - `test_get_job_waits_until_completed`: the last result is written after 1 s; `wait_seconds: 5` → `completed` within 3 s.
  - `test_get_job_wait_bounded` (Review Focus 5): a job that never completes, `wait_seconds: 2` → answers after 2-3 s, `running`.
  - `test_get_job_slices_each_item` (Review Focus 2): 3 items of 30,000 characters, `max_chars` 30,000 → 10,000 each, with `mcp_content_truncated`.
  - `test_foreign_job_not_found`.
  - `test_site_structure_tool`: `kind == "site"`; a `depth` over `SITE_MAX_DEPTH` → `invalid_request`.
  - `test_get_job_screenshot`: `ImageContent`; a missing one → `not_found`.
  - `test_cancel_job_then_not_found`, `test_jobs_disabled_tool_error` (`internal_error`, retryable).
  - `test_every_public_route_has_a_tool`: every `APIRoute` of the public app in the spec is a key of `TOOL_ROUTES`.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** implement.
- [ ] **Step 4:** PASS; `uv run pytest tests/mcp_server`.
- [ ] **Step 5:** commit `feat: add the MCP job tools (batch, site structure, status, screenshot, cancel)`.

### Task 7: UI shell, login and security

**Files:**
- Create: `src/text_extraction/ui/__init__.py`, `ui/routes.py` (`GET /ui`, `/ui/run/login`, the `/ui/run` guard), `ui/security.py` (headers middleware for `/ui`, `require_htmx`), `ui/forward.py` (P10-S14), `ui/templates/base.html`, `ui/templates/index.html`, `ui/templates/login.html`, `ui/templates/app.html`, `ui/static/htmx-2.0.11.min.js` (`package/dist/htmx.min.js` of `https://registry.npmjs.org/htmx.org/-/htmx.org-2.0.11.tgz`), `ui/static/ui.js` (keys and login, P10-S13), `ui/static/ui.css`
- Modify: `src/text_extraction/settings.py` (the `UI_*` settings of P10-S9), `src/text_extraction/app.py` (router and static mount when on, body limits of `/ui/run/*`), `pyproject.toml`, `uv.lock` (`jinja2`), `NOTICE` (htmx, 0BSD), `docs/configuration.md` (section "UI": the access rules of P10-S13), `.env.example`
- Test: `tests/ui/__init__.py`, `tests/ui/conftest.py` (an app with `UI_ENABLED`, a user key, the `FakeFetcher` pipeline, helper `run(client, op, **fields)` with `HX-Request`), `tests/ui/test_shell.py`

**Interfaces:**
- Produces: `create_ui_router(settings: Settings) -> APIRouter`; `templates: Jinja2Templates`; `require_htmx(request: Request) -> None`; `forward(...) -> Forwarded` (P10-S14); `ui_principal(request: Request) -> Principal` (`principal_for` and one `check_rate` hit, for the reading views); `HTMX_SHA256: Final[str]` (in the test).

- [ ] **Step 1: Failing tests:**
  - `test_ui_settings_defaults_and_bounds`, `test_disabled_404_and_no_jinja_import`, `test_page_and_static_served_without_key`.
  - `test_login_page_under_api_key_mode`: `/ui` holds the login form and no operation form.
  - `test_login_ok_renders_app_with_principal`.
  - `test_login_wrong_key_renders_nothing_else` (Review Focus 1).
  - `test_auth_none_renders_app_with_banner`.
  - `test_security_headers_on_every_ui_answer`: the exact P10-S12 values on `/ui`, on a static file and on a `/ui/run` answer.
  - `test_not_in_openapi`.
  - `test_vendored_htmx_checksum`: the file's SHA-256 equals `HTMX_SHA256`, recorded when vendoring.
  - `test_no_inline_script_or_style_or_hx_on` (Review Focus 3): in every template, each `<script>` has `src`, there is no `style=` and no `hx-on`.
  - `test_htmx_config_meta`: the six P10-S12 values.
  - `test_run_requires_hx_request_and_same_origin` (Review Focus 1): without `HX-Request` 403; `Origin: https://evil.example` 403; the page's own origin passes.
  - `test_forward_passes_only_listed_headers`: the inner request sees `Authorization`, `X-LLM-Api-Key`, and the outer trace id as `x-client-trace-id`, but not `Cookie`.
  - `test_ui_sets_no_cookie`.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** vendor htmx, add `jinja2`, implement.
- [ ] **Step 4:** PASS; the licence gate, mypy.
- [ ] **Step 5:** commit `feat: add the UI shell with login, vendored htmx, CSP and the in-process forward`.

### Task 8: UI single operations, results, previews and timeline

**Files:**
- Create: `src/text_extraction/ui/forms.py` (`OPERATIONS`, `build_body`, `UI_METADATA_SCHEMA`), `ui/texts.py` (`WARNING_TEXTS`), `ui/markdown.py` (safe rendering), `ui/previews.py` (P10-S16), `ui/templates/forms/{extract,file,screenshot,structured,chunk,info}.html`, `ui/templates/result.html`, `ui/templates/timeline.html`, `ui/templates/images.html`
- Modify: `src/text_extraction/ui/routes.py` (`/ui/run/{op}` for these ops, `/ui/run/images`), `src/text_extraction/vision/fetch.py` (`ImageFetcher.get(..., max_bytes: int | None = None)`), `src/text_extraction/app.py` (an `ImageFetcher` in `app.state.ui_images` while the UI is on; closed in the lifespan)
- Test: `tests/ui/test_operations.py`, `tests/ui/test_previews.py`, `tests/test_vision_fetch.py` (extend)

**Interfaces:**
- Consumes: Task 7's `forward`, `templates`, `require_htmx`, `ui_principal`; Task 3's `ItemRun`; `ImageFetcher`.
- Produces:
  - `Operation(method: Literal["GET", "POST", "DELETE"], path: str, form: str)`; `OPERATIONS: Mapping[str, Operation]`.
  - `build_body(op: str, form: Mapping[str, str]) -> dict[str, Any]` (raises `OptionsError` with a message).
  - `UI_METADATA_SCHEMA: Final[dict[str, Any]]`; `render_markdown(text: str) -> Markup`.
  - `timeline(item: JobItem | None, result: ExtractResult) -> list[TimelineStep]`, with `TimelineStep(label: str, detail: str, ms: int | None)`.
  - `async load_previews(images: Sequence[str], *, fetcher: ImageFetcher, mode: ComplianceMode, max_bytes: int) -> list[Preview]`, with `Preview(url: str, data_uri: str | None, skipped: str | None)`.

- [ ] **Step 1: Failing tests:**
  - `test_format_choice_text_and_markdown`: the body sends `format: text` or `markdown`; the download link is `.txt` or `.md`.
  - `test_metadata_structured_checkbox`: the body has `structured.strategy == "metadata"` and `UI_METADATA_SCHEMA`; `StructuredOptions(**…)` validates it; a JSON-LD page shows `headline` and `datePublished` in the values table and the rest under missing.
  - `test_describe_images_checkbox`: `describe_images.max_images == 5`; with the LLM off, the REST 400 is shown.
  - `test_result_escapes_every_field` (Review Focus 3): content, title, a metadata description, an alt text, a vision description and a structured value each hold `<script>alert(1)</script>` and `"><img src=x onerror=alert(1)>` → escaped everywhere.
  - `test_rendered_markdown_is_safe` (Review Focus 3): raw `<script>` is shown as text, `[x](javascript:alert(1))` is not a link, `![a](http://…)` becomes the text `a`.
  - `test_links_only_http`.
  - `test_options_json_merged_and_invalid_json_message`: invalid JSON → the message, and no inner request.
  - `test_inner_status_shown`: no key → 401 `unauthorized`; a 422 lists `details`.
  - `test_file_upload_forwarded`, `test_chunk_text`, `test_structured`, `test_info`.
  - `test_screenshot_embeds_png`: a stored artifact → `<img src="data:image/png;base64,`.
  - `test_timeline_from_fields`: a fast result shows compliance, fetch (`attempts`, `upstream_status`, `cache`), convert and the outcome; a browser result after `auto` shows render with "escalated from fast"; with a run (claim 2 in `browser`) the first step names it; `browser_unavailable` shows its text; an unknown warning shows its name.
  - `test_curl_line_hides_key`.
  - `test_previews_guarded` (Review Focus 4): pytest-httpserver serves a PNG, an SVG, a 2 MB JPEG and a redirect to `http://169.254.169.254/`. With `FETCH_ALLOW_PRIVATE_NETWORKS=false` and a resolver stub that maps the public test host to the server, the result is: the PNG shown, the SVG `unsupported`, the JPEG `too_large` at `UI_IMAGE_MAX_BYTES=1_000_000`, the redirect and `http://127.0.0.1:6379/` `refused`, and no request to a private address. One rate-limit hit for the call; `text_extraction_ui_image_previews_total` counted.
  - `test_image_fetcher_max_bytes_keyword`: the vision default is unchanged.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** implement P10-S14 to P10-S17.
- [ ] **Step 4:** PASS; mypy.
- [ ] **Step 5:** commit `feat: let the UI run single extractions with metadata, previews and a timeline`.

### Task 9: UI jobs: progress, batches from files, the site tree

**Files:**
- Create: `src/text_extraction/ui/url_lists.py` (P10-S19), `ui/site_tree.py` (P10-S20), `ui/templates/forms/{batch,batch_file,site_structure}.html`, `ui/templates/progress.html`, `ui/templates/batch_preview.html`, `ui/templates/tree.html`
- Modify: `src/text_extraction/ui/routes.py` (`batch`, `batch-file`, `batch-urls`, `site-structure`, `progress`, `site-tree`, `job`, `artifact`, `cancel`), `src/text_extraction/ui/static/ui.js` (tree selection, P10-S20)
- Test: `tests/ui/test_url_lists.py`, `tests/ui/test_site_tree.py`, `tests/ui/test_jobs_views.py`, `tests/ui/test_playground_browser.py` (`@pytest.mark.browser`)

**Interfaces:**
- Consumes: Tasks 1-3 and 7-8; `seen_key` (package 9).
- Produces:
  - `UrlList(urls: list[str], invalid: list[tuple[int, str]], duplicates: int, rows: int)`; `parse_url_file(data: bytes, *, max_rows: int) -> UrlList` and `parse_url_lines(values: Iterable[str], *, max_rows: int) -> UrlList` (both raise `UrlListError` with a message).
  - `split_batches(urls: Sequence[str], *, per_batch: int, max_batches: int) -> list[list[str]]` (raises `UrlListError`).
  - `TreeNode(label: str, url: str | None, kind: Literal["section", "page", "sitemap", "linked"], title: str | None, status: str | None, depth: int | None, lastmod: str | None, children: list[TreeNode])`; `SiteTree(roots: list[TreeNode], urls: int, hidden: int)`; `build_tree(items: Iterable[JobItem], *, max_urls: int) -> SiteTree`.

- [ ] **Step 1: Failing tests:**
  - `test_parse_url_file_bounds_and_errors` (Review Focus 2 and 3), one case each:
    - a CSV with a `URL` header in the second column;
    - a header-less CSV;
    - a `;` CSV;
    - a JSON array of strings, and one of objects;
    - a BOM, Latin-1 bytes, `ftp:` and `javascript:` URLs, `=HYPERLINK("http://evil")` and `@cmd` (reported with row numbers);
    - duplicates counted;
    - 100,000 rows accepted and 100,001 refused, each under 2 s;
    - JSON nested 100,000 levels, a truncated JSON and a 10 KB field, each with its message.
  - `test_batch_split_and_refusal` (Review Focus 2): 2,500 URLs at `BATCH_MAX_URLS=1000` → three batches forwarded and three progress bars; 5,001 at `BATCH_MAX_OPEN_PER_CLIENT=5` → refused, nothing sent; a 429 on the third part → two jobs shown with the refusal.
  - `test_batch_file_preview_then_start`.
  - `test_progress_view`: `<progress value="2" max="3">`; the failed count; "running in browser, claim 2" from a run; previous and next buttons with `offset`.
  - `test_progress_refresh_one_hit_and_stops` (Review Focus 5): five jobs → one `check_rate` hit per refresh; the trigger is present while one runs and gone once all are completed.
  - `test_cancel_button`.
  - `test_tree_by_path_segments`: pages, sitemap entries (one on `www.` of the same host), linked-only URLs and a query leaf, under the right sections with their kinds, in name order.
  - `test_tree_caps_and_reads_in_pages` (Review Focus 2): a site job of 300 pages and 200,000 sitemap entries across parts; `UI_TREE_MAX_URLS=5000` → 5,000 nodes and `hidden` = the rest; `read_job` is called with `limit=20` until `next_offset` is null; the view holds no more than one page of items at once (a spy on the reads).
  - `test_tree_checkboxes_named_only_for_urls`.
  - `test_extract_selected_starts_batch`.
  - `test_every_public_route_has_an_operation`: every spec `APIRoute` matches one `OPERATIONS` entry.
  - Browser (`test_playground_browser.py`): uvicorn on a free port in a thread, the app's fakeredis and an extract worker thread, Chromium:
    - `test_login_and_extract`: a wrong key shows the error and no form; the right key shows the tabs; an extract as Text shows the title; a reload keeps the login; the page has no console CSP errors and `context.cookies() == []`.
    - `test_tree_select_to_batch`: a site structure job completes, "Show as tree", checking a section checks its pages and makes the root indeterminate, the counter is right, "Extract selected" → progress until completed.
    - `test_csv_upload_to_progress`: a CSV of 3 URLs and a bad line → a preview listing the problem → "Start batch" → the progress bar reaches 3/3.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** implement P10-S18 to P10-S20.
- [ ] **Step 4:** PASS; `uv run pytest tests/ui` and `uv run pytest -m browser tests/ui`.
- [ ] **Step 5:** commit `feat: add progress views, batch files and the site tree to the UI`.

### Task 10: Helm, compose and docs

**Files:**
- Modify:
  - Helm: `deploy/helm/text-extraction/values.yaml` (`mcp`, `ui`, `config` keys), `templates/_helpers.tpl` and `templates/deployment.yaml` (API pods only), `templates/NOTES.txt` (URLs, the `AUTH_MODE=none` warning), `deploy/helm/text-extraction/README.md` (the values table; ingress timeouts of at least `MCP_WAIT_SECONDS` + 70 s for `/mcp` and `/ui`; an upload body size of at least `UI_BATCH_FILE_MAX_BYTES`), `deploy/helm/test.sh`.
  - CI and compose: `.github/workflows/ci.yml` (lint with `--set mcp.enabled=true --set ui.enabled=true`), `docker-compose.yml`, `.env.example`.
  - `README.md`: section "MCP" (`claude mcp add --transport http text-extraction https://<host>/mcp --header "Authorization: Bearer <key>"`, Claude Desktop through the `mcp-remote` bridge in `claude_desktop_config.json`, the tool table); section "Playground" (login, the tabs, batch files, the tree).
  - `docs/architecture.md`: section "MCP and UI" with D1-D8 as decided and P10-S1 to S22; the `/mcp` and `/ui` rows; the `DELETE` row; `items[].run` in "Jobs and the fair queue"; "Non-JSON content types" (MCP answers JSON, no SSE); the warning `mcp_content_truncated`.
  - `docs/configuration.md`.
- Test: `deploy/helm/test.sh`

- [ ] **Step 1: Failing checks in `deploy/helm/test.sh`:**
  - by default the API pod has `MCP_ENABLED` and `UI_ENABLED` `"false"`;
  - `--set mcp.enabled=true --set ui.enabled=true` → `"true"` on the API pod and on no worker;
  - `--set config.MCP_ENABLED=true` alone stays `"false"`;
  - `--set ui.enabled=true --set config.AUTH_MODE=none` → the NOTES warning.

  Run `deploy/helm/test.sh` (or the docker `alpine/helm:3.19.0` command of `CLAUDE.md`) → FAIL.
- [ ] **Step 2:** implement P10-S21 and the docs.
- [ ] **Step 3:** `deploy/helm/test.sh` PASS; `helm lint --strict` with both toggles; `docker compose config` shows both variables on `api`.
- [ ] **Step 4:** commit `feat: switch MCP and the UI in Helm and compose; document them`.

### Task 11: Done-when, live check

**Files:**
- Create: `tests/mcp_server/test_done_when.py`, `tests/ui/test_done_when.py`
- Modify: `docs/packages.md` (status row, package 10 note)

- [ ] **Step 1: MCP done-when** (default suite, no network). Setup:
  - the public app with `MCP_ENABLED=true`, served by uvicorn on `127.0.0.1` in a thread (the lifespan runs);
  - the real extract `Worker` in another thread on the app's fakeredis, as in `tests/site/test_done_when.py`;
  - pages and a site on pytest-httpserver (`FETCH_ALLOW_PRIVATE_NETWORKS=true`, `OUTBOUND_MIN_DELAY_MS=0`);
  - the client `fastmcp.Client(StreamableHttpTransport(url, headers={"Authorization": f"Bearer {USER_KEY}"}))`, the transport Claude Code uses.

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
  assert done.structured_content["data"]["items"][0]["run"]["pool"] == "extract"
  pdf = await client.call_tool("extract_file", {"content_base64": PDF_B64, "file_name": "a.pdf"})
  assert pdf.structured_content["data"]["document"]["kind"] == "pdf"
  site = await client.call_tool("site_structure", {"url": SITE_URL, "depth": 1})
  assert site.structured_content["data"]["kind"] == "site"
  refused = await client.call_tool(
      "extract_url", {"url": PAGE_URL, "options": {"compliance": "ignore"}}, raise_on_error=False
  )
  assert refused.is_error and refused.structured_content["error"]["code"] == "forbidden"
  ```

  It also calls `extract_structured`, `chunk`, `cancel_job` (then `get_job` → `not_found`) and `service_info`, and checks that a client with a wrong key fails to connect (401). Run: `uv run pytest tests/mcp_server/test_done_when.py -v` → PASS.
- [ ] **Step 2: UI done-when.** `tests/ui/test_done_when.py` drives every entry of `OPERATIONS`, and the views `progress`, `site-tree`, `batch-file`, `batch-urls` and `images`, through `/ui/run` against the same setup. The worker thread completes the batches, the site job and a P1 job; `screenshot` and `artifact` with the browser off show the REST 400 and a stored artifact. Each fragment must show the REST route's status and envelope. With `-m browser`, the three Chromium tests of Task 9 are the visible check: login; tree selection → batch → progress; CSV upload → progress. Run: `uv run pytest tests/ui -v` and `uv run pytest -m browser tests/ui -v` → PASS.
- [ ] **Step 3: Live check** (the done-when evidence for "usable from Claude"). Run the service with Valkey (`docker compose up` with `MCP_ENABLED` and `UI_ENABLED` on): in the sandbox if Docker or a Valkey is available, else on Jan's PC.
  - Claude Code: `claude mcp add --transport http text-extraction http://localhost:8080/mcp --header "Authorization: Bearer $API_KEY"`; list the tools, extract a WLO page, start a batch of three URLs and read it with `get_job`, map a site at depth 1, cancel a job.
  - Browser: open `http://localhost:8080/ui`, log in, extract a WLO page as Text and as Markdown with structured metadata and previews, map a WLO-type site at depth 1, select a section in the tree and extract it, and upload a CSV of ten URLs.

  The summary goes into the PR: tools used, answers, durations, and screenshots of the tree and the progress view.
- [ ] **Step 4:** label the PR `live`: `wlo_200` auto stays ≥ 80 % (Task 1 moved the route bodies; Task 3 touched the worker loop).
- [ ] **Step 5:** update `docs/packages.md`; commit `test: add the package 10 done-when tests and the live check`.

### Final: review and hand-over

- [ ] `requesting-code-review` over the whole branch, with the Review Focus above as the brief, plus: no secret in a log, tool argument or HTML; Task 1 changed no REST answer; `ui.js` stays small and does only what P10-S13 and P10-S20 say. Fix findings via `receiving-code-review`.
- [ ] `verification-before-completion`: `uv run pytest`, `uv run pytest -m browser tests/ui`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, the licence gate, `deploy/helm/test.sh` and helm lint, both done-when tests and the live summary; the done-when is met.
- [ ] `finishing-a-development-branch`: PR, squash merge once green (Jan's standing permission), progress memory updated.
