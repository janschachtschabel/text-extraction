# Package 4: Browser Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pages that need JavaScript get a real browser:
- `mode: "browser"` renders a page in Chromium; `mode: "auto"` escalates a fast result that needs it.
- Callers can run `actions`, pass `cookies` and `headers`, and take full-page screenshots at 1920×1080 (`POST /v1/screenshot`, `screenshot: true`).
- Cookie banners are opted out of; bot challenges are reported.
- For admins with `compliance: "ignore"` (sites whose owner allowed us by contract), an **unblock mode** gets past bot protection with Patchright and real Chrome.
- A browser worker image, pool and Helm deployment run all of it.

**Architecture:**
- **Where Chromium runs:** only in the new browser worker (`text-extraction-browser-worker`, image `text-extraction-browser`). It claims items from the package-3 fair queue, pool `browser`. API pods and extract workers never start a browser. A sync request puts a one-item job on the `browser` pool and waits for its result.
- **Hybrid page loading (D1, Jan 2026-10-24):** Chromium loads the page itself, document included, so the target sees a real browser. Our checks wrap it: compliance and the outbound gate run before the navigation; every later main-frame navigation is checked before Chromium may send it; HTTP redirect hops and the response's own statements (headers, meta, `noai`) are checked when the answer arrives, before any content is used.
- **SSRF:** all Chromium traffic, document included, goes through an in-process egress proxy. It resolves each host, refuses non-public addresses and counts bytes per page. It is a plain `CONNECT` tunnel, so TLS stays end-to-end and Chromium's own TLS fingerprint reaches the site.
- **One pipeline, plus a renderer:** the browser worker runs the existing `Pipeline` with a `Renderer` in place of the fetch. The rendered DOM goes through the same conversion, quality gate, metadata, fingerprint and cache as fast mode.
- **Auto mode:** a fast result whose `quality.browser_recommended` is set escalates. Its fast envelope is stored as a draft with the item, the browser worker renders, and the better of the two results is kept.
- **Unblock mode (D3, Jan 2026-10-24):** under `compliance: "ignore"` (admins only) and `BROWSER_UNBLOCK_ENABLED`, the render runs in Patchright with Google Chrome, headful under Xvfb, with Chrome's own user agent. It waits for non-interactive challenges to clear and reuses clearance cookies per host. Auto mode then also escalates bot walls and 403/429/503 challenge answers.

**Tech Stack:** existing, plus these, installed only in the browser image (extra `browser`) and the dev group:
- `playwright` (Apache-2.0);
- `patchright` (Apache-2.0);
- `duckduckgo-autoconsent-py` (MPL-2.0, used unmodified).

The browser image additionally holds Google Chrome stable (freeware licence, not GPL; only for unblock mode) and Xvfb (MIT/X11).

**Spec:**
- `docs/architecture.md`:
  - Overview (browser-worker pool, images);
  - Extraction pipeline steps 1, 3 and 4 (SSRF re-checked for browser sub-requests, auto quality gate);
  - Public API (`mode`, `actions`, `screenshot`, `POST /v1/screenshot`, `GET /v1/jobs/{id}/artifacts/{name}`);
  - Auth (admin: `compliance: ignore`, JavaScript actions, stealth browser);
  - Feature notes "Browser actions" and "Sync-to-job";
  - Observability (browser pool usage).
- "Lessons" table: cookie auto-consent; browser baseline 80 %, median 9-10 s.
- `docs/packages.md` row 4 (done-when per D2).
- `CLAUDE.md` rules 2 and 4-7. Rule 6 gets the unblock exception in Task 10.
- Jan, 2026-10-24: hybrid loading (D1), done-when `wlo_200` in auto mode ≥ 80 % (D2), Patchright now, tied to the option that switches the compliance checks off (D3). Reason: partners who allowed extraction by contract do not change their bot protection, so we must get past it technically. Once getting past is allowed, unblock mode should reach as much as it can.
- Reference (read only): the Playwright service at `/mnt/user-data/uploads/text-extraction/Wissen/text-extraction-main/text-extraction-main/text_extraction/grab_content.py`:
  - `default_goto` (load, then up to 8 s network idle);
  - `handle_cookie_consent` and `CUSTOM_CONSENT_RULES`;
  - `strip_hidden_elements`.
  Write the code fresh from what these do; do not copy it.

## Global Constraints

- English only. Python 3.13. New runtime dependencies only `playwright>=1.64`, `patchright>=1.64` and `duckduckgo-autoconsent-py>=16`. They go in the optional extra `browser` and the dev group, never in the API image.
- The API image must not contain Playwright, Patchright, Chrome or Chromium. No module imported by `text_extraction.app` or `text_extraction.worker` may import `playwright` or `patchright`.
- Ports: the browser worker serves only 9090 (`/health`, `/ready`, `/metrics`), like the extract worker.
- Stateless pods. Jobs, drafts, queue items, secrets, screenshots and clearance cookies live in Valkey with TTLs. Browser profiles are fresh temporary directories per launch or per page and are deleted after use.
- Every new setting is an env var with a documented default in `docs/configuration.md` and `.env.example`, in the task that adds it. Helm sets it where the chart owns it.
- Result envelope everywhere: a failed render is 200 with `status: failed`. 5xx only for our own faults.
- SSRF: every byte a browser sends goes through the egress proxy, in both modes. No request leaves a browser directly: no QUIC, no WebRTC UDP, no DNS of its own.
- Outbound gate in both modes: a render holds its host's turn. Unblock mode changes how we look, never how often we come. No IP or proxy rotation.
- Never log URLs, query strings, cookies, header values, action text or scripts. Host and `path_hash` at most.
- The fast pipeline's answers for `mode: "fast"`, and for `mode: "auto"` while `BROWSER_ENABLED=false`, must not change.
- Not in this package: PDF and Office routing (package 5: a download in browser mode is `unsupported_content_type`), S3 offload (package 11), actions/cookies/headers in batches (B6), captcha-solving services (B15).

## Rulings in this plan

- **B1 (where the browser runs):** see Architecture.
  - A sync request creates a job of kind `extract` or `screenshot` with one URL.
  - It enqueues the item on pool `browser` under the client `<key name>#live`, so the key's own batches do not queue it.
  - It polls the job: 0.2 s, rising to 0.5 s.
  - It waits `BROWSER_QUEUE_WAIT_SECONDS` (10) for a worker to claim the item (job `running`), then `BROWSER_TIMEOUT_SECONDS` + 5 s for the result.
  - A request that gives up deletes the job, so a worker that claims it later drops it.
- **B2 (hybrid page loading, D1).** One render runs these steps:
  1. **Before the navigation:** `_check_origin` for the request URL (missing compliance files fetched by our `Fetcher` in the turn). A block ends the item without any browser request. Then the host's gate turn is taken and held for the render.
  2. **The first navigation:** `page.goto(url)`. Chromium fetches the document itself, through the proxy.
  3. **Later main-frame navigations** (a JavaScript redirect, a meta refresh, a clicked link): Chromium may send them only after a check. `context.route` intercepts each main-frame document request after the first and runs `_check_origin` for its URL, moving the turn when the host differs. Then `route.continue_()`, or `route.abort()` and the block's error. A non-GET navigation is allowed only in unblock mode (B14); otherwise it is aborted with the warning `navigation_refused_non_get`.
  4. **HTTP redirect hops**, which Playwright cannot intercept, and the final document are checked when the response arrives: the robots, TDM and `noai` verdicts of every URL in `response.request.redirected_from`, and `after_fetch` on the final response's headers and the rendered DOM. A block discards the render and ends with the block's code. A hop on another host gets its turn from then on; its first request went out ungated (the one accepted gap of the hybrid).
  5. **Headers and errors:** the caller's `headers` are added by the route handler only to requests on the request URL's host, so no third party (analytics, CDN) sees them. The final response's status and content type are judged as in fast mode: non-2xx is `target_http_error`; a download or non-HTML type is `unsupported_content_type`, which package 5 routes.
- **B3 (outbound limits):** a render holds the host's gate turn from the first navigation until the page is closed. Sub-resources take no turn, like a visitor's browser; Chromium caps them at 6 connections per host. Extraction renders in standard mode block images, media and fonts (`BROWSER_BLOCKED_RESOURCES`). Screenshot renders and unblock renders block nothing.
- **B4 (SSRF):** all browser traffic goes through the egress proxy (`browser_proxy.py`), bound to 127.0.0.1.
  - Chromium and Chrome launch flags: `--proxy-server` and `--proxy-bypass-list=<-loopback>`; `--disable-quic`; `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`; `--disable-dev-shm-usage`.
  - Each page gets its own proxy credentials, so the proxy can tell pages apart; the per-page byte cap is `BROWSER_MAX_BYTES`.
  - Standard mode blocks service workers and refuses downloads.
- **B5 (auto escalation):**
  - `quality.browser_recommended` becomes `js_heavy or bot_challenge or soft_404`, as architecture step 4 says.
  - A fast result escalates when it is `mode_used: "fast"`, comes from 2xx HTML, has `browser_recommended`, and `BROWSER_ENABLED` is true. Under unblock (B14) it also escalates on `bot_protection`, and on a 403, 429 or 503 answer whose body is a challenge.
  - A result that will escalate is neither cached nor fingerprinted by the fast run. Its envelope is stored as a draft (`job:{<id>}:drafts`, index → JSON, job TTL) when the item is queued on `browser`.
  - The browser worker renders an `auto` item and keeps the better of draft and render: higher status (`success` > `partial` > `failed`), then more words, and on a tie the rendered one. An `auto` item without a draft (a batch item that was moved before the draft write) runs the fast step itself first.
  - The extract worker moves an escalating batch item to the `browser` pool (same job and index) and completes it in `extract` without a result.
  - If no worker claims a sync `auto` item in time, or the render does not answer, the request returns its fast result with the warning `browser_unavailable`. Sync `browser` requests return `failed`/`timeout`, retryable.
- **B6 (caller secrets):** `actions`, `cookies` and `headers` are accepted by `/v1/extract` and `/v1/screenshot` only, not by `/v1/batch`.
  - They are stored in `job:{<id>}:secrets` with a TTL of `2 × BROWSER_TIMEOUT_SECONDS + BROWSER_QUEUE_WAIT_SECONDS + 60` s. The worker deletes them when it finishes the item. They never appear in `GET /v1/jobs`.
  - Requests carrying any of them bypass the result cache and fingerprints, both reads and writes.
  - `headers` reach only requests on the requested URL's host (B2 step 5). Typical use: an `Authorization` header or a session token for a partner's logged-in pages.
  - `cookies` may only name that host or a parent domain of it.
  - `evaluate` is admin-only: 403 `forbidden` for users.
- **B7 (screenshots):** PNG only, viewport 1920×1080.
  - `full_page` (default true) is clipped at `SCREENSHOT_MAX_HEIGHT_PX` (16384), with the warning `screenshot_clipped`. A PNG over `SCREENSHOT_MAX_BYTES` (10 MiB) fails with `too_large`.
  - Stored as `job:{<id>}:artifact:screenshot.png` for `ARTIFACT_TTL_SECONDS` (3600). Served by `GET /v1/jobs/{id}/artifacts/screenshot.png` (`image/png`, own jobs only, otherwise 404).
  - While Valkey's memory guard is high, nothing is stored and the item fails `internal_error`, retryable.
  - A screenshot is `success` once stored. Its `quality` is reported but does not fail it.
- **B8 (user agent):** standard mode sends Chromium's own UA plus ` text-extraction/<version> (+https://github.com/janschachtschabel/text-extraction)`, and the robots token is unchanged. Unblock mode (B14) sends Chrome's unmodified UA.
- **B9 (switches):**
  - `BROWSER_ENABLED` (default false). Compose and Helm set it from the browser worker toggle. While false, `mode: "browser"`, `screenshot: true`, `POST /v1/screenshot` and a batch with `mode: "browser"` answer 400 `invalid_request` ("Browser mode is not enabled on this service."), and `auto` never escalates.
  - `BROWSER_UNBLOCK_ENABLED` (default false). While false, `compliance: "ignore"` renders in standard mode.
- **B10 (browser results):**
  - `mode_used: "browser"`, and `timings.render_ms` (0 for fast results).
  - Cached while fresh (`RESULT_CACHE_TTL_SECONDS`) but never revalidated: no validators are stored, and `raw_sha256` is the hash of the rendered HTML.
  - `attempts` counts the main-frame document responses of the item, hops included.
  - The new boolean `unblocked` is true when unblock mode rendered the result.
- **B11 (consent):** every render runs autoconsent with `optOut` (`BROWSER_AUTOCONSENT`, default true), with the predecessor's custom rule for Government Site Builder banners. The `accept_cookies` action runs it with `optIn` instead.
- **B12 (challenges in standard mode):** nothing waits for or solves a challenge. The rendered page goes through `assess`. In addition, `bot_challenge` is set on a thin page (under 100 words) whose render contacted a challenge host: `challenges.cloudflare.com`, `hcaptcha.com` or one of its subdomains, `geo.captcha-delivery.com`. reCAPTCHA is left out: it sits on too many ordinary contact forms.
- **B13 (sync bound):** a sync request without `wait_seconds` can take the fast phase plus `BROWSER_QUEUE_WAIT_SECONDS + BROWSER_TIMEOUT_SECONDS + 5` (about 45 s after a quick fast phase; unblock uses `BROWSER_UNBLOCK_TIMEOUT_SECONDS`, 60). `graceful_shutdown_seconds` covers the larger of both while `BROWSER_ENABLED`. Callers who need less use `wait_seconds` and get a 202 with the job.
- **B14 (unblock mode, D3).** It applies when the resolved compliance mode is `ignore` (admins only; `resolve_mode` already refuses `ignore` to users) and `BROWSER_UNBLOCK_ENABLED` is true. It also applies to batches with `compliance: "ignore"`.
  - **Driver:** Patchright, with `channel="chrome"` (`BROWSER_UNBLOCK_CHANNEL`, default `chrome`; `chromium` uses Patchright's own build). Headful under Xvfb by default (`BROWSER_UNBLOCK_HEADLESS=false`); the worker starts Xvfb itself. One persistent context per page in a fresh temporary profile, `no_viewport=True` with a 1920×1080 window. No custom UA, no extra context headers. Caller `headers` still go only to the target host via the route handler.
  - **What it changes:** robots and TDM are not checked (that is `ignore`). Nothing is blocked (B3), service workers and non-GET navigations are allowed, and the proxy and the gate stay.
  - **Challenges:** after load, while the DOM is a challenge (`assess` or B12) and time is left, the render waits up to `BROWSER_UNBLOCK_CHALLENGE_WAIT_SECONDS` (20). It re-checks every second and follows the challenge's own navigation. Non-interactive challenges clear this way (Cloudflare managed and Turnstile in non-interactive mode, Anubis proof-of-work, DataDome and Akamai JS checks).
  - **Clearance reuse:** after a render whose last document was 2xx and not a challenge, the cookies of the target host and its parent domain are stored in `unblock:cookies:{<host>}` (JSON, TTL = earliest cookie expiry, at most `BROWSER_UNBLOCK_COOKIE_TTL_SECONDS`, 1800). The next unblock render of that host adds them first. They are used only by unblock renders and are never shown.
  - **Escalation:** under unblock, `auto` also escalates per B5.
  - **Result:** `unblocked: true` (B10).
- **B15 (what unblock does not do):**
  - No interactive captcha solving (clicking or third-party solver services).
  - No IP or proxy rotation; CLAUDE.md rule 6 keeps that.
  - If a partner blocks by IP, the right fix is that they allow our egress IP. A later package can add an upstream proxy option if Jan wants it.
- **D2 (done-when, Jan 2026-10-24):**
  - The gate is `wlo_200` with `mode: "auto"` at or above 80 %.
  - Reported alongside: the smoke set (`mode: "auto"`), and `wlo_200` with `mode: "browser"`.

## Review Focus

1. **A page navigates its main frame to a disallowed target:** a JavaScript redirect, a meta refresh or a clicked link to a private address, a robots-disallowed path (under `enforce`) or another scheme. Chromium never sends the request: `blocked_url`, `blocked_by_robots` or `invalid_url`, as fast mode would answer. An HTTP redirect to a disallowed path is caught when it arrives, and its content is discarded. Owner: Tasks 3 and 4.
2. **A document, sub-resource, iframe or WebSocket points at a forbidden address:** `127.0.0.1`, `169.254.169.254`, `[::1]`, or a public name that resolves to a private IP. The proxy refuses it with 403, in both modes. For a document the result is `blocked_url`; otherwise the page still renders without it. Owner: Tasks 2, 4 and 6.
3. **Chromium or Chrome dies mid-render** (killed, out of memory): the pages in flight fail alone with `internal_error`, retryable, and the pool relaunches the browser. The worker loop goes on. A worker killed outright leaves its lease to expire, and the item runs again. Owner: Tasks 4, 6 and 7.
4. **No browser worker is running** (scaled to zero, crashed, or not installed while `BROWSER_ENABLED` is true): a sync `auto` request answers its fast result with `browser_unavailable` after `BROWSER_QUEUE_WAIT_SECONDS`, and its job is gone. A sync `browser` request answers `failed`/`timeout`. Owner: Task 8.
5. **Unblock mode must stay where it belongs, and caller secrets must not leak:**
   - unblock never runs for a user key, never with `BROWSER_UNBLOCK_ENABLED=false`, and never without `compliance: "ignore"`;
   - stored clearance cookies are never used by a standard render;
   - caller `headers` reach no other host;
   - `GET /v1/jobs/{id}` and the logs never show headers, cookies or action text.
   Owner: Tasks 3, 6, 7 and 8.

---

### Task 1: Request and result models, settings, escalation rules

**Files:**
- Create: `src/text_extraction/browser_models.py`, `src/text_extraction/escalation.py`, `tests/test_browser_models.py`, `tests/test_escalation.py`
- Modify: `extract_models.py`, `job_models.py`, `quality.py`, `settings.py`, `tests/test_quality.py`, `tests/test_settings.py`, `docs/configuration.md`, `.env.example`

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
    - `ScreenshotResult(url, final_url, upstream_status, mode_used, unblocked, attempts, title, screenshot: ArtifactRef | None, quality, timings, compliance, warnings)` and `def screenshot_view(result: ExtractResult) -> ScreenshotResult`.
  - `extract_models.py`:
    - `ExtractRequest.mode: Literal["fast", "auto", "browser"]`, default `"auto"`.
    - New fields: `actions: list[Action] = []`, `cookies: list[BrowserCookie] = []`, `headers: dict[str, str] = {}`, `screenshot: bool = False`, `full_page: bool = True`.
    - A model validator: with `mode: "fast"` all of them must be at their defaults (422). A cookie `domain` must be the URL's host or a parent domain of it, not a bare public suffix (one label), per B6.
    - `def secrets(self) -> BrowserSecrets`.
    - `ExtractResult.mode_used: Literal["fast", "browser"]`.
    - `ExtractResult.screenshot: ArtifactRef | None = None` and `ExtractResult.unblocked: bool = False`.
    - `Timings.render_ms: int = 0`. Defaults keep old cached entries valid.
  - `job_models.py`:
    - `JobKind` gains `"screenshot"`.
    - `JobOptions.mode` gains `"browser"`.
    - `JobOptions` gains `screenshot: bool = False` and `full_page: bool = True`.
    - `BatchRequest.mode` gains `"browser"`.
  - `escalation.py`:
    - `def needs_browser(envelope: Envelope[ExtractResult], *, unblock: bool) -> bool` (B5, including the unblock triggers);
    - `def better(fast: Envelope[ExtractResult], rendered: Envelope[ExtractResult]) -> Envelope[ExtractResult]` (B5 ranking);
    - `def unblock_applies(mode: ComplianceMode, settings: Settings) -> bool` (B14);
    - `BROWSER_UNAVAILABLE_WARNING = "browser_unavailable"`.
  - `quality.py`: `Quality.challenge_status: bool = False`, set by the pipeline when a 403/429/503 body is a challenge; the fast pipeline assesses such bodies only under unblock.
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
    - `browser_unblock_enabled: bool = False`
    - `browser_unblock_channel: Literal["chrome", "chromium"] = "chrome"`
    - `browser_unblock_headless: bool = False`
    - `browser_unblock_timeout_seconds: float = 60` (10..180)
    - `browser_unblock_challenge_wait_seconds: float = 20` (0..60)
    - `browser_unblock_cookie_ttl_seconds: int = 1800` (0 = no reuse)
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
    - `test_bot_protection_and_challenge_status_escalate_only_under_unblock`;
    - `test_better_prefers_status_then_words_then_rendered`: `success` 40 words beats `partial` 500 words; `success` 175 beats `success` 42; equal words go to the rendered one;
    - `test_unblock_applies_only_with_ignore_and_switch`.
  - `test_quality.py`: `test_soft_404_recommends_browser`.
  - `test_settings.py`: defaults and bounds of the new settings; an invalid entry in `BROWSER_BLOCKED_RESOURCES` (`document`, `bogus`) is a `ConfigError`.
- [ ] **Step 2:** `uv run pytest tests/test_browser_models.py tests/test_escalation.py tests/test_quality.py tests/test_settings.py -q`. Expect failures.
- [ ] **Step 3:** Implement. Update the `mode` description: `fast` is HTTP only, `auto` uses a browser when the page needs one and browser mode is enabled, `browser` always renders. Update the `compliance` description: `ignore` (admins) also allows unblock mode where the operator enabled it. Document the settings.
- [ ] **Step 4:** Full gates: `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`.
- [ ] **Step 5:** Commit `feat: add browser request models, settings and escalation rules`.

### Task 2: Egress proxy for the browsers

**Files:**
- Create: `src/text_extraction/browser_proxy.py`, `tests/test_browser_proxy.py`
- Modify: `netguard.py` (expose the resolver)

**Interfaces:**
- Consumes: `is_public_address`, and the resolution in `GuardedBackend._resolve` (netguard.py).
- Produces:
  - `netguard.py`: `async def resolve_allowed(host: str, port: int, policy: AddressPolicy) -> list[IPv4Address | IPv6Address]`. It raises `BlockedAddressError` when no address passes and `socket.gaierror` for DNS failures. `GuardedBackend` uses it too.
  - `browser_proxy.py`:
    - `class EgressProxy` with `__init__(settings: Settings, *, policy: AddressPolicy | None = None, registry: CollectorRegistry)`. `async start()` binds `127.0.0.1:0`; `async stop()`; property `url`.
    - `def open_context(self) -> ProxyContext`: new random credentials. `ProxyContext` has `username`, `password`, `bytes: int`, `hosts: frozenset[str]` (capped at 200), `refused: tuple[tuple[str, str], ...]` (host and reason, capped at 50), `capped: bool`, and `close()`, which ends its tunnels.
    - Metric `text_extraction_browser_proxy_refused_total{reason}`, with reasons `blocked_address`, `dns_error`, `bytes_cap`, `auth`, `bad_request`.

Behaviour:
- **Requests and auth:** HTTP/1.1 `CONNECT host:port` and absolute-form plain `http://` requests. Without `Proxy-Authorization` for a known open context, the answer is 407 with `Proxy-Authenticate: Basic realm="browser"`.
- **Dialing:** the host is resolved once per connection with `resolve_allowed`, and the proxy dials the first allowed address that answers. A plain HTTP request has its `Host` pinned and `Connection: close`, as in the Selenium service's `egress_proxy.py` (reference only).
- **Limits:** bytes are counted both ways per context. Past `BROWSER_MAX_BYTES`, new connections of that context get 403 and `capped` is set. A request head over 64 KiB or older than 15 s is refused. A tunnel lives at most `BROWSER_UNBLOCK_TIMEOUT_SECONDS + 5`. At most 512 connections over all contexts.
- **Refusals** answer 403 (blocked, capped) or 502 (DNS, connect failure) with `Connection: close`, and are logged with the host only.

- [ ] **Step 1: Write the failing tests** (a local asyncio echo HTTP server; the policy is injected so that 127.0.0.1 counts as public, except where a test checks refusals):
  - `test_connect_tunnel_relays_bytes`;
  - `test_plain_http_request_is_forwarded_with_pinned_host`;
  - `test_missing_or_unknown_credentials_get_407`;
  - `test_private_address_is_refused` (default policy, targets `127.0.0.1`, `[::1]` and `169.254.169.254`; 403 and the metric counted);
  - `test_name_resolving_to_private_ip_is_refused` (resolver monkeypatched);
  - `test_bytes_are_counted_per_context_and_capped`;
  - `test_hosts_seen_and_refusals_are_recorded`;
  - `test_closing_a_context_ends_its_tunnels`;
  - `test_oversized_or_slow_request_head_is_refused`.
- [ ] **Step 2:** Run them; expect failures.
- [ ] **Step 3:** Implement `resolve_allowed`, refactor `GuardedBackend` onto it (the existing netguard tests stay green), and implement the proxy.
- [ ] **Step 4:** Full gates.
- [ ] **Step 5:** Commit `feat: add the SSRF-guarded egress proxy for the browsers`.

### Task 3: Pipeline with a renderer

**Files:**
- Create: `src/text_extraction/render.py` (protocol and data only, no Playwright), `tests/test_pipeline_render.py`
- Modify: `pipeline.py`, `tests/fakes.py`

**Interfaces:**
- Consumes: Task 1 models, `needs_browser`, `better`, `unblock_applies`.
- Produces:
  - `render.py`:
    - `NavigationCheck = Callable[[str], Awaitable[None]]`: called before a later main-frame navigation is sent. It raises `FetchError` (block, invalid URL) or a gate refusal.
    - `@dataclass(frozen=True) class MainResponse`: `url`, `status`, `headers: dict[str, str]`, `content_type`, `charset`, `redirects: tuple[str, ...]`.
    - `@dataclass(frozen=True) class RenderOptions`: `actions`, `cookies`, `headers`, `header_host: str`, `screenshot: bool`, `full_page: bool`, `allow_evaluate: bool`, `unblock: bool`, `blocked_resources: frozenset[str]`.
    - `@dataclass(frozen=True) class Rendered`: `html: str`, `final_url: str`, `responses: tuple[MainResponse, ...]` (every main-frame document answer, in order), `screenshot: bytes | None`, `screenshot_size: tuple[int, int] | None`, `warnings: tuple[str, ...]`, `hosts: frozenset[str]`, `cookies: tuple[CookieData, ...]` (the target host's, for B14).
    - `class Renderer(Protocol)`: `user_agent: str`, and `async def render(self, url: str, check: NavigationCheck, options: RenderOptions, *, deadline: float) -> Rendered`. `deadline` is `loop.time()` based.
    - `class ArtifactSink(Protocol)`: `async def put_screenshot(self, png: bytes, width: int, height: int) -> ArtifactRef`.
    - `class ClearanceStore(Protocol)`: `async get(host) -> list[CookieData]` and `async put(host, cookies) -> None`.
  - `pipeline.py`:
    - `Pipeline.__init__(..., renderer: Renderer | None = None, unblock_renderer: Renderer | None = None, clearance: ClearanceStore | None = None, timeout_seconds: float | None = None)`. The deadline is `timeout_seconds` (or `BROWSER_UNBLOCK_TIMEOUT_SECONDS` for an unblock render), else `EXTRACT_TIMEOUT_SECONDS`.
    - `Pipeline.run(req, principal, *, gate_refusals="fail", artifacts: ArtifactSink | None = None, draft: Envelope[ExtractResult] | None = None)`.
    - `create_pipeline(...)` passes the new arguments through.

Behaviour of a pipeline with a renderer (one without behaves as today, except the B5 rules for results that will escalate):
- **Which path:** `mode: "fast"` never renders. `browser`, `screenshot`, or `auto` with a `draft` renders. `auto` without a draft runs the fast steps first and renders only when the result `needs_browser`. The render uses `unblock_renderer` when `unblock_applies`; otherwise `renderer`.
- **B2 step 1:** `_check_origin(url)` (under `ignore` it decides nothing, as today), a block, then `turn.take` (held).
- **B2 step 3:** the `NavigationCheck` given to the renderer runs `_check_origin` for the URL and `turn.take` for its host, and counts nothing. `validate_url` failures map to `invalid_url`.
- **B2 step 4:**
  - Each `MainResponse` URL and redirect is checked with the cached origin verdicts: `cached_before_fetch`, or `before_fetch` for missing files, fetched in the hop host's turn.
  - `after_fetch` runs on the last response (headers) and the rendered HTML.
  - A block discards the render. `attempts += len(responses)`, and each response's status is recorded with the gate for its host.
  - A final non-2xx or non-HTML answer ends as in fast mode.
- **B14 clearance:** before an unblock render the clearance cookies for the host join `options.cookies`; after a 2xx non-challenge unblock render `rendered.cookies` are stored.
- **Rendered HTML:** goes through `_process` with a `FetchResult` built from the last `MainResponse` and `rendered.html` (`raw_sha256 = sha256(html)`).
  - `mode_used="browser"`, `final_url`, `timings.render_ms`, `unblocked`; the renderer's warnings are appended.
  - `bot_challenge` is also set per B12 from `rendered.hosts`.
  - Rendered HTML over `CONVERT_MAX_BYTES` is `too_large`.
  - Then `better(draft or fast, rendered)` when an `auto` comparison exists.
- **Screenshot:** `rendered.screenshot` goes to `artifacts.put_screenshot`, which is required when `screenshot` is set, and becomes `result.screenshot`. Status per B7.
- **Cache:**
  - Browser results store no validators, so they are never revalidated (B10).
  - Requests with secrets neither read nor write the cache or fingerprints (B6).
  - An `auto` result that will escalate is neither cached nor fingerprinted (B5). Under unblock the fast pipeline also assesses 403/429/503 bodies to set `challenge_status`.
- **Errors:** a `FetchError` or gate refusal from the check or the renderer ends the run as today. The turn is given back when the render ends, on every path.

- [ ] **Step 1: Write the failing tests.** `tests/fakes.py` gets `FakeRenderer`, which returns scripted `Rendered` values and calls `check` for scripted navigations, plus `FakeArtifacts` and `FakeClearance`. Tests in `test_pipeline_render.py`:
  - `test_browser_mode_renders_after_compliance_and_turn`;
  - `test_robots_block_of_start_url_never_renders`;
  - `test_auto_returns_fast_result_without_render_when_not_needed`;
  - `test_auto_renders_js_heavy_page_and_keeps_the_better_result`: two cases, the rendered result wins, and the fast result wins;
  - `test_auto_with_draft_renders_without_fast_fetch`;
  - `test_navigation_to_robots_disallowed_url_is_refused_before_send` (under `enforce`; the check raised `blocked_by_robots`);
  - `test_navigation_to_private_address_is_refused` (`blocked_url`);
  - `test_navigation_to_other_host_moves_the_turn` (the gate fake shows one turn at a time);
  - `test_http_redirect_to_disallowed_path_discards_the_render` (`blocked_by_robots`, no content);
  - `test_noai_meta_in_rendered_dom_blocks_under_enforce`;
  - `test_attempts_count_main_responses`;
  - `test_turn_is_held_during_render_and_released_after`, also when the renderer raises;
  - `test_screenshot_is_stored_and_referenced`;
  - `test_requests_with_secrets_bypass_cache_and_fingerprints`;
  - `test_browser_results_are_never_revalidated`;
  - `test_auto_result_that_will_escalate_is_not_cached` (no renderer, `BROWSER_ENABLED=true`);
  - `test_download_or_non_html_is_unsupported_content_type`;
  - `test_challenge_host_marks_thin_page_as_bot_challenge`;
  - `test_unblock_renderer_used_only_with_ignore_and_switch` (admin and `ignore` uses the unblock renderer; user, `enforce`, or the switch off uses the standard one);
  - `test_clearance_cookies_loaded_and_stored_only_for_unblock`;
  - `test_fast_mode_unchanged`: one existing fast case in each of `browser_enabled` false and true.
- [ ] **Step 2:** Run the new tests; expect failures.
- [ ] **Step 3:** Implement. Keep the turn logic in `_Turn`; the render path holds it until the end. No module here imports `playwright` or `patchright`.
- [ ] **Step 4:** Full gates. All package-2 and package-3 tests stay green.
- [ ] **Step 5:** Commit `feat: render pages through a renderer in the pipeline`.

### Task 4: Chromium pool and standard renderer

**Files:**
- Create: `src/text_extraction/browser.py`, `src/text_extraction/browser_scripts.py` (the JavaScript strings and consent rules), `tests/browser/__init__.py`, `tests/browser/conftest.py`, `tests/browser/site.py`, `tests/browser/test_renderer.py`
- Modify: `pyproject.toml` (extra `browser`, dev group, pytest marker `browser`), `uv.lock`, `.github/workflows/ci.yml` (job `browser`)

**Interfaces:**
- Consumes: `EgressProxy` (Task 2); `Renderer`, `Rendered`, `RenderOptions`, `NavigationCheck`, `MainResponse` (Task 3).
- Produces:
  - `class BrowserPool` with `__init__(settings: Settings, proxy: EgressProxy, registry: CollectorRegistry, *, launcher: Launcher)`, `async start()`, `async stop()`, `async page(options) -> AsyncContextManager[Page]`, and `user_agent: str`.
    - `Launcher` is a protocol with one method that launches a browser and opens a context. Task 6 adds the Patchright launcher, so the pool, recycling and crash handling are shared.
  - `class ChromiumRenderer(Renderer)` with `__init__(pool: BrowserPool, settings: Settings)`.
  - Shared helpers used by Task 6: `async render_page(page, url, check, options, *, deadline, settings) -> Rendered`, `async run_consent(page, opt)`, `async strip_hidden(page)`.
  - Metrics, labelled `driver` (`playwright`, `patchright`):
    - `text_extraction_browser_pages_in_use` (gauge);
    - `text_extraction_browser_pages_total{outcome}` (outcome `ok`, `error`, `crash`, `timeout`);
    - `text_extraction_browser_render_seconds` (histogram, buckets up to 90);
    - `text_extraction_browser_launches_total{reason}` (`start`, `recycle`, `crash`).

Behaviour:
- **Pool:** one browser per driver per process, launched lazily. Chromium is headless with the B4 flags and the proxy, and `executable_path=BROWSER_EXECUTABLE_PATH` when set. A semaphore of `BROWSER_CONCURRENCY` pages over both drivers.
  - After `BROWSER_RECYCLE_AFTER_PAGES` pages, a new browser is launched for new pages and the old one is closed once its pages end.
  - A `disconnected` browser is relaunched on the next page. Its pages in flight raise, and the renderer maps that to `internal_error`, retryable (outcome `crash`).
- **Standard context:** viewport 1920×1080, `service_workers="block"`, `accept_downloads=False`, `locale="de-DE"`, the page's own proxy credentials and the UA (B8). The caller's cookies are added, with the domain defaulting to the request host.
- **Routing:** `context.route("**/*")`:
  - a main-frame document request after the first: `await check(url)` then `continue_`, or `abort` with the error kept for the result; non-GET is aborted (B2 step 3);
  - a resource type in `options.blocked_resources` is aborted;
  - a request on `options.header_host` continues with the caller's headers added;
  - everything else continues unchanged.
- **`render_page` steps, all within `deadline`:**
  - `goto(url, wait_until="load")`, then up to `BROWSER_IDLE_WAIT_SECONDS` for network idle;
  - consent `optOut` (B11, best effort, 3 s);
  - the actions (Task 5);
  - strip hidden elements: `display:none` subtrees, and `visibility:hidden` leaves that are not script, style, link, meta, template, noscript, title or head;
  - `page.content()`, then the screenshot when asked: PNG, `full_page` clipped at `SCREENSHOT_MAX_HEIGHT_PX`, with the warning `screenshot_clipped`.
- **What it collects:** main-frame `response` events give `MainResponse` (with `request.redirected_from` for hops); a download gives a `MainResponse` with its content type and stops. A document refused by the proxy (403 with `X-Extraction-Policy: blocked`) is `FetchError(BLOCKED_URL)`.
- **Close:** the context is always closed, and the proxy context ends with it. Its `hosts` and `capped` feed `Rendered.hosts` and the warning `browser_bytes_capped`.
- **Errors:** a navigation timeout is `FetchError(TIMEOUT, retryable=True)`. A page that kept navigating so that `content()` failed is `FetchError(CONVERSION_FAILED)`.

`tests/browser/site.py` is a local test site on `asyncio.start_server`, or `http.server` in a thread. Its pages:
- `/static`; `/js` (text written by a script);
- `/xhr` (text fetched from `/api`);
- `/redirect-http` (302 to `/static`); `/redirect-js` (`location` to `/static`); `/meta-refresh`;
- `/to-private` (`location` to `http://169.254.169.254/`); `/img-private` (`<img>` and `fetch()` to `127.0.0.1:<other port>`);
- `/hidden-tabs`; `/consent` (a `#cookiebanner` with the Government Site Builder button);
- `/tall` (20000 px); `/never-idle` (long polling);
- `/form-post`; `/set-cookie`; `/echo-headers` (shows the request headers it got);
- `/challenge` (serves a challenge page until a cookie set by its own script after 2 s is present, then the content); `/download.pdf`.

`tests/browser/conftest.py`:
- finds Chromium: `BROWSER_EXECUTABLE_PATH`, else Playwright's own, else `/opt/pw-browsers/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell`, else skip;
- serves the site on two loopback ports (the second one is the "third party") with a proxy policy that admits 127.0.0.1 on those ports only, so private-address tests still refuse other ports and `169.254.169.254`.

- [ ] **Step 1: Write the failing tests** (`@pytest.mark.browser`, real Chromium, the real `Pipeline` with the fake gate where it helps):
  - `test_static_page_renders`;
  - `test_script_written_text_is_extracted`; `test_xhr_content_is_extracted`;
  - `test_http_redirect_is_reported_as_main_responses`;
  - `test_js_redirect_and_meta_refresh_go_through_check`;
  - `test_navigation_to_metadata_ip_is_refused` (`blocked_url`, never sent);
  - `test_private_subresources_are_refused_and_page_still_renders`;
  - `test_caller_headers_reach_target_host_only` (`/echo-headers` on the second port shows none);
  - `test_hidden_tabs_are_stripped`; `test_consent_banner_is_opted_out`;
  - `test_full_page_screenshot_is_1920_wide_and_clipped` (PNG header width 1920, height 16384, warning);
  - `test_never_idle_page_finishes_within_deadline`;
  - `test_form_post_navigation_is_refused`;
  - `test_download_is_reported_as_non_html`;
  - `test_images_blocked_for_extraction_not_for_screenshot`;
  - `test_browser_crash_fails_page_and_pool_relaunches` (close the browser mid-render);
  - `test_recycle_launches_new_browser`;
  - `test_user_agent_is_honest`.
- [ ] **Step 2:** `uv run pytest -m browser tests/browser -q`; expect failures. `pyproject.toml` excludes the marker by default (`addopts = "-m 'not browser'"`), so `uv run pytest` stays fast.
- [ ] **Step 3:** Add the extra and the dev dependencies (check the licences in the lock: Apache-2.0 for `playwright` and `patchright`, MPL-2.0 for `duckduckgo-autoconsent-py`). Implement.
- [ ] **Step 4:** In CI, add the job `browser`: `uv sync --locked`, `uv run playwright install --with-deps --only-shell chromium`, `uv run pytest -m browser -q`. Run the browser tests locally, then the full gates.
- [ ] **Step 5:** Commit `feat: add the Chromium pool and renderer`.

### Task 5: Browser actions

**Files:**
- Create: `src/text_extraction/browser_actions.py`, `tests/browser/test_actions.py`
- Modify: `browser.py`, `tests/browser/site.py`

**Interfaces:**
- Consumes: the `Action` models (Task 1); the page and deadline inside `render_page`.
- Produces: `async def run_actions(page: Page, actions: Sequence[Action], *, allow_evaluate: bool, deadline: float, consent: ConsentRunner) -> list[str]`, which returns warnings. `ConsentRunner` is `run_consent` with `opt: Literal["optOut", "optIn"]`.

Behaviour:
- **Order:** actions run in order after the consent step, each bounded by the time left.
- **Failure:** a failing action (no element, timeout, navigation refused) stops the remaining ones and adds the warning `action_<index>_failed`. The page is extracted as it is.
- **Per action:**
  - `click`, `type`, `press`, `select`: locators, first match;
  - `scroll`: `times` viewport heights, pausing `pause_ms`;
  - `infinite_scroll`: scroll to the bottom until the page height stops growing or `max_scrolls`;
  - `expand_all`: opens closed `<details>` and clicks up to 50 visible elements with `aria-expanded="false"`;
  - `accept_cookies`: the consent runner with `optIn`;
  - `evaluate`: runs only when `allow_evaluate`; otherwise it never reaches here (the API answers 403, Task 8) and raises `ValueError` as a guard.
- **Navigation:** a click that navigates goes through the route check (Task 4). The action waits for `load` after a navigation.
- **Drivers:** the module imports only the shared Playwright API types, so the Patchright page (same API) works with it.

- [ ] **Step 1: Write the failing tests** (site pages `/actions`: a button revealing text, a `<details>`, a select, an input echo, a lazy list growing on scroll, a link to `/static`):
  - `test_click_reveals_text`; `test_type_and_press_enter`; `test_select_option`;
  - `test_expand_all_opens_details_and_aria_toggles`;
  - `test_infinite_scroll_loads_more_and_stops`;
  - `test_failed_action_stops_the_rest_with_warning`;
  - `test_click_navigation_goes_through_check`;
  - `test_evaluate_requires_admin_flag`;
  - `test_accept_cookies_opts_in`;
  - `test_actions_respect_the_deadline`.
- [ ] **Step 2:** Run them; expect failures.
- [ ] **Step 3:** Implement and wire it into `render_page`.
- [ ] **Step 4:** Browser tests and full gates.
- [ ] **Step 5:** Commit `feat: add browser actions`.

### Task 6: Unblock renderer (Patchright, Chrome, challenges, clearance)

**Files:**
- Create: `src/text_extraction/browser_unblock.py`, `src/text_extraction/clearance.py`, `tests/test_clearance.py`, `tests/browser/test_unblock.py`
- Modify: `browser.py`

**Interfaces:**
- Consumes: `BrowserPool`, `Launcher`, `render_page` (Task 4); `run_actions` (Task 5); `ClearanceStore` (Task 3).
- Produces:
  - `class PatchrightLauncher(Launcher)`: `patchright.async_api`, `channel=BROWSER_UNBLOCK_CHANNEL`, `headless=BROWSER_UNBLOCK_HEADLESS`. It uses `launch_persistent_context(user_data_dir=<fresh temp dir>, no_viewport=True, args=[B4 flags, "--window-size=1920,1080"], proxy=<page credentials>)`, with no `user_agent` and no extra headers. The temp dir is removed when the page closes.
  - `class Xvfb`: `async start(display=":99")` and `async stop()`, run by the browser worker when `BROWSER_UNBLOCK_ENABLED` and not headless. It sets `DISPLAY` for the launcher.
  - `class UnblockRenderer(Renderer)` with `__init__(pool: BrowserPool, settings: Settings)`.
    - It runs `render_page` with the unblock `RenderOptions`: nothing blocked, non-GET allowed, the route handler installed only when caller headers exist.
    - Then the challenge wait (B14): while `assess(html)` or the B12 host check says challenge and time is left, sleep 1 s, follow navigations and re-read.
    - It adds the warning `challenge_not_cleared` when the wait ends on a challenge.
    - `Rendered.cookies` holds the target host's cookies.
  - `clearance.py`: `class ValkeyClearance(ClearanceStore)` with `__init__(valkey, settings)`. Key `unblock:cookies:{<host>}`, JSON list, TTL = min(earliest expiry, `BROWSER_UNBLOCK_COOKIE_TTL_SECONDS`). A TTL of 0 disables it.

- [ ] **Step 1: Write the failing tests:**
  - `test_clearance.py` (fakeredis):
    - `test_round_trip_and_ttl_from_earliest_expiry`;
    - `test_ttl_zero_disables_reuse`;
    - `test_hosts_are_kept_apart`.
  - `tests/browser/test_unblock.py` (`@pytest.mark.browser`). They run with Patchright and `channel="chromium"`, headless, against the site, so CI needs no Chrome. Skip when Patchright's Chromium is missing.
    - `test_challenge_page_clears_within_wait` (`/challenge`);
    - `test_challenge_not_cleared_adds_warning` (wait 0);
    - `test_navigator_webdriver_is_false`;
    - `test_unmodified_user_agent` (no `text-extraction` token);
    - `test_private_addresses_still_refused` (proxy in place);
    - `test_caller_headers_reach_target_host_only`;
    - `test_profile_dir_is_removed`.
- [ ] **Step 2:** Run them; expect failures.
- [ ] **Step 3:** Implement. The CI job `browser` additionally runs `uv run patchright install chromium`.
- [ ] **Step 4:** Browser tests and full gates.
- [ ] **Step 5:** Commit `feat: add the unblock renderer with Patchright`.

### Task 7: Browser worker, escalation in the extract worker, drafts, artifacts and secrets

**Files:**
- Create: `src/text_extraction/browser_worker.py`, `tests/test_browser_worker.py`
- Modify: `jobs.py`, `scheduler.py`, `worker.py`, `pyproject.toml` (script `text-extraction-browser-worker`), `tests/test_jobs.py`, `tests/test_scheduler.py`, `tests/test_worker.py`

**Interfaces:**
- Consumes: Tasks 1-6.
- Produces:
  - `jobs.py`:
    - `JobStore.create(..., secrets: BrowserSecrets | None = None)`: secrets go to `job:{<id>}:secrets` with the B6 TTL.
    - `async secrets(job_id) -> BrowserSecrets` (empty when gone) and `async drop_secrets(job_id) -> None`.
    - `async put_draft(job_id, index, envelope) -> None` and `async draft(job_id, index) -> Envelope[ExtractResult] | None`, in `job:{<id>}:drafts` with the job's remaining TTL.
    - `async put_artifact(job_id, name, data: bytes) -> datetime`, which returns the expiry, and `async artifact(job_id, name) -> bytes | None`. The key is `job:{<id>}:artifact:<name>` with `ARTIFACT_TTL_SECONDS`.
    - `delete` also removes secrets, drafts and the known artifact names.
    - `async clear_lease(job_id) -> None`, the same as `mark_running(job_id, lease_seconds=None)`.
  - `scheduler.py`: `FairQueue.enqueue_items(job_id: str, client: str, items: list[tuple[int, str]]) -> None`. `enqueue` becomes a call of it with `enumerate(urls)`.
  - `worker.py`:
    - `class ItemRunner(Protocol)`: `lease_seconds: float`, and `async run(job: JobRecord, item: QueueItem, url: str, *, gate_refusals) -> Envelope[ExtractResult] | None`. None means the item was moved: it is completed without a result and counted as `escalated`.
    - `Worker(settings, valkey, runner: ItemRunner, store, queue, registry, ...)` replaces the `pipeline` argument.
    - `ExtractRunner(pipeline, store, settings, browser_queue: FairQueue | None)`: after a run with `job.options.mode == "auto"` whose envelope `needs_browser` (with `unblock_applies(job.options.compliance)`), and a browser queue (that is, `BROWSER_ENABLED`), it puts the draft first, then calls `browser_queue.enqueue_items(job.id, item.client, [(item.index, url)])`, and returns None.
    - `build_worker` passes `FairQueue(valkey, settings, "browser")` when `BROWSER_ENABLED`.
  - `browser_worker.py`:
    - `BrowserRunner(pipeline, store, settings)`: `lease_seconds = 2 × BROWSER_UNBLOCK_TIMEOUT_SECONDS + 10`.
      - It reads the secrets and the draft, and builds the request from `job.options` and secrets. Kind `screenshot` gives `mode="browser"`, `screenshot=True`, `metadata=False`. The owner becomes the principal; `allow_evaluate` is set when the owner role is admin.
      - It runs with an `ArtifactSink` bound to the job (`put_artifact` with `screenshot.png`, and `ArtifactRef.url = /v1/jobs/<id>/artifacts/screenshot.png`). It drops the secrets in a `finally`.
      - A memory guard that is high makes the sink raise. The pipeline maps that to `internal_error`, retryable (B7).
    - `build_browser_worker(settings, valkey, registry) -> BrowserWorkerParts`, and `main()`.
      - It starts the proxy, Xvfb (when needed) and the pool before `/ready`, and stops them after the loops.
      - `WORKER_CONCURRENCY` for this pool is `BROWSER_CONCURRENCY`.
      - The pipeline gets the standard renderer, the unblock renderer (when `BROWSER_UNBLOCK_ENABLED`) and `ValkeyClearance`, with deadline `BROWSER_TIMEOUT_SECONDS`.
      - `main(["--check"])` launches each enabled driver once and exits 0 (used by the image workflow).

- [ ] **Step 1: Write the failing tests:**
  - `test_jobs.py`:
    - `test_secrets_round_trip_and_ttl`; `test_secrets_not_in_job_view`;
    - `test_draft_round_trip`;
    - `test_artifact_round_trip_and_ttl`; `test_delete_removes_secrets_drafts_and_artifacts`;
    - `test_all_job_keys_share_one_hash_tag` (extended).
  - `test_scheduler.py`: `test_enqueue_items_keeps_given_indexes`.
  - `test_worker.py`:
    - `test_auto_item_needing_browser_moves_to_browser_pool_with_draft` (completed in `extract`, queued in `browser` with the same index, draft stored, no result written, metric `escalated`);
    - `test_no_move_when_browser_disabled`;
    - `test_existing_worker_tests_pass_with_extract_runner`.
  - `test_browser_worker.py` (a `FakeRenderer` pipeline):
    - `test_screenshot_job_stores_artifact_and_result`;
    - `test_draft_is_compared_with_render`;
    - `test_secrets_are_used_and_dropped`, also when the run raises;
    - `test_admin_owner_allows_evaluate_user_does_not`;
    - `test_unblock_only_for_admin_ignore_jobs` (Review Focus 5);
    - `test_memory_guard_high_fails_screenshot_retryable`;
    - `test_lost_lease_and_exhausted_items_behave_as_extract_pool` (one case each, reusing the package-3 scenarios);
    - `test_ready_only_after_browser_started` (pool start faked).
- [ ] **Step 2:** Run them; expect failures.
- [ ] **Step 3:** Implement. The worker's module docstring describes the runner split. `browser_worker.py` is the only module outside `browser*.py` that imports the browser modules.
- [ ] **Step 4:** Full gates, plus `uv run python -c "import text_extraction.app, text_extraction.worker, sys; sys.exit('playwright' in sys.modules or 'patchright' in sys.modules)"`.
- [ ] **Step 5:** Commit `feat: add the browser worker and auto escalation in the extract worker`.

### Task 8: API: browser dispatch, screenshot and artifact routes

**Files:**
- Create: `src/text_extraction/browser_dispatch.py`, `src/text_extraction/screenshot_api.py`, `tests/test_browser_dispatch.py`, `tests/test_screenshot_api.py`
- Modify: `extract_api.py`, `jobs_api.py`, `app.py`, `server.py`, `tests/test_extract_api.py`, `tests/test_jobs_api.py`, `tests/test_server.py`

**Interfaces:**
- Consumes: Tasks 1, 3 and 7.
- Produces:
  - `browser_dispatch.py`:
    - `class BrowserDispatcher` with `__init__(store: JobStore, queue: FairQueue, settings: Settings, *, clock=time.monotonic, sleep=asyncio.sleep)`.
    - `async submit(kind: Literal["extract", "screenshot"], req: ExtractRequest, principal: Principal, compliance: ComplianceMode, *, job_id: str | None = None, draft: Envelope[ExtractResult] | None = None) -> str`. It creates the job with the secrets, or reuses `job_id` and clears its lease; stores the draft; then enqueues item 0 on `browser` under `<name>#live`.
    - `async wait(job_id: str, *, unblock: bool, detached: Callable[[], bool]) -> Envelope[ExtractResult] | None`. It returns None when the item was not claimed within `BROWSER_QUEUE_WAIT_SECONDS`, gave no result within the render timeout (unblock or standard) + 5 s after the claim, or `detached()` turned true.
    - `async abandon(job_id: str) -> None`, which deletes the job quietly.
    - `class Handover`: `job_id: str | None` (the P1 job the route created) and `browser_job: str | None` (the dispatched job), with `detach()` and `detached`.
  - `extract_api.py`: the route's task becomes `_flow(body, principal, handover)`:
    1. 400 per B9; 403 for `evaluate` by a user.
    2. `mode: "browser"`, or `screenshot`: submit, then wait.
    3. Otherwise `pipeline.run`. If `auto` and `needs_browser`, submit with `job_id=handover.job_id` and the fast envelope as draft. If the route had already handed over, return None (the worker writes item 0). Otherwise wait; on None, abandon and answer the fast envelope with `browser_unavailable` (`auto`) or `failed`/`timeout`, retryable (`browser`).
    4. When `wait_seconds` passes while a browser job is waited for, the route detaches the task and answers 202 with `handover.browser_job`; no P1 job is created. Otherwise P1 is unchanged, and the done-callback stores nothing for a None result.
  - `screenshot_api.py`:
    - `POST /v1/screenshot` → `Envelope[ScreenshotResult]`, via the same dispatch with kind `screenshot`, and 202 with `wait_seconds`.
    - `GET /v1/jobs/{id}/artifacts/{name}` → `image/png`. The name matches `[a-z0-9_.-]{1,64}`. Unknown or expired jobs, foreign jobs and missing artifacts answer 404 `not_found`; `JOBS_ENABLED=false` answers 503.
  - `jobs_api.py`:
    - `POST /v1/batch` with `mode: "browser"` enqueues on `browser`, or answers 400 per B9.
    - The lost view (P1) does not apply to a job whose lease was cleared.
    - A `screenshot` job's items are shown as stored.
  - `app.py`: `app.state.browser_dispatcher`, the browser queue, and `QueueGauges` for `browser` when `BROWSER_ENABLED`.
  - `server.py`: `graceful_shutdown_seconds` = max(the current value, `BROWSER_QUEUE_WAIT_SECONDS + max(BROWSER_TIMEOUT_SECONDS, BROWSER_UNBLOCK_TIMEOUT_SECONDS if unblock enabled) + 10`) when `BROWSER_ENABLED` (B13).

- [ ] **Step 1: Write the failing tests** (fakeredis; a fake worker task that claims from `browser` and writes a result):
  - `test_browser_dispatch.py`:
    - `test_submit_creates_job_with_secrets_draft_and_live_client`; `test_submit_reuses_handover_job`;
    - `test_wait_returns_result`; `test_wait_gives_up_when_unclaimed`; `test_wait_gives_up_after_render_timeout`;
    - `test_abandon_deletes_job`.
  - `test_extract_api.py`:
    - `test_browser_mode_disabled_is_400`; `test_evaluate_by_user_is_403`;
    - `test_auto_escalates_and_returns_browser_result`;
    - `test_auto_without_worker_returns_fast_with_browser_unavailable` (Review Focus 4); `test_browser_mode_without_worker_is_timeout`;
    - `test_wait_seconds_during_browser_phase_answers_202_with_browser_job`;
    - `test_handover_during_fast_phase_then_escalation_writes_into_p1_job`;
    - `test_bot_wall_escalates_only_for_admin_ignore_with_unblock` (Review Focus 5);
    - `test_secrets_never_in_job_view_or_logs` (with structlog capture);
    - `test_fast_mode_and_auto_with_browser_disabled_unchanged`.
  - `test_screenshot_api.py`:
    - `test_screenshot_returns_artifact_ref`; `test_artifact_download_png`;
    - `test_foreign_or_unknown_artifact_is_404`; `test_screenshot_disabled_is_400`.
  - `test_jobs_api.py`: `test_batch_browser_mode_goes_to_browser_pool`; `test_batch_rejects_actions_cookies_headers` (422, extra fields forbidden).
  - `test_server.py`: `test_graceful_seconds_cover_browser_wait`.
- [ ] **Step 2:** Run them; expect failures.
- [ ] **Step 3:** Implement. Update the OpenAPI descriptions of `mode`, `actions`, `cookies`, `headers`, `screenshot`, `compliance` and the new routes. `GET .../artifacts/{name}` declares `image/png` in `responses`.
- [ ] **Step 4:** Full gates.
- [ ] **Step 5:** Commit `feat: dispatch browser work from the API and add screenshots`.

### Task 9: Image, compose, Helm and CI

**Files:**
- Modify: `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `.env.example`, `.github/workflows/image.yml`, `.github/workflows/ci.yml`, `deploy/helm/text-extraction/values.yaml`, `templates/*` (worker deployment, service, KEDA/HPA, NetworkPolicy, PrometheusRule, ConfigMap, `_helpers.tpl`, `NOTES.txt`), `docs/configuration.md`
- Create: `deploy/helm/text-extraction/templates/browser-worker-*.yaml`, if the extract templates cannot be parametrised by pool cleanly. Prefer one template ranged over a pool list.

**Interfaces:**
- Consumes: entry point `text-extraction-browser-worker` and `--check` (Task 7).
- Produces:
  - **Dockerfile:**
    - Build stage `build-browser` runs `uv sync --locked --no-dev --no-editable --extra browser`.
    - Target `browser`: `python:3.13-slim`. As root it installs `xvfb`, runs `playwright install --with-deps --only-shell chromium` (with `PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright`), and `patchright install --with-deps chrome` (Google Chrome stable into its system path), then removes the apt lists.
    - It runs as non-root 10001 with `CMD ["text-extraction-browser-worker"]`. The default target stays the API image, unchanged in content.
  - **image.yml:** a matrix over `api` (image `ghcr.io/<repo>`) and `browser` (image `ghcr.io/<repo>-browser`), same tags.
    - A step after the API build fails if Playwright or Patchright is importable in the API image.
    - A step after the browser build runs `text-extraction-browser-worker --check` with `BROWSER_UNBLOCK_ENABLED=true`.
  - **Compose:** service `browser-worker` built with `target: browser`, `BROWSER_ENABLED=true` for all app services, `BROWSER_UNBLOCK_ENABLED` passed through from `.env` (default false), `read_only` with tmpfs `/tmp` (size 1g), `shm_size: 1g`, `stop_grace_period: 90s`, and a `/ready` healthcheck.
  - **Helm:**
    - `workers.browser`: `enabled: true`, `unblock: false`, `image.repository`, `concurrency: 4`, resources (requests 1 CPU / 2Gi, limits 2 CPU / 4Gi), `terminationGracePeriodSeconds: 150`, KEDA (`pool="browser"`, threshold 8) or HPA, `emptyDir` `/tmp` (sizeLimit 2Gi), `readOnlyRootFilesystem: true`, all capabilities dropped, seccomp `RuntimeDefault`.
    - `BROWSER_ENABLED` is rendered from `workers.browser.enabled` into the API, extract and browser pods; `BROWSER_UNBLOCK_ENABLED` from `workers.browser.unblock`. Matching `config.*` entries are ignored.
    - The grace-period validate check covers the browser bound.
    - Alerts `TextExtractionBacklog` and `TextExtractionWorkersDown` per pool.
    - NetworkPolicy: browser workers accept 9090 from `metricsFrom` only.
  - **CI:** the compose job's config check covers the new service. The `helm` job lints with browser workers on and off.

- [ ] **Step 1:** Write the checks first:
  - `helm template` assertions in a script `deploy/helm/test.sh`, or extend the existing CI step. The browser Deployment exists only when enabled; `BROWSER_ENABLED` and `BROWSER_UNBLOCK_ENABLED` follow the toggles; the KEDA query names `pool="browser"`; render fails when `terminationGracePeriodSeconds` is too small.
  - `docker compose config -q` passes with the dummy env.
- [ ] **Step 2:** Run `helm lint` and the template checks (helm from the scratchpad build); expect the new assertions to fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `helm lint` on and off, `docker compose config -q`, full gates. The Docker build is verified on Jan's PC through Remote Control, in a new clone folder (never `C:\Users\jan\staging\Windsurf\text-extraction`):
  - `docker compose up --build -d --wait` with `BROWSER_UNBLOCK_ENABLED=true`;
  - `POST /v1/extract` with `mode: "browser"` on `https://www.zoerr.de/edu-sharing/components/render/33001463-6df3-40ac-8c4c-5d06eb7fd7dd` returns `mode_used: "browser"` and at least 50 words;
  - `POST /v1/screenshot` on `https://klexikon.zum.de/wiki/Dante_Alighieri` returns a PNG 1920 px wide via the artifact route;
  - an admin `mode: "browser"`, `compliance: "ignore"` request returns `unblocked: true`;
  - `docker images` shows the API image without a browser (both sizes noted).
- [ ] **Step 5:** Commit `feat: ship the browser worker image, compose service and Helm deployment`.

### Task 10: Live gate, docs and done-when

**Files:**
- Modify: `tests/live/run_live.py`, `tests/live/README.md`, `tests/test_live_run.py`, `.github/workflows/live.yml`, `docs/architecture.md`, `docs/packages.md`, `docs/configuration.md`, `README.md`, `CLAUDE.md`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `run_live.py --mode fast|auto|browser` (default `auto`), sent as the request's `mode`. Each JSONL line gains `mode_used`, and the summary counts results per `mode_used`.
  - `live.yml`:
    - installs Chromium (`uv run playwright install --with-deps --only-shell chromium`);
    - starts the browser worker next to the service with `BROWSER_ENABLED=true` (unblock off: the gate measures standard behaviour);
    - the gate (D2) runs `wlo_200` with `--mode auto --min-ok-rate 80`;
    - reports: `smoke` with `--mode auto`, and `wlo_200` with `--mode browser`.

    The defaults of the manual run change accordingly.
  - Docs:
    - architecture: a new section "Browser worker" holding B1-B15 and D1-D3 with Jan's reasons, plus the updated pipeline step 4, Auth (unblock under `ignore`) and Public API rows;
    - `packages.md` row 4: done-when "`wlo_200` in auto mode at or above the 80 % browser baseline; smoke set reported";
    - `CLAUDE.md` rule 6 gains: "Exception: unblock mode (admin, `compliance: ignore`, `BROWSER_UNBLOCK_ENABLED`) for sites that allowed extraction by contract may present a stealth browser with an unmodified Chrome user agent; outbound limits still apply and IP/proxy rotation stays excluded.";
    - configuration: all settings and the Helm keys;
    - README: a `mode: "browser"` example, a screenshot example with the artifact download, and an unblock note.

- [ ] **Step 1: Write the failing test:** `test_live_run.py::test_mode_option_is_sent_and_reported`.
- [ ] **Step 2:** Run it; expect failure.
- [ ] **Step 3:** Implement the harness change, the workflow and the docs.
- [ ] **Step 4:** Full gates. Then label the PR `live` and wait for the run.
  - Done when `wlo_200` in `auto` mode reaches at least 80 % and the reports are attached to the PR.
  - A miss goes to `systematic-debugging` on the buckets (per domain: fast vs. browser, as in `domain_comparison_simple_vs_browser_20260928.csv`) before any setting is tuned.
- [ ] **Step 5:** Commit `docs: document the browser worker; live gate at the browser baseline`.
