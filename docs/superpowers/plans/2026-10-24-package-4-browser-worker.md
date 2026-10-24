# Package 4: Browser Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pages that need JavaScript get a real browser:
- `mode: "browser"` renders a page in Chromium; `mode: "auto"` escalates a fast result that needs it.
- Callers can run `actions`, pass `cookies` and `headers`, and take full-page screenshots at 1920×1080 (`POST /v1/screenshot`, `screenshot: true`).
- Cookie banners are opted out of and bot challenges are reported.
- A browser worker image, pool and Helm deployment run all of it.

**Architecture:**
- **Where Chromium runs:** only in the new browser worker (`text-extraction-browser-worker`, image `text-extraction-browser`). It claims items from the package-3 fair queue, pool `browser`. API pods and extract workers never start a browser. A sync request puts a one-item job on the `browser` pool and waits for its result.
- **One pipeline, plus a renderer:** the browser worker runs the existing `Pipeline` with a `Renderer`. The page's document is fetched exactly as in fast mode: compliance per hop, outbound gate, SSRF guard, byte limits, `noai` checks. Chromium then gets that document through `route.fulfill` and runs its scripts. Every later main-frame navigation goes through the same fetch path (client redirects, meta refresh, clicked links). The rendered DOM goes through the same conversion, quality gate, metadata, fingerprint and cache.
- **Sub-resources** (scripts, XHR, CSS, iframes) are loaded by Chromium itself through an in-process egress proxy. The proxy resolves each host, refuses non-public addresses and counts bytes per page.
- **Auto mode:** the fast result escalates when `quality.browser_recommended` is set. The browser worker then runs the item as `auto`: fetch, fast verdict, render if still needed, and keep the better result.

**Tech Stack:** existing, plus `playwright` (Apache-2.0) and `duckduckgo-autoconsent-py` (MPL-2.0, used unmodified). Both are installed only in the browser image (extra `browser`) and in the dev group.

**Spec:**
- `docs/architecture.md`:
  - Overview (browser-worker pool, images);
  - Extraction pipeline steps 1, 3 and 4 (SSRF re-checked for browser sub-requests, auto quality gate);
  - Public API (`mode`, `actions`, `screenshot`, `POST /v1/screenshot`, `GET /v1/jobs/{id}/artifacts/{name}`);
  - Auth (admin: JavaScript actions, stealth browser);
  - Feature notes "Browser actions" and "Sync-to-job";
  - Observability (browser pool usage).
- "Lessons" table: cookie auto-consent; browser baseline 80 %, median 9-10 s.
- `docs/packages.md` row 4.
- `CLAUDE.md` rules 2 and 4-7.
- Reference (read only): the Playwright service at `/mnt/user-data/uploads/text-extraction/Wissen/text-extraction-main/text-extraction-main/text_extraction/grab_content.py`:
  - `default_goto` (load, then up to 8 s network idle);
  - `handle_cookie_consent` and `CUSTOM_CONSENT_RULES`;
  - `strip_hidden_elements`.
  Write the code fresh from what these do; do not copy it.

## Global Constraints

- English only. Python 3.13. New runtime dependencies only `playwright>=1.64` and `duckduckgo-autoconsent-py>=16`. They go in the optional extra `browser` and the dev group, never in the API image. Patchright is not added unless Jan decides otherwise (D3).
- The API image must not contain Playwright or Chromium. No module imported by `text_extraction.app` or `text_extraction.worker` may import `playwright`.
- Ports: the browser worker serves only 9090 (`/health`, `/ready`, `/metrics`), like the extract worker.
- Stateless pods. Jobs, queue items, secrets and screenshots live in Valkey with TTLs. Chromium's profile is a fresh temporary directory per launch and is never reused as state.
- Every new setting is an env var with a documented default in `docs/configuration.md` and `.env.example`, in the task that adds it. Helm sets it where the chart owns it.
- Result envelope everywhere: a failed render is 200 with `status: failed`. 5xx only for our own faults.
- SSRF: every byte Chromium sends goes through the egress proxy or through our `Fetcher`. No request leaves Chromium directly: no QUIC, no WebRTC UDP, no DNS of its own.
- Never log URLs, query strings, cookies, header values, action text or scripts. Host and `path_hash` at most.
- The fast pipeline's answers for `mode: "fast"`, and for `mode: "auto"` while `BROWSER_ENABLED=false`, must not change.
- Not in this package: PDF and Office routing (package 5: a download in browser mode is `unsupported_content_type`), S3 offload (package 11), Patchright (D3, recommended), actions/cookies/headers in batches (B6).

## Rulings in this plan

- **B1 (where the browser runs):** see Architecture.
  - A sync request creates a job of kind `extract` or `screenshot` with one URL.
  - It enqueues the item on pool `browser` under the client `<key name>#live`, so the key's own batches do not queue it.
  - It polls the job: 0.2 s, rising to 0.5 s.
  - It waits `BROWSER_QUEUE_WAIT_SECONDS` (10) for a worker to claim the item (job `running`), then `BROWSER_TIMEOUT_SECONDS` + 5 s for the result.
  - A request that gives up deletes the job, so a worker that claims it later drops it.
- **B2 (how the page is loaded): Decision D1.** Recommended: main-frame documents through our `Fetcher`, as in Architecture.
  - Consequence: robots, TDM and `noai` hold for every hop of every main-frame navigation, before it is requested.
  - Consequence: bot walls that bind their clearance cookie to Chromium's own TLS fingerprint stay closed. They are reported as `bot_protection`.
  - A non-GET main-frame navigation (a form POST from an action) is refused, and the action reports it.
- **B3 (outbound limits):** a render holds the host's gate turn from its document fetch until the page is closed. A main-frame navigation to another host moves the turn, as a redirect does.
  - Sub-resources take no turn, like a visitor's browser. Chromium caps them at 6 connections per host.
  - Extraction renders block images, media and fonts (`BROWSER_BLOCKED_RESOURCES`). Screenshot renders block nothing.
- **B4 (SSRF):** all Chromium traffic goes through the egress proxy (`browser_proxy.py`), bound to 127.0.0.1. Chromium launch flags:
  - `--proxy-server` and `--proxy-bypass-list=<-loopback>`;
  - `--disable-quic`;
  - `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`;
  - `--disable-dev-shm-usage`.

  Each browser context gets its own proxy credentials, so the proxy can tell pages apart. Its per-page byte cap is `BROWSER_MAX_BYTES`. Service workers are blocked and downloads refused.
- **B5 (auto escalation):**
  - `quality.browser_recommended` becomes `js_heavy or bot_challenge or soft_404`, as architecture step 4 says.
  - A fast result escalates when it is `mode_used: "fast"`, comes from 2xx HTML, has `browser_recommended`, and `BROWSER_ENABLED` is true. A result that will escalate is neither cached nor fingerprinted by the fast run.
  - The browser worker runs the item as `auto`. It keeps the better result: higher status (`success` > `partial` > `failed`), then more words, and on a tie the rendered one.
  - The extract worker moves an escalating batch item to the `browser` pool (same job and index) and completes it in `extract` without a result.
  - If no worker claims a sync `auto` item in time, or the render does not answer, the request returns its fast result with the warning `browser_unavailable`. Sync `browser` requests return `failed`/`timeout`, retryable.
- **B6 (caller secrets):** `actions`, `cookies` and `headers` are accepted by `/v1/extract` and `/v1/screenshot` only, not by `/v1/batch`.
  - They are stored in `job:{<id>}:secrets` with a TTL of `2 × BROWSER_TIMEOUT_SECONDS + BROWSER_QUEUE_WAIT_SECONDS + 60` s. The worker deletes them when it finishes the item. They never appear in `GET /v1/jobs`.
  - Requests carrying any of them bypass the result cache and fingerprints, both reads and writes.
  - `headers` go only to main-frame documents on the requested URL's host.
  - `cookies` may only name that host or a parent domain of it.
  - `evaluate` is admin-only: 403 `forbidden` for users.
- **B7 (screenshots):** PNG only, viewport 1920×1080.
  - `full_page` (default true) is clipped at `SCREENSHOT_MAX_HEIGHT_PX` (16384), with the warning `screenshot_clipped`. A PNG over `SCREENSHOT_MAX_BYTES` (10 MiB) fails with `too_large`.
  - Stored as `job:{<id>}:artifact:screenshot.png` for `ARTIFACT_TTL_SECONDS` (3600). Served by `GET /v1/jobs/{id}/artifacts/screenshot.png` (`image/png`, own jobs only, otherwise 404).
  - While Valkey's memory guard is high, nothing is stored and the item fails `internal_error`, retryable.
  - A screenshot is `success` once stored. Its `quality` is reported but does not fail it.
- **B8 (honest user agent):** Chromium's own UA plus ` text-extraction/<version> (+https://github.com/janschachtschabel/text-extraction)`. Browser-mode document fetches send the same UA. The robots token is unchanged.
- **B9 (switch):** `BROWSER_ENABLED` (default false; compose and Helm set it from the browser worker toggle).
  - While false, `mode: "browser"`, `screenshot: true` and `POST /v1/screenshot` answer 400 `invalid_request` ("Browser mode is not enabled on this service."), and `auto` never escalates.
  - A batch with `mode: "browser"` is also 400 while false.
- **B10 (browser results):**
  - `mode_used: "browser"`, and `timings.render_ms` (0 for fast results).
  - Cached while fresh (`RESULT_CACHE_TTL_SECONDS`) but never revalidated: no validators are stored, and `raw_sha256` is the hash of the rendered HTML.
  - `attempts` counts every document fetch of the item, including navigations.
- **B11 (consent):** every render runs autoconsent with `optOut` (`BROWSER_AUTOCONSENT`, default true), with the predecessor's custom rule for Government Site Builder banners. The `accept_cookies` action runs it with `optIn` instead.
- **B12 (challenges):** nothing waits for or solves a challenge. The rendered page goes through `assess`. In addition, `bot_challenge` is set on a thin page (under 100 words) whose render contacted a challenge host: `challenges.cloudflare.com`, `hcaptcha.com` or one of its subdomains, `geo.captcha-delivery.com`. reCAPTCHA is left out: it sits on too many ordinary contact forms.
- **B13 (sync bound):** a sync request without `wait_seconds` can take the fast phase plus `BROWSER_QUEUE_WAIT_SECONDS + BROWSER_TIMEOUT_SECONDS + 5` (about 45 s after a quick fast phase). `graceful_shutdown_seconds` covers that while `BROWSER_ENABLED`. Callers who need less use `wait_seconds` and get a 202 with the job.
- **D2 (done-when): Decision D2.** Recommended:
  - the gate is `wlo_200` with `mode: "auto"` at or above 80 %;
  - the smoke set (`mode: "auto"`) and `wlo_200` with `mode: "browser"` are reported.

  Why: the smoke set holds expected failures (too-short pages, an error page, a PDF that only package 5 converts), so at most about 20 of its 24 URLs (83 %) can pass. Package 2 ruled the same way.

## Review Focus

1. **A page navigates its main frame to a disallowed target:** a JavaScript redirect, a meta refresh or a clicked link to a private address, a robots-disallowed path (under `enforce`) or another scheme. The navigation is refused before any request: `blocked_url`, `blocked_by_robots` or `invalid_url`, as fast mode would answer. Owner: Tasks 3 and 4.
2. **A sub-resource, iframe or WebSocket points at a forbidden address:** `127.0.0.1`, `169.254.169.254`, `[::1]`, or a public name that resolves to a private IP. The proxy refuses it with 403, and the page still renders without it. Owner: Tasks 2 and 4.
3. **Chromium dies mid-render** (killed, out of memory): the pages in flight fail alone with `internal_error`, retryable, and the pool relaunches Chromium. The worker loop goes on. A worker killed outright leaves its lease to expire, and the item runs again. Owner: Tasks 4 and 6.
4. **No browser worker is running** (scaled to zero, crashed, or not installed while `BROWSER_ENABLED` is true): a sync `auto` request answers its fast result with `browser_unavailable` after `BROWSER_QUEUE_WAIT_SECONDS`, and its job is gone. A sync `browser` request answers `failed`/`timeout`. Owner: Task 7.
5. **Caller `headers` and `cookies` must not leak:** a redirect to another host does not get them; a third-party script on the page does not get the headers; `GET /v1/jobs/{id}` and the logs never show them. Owner: Tasks 3, 6 and 7.

---

### Task 1: Request and result models, settings, escalation rules

**Files:**
- Create: `src/text_extraction/browser_models.py`, `src/text_extraction/escalation.py`, `tests/test_browser_models.py`, `tests/test_escalation.py`
- Modify: `extract_models.py`, `job_models.py`, `quality.py`, `settings.py`, `tests/test_quality.py`, `docs/configuration.md`, `.env.example`

**Interfaces:**
- Produces:
  - `browser_models.py`:
    - `Action`: a discriminated union on `type` of these frozen models:
      - `ClickAction(selector)`
      - `ScrollAction(times: int = 1, 1..50; pause_ms: int = 500, 0..5000)`
      - `WaitForAction(selector: str | None, ms: int | None ≤ 10000)`: exactly one of the two
      - `TypeAction(selector, text ≤ 1000)`
      - `PressAction(key ≤ 32, selector: str | None)`
      - `SelectAction(selector, value ≤ 500)`
      - `ExpandAllAction()`
      - `InfiniteScrollAction(max_scrolls: int = 10, 1..50; pause_ms: int = 1000, 0..5000)`
      - `AcceptCookiesAction()`
      - `EvaluateAction(script ≤ 10000)`

      Every `selector` is 1-500 characters. A list holds at most 20 actions (`MAX_ACTIONS = 20`).
    - `BrowserCookie(name ≤ 256, value ≤ 4096, domain: str | None, path: str = "/", secure: bool = False, http_only: bool = False)`: at most 50.
    - `validate_headers(headers: dict[str, str]) -> dict[str, str]`:
      - at most 20 headers; names are tokens, values printable ASCII ≤ 4096;
      - refused names: `host`, `content-length`, `connection`, `transfer-encoding`, `te`, `upgrade`, `cookie`, `accept-encoding`, `user-agent`, any `proxy-*`;
      - names come back lower-cased.
    - `BrowserSecrets(actions: list[Action] = [], cookies: list[BrowserCookie] = [], headers: dict[str, str] = {})` with `def empty(self) -> bool`.
    - `ArtifactRef(name: str, url: str, content_type: str, bytes: int, width: int, height: int, expires_at: datetime)`.
    - `ScreenshotRequest(url, full_page: bool = True, actions, cookies, headers, compliance, wait_seconds)` with the same field limits as `ExtractRequest`.
    - `ScreenshotResult(url, final_url, upstream_status, mode_used, attempts, title, screenshot: ArtifactRef | None, quality, timings, compliance, warnings)` and `def screenshot_view(result: ExtractResult) -> ScreenshotResult`.
  - `extract_models.py`:
    - `ExtractRequest.mode: Literal["fast", "auto", "browser"]`, default `"auto"`.
    - New fields: `actions: list[Action] = []`, `cookies: list[BrowserCookie] = []`, `headers: dict[str, str] = {}`, `screenshot: bool = False`, `full_page: bool = True`.
    - A model validator: with `mode: "fast"` all of them must be at their defaults (422). A cookie `domain` must be the URL's host or a parent domain of it, not a bare public suffix (one label), per B6.
    - `def secrets(self) -> BrowserSecrets`.
    - `ExtractResult.mode_used: Literal["fast", "browser"]`.
    - `ExtractResult.screenshot: ArtifactRef | None = None`.
    - `Timings.render_ms: int = 0`. Defaults keep old cached entries valid.
  - `job_models.py`:
    - `JobKind` gains `"screenshot"`.
    - `JobOptions.mode` gains `"browser"`.
    - `JobOptions` gains `screenshot: bool = False` and `full_page: bool = True`.
    - `BatchRequest.mode` gains `"browser"`.
  - `escalation.py`:
    - `def needs_browser(envelope: Envelope[ExtractResult]) -> bool` (B5);
    - `def better(fast: Envelope[ExtractResult], rendered: Envelope[ExtractResult]) -> Envelope[ExtractResult]` (B5 ranking);
    - `BROWSER_UNAVAILABLE_WARNING = "browser_unavailable"`.
  - Settings, with bounds:
    - `browser_enabled: bool = False`
    - `browser_timeout_seconds: float = 30` (5..120)
    - `browser_queue_wait_seconds: float = 10` (0..60)
    - `browser_concurrency: int = 4` (≥ 1)
    - `browser_idle_wait_seconds: float = 8` (0..30)
    - `browser_recycle_after_pages: int = 200` (≥ 1)
    - `browser_max_bytes: int = 50_000_000` (≥ 1 MB)
    - `browser_blocked_resources: str = "image,media,font"`. Validated against Playwright's resource types: document is refused, empty means none.
    - `browser_autoconsent: bool = True`
    - `browser_executable_path: Path | None = None` (development only)
    - `screenshot_max_bytes: int = 10_485_760`
    - `screenshot_max_height_px: int = 16384` (1080..32768)
    - `artifact_ttl_seconds: int = 3600` (≥ 60)

- [ ] **Step 1: Write the failing tests.**
  - `test_browser_models.py`:
    - `test_actions_parse_by_type`;
    - `test_more_than_20_actions_is_rejected`;
    - `test_wait_for_needs_exactly_one_of_selector_and_ms`;
    - `test_refused_header_names` (parametrized over the refused list, plus a `proxy-authorization`);
    - `test_header_values_must_be_printable_ascii`;
    - `test_fast_mode_rejects_browser_options` (each of actions, cookies, headers, screenshot gives 422 via `TestClient` or `ValidationError`);
    - `test_cookie_domain_must_match_request_host` (`example.org` for `https://www.example.org/` passes; `other.org` and `org` fail);
    - `test_screenshot_view_keeps_the_artifact`.
  - `test_escalation.py`:
    - `test_js_heavy_fast_result_needs_browser`;
    - `test_browser_result_never_escalates` (`mode_used == "browser"`);
    - `test_non_html_or_failed_fetch_does_not_escalate` (`quality is None`);
    - `test_better_prefers_status_then_words_then_rendered`: `success` 40 words beats `partial` 500 words; `success` 175 beats `success` 42; equal words go to the rendered one.
  - `test_quality.py`: `test_soft_404_recommends_browser`.
  - `tests/test_settings.py`: defaults and bounds of the new settings; an invalid entry in `BROWSER_BLOCKED_RESOURCES` (`document`, `bogus`) is a `ConfigError`.
- [ ] **Step 2:** `uv run pytest tests/test_browser_models.py tests/test_escalation.py tests/test_quality.py tests/test_settings.py -q`. Expect failures.
- [ ] **Step 3:** Implement. Update the `mode` description: `fast` is HTTP only, `auto` uses a browser when the page needs one and browser mode is enabled, `browser` always renders. Document the settings.
- [ ] **Step 4:** Full gates: `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`.
- [ ] **Step 5:** Commit `feat: add browser request models, settings and escalation rules`.

### Task 2: Egress proxy for Chromium

**Files:**
- Create: `src/text_extraction/browser_proxy.py`, `tests/test_browser_proxy.py`
- Modify: `netguard.py` (expose the resolver)

**Interfaces:**
- Consumes: `is_public_address`, and the resolution in `GuardedBackend._resolve` (netguard.py).
- Produces:
  - `netguard.py`: `async def resolve_allowed(host: str, port: int, policy: AddressPolicy) -> list[IPv4Address | IPv6Address]`. It raises `BlockedAddressError` when no address passes and `socket.gaierror` for DNS failures. `GuardedBackend` uses it too.
  - `browser_proxy.py`:
    - `class EgressProxy` with `__init__(settings: Settings, *, policy: AddressPolicy | None = None, registry: CollectorRegistry)`. `async start()` binds `127.0.0.1:0`; `async stop()`; property `url`.
    - `def open_context(self) -> ProxyContext`: new random credentials. `ProxyContext` has `username`, `password`, `bytes: int`, `hosts: frozenset[str]` (capped at 200), `capped: bool`, and `close()`, which ends its tunnels.
    - Metric `text_extraction_browser_proxy_refused_total{reason}`, with reasons `blocked_address`, `dns_error`, `bytes_cap`, `auth`, `bad_request`.

Behaviour:
- **Requests and auth:** HTTP/1.1 `CONNECT host:port` and absolute-form plain `http://` requests. Without `Proxy-Authorization` for a known open context, the answer is 407 with `Proxy-Authenticate: Basic realm="browser"`.
- **Dialing:** the host is resolved once per connection with `resolve_allowed`, and the proxy dials the first allowed address that answers. A plain HTTP request has its `Host` pinned and `Connection: close`, as in the Selenium service's `egress_proxy.py` (reference only).
- **Limits:** bytes are counted both ways per context. Past `BROWSER_MAX_BYTES`, new connections of that context get 403 and `capped` is set. A request head over 64 KiB or older than 15 s is refused. A tunnel lives at most `BROWSER_TIMEOUT_SECONDS + 5`. At most 512 connections over all contexts.
- **Refusals** answer 403 (blocked, capped) or 502 (DNS, connect failure) with `Connection: close`, and are logged with the host only.

- [ ] **Step 1: Write the failing tests** (a local asyncio echo HTTP server; the policy is injected so that 127.0.0.1 counts as public, except where a test checks refusals):
  - `test_connect_tunnel_relays_bytes`;
  - `test_plain_http_request_is_forwarded_with_pinned_host`;
  - `test_missing_or_unknown_credentials_get_407`;
  - `test_private_address_is_refused` (default policy, targets `127.0.0.1`, `[::1]` and `169.254.169.254`; 403 and the metric counted);
  - `test_name_resolving_to_private_ip_is_refused` (resolver monkeypatched);
  - `test_bytes_are_counted_per_context_and_capped`;
  - `test_hosts_seen_are_recorded`;
  - `test_closing_a_context_ends_its_tunnels`;
  - `test_oversized_or_slow_request_head_is_refused`.
- [ ] **Step 2:** Run them; expect failures.
- [ ] **Step 3:** Implement `resolve_allowed`, refactor `GuardedBackend` onto it (the existing netguard tests stay green), and implement the proxy.
- [ ] **Step 4:** Full gates.
- [ ] **Step 5:** Commit `feat: add the SSRF-guarded egress proxy for the browser`.

### Task 3: Pipeline with a renderer

**Files:**
- Create: `src/text_extraction/render.py` (protocol and data only, no Playwright), `tests/test_pipeline_render.py`
- Modify: `pipeline.py`, `fetch.py`, `tests/test_fetch.py`, `tests/fakes.py`

**Interfaces:**
- Consumes: Task 1 models and `needs_browser`/`better`.
- Produces:
  - `fetch.py`:
    - `Fetcher.fetch(..., headers: Mapping[str, str] | None = None, host_headers: tuple[str, Mapping[str, str]] | None = None)`. `headers` are merged over the defaults on every request of the chain, so a `user-agent` given here replaces ours. `host_headers` is `(host, headers)`: those headers go only to requests whose host is that host.
    - `FetchResult.cookies: tuple[CookieData, ...]`, the jar's cookies after the chain. `CookieData(name, value, domain, path, secure, http_only, expires: float | None)`.
  - `render.py`:
    - `NavigateHook = Callable[[str, Mapping[str, str]], Awaitable[FetchResult]]`: fetches a main-frame navigation; raises `FetchError`.
    - `@dataclass(frozen=True) class RenderOptions`: `actions`, `cookies`, `headers`, `screenshot: bool`, `full_page: bool`, `allow_evaluate: bool`.
    - `@dataclass(frozen=True) class Rendered`: `html: str`, `final_url: str`, `document: FetchResult` (the last main document), `screenshot: bytes | None`, `screenshot_size: tuple[int, int] | None`, `warnings: tuple[str, ...]`, `hosts: frozenset[str]`.
    - `class Renderer(Protocol)`: `async def render(self, document: FetchResult, navigate: NavigateHook, options: RenderOptions, *, deadline: float) -> Rendered`. `deadline` is `loop.time()` based.
    - `class ArtifactSink(Protocol)`: `async def put_screenshot(self, png: bytes, width: int, height: int) -> ArtifactRef`.
  - `pipeline.py`:
    - `Pipeline.__init__(..., renderer: Renderer | None = None, timeout_seconds: float | None = None)`. The deadline is `timeout_seconds` or `EXTRACT_TIMEOUT_SECONDS`.
    - `Pipeline.run(req, principal, *, gate_refusals="fail", artifacts: ArtifactSink | None = None)`.
    - `create_pipeline(..., renderer=None, timeout_seconds=None)` passes both through.

Behaviour of a pipeline with a renderer (a pipeline without one behaves as today, except the B5 cache rule):
- **Fast mode:** `mode: "fast"` never renders. A request in `browser` or `auto` mode, or with `screenshot`, follows the steps of package 2 up to the after-fetch compliance check. It holds the gate turn instead of giving it back after the fetch.
- **Request headers:** browser-mode document fetches send `user-agent` = the renderer's UA (B8) and the caller's `headers` (to the request's host only, B6).
- **Not HTML:** a 2xx `text/plain` document converts as in fast mode, with no render. Any other non-HTML type is `unsupported_content_type`, as today.
- **Auto mode:** with `mode: "auto"` and no screenshot, the fetched HTML is processed first. A result that does not `needs_browser` is returned and cached as today. Otherwise the page renders and the final envelope is `better(fast, rendered)`.
- **Rendering:** the renderer gets the document and a `NavigateHook`. The hook runs `_check_origin` and the redirect checks of `_fetch` for the new URL, moves the turn if the host differs, counts an attempt, records the outcome, and applies `after_fetch` compliance; a block raises `FetchError` with the block's code. It sends the caller's headers only when the URL's host is the request's host.
- **Rendered HTML:** goes through `_process` with `rendered.document` for headers and language. Its fingerprint uses `sha256(rendered.html)`. `mode_used="browser"`, `final_url=rendered.final_url`, `timings.render_ms`, and the renderer's warnings are appended.
  - `bot_challenge` is also set per B12 from `rendered.hosts`.
  - Rendered HTML over `CONVERT_MAX_BYTES` is `too_large`.
- **Screenshot:** `rendered.screenshot` goes to `artifacts.put_screenshot`, which is required when `screenshot` is set. It becomes `result.screenshot`. For a screenshot request the envelope status is `success` once stored (B7).
- **Cache:** browser results store no validators, so they are never revalidated (B10). Requests with secrets neither read nor write the cache or fingerprints (B6). Without a renderer and with `BROWSER_ENABLED`, an `auto` result that `needs_browser` is neither cached nor fingerprinted (B5).
- **Errors:** a `FetchError` from the hook or the renderer ends the run as today, and `upstream_status` is the failing document's. The turn is given back when the render ends, on every path.

- [ ] **Step 1: Write the failing tests.** `tests/fakes.py` gets `FakeRenderer`, which returns scripted `Rendered` values and can call `navigate`, plus `FakeArtifacts`. Tests in `test_pipeline_render.py`:
  - `test_browser_mode_renders_the_fetched_document`: the renderer got the fetched bytes, `mode_used == "browser"` and `render_ms > 0`.
  - `test_auto_returns_fast_result_without_render_when_not_needed`.
  - `test_auto_renders_js_heavy_page_and_keeps_the_better_result`: two cases, the rendered result wins, and the fast result wins.
  - `test_navigation_to_robots_disallowed_url_fails_blocked_by_robots`: under `enforce`, the fake fetcher never saw the URL.
  - `test_navigation_to_private_address_fails_blocked_url`.
  - `test_navigation_to_other_host_moves_the_turn_and_counts_an_attempt`: the gate fake shows one turn at a time.
  - `test_caller_headers_only_reach_the_request_host`: the fake fetcher records the headers per URL, and a cross-host redirect hop and a cross-host navigation have no caller header.
  - `test_turn_is_held_during_render_and_released_after`, also when the renderer raises.
  - `test_screenshot_is_stored_and_referenced`.
  - `test_requests_with_secrets_bypass_cache_and_fingerprints`.
  - `test_browser_results_are_never_revalidated`: a stale entry yields an unconditional fetch.
  - `test_auto_result_that_will_escalate_is_not_cached` (no renderer, `BROWSER_ENABLED=true`).
  - `test_plain_text_is_not_rendered`, `test_pdf_in_browser_mode_is_unsupported_content_type`.
  - `test_challenge_host_marks_thin_page_as_bot_challenge`.
  - `test_fast_mode_unchanged`: one existing fast case in each of `browser_enabled` false and true.

  `test_fetch.py` gets:
  - `test_extra_headers_override_defaults`;
  - `test_scoped_headers_not_sent_to_other_hosts` (redirect chain);
  - `test_cookies_of_chain_are_returned`.
- [ ] **Step 2:** Run the new tests; expect failures.
- [ ] **Step 3:** Implement. Keep the turn logic in `_Turn`. `_fetch` gains `hold: bool = False`, and a held turn is given back by the caller. The navigate hook reuses `_check_origin`, `_fetch` (with `hold=True`) and `after_fetch`. No module here imports `playwright`.
- [ ] **Step 4:** Full gates. All package-2 and package-3 tests stay green.
- [ ] **Step 5:** Commit `feat: render pages through a renderer in the pipeline`.

### Task 4: Chromium pool and renderer

**Files:**
- Create: `src/text_extraction/browser.py`, `src/text_extraction/browser_scripts.py` (the JavaScript strings and consent rules), `tests/browser/__init__.py`, `tests/browser/conftest.py`, `tests/browser/site.py`, `tests/browser/test_renderer.py`
- Modify: `pyproject.toml` (extra `browser`, dev group, pytest marker `browser`), `uv.lock`, `.github/workflows/ci.yml` (job `browser`)

**Interfaces:**
- Consumes: `EgressProxy` (Task 2); `Renderer`, `Rendered`, `RenderOptions`, `NavigateHook` (Task 3).
- Produces:
  - `class BrowserPool` with `__init__(settings: Settings, proxy: EgressProxy, registry: CollectorRegistry)`, `async start()`, `async stop()`, and `user_agent: str` (B8, read from the launched browser).
  - `class ChromiumRenderer(Renderer)` with `__init__(pool: BrowserPool, settings: Settings)`, `user_agent`, and `async render(...)`.
  - Metrics:
    - `text_extraction_browser_pages_in_use` (gauge);
    - `text_extraction_browser_pages_total{outcome}` (outcome `ok`, `error`, `crash`, `timeout`);
    - `text_extraction_browser_render_seconds` (histogram, buckets up to 60);
    - `text_extraction_browser_launches_total{reason}` (`start`, `recycle`, `crash`).

Behaviour:
- **Pool:** one Chromium per process, launched headless with the B4 flags and the proxy, and `executable_path=BROWSER_EXECUTABLE_PATH` when set. A semaphore of `BROWSER_CONCURRENCY` pages.
  - After `BROWSER_RECYCLE_AFTER_PAGES` contexts, a new browser is launched for new pages and the old one is closed once its pages end.
  - A `disconnected` browser is relaunched on the next page. Its pages in flight raise, and the renderer maps that to `internal_error`, retryable (outcome `crash`).
- **Per render:**
  - a new context: viewport 1920×1080, `service_workers="block"`, `accept_downloads=False`, `locale="de-DE"`, the context's own proxy credentials and the UA;
  - the caller's cookies are added (domain defaulting to the request host), then the cookies of `document.cookies`.
- **Routing:** `context.route("**/*")`:
  - the first main-frame document is fulfilled from `document`: status, headers without hop-by-hop, `content-encoding` and `content-length`, and the body;
  - a later main-frame document with method GET calls `navigate(url, request headers)`. A redirected answer is fulfilled as a 302 to its `final_url`, and that URL is then fulfilled from the stored result without fetching again. Any other method is aborted with the warning `navigation_refused_non_get`;
  - a resource type in `BROWSER_BLOCKED_RESOURCES` is aborted, unless `options.screenshot` is set;
  - everything else continues to Chromium's network, which means the proxy.
- **Waits and steps, all within `deadline`:**
  - `goto(document.final_url, wait_until="load")`, then up to `BROWSER_IDLE_WAIT_SECONDS` for network idle;
  - consent `optOut` (B11, best effort, 3 s);
  - the actions (Task 5 adds the executor; this task runs none);
  - strip hidden elements: `display:none` subtrees, and `visibility:hidden` leaves that are not script, style, link, meta, template, noscript, title or head;
  - `page.content()`, then the screenshot when asked: PNG, `full_page` clipped at `SCREENSHOT_MAX_HEIGHT_PX`, with the warning `screenshot_clipped`.
- **Close:** the context is always closed, and the proxy context ends with it. Its `hosts` and `capped` feed `Rendered.hosts` and the warning `browser_bytes_capped`.
- **Errors:** a navigation timeout is `FetchError(TIMEOUT, retryable=True)`. A page that kept navigating so that `content()` failed is `FetchError(CONVERSION_FAILED)`.

`tests/browser/site.py` is a local aiohttp-free test site on `asyncio.start_server`, or `http.server` in a thread. Its pages:
- `/static`; `/js` (text written by a script);
- `/xhr` (text fetched from `/api`);
- `/redirect-js` (`location` to `/static`); `/meta-refresh`;
- `/to-private` (`location` to `http://169.254.169.254/`); `/img-private` (`<img>` and `fetch()` to `127.0.0.1:<other port>`);
- `/hidden-tabs`; `/consent` (a `#cookiebanner` with the Government Site Builder button);
- `/tall` (20000 px); `/never-idle` (long polling);
- `/form-post`; `/set-cookie`.

`tests/browser/conftest.py`:
- finds Chromium: `BROWSER_EXECUTABLE_PATH`, else Playwright's own, else `/opt/pw-browsers/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell`, else skip;
- runs the site with `FETCH_ALLOW_PRIVATE_NETWORKS=true` and a proxy policy that admits 127.0.0.1 on the site's port only, so private-address tests still refuse other ports and `169.254.169.254`.

- [ ] **Step 1: Write the failing tests** (`@pytest.mark.browser`, real Chromium, the real `Pipeline` with the fake gate where it helps):
  - `test_static_page_renders`;
  - `test_script_written_text_is_extracted`; `test_xhr_content_is_extracted`;
  - `test_js_redirect_goes_through_navigate`; `test_meta_refresh_goes_through_navigate`;
  - `test_navigation_to_metadata_ip_is_refused` (`blocked_url`);
  - `test_private_subresources_are_refused_and_page_still_renders`;
  - `test_hidden_tabs_are_stripped`; `test_consent_banner_is_opted_out`;
  - `test_full_page_screenshot_is_1920_wide_and_clipped` (PNG header width 1920, height 16384, warning);
  - `test_never_idle_page_finishes_within_deadline`;
  - `test_form_post_navigation_is_refused`;
  - `test_document_cookies_reach_the_page` (`document.cookie` shows the server's cookie);
  - `test_images_blocked_for_extraction_not_for_screenshot`;
  - `test_browser_crash_fails_page_and_pool_relaunches` (close the browser mid-render);
  - `test_recycle_launches_new_browser`;
  - `test_user_agent_is_honest`.
- [ ] **Step 2:** `uv run pytest -m browser tests/browser -q`; expect failures. `pyproject.toml` excludes the marker by default (`addopts = "-m 'not browser'"`), so `uv run pytest` stays fast.
- [ ] **Step 3:** Add the extra and the dev dependencies (check both licences in the lock: Apache-2.0 and MPL-2.0). Implement.
- [ ] **Step 4:** In CI, add the job `browser`: `uv sync --locked`, `uv run playwright install --with-deps --only-shell chromium`, `uv run pytest -m browser -q`. Run the browser tests locally, then the full gates.
- [ ] **Step 5:** Commit `feat: add the Chromium pool and renderer`.

### Task 5: Browser actions

**Files:**
- Create: `src/text_extraction/browser_actions.py`, `tests/browser/test_actions.py`
- Modify: `browser.py`, `tests/browser/site.py`

**Interfaces:**
- Consumes: the `Action` models (Task 1); the page and deadline inside `ChromiumRenderer.render`.
- Produces: `async def run_actions(page: Page, actions: Sequence[Action], *, allow_evaluate: bool, deadline: float, consent: ConsentRunner) -> list[str]`, which returns warnings. `ConsentRunner` is the consent call of Task 4 with `opt: Literal["optOut", "optIn"]`.

Behaviour:
- **Order:** actions run in order after the consent step, each bounded by the time left.
- **Failure:** a failing action (no element, timeout, navigation refused) stops the remaining ones and adds the warning `action_<index>_failed`. The page is extracted as it is.
- **Per action:**
  - `click`, `type`, `press`, `select`: Playwright locators, first match;
  - `scroll`: `times` viewport heights, pausing `pause_ms`;
  - `infinite_scroll`: scroll to the bottom until the page height stops growing or `max_scrolls`;
  - `expand_all`: opens closed `<details>` and clicks up to 50 visible elements with `aria-expanded="false"`;
  - `accept_cookies`: the consent runner with `optIn`;
  - `evaluate`: runs only when `allow_evaluate`; otherwise it never reaches here (the API answers 403, Task 7) and raises `ValueError` as a guard.
- **Navigation:** a click that navigates goes through `navigate` (Task 4 routing). The action waits for `load` after a navigation.

- [ ] **Step 1: Write the failing tests** (site pages `/actions`: a button revealing text, a `<details>`, a select, an input echo, a lazy list growing on scroll, a link to `/static`):
  - `test_click_reveals_text`; `test_type_and_press_enter`; `test_select_option`;
  - `test_expand_all_opens_details_and_aria_toggles`;
  - `test_infinite_scroll_loads_more_and_stops`;
  - `test_failed_action_stops_the_rest_with_warning`;
  - `test_click_navigation_goes_through_navigate`;
  - `test_evaluate_requires_admin_flag`;
  - `test_accept_cookies_opts_in`;
  - `test_actions_respect_the_deadline`.
- [ ] **Step 2:** Run them; expect failures.
- [ ] **Step 3:** Implement and wire it into `ChromiumRenderer.render`.
- [ ] **Step 4:** Browser tests and full gates.
- [ ] **Step 5:** Commit `feat: add browser actions`.

### Task 6: Browser worker, escalation in the extract worker, artifacts and secrets

**Files:**
- Create: `src/text_extraction/browser_worker.py`, `tests/test_browser_worker.py`
- Modify: `jobs.py`, `scheduler.py`, `worker.py`, `queue_metrics.py`, `pyproject.toml` (script `text-extraction-browser-worker`), `tests/test_jobs.py`, `tests/test_scheduler.py`, `tests/test_worker.py`

**Interfaces:**
- Consumes: Tasks 1-5.
- Produces:
  - `jobs.py`:
    - `JobStore.create(..., secrets: BrowserSecrets | None = None)`: secrets go to `job:{<id>}:secrets` with the B6 TTL.
    - `async secrets(job_id) -> BrowserSecrets` (empty when gone) and `async drop_secrets(job_id) -> None`.
    - `async put_artifact(job_id, name, data: bytes) -> datetime`, which returns the expiry, and `async artifact(job_id, name) -> bytes | None`. The key is `job:{<id>}:artifact:<name>` with `ARTIFACT_TTL_SECONDS`; `delete` removes the known artifact names too.
    - `async clear_lease(job_id) -> None`, the same as `mark_running(job_id, lease_seconds=None)`.
  - `scheduler.py`: `FairQueue.enqueue_items(job_id: str, client: str, items: list[tuple[int, str]]) -> None`. `enqueue` becomes a call of it with `enumerate(urls)`.
  - `worker.py`:
    - `class ItemRunner(Protocol)`: `lease_seconds: float`, and `async run(job: JobRecord, item: QueueItem, url: str, *, gate_refusals) -> Envelope[ExtractResult] | None`. None means the item was moved: it is completed without a result and counted as `escalated`.
    - `Worker(settings, valkey, runner: ItemRunner, store, queue, registry, ...)` replaces the `pipeline` argument.
    - `ExtractRunner(pipeline, settings, browser_queue: FairQueue | None)`: after a run with `job.options.mode == "auto"` whose envelope `needs_browser`, and a browser queue (that is, `BROWSER_ENABLED`), it calls `browser_queue.enqueue_items(job.id, item.client, [(item.index, url)])` and returns None.
    - `build_worker` passes `FairQueue(valkey, settings, "browser")` when `BROWSER_ENABLED`.
  - `browser_worker.py`:
    - `BrowserRunner(pipeline, store, settings)`: `lease_seconds = 2 × BROWSER_TIMEOUT_SECONDS + 10`.
      - It reads the secrets and builds the request from `job.options` and secrets. Kind `screenshot` gives `mode="browser"`, `screenshot=True`, `metadata=False`. The owner becomes the principal; `allow_evaluate` is set when the owner role is admin.
      - It runs with an `ArtifactSink` bound to the job (`put_artifact` with `screenshot.png`, and `ArtifactRef.url = /v1/jobs/<id>/artifacts/screenshot.png`). It drops the secrets in a `finally`.
      - A memory guard that is high makes the sink raise. The pipeline maps that to `internal_error`, retryable (B7).
    - `build_browser_worker(settings, valkey, registry) -> BrowserWorkerParts`, and `main()`.
      - It starts the proxy and pool before `/ready`, and stops them after the loops.
      - `WORKER_CONCURRENCY` for this pool is `BROWSER_CONCURRENCY`.
      - The pipeline deadline is `BROWSER_TIMEOUT_SECONDS`.
  - `queue_metrics.py`: nothing new in the API. The app creates a second `QueueGauges` for pool `browser` when `BROWSER_ENABLED` (wired in Task 7).

- [ ] **Step 1: Write the failing tests:**
  - `test_jobs.py`:
    - `test_secrets_round_trip_and_ttl`; `test_secrets_not_in_job_view`;
    - `test_artifact_round_trip_and_ttl`; `test_delete_removes_secrets_and_artifacts`;
    - `test_all_job_keys_share_one_hash_tag` (extended).
  - `test_scheduler.py`: `test_enqueue_items_keeps_given_indexes`.
  - `test_worker.py`:
    - `test_auto_item_needing_browser_moves_to_browser_pool` (completed in `extract`, queued in `browser` with the same index, no result written, metric `escalated`);
    - `test_no_move_when_browser_disabled`;
    - `test_existing_worker_tests_pass_with_extract_runner`.
  - `test_browser_worker.py` (a `FakeRenderer` pipeline):
    - `test_screenshot_job_stores_artifact_and_result`;
    - `test_secrets_are_used_and_dropped`, also when the run raises;
    - `test_admin_owner_allows_evaluate_user_does_not`;
    - `test_memory_guard_high_fails_screenshot_retryable`;
    - `test_lost_lease_and_exhausted_items_behave_as_extract_pool` (one case each, reusing the package-3 scenarios);
    - `test_ready_only_after_browser_started` (pool start faked).
- [ ] **Step 2:** Run them; expect failures.
- [ ] **Step 3:** Implement. The worker's module docstring describes the runner split. `browser_worker.py` is the only module outside `browser.py` and `browser_actions.py` that imports them.
- [ ] **Step 4:** Full gates, plus `uv run python -c "import text_extraction.app, text_extraction.worker, sys; sys.exit('playwright' in sys.modules)"`.
- [ ] **Step 5:** Commit `feat: add the browser worker and auto escalation in the extract worker`.

### Task 7: API: browser dispatch, screenshot and artifact routes

**Files:**
- Create: `src/text_extraction/browser_dispatch.py`, `src/text_extraction/screenshot_api.py`, `tests/test_browser_dispatch.py`, `tests/test_screenshot_api.py`
- Modify: `extract_api.py`, `jobs_api.py`, `app.py`, `server.py`, `tests/test_extract_api.py`, `tests/test_jobs_api.py`, `tests/test_server.py`

**Interfaces:**
- Consumes: Tasks 1, 3 and 6.
- Produces:
  - `browser_dispatch.py`:
    - `class BrowserDispatcher` with `__init__(store: JobStore, queue: FairQueue, settings: Settings, *, clock=time.monotonic, sleep=asyncio.sleep)`.
    - `async submit(kind: Literal["extract", "screenshot"], req: ExtractRequest, principal: Principal, compliance: ComplianceMode, *, job_id: str | None = None) -> str`. It creates the job with the secrets, or reuses `job_id` and clears its lease, then enqueues item 0 on `browser` under `<name>#live`.
    - `async wait(job_id: str, *, detached: Callable[[], bool]) -> Envelope[ExtractResult] | None`. It returns None when the item was not claimed within `BROWSER_QUEUE_WAIT_SECONDS`, gave no result within `BROWSER_TIMEOUT_SECONDS + 5` after the claim, or `detached()` turned true.
    - `async abandon(job_id: str) -> None`, which deletes the job quietly.
    - `class Handover`: `job_id: str | None` (the P1 job the route created) and `browser_job: str | None` (the dispatched job), with `detach()` and `detached`.
  - `extract_api.py`: the route's task becomes `_flow(body, principal, handover)`:
    1. 400 per B9; 403 for `evaluate` by a user.
    2. `mode: "browser"`, or `screenshot`: submit, then wait.
    3. Otherwise `pipeline.run`. If `auto` and `needs_browser`, submit with `job_id=handover.job_id`. If the route had already handed over, return None (the worker writes item 0). Otherwise wait; on None, abandon and answer the fast envelope with `browser_unavailable` (`auto`) or `failed`/`timeout`, retryable (`browser`).
    4. When `wait_seconds` passes while a browser job is waited for, the route detaches the task and answers 202 with `handover.browser_job`; no P1 job is created. Otherwise P1 is unchanged, and the done-callback stores nothing for a None result.
  - `screenshot_api.py`:
    - `POST /v1/screenshot` → `Envelope[ScreenshotResult]`, via the same dispatch with kind `screenshot`, and 202 with `wait_seconds`.
    - `GET /v1/jobs/{id}/artifacts/{name}` → `image/png`. The name matches `[a-z0-9_.-]{1,64}`. Unknown or expired jobs, foreign jobs and missing artifacts answer 404 `not_found`; `JOBS_ENABLED=false` answers 503.
  - `jobs_api.py`:
    - `POST /v1/batch` with `mode: "browser"` enqueues on `browser`, or answers 400 per B9.
    - The lost view (P1) does not apply to a job whose lease was cleared.
    - A `screenshot` job's items are shown as stored.
  - `app.py`: `app.state.browser_dispatcher`, the browser queue, and `QueueGauges` for `browser` when `BROWSER_ENABLED`.
  - `server.py`: `graceful_shutdown_seconds` = max(the current value, `BROWSER_QUEUE_WAIT_SECONDS + BROWSER_TIMEOUT_SECONDS + 10`) when `BROWSER_ENABLED` (B13).

- [ ] **Step 1: Write the failing tests** (fakeredis; a fake worker task that claims from `browser` and writes a result):
  - `test_browser_dispatch.py`:
    - `test_submit_creates_job_with_secrets_and_live_client`; `test_submit_reuses_handover_job`;
    - `test_wait_returns_result`; `test_wait_gives_up_when_unclaimed`; `test_wait_gives_up_after_render_timeout`;
    - `test_abandon_deletes_job`.
  - `test_extract_api.py`:
    - `test_browser_mode_disabled_is_400`; `test_evaluate_by_user_is_403`;
    - `test_auto_escalates_and_returns_browser_result`;
    - `test_auto_without_worker_returns_fast_with_browser_unavailable` (Review Focus 4); `test_browser_mode_without_worker_is_timeout`;
    - `test_wait_seconds_during_browser_phase_answers_202_with_browser_job`;
    - `test_handover_during_fast_phase_then_escalation_writes_into_p1_job`;
    - `test_secrets_never_in_job_view_or_logs` (Review Focus 5, with `caplog`/structlog capture);
    - `test_fast_mode_and_auto_with_browser_disabled_unchanged`.
  - `test_screenshot_api.py`:
    - `test_screenshot_returns_artifact_ref`; `test_artifact_download_png`;
    - `test_foreign_or_unknown_artifact_is_404`; `test_screenshot_disabled_is_400`.
  - `test_jobs_api.py`: `test_batch_browser_mode_goes_to_browser_pool`; `test_batch_rejects_actions_cookies_headers` (422, extra fields forbidden).
  - `test_server.py`: `test_graceful_seconds_cover_browser_wait`.
- [ ] **Step 2:** Run them; expect failures.
- [ ] **Step 3:** Implement. Update the OpenAPI descriptions of `mode`, `actions`, `cookies`, `headers`, `screenshot` and the new routes. `GET .../artifacts/{name}` declares `image/png` in `responses`.
- [ ] **Step 4:** Full gates.
- [ ] **Step 5:** Commit `feat: dispatch browser work from the API and add screenshots`.

### Task 8: Image, compose, Helm and CI

**Files:**
- Modify: `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `.env.example`, `.github/workflows/image.yml`, `.github/workflows/ci.yml`, `deploy/helm/text-extraction/values.yaml`, `templates/*` (worker deployment, service, KEDA/HPA, NetworkPolicy, PrometheusRule, ConfigMap, `_helpers.tpl`, `NOTES.txt`), `docs/configuration.md`
- Create: `deploy/helm/text-extraction/templates/browser-worker-*.yaml`, if the extract templates cannot be parametrised by pool cleanly. Prefer one template ranged over a pool list.

**Interfaces:**
- Consumes: entry point `text-extraction-browser-worker` (Task 6).
- Produces:
  - **Dockerfile:**
    - Build stage `build-browser` runs `uv sync --locked --no-dev --no-editable --extra browser`.
    - Target `browser`: `python:3.13-slim`, then `playwright install --with-deps --only-shell chromium` as root, with `PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright`, and apt lists removed. It runs as non-root 10001 with `CMD ["text-extraction-browser-worker"]`.
    - The default target stays the API image, unchanged in content.
  - **image.yml:** a matrix over `api` (image `ghcr.io/<repo>`) and `browser` (image `ghcr.io/<repo>-browser`), same tags.
    - A step after the API build fails if Playwright is importable in the API image.
    - A step after the browser build runs `text-extraction-browser-worker --check`. `--check` launches Chromium once and exits 0; add it in this task to `browser_worker.main`.
  - **Compose:** service `browser-worker` built with `target: browser`, `BROWSER_ENABLED=true` for all app services, `read_only` with tmpfs `/tmp` (size 512m), `stop_grace_period: 75s`, and a `/ready` healthcheck.
  - **Helm:**
    - `workers.browser`: `enabled: true`, `image.repository`, `concurrency: 4`, resources (requests 1 CPU / 1536Mi, limits 2 CPU / 3Gi), `terminationGracePeriodSeconds: 90`, KEDA (`pool="browser"`, threshold 8) or HPA, `emptyDir` `/tmp` (sizeLimit 1Gi), `readOnlyRootFilesystem: true`, all capabilities dropped, seccomp `RuntimeDefault`.
    - `BROWSER_ENABLED` is rendered from `workers.browser.enabled` into the API and extract worker pods; a `config.BROWSER_ENABLED` entry is ignored.
    - The grace-period validate check covers the browser bound.
    - Alerts `TextExtractionBacklog` and `TextExtractionWorkersDown` per pool.
    - NetworkPolicy: browser workers accept 9090 from `metricsFrom` only.
  - **CI:** the compose job's config check covers the new service. The `helm` job lints with browser workers on and off.

- [ ] **Step 1:** Write the checks first:
  - `helm template` assertions in a script `deploy/helm/test.sh`, or extend the existing CI step. The browser Deployment exists only when enabled; `BROWSER_ENABLED` follows the toggle; the KEDA query names `pool="browser"`; render fails when `terminationGracePeriodSeconds` is too small.
  - `docker compose config -q` passes with the dummy env.
- [ ] **Step 2:** Run `helm lint` and the template checks (helm from the scratchpad build); expect the new assertions to fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `helm lint` on and off, `docker compose config -q`, full gates. The Docker build is verified on Jan's PC through Remote Control, in a new clone folder (never `C:\Users\jan\staging\Windsurf\text-extraction`):
  - `docker compose up --build -d --wait`;
  - `POST /v1/extract` with `mode: "browser"` on `https://www.zoerr.de/edu-sharing/components/render/33001463-6df3-40ac-8c4c-5d06eb7fd7dd` returns `mode_used: "browser"` and at least 50 words;
  - `POST /v1/screenshot` on `https://klexikon.zum.de/wiki/Dante_Alighieri` returns a PNG 1920 px wide via the artifact route;
  - `docker images` shows the API image without Chromium (size noted).
- [ ] **Step 5:** Commit `feat: ship the browser worker image, compose service and Helm deployment`.

### Task 9: Live gate, docs and done-when

**Files:**
- Modify: `tests/live/run_live.py`, `tests/live/README.md`, `tests/test_live_run.py`, `.github/workflows/live.yml`, `docs/architecture.md`, `docs/packages.md`, `docs/configuration.md`, `README.md`, `CLAUDE.md` (only if a command changes)

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `run_live.py --mode fast|auto|browser` (default `auto`), sent as the request's `mode`. Each JSONL line gains `mode_used`, and the summary counts results per `mode_used`.
  - `live.yml`:
    - installs Chromium (`uv run playwright install --with-deps --only-shell chromium`);
    - starts the browser worker next to the service with `BROWSER_ENABLED=true`;
    - the gate (D2) runs `wlo_200` with `--mode auto --min-ok-rate 80`;
    - reports: `smoke` with `--mode auto`, and `wlo_200` with `--mode browser`.

    The defaults of the manual run change accordingly.
  - Docs:
    - architecture: a new section "Browser worker" holding B1-B13 and D1-D3, plus the updated pipeline step 4 and Public API rows;
    - `packages.md` row 4: done-when as decided in D2;
    - configuration: all settings and the Helm keys;
    - README: a `mode: "browser"` example, a screenshot example and the artifact download.

- [ ] **Step 1: Write the failing test:** `test_live_run.py::test_mode_option_is_sent_and_reported`.
- [ ] **Step 2:** Run it; expect failure.
- [ ] **Step 3:** Implement the harness change, the workflow and the docs.
- [ ] **Step 4:** Full gates. Then label the PR `live` and wait for the run.
  - Done when `wlo_200` in `auto` mode reaches at least 80 % and the reports are attached to the PR.
  - A miss goes to `systematic-debugging` on the buckets (per domain: fast vs. browser, as in `domain_comparison_simple_vs_browser_20260928.csv`) before any setting is tuned.
- [ ] **Step 5:** Commit `docs: document the browser worker; live gate at the browser baseline`.

## Decisions for Jan (decision cards)

- **D1, how the browser loads the page:**
  - *Through our fetcher* (recommended, B2): every main-frame document is checked per hop like fast mode (robots, TDM, `noai`, gate, SSRF), and Chromium runs the scripts. Bot walls tied to Chromium's TLS fingerprint stay closed.
  - *Chromium loads everything*: the start URL and the final URL are checked, and redirect hops in between are not checked before they are requested. Somewhat more pages get past bot walls.
- **D2, what the done-when measures:**
  - *`wlo_200` in auto mode ≥ 80 %* (recommended), smoke reported.
  - *Smoke set ≥ 80 %*, literally per `packages.md`: at most about 83 % is reachable, and it is flaky.
  - *Both.*
- **D3, Patchright (stealth):**
  - *Later, not in package 4* (recommended): it conflicts with the honest-UA rule, adds a second browser driver, and is not needed for the done-when.
  - *Build it admin-only now*: `stealth: true` for admins and a second driver in the browser image.
