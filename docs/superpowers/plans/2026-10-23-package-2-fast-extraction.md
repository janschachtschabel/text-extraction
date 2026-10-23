# Package 2: Fast Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `POST /v1/extract` turns a public URL into clean Markdown, text or HTML in fast mode. The fetch is guarded against SSRF, size and time abuse. The extraction respects robots.txt and EU TDM opt-outs and stays within per-domain outbound limits. Each result carries DOM metadata with licences, a quality verdict, a fingerprint for change detection, and a Valkey result cache. A live regression harness measures it against the 68 % fast baseline.

**Architecture:** The new modules form a pipeline in `pipeline.py`:
1. guard (`netguard.py`)
2. compliance (`compliance.py`)
3. outbound gate (`outbound.py`)
4. fetch (`fetch.py`)
5. convert (`convert/`)
6. quality gate (`quality.py`) and metadata (`metadata.py`)
7. fingerprint and cache (`fingerprint.py`, `result_cache.py`)

The pipeline returns an `ExtractResult` inside the existing `Envelope`. All shared state (robots cache, domain gate, results, fingerprints) lives in Valkey. The SSRF guard is an `httpcore` network backend, so every connection is checked, including each redirect hop, against the address actually dialled. That defeats DNS rebinding.

**Tech Stack:** httpx (+h2), httpcore, Trafilatura (Apache-2.0 ≥ 1.8), BeautifulSoup4 + lxml, extruct, protego, datasketch, py3langid, charset-normalizer; dev: pytest-httpserver, fakeredis[lua].

**Spec:** `docs/architecture.md` (pipeline steps 1-5 and 7, envelope, error codes, DOM metadata, change detection), `docs/packages.md` row 2, `CLAUDE.md`.

**Reference code (read-only, not in the repo):**
- Jan's Selenium service (Apache-2.0, same owner, so porting with attribution is allowed): `/mnt/user-data/uploads/text-extraction/Wissen/Website-Textextraction-Selenium/app/` (`html_converter.py`, `markup.py`, `embedded_content.py`, `links.py`, `mathml.py`, `security.py`, `robots.py`, `http_fetcher.py`, `body_reader.py`).
- The Playwright service: `/mnt/user-data/uploads/text-extraction/Wissen/text-extraction-main/text-extraction-main/` (`text_extraction/grab_content.py` soft-404 and bot phrases, `domain_boilerplate.json`, `test/live/*`).

## Global Constraints

- English only. Python 3.13. Licence Apache-2.0. No AGPL/GPL dependency: check each new package's licence in `uv.lock` metadata, and require `trafilatura>=1.8`, which is Apache-2.0.
- Every outbound request (target, robots.txt, tdmrep.json, ai.txt) goes through the guarded `Fetcher`. No `trust_env` and no proxies.
- User agent: `text-extraction/<version> (+https://github.com/janschachtschabel/text-extraction)`. The robots.txt product token is `text-extraction`.
- Never log query strings or full target URLs. Log `host` and `path_hash` (first 12 hex characters of the sha256 of the path+query) at most.
- Client trace headers (`x-client-trace-id`, B3) are never forwarded to target sites (ruling: privacy).
- HTTP status of `/v1/extract`:
  - 200 for every pipeline outcome, with `status: failed|partial` and an error code;
  - 422 `invalid_request` for a malformed body;
  - 403 `forbidden` for a compliance mode the caller may not use;
  - 401 and 429 as in package 1;
  - 5xx only for our own faults.
- Every new setting is an env var with a documented default in `docs/configuration.md` and `.env.example`, in the task that adds it.
- No browser yet: `mode` accepts only `fast` and `auto`. In `auto`, the quality gate's verdict is reported as `quality.browser_recommended`. Escalation arrives with package 4.
- Not in this package: PDF, Office and images (package 5, `unsupported_content_type` until then), PII, screenshots, chunking, jobs and `wait_seconds` (package 3), LSH near-duplicate search.

## Review Focus

1. A hostname that resolves to a private, loopback, link-local or metadata IP, directly or on a redirect hop, gives `blocked_url`, and no connection is made. That includes IPv4-mapped IPv6 and mixed answers (one public, one private address). Owner: Task 1.
2. An endless, huge or gzip-bomb body stops at `FETCH_MAX_BYTES` of decoded bytes with `too_large`, without buffering more than the cap. Owner: Task 2.
3. A target that drips bytes slowly hits the overall fetch deadline (`timeout`), not a per-read timeout that never fires. Owner: Task 2.
4. A page in ISO-8859-1 or Windows-1252 with a missing or wrong charset declaration still yields correct German umlauts. Owner: Task 5.
5. robots.txt that is 404, 5xx, unreachable, huge or malformed follows RFC 9309: 4xx means allow all; 5xx or unreachable means disallow all under `enforce`; parsing is capped at 500 KiB. Owner: Task 4.

---

### Task 1: URL validation and SSRF guard

**Files:**
- Create: `src/text_extraction/netguard.py`, `tests/test_netguard.py`
- Modify: `settings.py`, `pyproject.toml` (deps `httpx[http2]`, `httpcore`; dev dep `pytest-httpserver`), `docs/configuration.md`, `.env.example`

**Interfaces:**
- Produces:
  - Settings fields:
    - `fetch_timeout_seconds: float = Field(20, gt=0)`
    - `fetch_max_bytes: int = Field(20_000_000, ge=1024)`
    - `fetch_max_redirects: int = Field(5, ge=0, le=20)`
    - `fetch_allow_private_networks: bool = False` (dev and tests only; documented as unsafe)
  - `class InvalidUrlError(ValueError)` and `class BlockedAddressError(OSError)`.
  - `def validate_url(url: str) -> str`: returns the URL unchanged. It raises `InvalidUrlError` for:
    - a scheme other than `http`/`https`;
    - a missing host;
    - userinfo in the URL;
    - a URL longer than 2048 characters;
    - a port outside 1-65535.
  - `def is_public_address(ip: IPv4Address | IPv6Address) -> bool`: false for private, loopback, link-local (incl. 169.254.169.254), multicast, reserved, unspecified, shared (100.64/10), documentation and IPv6 ULA addresses. An IPv4-mapped or 6to4 IPv6 address is unwrapped and judged by its embedded IPv4 address.
  - `AddressPolicy = Callable[[IPv4Address | IPv6Address], bool]`.
  - `class GuardedTransport(httpx.AsyncBaseTransport)`: `__init__(self, policy: AddressPolicy, *, http2: bool = True)`, an `httpcore.AsyncConnectionPool` on a `GuardedBackend` (see Step 1).
  - `class GuardedBackend(httpcore.AsyncNetworkBackend)`: `__init__(self, policy: AddressPolicy, inner: httpcore.AsyncNetworkBackend | None = None)`. `connect_tcp` resolves the host with `loop.getaddrinfo` (an IP literal is used as is). If any resolved address fails the policy, it raises `BlockedAddressError` (ruling: a mixed answer is blocked). Otherwise it connects `inner` to the first address. `connect_unix_socket` always raises `BlockedAddressError`.

- [ ] **Step 1: Failing tests:**
  - `test_validate_url_accepts_http_and_https`
  - `test_validate_url_rejects` (parametrized: `ftp://x`, `http://`, `http://user:pw@x.org/`, a 2049-character URL, `http://x.org:0/`, `javascript:alert(1)`)
  - `test_private_addresses_are_not_public` (parametrized: `10.0.0.1`, `172.16.0.1`, `192.168.1.1`, `127.0.0.1`, `169.254.169.254`, `100.64.0.1`, `0.0.0.0`, `::1`, `fc00::1`, `fe80::1`, `::ffff:127.0.0.1`, `2002:7f00:1::`, `224.0.0.1`)
  - `test_public_addresses_are_public` (`93.184.216.34`, `2606:2800:220:1::`)
  - `test_backend_refuses_loopback_literal`
  - `test_backend_refuses_mixed_dns_answer`: monkeypatch `getaddrinfo` to return one public and one private address.
  - `test_backend_connects_when_policy_allows`: pytest-httpserver on 127.0.0.1 with a policy that allows 127.0.0.1; a real httpx request through `httpx.AsyncHTTPTransport` with this backend succeeds.

  To build the transport: `httpx.AsyncHTTPTransport()` plus `transport._pool._network_backend = backend` is private API. Instead build `httpcore.AsyncConnectionPool(network_backend=backend, http2=True)` and wrap it in a small `httpx.AsyncBaseTransport` adapter in `netguard.py`. Name it `GuardedTransport(policy, *, http2: bool = True)`, so Task 2 consumes one class.
- [ ] **Step 2: Run, see FAIL.** **Step 3: Implement.** **Step 4: Run, see PASS; run all gates.**
- [ ] **Step 5:** Document the four settings in `docs/configuration.md` and `.env.example`, then commit `feat: add URL validation and SSRF-guarded transport`.

### Task 2: Fetcher

**Files:**
- Create: `src/text_extraction/fetch.py`, `tests/test_fetch.py`
- Modify: `docs/configuration.md` (user agent line)

**Interfaces:**
- Consumes: `GuardedTransport`, `validate_url`, `InvalidUrlError`, `BlockedAddressError`, `is_public_address`, the fetch settings, and `ErrorCode`.
- Produces:
  - `@dataclass(frozen=True) class Validators`: `etag: str | None`, `last_modified: str | None`.
  - `@dataclass(frozen=True) class FetchResult`: `url: str`, `final_url: str`, `status: int`, `headers: dict[str, str]` (lower-case names), `content: bytes`, `content_type: str | None` (media type only, lower-case), `charset: str | None`, `elapsed_ms: int`, `redirects: list[str]`, `not_modified: bool`, `raw_sha256: str`.
  - `class FetchError(Exception)`: `code: ErrorCode`, `message: str`, `retryable: bool`, `upstream_status: int | None`.
  - `class Fetcher`:
    - `__init__(self, settings: Settings, *, policy: AddressPolicy | None = None)`. A missing policy defaults to `is_public_address`, or to allow-all when `fetch_allow_private_networks` is set.
    - `async fetch(self, url: str, *, validators: Validators | None = None, max_bytes: int | None = None, timeout_seconds: float | None = None) -> FetchResult`
    - `async aclose(self) -> None`
  - `USER_AGENT: str`

Behaviour:
- Redirects are followed manually, up to `fetch_max_redirects`. Each hop is re-validated with `validate_url` and connected through the guard.
- The body is streamed and decoded, and stops at `max_bytes` (default `fetch_max_bytes`) with `too_large`.
- The whole call, including redirects, runs under one `asyncio.timeout(timeout_seconds or fetch_timeout_seconds)`, which maps to `timeout`.
- Validators are sent as `If-None-Match` / `If-Modified-Since`, and a 304 gives `not_modified=True` with empty content.
- A status ≥ 400 is returned, not raised: the pipeline decides.
- Exception mapping:

  | Exception | Code | Retryable |
  | --- | --- | --- |
  | `InvalidUrlError` | `invalid_url` | false |
  | `BlockedAddressError` | `blocked_url` | false |
  | DNS failure (`socket.gaierror`) | `dns_error` | true |
  | other connect or read errors | `connection_error` | true |
  | too many redirects | `target_http_error` | false, with `upstream_status` of the last 3xx |

- `Accept: text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5`, `Accept-Language: de,en;q=0.8`.

- [ ] **Step 1: Failing tests** (pytest-httpserver; tests use a policy allowing 127.0.0.1 only):
  - `test_fetch_returns_body_status_and_content_type`
  - `test_fetch_follows_redirect_and_records_chain`
  - `test_redirect_to_disallowed_address_is_blocked`: the server on 127.0.0.1 redirects to `http://127.0.0.2:<port>/`, the policy allows only 127.0.0.1, and the result is `FetchError(blocked_url)`.
  - `test_too_many_redirects`
  - `test_body_over_cap_is_too_large`: a 2 MB body with `max_bytes=1_000_000`.
  - `test_gzip_bomb_is_too_large`: a gzip body that decodes to 50 MB, `max_bytes=1_000_000`. Assert that peak decoded bytes read stay below `max_bytes + 65536` by counting chunks.
  - `test_slow_drip_hits_overall_deadline`: the handler sends one byte every 0.2 s for 10 s, `timeout_seconds=1`, so the result is `timeout` in under 2 s.
  - `test_http_error_status_is_returned_not_raised` (404)
  - `test_conditional_request_304`
  - `test_dns_failure_is_dns_error`: a `.invalid` host.
  - `test_user_agent_header_sent`
  - `test_no_client_trace_headers_forwarded`: the fetcher never adds `x-client-trace-id`/`b3`.
- [ ] **Steps 2-4:** RED, implement, GREEN, then all gates.
- [ ] **Step 5:** Commit `feat: add guarded HTTP fetcher with byte and time limits`.

### Task 3: Per-domain outbound gate

**Files:**
- Create: `src/text_extraction/outbound.py`, `tests/test_outbound.py`
- Modify: `settings.py`, `pyproject.toml` (dev `fakeredis[lua]`), docs, `.env.example`

**Interfaces:**
- Produces:
  - Settings fields:
    - `outbound_max_concurrency_per_host: int = Field(2, ge=1)`
    - `outbound_min_delay_ms: int = Field(1000, ge=0)`
    - `outbound_max_crawl_delay_seconds: float = Field(10, ge=0)`
    - `outbound_max_wait_seconds: float = Field(10, ge=0)`
    - `outbound_backoff_seconds: float = Field(30, ge=1)`
    - `outbound_max_backoff_seconds: float = Field(300, ge=1)`
    - `circuit_failure_threshold: int = Field(5, ge=1)`
    - `circuit_open_seconds: float = Field(60, ge=1)`
  - `class OutboundBusy(Exception)` (`retry_after: float`) and `class CircuitOpen(Exception)` (`retry_after: float`).
  - `class DomainGate`:
    - `__init__(self, valkey: Redis, settings: Settings, *, clock: Callable[[], float] = time.time, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep)`
    - `def slot(self, host: str, crawl_delay: float | None = None) -> AbstractAsyncContextManager[None]`
    - `async def record(self, host: str, *, status: int | None, retry_after: float | None) -> None`

Behaviour:
- `slot` waits up to `outbound_max_wait_seconds` for both of these:
  - an in-flight lease: key `out:inflight:{host}`, INCR with a 120 s safety TTL, DECR on exit;
  - the per-host spacing slot: key `out:next:{host}`, decided atomically with a Lua script. The delay is `max(outbound_min_delay_ms/1000, min(crawl_delay, outbound_max_crawl_delay_seconds))`.
- Before waiting, `slot` checks `out:cooldown:{host}` and `out:circuit:{host}`. It raises `CircuitOpen` while either is set, and `OutboundBusy` when the wait would exceed the maximum.
- `record` behaviour:
  - Status 429 or 503 sets the cooldown to `Retry-After` (seconds or HTTP date), or to `outbound_backoff_seconds`, capped at `outbound_max_backoff_seconds`.
  - `status is None` (a connection failure) or a 5xx increments `out:fail:{host}`. Reaching the threshold opens the circuit for `circuit_open_seconds`.
  - Any status < 500 other than 429 resets the failure count.
- Hosts are lower-cased IDNA.

- [ ] **Step 1: Failing tests** (fakeredis with Lua, injected clock and sleep):
  - `test_concurrency_limit_per_host`
  - `test_min_delay_spaces_requests`
  - `test_crawl_delay_used_and_capped`
  - `test_wait_longer_than_max_raises_busy`
  - `test_429_with_retry_after_sets_cooldown`
  - `test_retry_after_http_date`
  - `test_503_without_retry_after_uses_default_backoff`
  - `test_circuit_opens_after_threshold_and_closes_after_timeout`
  - `test_success_resets_failures`
  - `test_lease_released_on_exception`
  - `test_hosts_are_independent`
- [ ] **Steps 2-4:** RED, implement, GREEN, then all gates.
- [ ] **Step 5:** Commit `feat: add per-domain outbound gate in Valkey`.

### Task 4: Compliance (robots.txt, TDMRep, ai.txt, noai)

**Files:**
- Create: `src/text_extraction/compliance.py`, `tests/test_compliance.py`
- Modify: `settings.py`, `pyproject.toml` (`protego`), docs, `.env.example`

**Interfaces:**
- Consumes: `Fetcher`, `FetchResult`, `FetchError`, `Principal`, `ServiceError`.
- Produces:
  - Settings fields:
    - `compliance_default: Literal["enforce","report","ignore"] = "enforce"`
    - `compliance_allow_override: bool = True`
    - `compliance_cache_seconds: int = Field(3600, ge=0)`
  - `ComplianceMode = Literal["enforce", "report", "ignore"]`
  - `def resolve_mode(requested: ComplianceMode | None, principal: Principal, settings: Settings) -> ComplianceMode`:
    - `None` returns the default;
    - `ignore` is for admins only;
    - for a non-admin, any value other than the default needs `compliance_allow_override`;
    - otherwise it raises `ServiceError(forbidden, ..., 403)`.
  - `class ComplianceReport(BaseModel)`:
    - `mode: ComplianceMode`
    - `decision: Literal["allowed", "blocked", "reported", "skipped"]`
    - `robots: Literal["allowed", "disallowed", "unavailable", "not_checked"]`
    - `crawl_delay: float | None`
    - `ai_txt: Literal["allowed", "disallowed", "absent", "not_checked"]`
    - `tdm_reserved: bool | None`
    - `tdm_policy: str | None`
    - `tdm_source: Literal["tdmrep.json", "header", "meta"] | None`
    - `noai: bool`
    - `reasons: list[str]`
  - `class ComplianceChecker`:
    - `__init__(self, fetcher: Fetcher, valkey: Redis, settings: Settings)`
    - `async def before_fetch(self, url: str, mode: ComplianceMode) -> ComplianceReport`: robots.txt, tdmrep.json and ai.txt, each cached in Valkey under `cmp:{kind}:{scheme}://{host}:{port}` for `compliance_cache_seconds`, each fetched with `max_bytes=512_000` and `timeout_seconds=10`.
    - `def after_fetch(self, report: ComplianceReport, fetched: FetchResult, html: str | None) -> ComplianceReport`: the `tdm-reservation` and `tdm-policy` headers, `<meta name="tdm-reservation">`, and `noai`/`noml` in `X-Robots-Tag` or `<meta name="robots">`.
  - Module-level `def blocking_code(report: ComplianceReport) -> ErrorCode | None`: `blocked_by_robots` or `tdm_reserved` (also for `noai` and for ai.txt `disallowed`), but only when `mode == "enforce"`.
- Rules:
  - `ignore` skips every check (`decision="skipped"`).
  - robots.txt uses RFC 9309 semantics via protego with the product token `text-extraction`:
    - 2xx: parse;
    - 4xx: allow all;
    - 5xx or a fetch error: `unavailable`, which counts as disallowed under `enforce`.
  - tdmrep.json uses the W3C TDMRep JSON array. The first entry whose `location` pattern (`*` wildcard, path match) matches wins.
  - ai.txt is parsed with protego like robots.txt; 404 means `absent`.
  - `llms.txt` is not a restriction and is not checked (ruling).
  - Under `report`, the decision is `reported` when something would block, and nothing blocks.

- [ ] **Step 1: Failing tests** (fake Fetcher returning canned `FetchResult`/`FetchError`, fakeredis):
  - `test_robots_disallow_blocks_in_enforce`
  - `test_robots_disallow_only_reported_in_report_mode`
  - `test_robots_404_allows_all`
  - `test_robots_503_is_unavailable_and_blocks_in_enforce`
  - `test_robots_crawl_delay_reported`
  - `test_robots_cached_in_valkey` (second check makes no fetch)
  - `test_wildcard_and_user_agent_group` (`User-agent: text-extraction` group wins over `*`)
  - `test_tdmrep_json_location_match`
  - `test_tdm_header_and_meta`
  - `test_x_robots_tag_noai`
  - `test_ai_txt_disallow`
  - `test_ignore_skips_all_fetches`
  - `test_resolve_mode_user_cannot_ignore_403`
  - `test_resolve_mode_override_disabled`
  - `test_admin_may_ignore`
  - `test_malformed_robots_does_not_crash`
- [ ] **Steps 2-4:** RED, implement, GREEN, then all gates.
- [ ] **Step 5:** Commit `feat: add robots.txt, TDMRep and ai.txt compliance checks`.

### Task 5: HTML and text conversion

**Files:**
- Create: `src/text_extraction/convert/__init__.py`, `convert/decode.py`, `convert/markup.py`, `convert/embedded.py`, `convert/links.py`, `convert/mathml.py`, `convert/html.py`, `tests/test_convert_*.py`
- Modify: `pyproject.toml` (`trafilatura>=1.8`, `beautifulsoup4`, `lxml`, `charset-normalizer`), `NOTICE` (create: "Portions adapted from Website-Textextraction-Selenium, Copyright Jan Schachtschabel, Apache-2.0")

**Interfaces:**
- Produces:
  - `OutputFormat = Literal["markdown", "text", "html"]`
  - `def decode_html(data: bytes, content_type_charset: str | None) -> str`. Charset order: the header charset, then BOM, then `<meta charset>`/`http-equiv` in the first 4 KiB, then charset-normalizer. A declared charset that fails to decode falls through to the next source.
  - `@dataclass(frozen=True) class Conversion`: `content: str`, `converter: Literal["trafilatura", "bs4", "plain"]`, `title: str | None`, `warnings: list[str]`.
  - `def convert_html(html: str, url: str, fmt: OutputFormat) -> Conversion`
  - `def convert_plain(text: str) -> Conversion`
  - `@dataclass(frozen=True) class Link`: `url: str`, `text: str`, `kind: Literal["internal", "external", "file"]`.
  - `def page_links(html: str, base_url: str) -> list[Link]`
- Port, adapting to the interfaces above, from the Selenium reference: `markup.prepare_html`, hidden-element stripping, table structure, `embedded_content`, `links.absolute_url` and `DOWNLOAD_EXTS`, `mathml` (MathML to `$$…$$` LaTeX), `html_converter` (page heading, linked documents kept below the text, `include_links`/`include_tables`). Formats:
  - `markdown`: Trafilatura `output_format="markdown"`.
  - `text`: Trafilatura `"txt"`.
  - `html`: Trafilatura `"html"`.
  
  The fallback is BeautifulSoup text. MarkItDown is dropped until package 5. Port the matching tests from the reference `tests/test_conversion*.py` where they apply.

- [ ] **Step 1: Failing tests:**
  - `test_decode_latin1_without_declaration` (Review Focus 4)
  - `test_decode_wrong_header_charset_falls_back`
  - `test_markdown_keeps_heading_and_tables`
  - `test_text_and_html_formats`
  - `test_mathml_to_latex`
  - `test_linked_documents_kept` (a link-only list of PDFs)
  - `test_hidden_and_nav_removed`
  - `test_relative_links_made_absolute`
  - `test_page_links_kinds`
  - `test_fallback_to_bs4_when_trafilatura_empty`
  - `test_plain_text_passthrough`
- [ ] **Steps 2-4:** RED, implement, GREEN, then all gates.
- [ ] **Step 5:** Commit `feat: convert HTML to Markdown, text and HTML with MathML and linked files`.

### Task 6: Domain boilerplate rules and quality gate

**Files:**
- Create: `src/text_extraction/boilerplate.py`, `src/text_extraction/data/domain_boilerplate.json` (ported from the reference; ruling, see below), `src/text_extraction/quality.py`, `tests/test_boilerplate.py`, `tests/test_quality.py`
- Modify: `settings.py` (`quality_min_words: int = Field(50, ge=0)`), docs, `.env.example`

**Interfaces:**
- Produces:
  - `def load_boilerplate_rules() -> Mapping[str, frozenset[str]]`: package data, cached.
  - `def strip_boilerplate(host: str, text: str, rules: Mapping[str, frozenset[str]]) -> str`: removes lines that match a rule exactly after whitespace trimming. The rule host matches the exact host or `www.`-less.
  - `class Quality(BaseModel)`: `visible_chars: int`, `word_count: int`, `script_count: int`, `js_heavy: bool`, `bot_challenge: bool`, `soft_404: bool`, `login_wall: bool`, `browser_recommended: bool`.
  - `def assess(html: str | None, content: str, *, min_words: int) -> Quality`
  - `def quality_error(q: Quality, min_words: int) -> tuple[Literal["failed", "partial"], ErrorCode] | None`
- Rules:
  - Visible characters are the text without scripts or styles.
  - `js_heavy` means `visible_chars < 500` and (`script_count ≥ 1` or a `<noscript>` hint or an empty SPA root `#root`, `#app`, `#__next`).
  - `browser_recommended = js_heavy or bot_challenge`.
  - Bot challenge phrases are reimplemented, not copied: Cloudflare "Just a moment", `cf-chl`, "Checking your browser", "Verifying you are human", "Enable JavaScript and cookies to continue", Anubis, DataDome, PerimeterX, "captcha". Soft-404 phrases (de and en, e.g. "Seite nicht gefunden", "page not found", "404") count only when `word_count ≤ 50`. A login wall is a password input plus `word_count < 100`.
  - `quality_error` priority: `bot_challenge` gives failed `bot_protection`; `soft_404` gives failed `likely_error_page`; `login_wall` gives failed `login_required`; 0 words gives failed `empty_content`; fewer than `min_words` gives partial `content_too_short`.
- Ruling: `domain_boilerplate.json` is ported from the WLO Playwright service as data (learned lines, not code), with a source note in `NOTICE`. Jan has been asked to confirm.

- [ ] **Step 1: Failing tests:**
  - `test_strip_boilerplate_exact_lines_only`
  - `test_www_host_matches`
  - `test_js_heavy_spa_shell`
  - `test_cloudflare_challenge`
  - `test_soft_404_short_page`
  - `test_long_page_mentioning_404_is_not_soft_404`
  - `test_login_wall`
  - `test_too_short_is_partial`
  - `test_empty_is_failed`
  - `test_normal_article_passes`
- [ ] **Steps 2-4:** RED, implement, GREEN, then all gates.
- [ ] **Step 5:** Commit `feat: add domain boilerplate rules and quality gate`.

### Task 7: DOM metadata and licences

**Files:**
- Create: `src/text_extraction/metadata.py`, `tests/test_metadata.py`
- Modify: `pyproject.toml` (`extruct`, `py3langid`)

**Interfaces:**
- Produces:
  - `class License(BaseModel)`: `id: str | None` (SPDX-style, e.g. `CC-BY-SA-4.0`, `CC0-1.0`, `PDM-1.0`), `url: str | None`, `label: str | None`, `source: Literal["rel-license", "json-ld", "microdata", "rdfa", "meta", "opengraph", "text"]`, `confidence: Literal["high", "medium", "low"]`.
  - `class Image(BaseModel)`: `url: str`, `alt: str`.
  - `class PageMetadata(BaseModel)`: `title`, `description`, `authors: list[str]`, `published`, `modified`, `site_name`, `canonical_url`, `language`, `keywords: list[str]`, `license: License | None` (the best candidate), `licenses: list[License]`, `lrmi: dict[str, Any]`, `images: list[Image]` (at most 20).
  - `def normalize_license(value: str) -> str | None`: handles CC URLs in every version, jurisdiction port and http/https form, plus text forms such as "CC BY-SA 4.0" and "Namensnennung - Weitergabe unter gleichen Bedingungen 4.0".
  - `def extract_metadata(html: str, url: str, headers: Mapping[str, str]) -> PageMetadata`
  - `def detect_language(html_lang: str | None, header_lang: str | None, text: str) -> str | None`: html `lang`, then `Content-Language`, then py3langid on at least 50 words. Returns ISO 639-1.
- Licence confidence: `high` for `rel=license` and JSON-LD/microdata `license`; `medium` for meta, `dc.rights` and OpenGraph; `low` for a text match in the footer. Best = highest confidence, then first.
- LRMI keys copied when present: `educationalLevel`, `learningResourceType`, `audience`/`educationalRole`, `typicalAgeRange`, `teaches`, `educationalAlignment`, `inLanguage`, `isAccessibleForFree`, `timeRequired`, `interactivityType`.

- [ ] **Step 1: Failing tests:**
  - `test_rel_license_cc_by_sa`
  - `test_jsonld_license_and_lrmi`
  - `test_text_license_low_confidence`
  - `test_normalize_license_variants` (parametrized ≥ 8 cases incl. CC0, PDM, a German text form and an unknown string giving None)
  - `test_opengraph_and_canonical`
  - `test_images_with_alt_capped_at_20`
  - `test_language_from_html_then_header_then_detection`
  - `test_broken_jsonld_is_ignored`
- [ ] **Steps 2-4:** RED, implement, GREEN, then all gates.
- [ ] **Step 5:** Commit `feat: extract DOM metadata, licences and LRMI fields`.

### Task 8: Fingerprints, change detection and result cache

**Files:**
- Create: `src/text_extraction/fingerprint.py`, `src/text_extraction/result_cache.py`, `tests/test_fingerprint.py`, `tests/test_result_cache.py`
- Modify: `settings.py`, `pyproject.toml` (`datasketch`), docs, `.env.example`

**Interfaces:**
- Produces:
  - Settings fields:
    - `result_cache_ttl_seconds: int = Field(3600, ge=0)` (fresh; 0 disables hits)
    - `result_store_ttl_seconds: int = Field(86400, ge=0)` (kept for revalidation)
    - `fingerprint_ttl_seconds: int = Field(2_592_000, ge=0)`
  - `def normalize_text(content: str) -> str`: strips Markdown syntax (headings, emphasis, link targets, list markers, table pipes), casefolds, and collapses whitespace.
  - `class Fingerprint(BaseModel)`: `raw_sha256: str`, `text_sha256: str`, `minhash: str` (base64 of 128 little-endian uint32 values), `similarity_to_previous: float | None`, `changed: bool | None`.
  - `def compute_fingerprint(raw_sha256: str, content: str) -> Fingerprint`: 128 permutations, seed 1, word 5-shingles (fewer words: the word set).
  - `def similarity(a: str, b: str) -> float`: Jaccard estimate from two encoded minhashes.
  - `class FingerprintStore`: `__init__(valkey, settings)`, `async def compare_and_save(self, url: str, fp: Fingerprint) -> Fingerprint`. It fills `similarity_to_previous` and `changed` (`text_sha256` differs) from the previous fingerprint. The key is `fp:{sha256(normalized url)}`. A normalized URL has a lower-case scheme and host, no fragment, and no default port.
  - `@dataclass class CachedEntry`: `stored_at: float`, `validators: Validators`, `raw_sha256: str`, `result: dict[str, Any]`.
  - `class ResultCache`:
    - `__init__(valkey, settings, clock=time.time)`
    - `async def get(self, key: str) -> tuple[CachedEntry, bool] | None` (the bool is whether the entry is fresh)
    - `async def put(self, key: str, entry: CachedEntry) -> None` (skips entries whose JSON is over 2 MB)
    - `def cache_key(url: str, options: Mapping[str, Any]) -> str`: sha256 of the normalized URL plus sorted options JSON, prefixed `res:`.

- [ ] **Step 1: Failing tests:**
  - `test_normalize_strips_markdown_and_case`
  - `test_same_text_same_hash_regardless_of_markdown`
  - `test_minhash_similarity_close_for_small_edit` (> 0.8)
  - `test_minhash_similarity_low_for_different_text` (< 0.2)
  - `test_short_text_fingerprint`
  - `test_store_reports_changed_and_similarity`
  - `test_first_fingerprint_has_no_previous`
  - `test_cache_fresh_then_stale_then_gone` (injected clock)
  - `test_cache_key_order_independent`
  - `test_oversized_entry_not_cached`
- [ ] **Steps 2-4:** RED, implement, GREEN, then all gates.
- [ ] **Step 5:** Commit `feat: add text fingerprints, change detection and result cache`.

### Task 9: Extraction pipeline and `POST /v1/extract`

**Files:**
- Create: `src/text_extraction/pipeline.py`, `src/text_extraction/extract_api.py`, `tests/test_pipeline.py`, `tests/test_extract_api.py`
- Modify: `app.py` (wire the pipeline into `app.state.pipeline`, add a lifespan that closes the fetcher), `server.py` (`timeout_graceful_shutdown=30`, deferred from package 1), `settings.py` (`extract_timeout_seconds: float = Field(25, gt=0)`), docs, `.env.example`, `tests/test_errors.py` (`ErrorCode` drift test against `docs/architecture.md`, deferred from package 1)

**Interfaces:**
- Consumes: everything in Tasks 1-8, `Principal`, `require_role`, `rate_limited`, `Envelope`.
- Produces:
  - `class ExtractRequest(BaseModel, extra="forbid")`: `url: str` (max length 2048), `mode: Literal["fast", "auto"] = "auto"`, `format: OutputFormat = "markdown"`, `metadata: bool = True`, `links: bool = False`, `compliance: ComplianceMode | None = None`, `cache: bool = True`.
  - `class Timings(BaseModel)`: `total_ms: int`, `compliance_ms: int`, `fetch_ms: int`, `convert_ms: int`.
  - `class ExtractResult(BaseModel)`:
    - `url`, `final_url: str | None`, `upstream_status: int | None`, `content_type: str | None`
    - `mode_used: Literal["fast"]`, `attempts: int`
    - `language: str | None`, `word_count: int`, `title: str | None`
    - `format: OutputFormat`, `content: str | None`
    - `quality: Quality | None`, `timings: Timings`, `compliance: ComplianceReport`
    - `metadata: PageMetadata | None`, `links: list[Link] | None`, `fingerprint: Fingerprint | None`
    - `cache: Literal["miss", "hit", "revalidated", "bypass"]`, `warnings: list[str]`
  - `class Pipeline`: `__init__(fetcher, gate, checker, cache, fingerprints, settings, registry)`, `async def run(self, req: ExtractRequest, principal: Principal) -> Envelope[ExtractResult]`
  - The route is `POST /v1/extract` with `require_role("user")` and `rate_limited`, and returns `Envelope[ExtractResult]`.
- Flow:
  1. `resolve_mode`.
  2. `validate_url`.
  3. A fresh cache hit, when `cache` is true, returns the stored result with `cache="hit"`.
  4. `before_fetch` compliance; a block returns a failed envelope with the compliance report.
  5. Fetch inside `gate.slot(host, crawl_delay)`, then `gate.record`. A stale entry's validators are sent; a 304 or the same `raw_sha256` reuses the stored result with `cache="revalidated"`.
  6. Upstream ≥ 400 gives failed `target_http_error` with `upstream_status`.
  7. Content type:
     - HTML and XHTML: decode, `after_fetch`, `convert_html`, `strip_boilerplate`;
     - `text/plain`: `convert_plain`;
     - anything else: failed `unsupported_content_type`.
  8. Quality, metadata, links, fingerprint and cache put.
- Everything runs under `asyncio.timeout(extract_timeout_seconds)` and maps to `timeout`. `FetchError` maps to its code; `OutboundBusy` gives `rate_limited` (retryable); `CircuitOpen` gives `connection_error` (retryable).
- Metrics on the injected registry:
  - `text_extraction_extract_total{mode,outcome}`, where outcome is `success`, `partial` or the error code;
  - `text_extraction_extract_duration_seconds{mode}`.
- Log one `extract_done` line with `host`, `path_hash`, `outcome`, `upstream_status` and `total_ms`.

- [ ] **Step 1: Failing tests:**
  - `test_pipeline.py`: a fake fetcher, fakeredis and a real converter. One test per flow branch: success, robots block, report mode, 404, unsupported type, too short partial, cache hit, revalidated by 304, revalidated by same hash, timeout, busy, circuit open, links on request, metadata off.
  - `test_extract_api.py`: the real app via TestClient with the pipeline's fetcher replaced through `app.state`:
    - `test_requires_key`
    - `test_unknown_field_is_422_invalid_request`
    - `test_user_cannot_ignore_compliance_403`
    - `test_success_envelope_shape`
    - `test_metrics_count_outcomes`
  - `test_error_codes_match_architecture`
- [ ] **Steps 2-4:** RED, implement, GREEN, then all gates. Manual check: with Valkey from docker and `FETCH_ALLOW_PRIVATE_NETWORKS=true`, serve a local HTML page with `python -m http.server` and `curl` the endpoint.
- [ ] **Step 5:** Commit `feat: add POST /v1/extract fast-mode pipeline`.

### Task 10: Live regression harness

**Files:**
- Create:
  - `tests/live/README.md`
  - `tests/live/run_live.py`
  - `tests/live/urls/smoke.csv` (the 24 curated URLs, ported)
  - `tests/live/urls/wlo_50.csv`, `wlo_200.csv`, `wlo_4000.csv` (ported)
  - `.github/workflows/live.yml`
- Modify: `pyproject.toml` (ruff and mypy cover `tests/live`; pytest ignores `tests/live`), `docs/packages.md` (row 2 done-when: "`wlo_200` at or above the 68 % fast baseline; smoke set reported")

**Interfaces:**
- `run_live.py` CLI: `--base-url` (default `http://127.0.0.1:8080`), `--api-key` (env `LIVE_API_KEY`), `--urls PATH`, `--concurrency 8`, `--min-words 50`, `--compliance report`, `--min-ok-rate FLOAT` (exit 1 below it), `--out results/live_<ts>.jsonl`.
- Requests use `format=text`, `mode=auto`, `cache=false`.
- Per-domain queues run one request at a time per domain, round-robin across domains, under global concurrency.
- Buckets come from the envelope:
  - `ok`: success or partial, `word_count ≥ min_words`, and no error-page code;
  - `too_short`;
  - `likely_error_page` (incl. `bot_protection` and `login_required`);
  - `no_content` (`empty_content`, `unsupported_content_type`);
  - `target_http_error`;
  - `connection_error` (`dns_error`, `connection_error`, `timeout`);
  - `blocked` (compliance, which counts only under `enforce`);
  - `rate_limited`;
  - `service_error` (HTTP ≥ 500 or `internal_error`);
  - `invalid_request`.
- The summary table lists buckets overall and per domain, the ok rate, and the count that `enforce` would have blocked (from `compliance.decision == "reported"`).
- `live.yml` triggers:
  - `workflow_dispatch` with inputs `url_set` (default `wlo_200`) and `min_ok_rate` (default `68`);
  - `pull_request` when the PR carries the label `live`;
  - a weekly schedule on Monday at 03:00 UTC.
  
  The job runs a Valkey service container, starts `uv run text-extraction` with `AUTH_MODE=none` and `RATE_LIMIT_ENABLED=false`, waits for `:9090/ready`, runs the harness, and uploads the JSONL artifact. `timeout-minutes: 60`.
- Ruling, done-when measurement: the 68 % baseline was measured on 4,000 WLO URLs, of which `wlo_200.csv` is a sample. `smoke.csv` is a curated watchlist of known-hard and expected-failure pages, so its ok rate is not comparable. The gate is therefore `wlo_200` ≥ 68 % ok under `compliance=report`. `smoke.csv` runs as a report.

- [ ] **Step 1:** Unit-test the classifier: `tests/test_live_classify.py` with one case per bucket. It runs in the normal suite, so `classify` lives in `tests/live/classify.py`, imported by both.
- [ ] **Step 2:** Implement, actionlint the workflow, run all gates.
- [ ] **Step 3:** Commit `test: add live WLO regression harness and workflow`.
- [ ] **Step 4: Verify on GitHub:** label the PR `live`. The workflow must report an ok rate ≥ 68 % on `wlo_200`. Record the bucket table in the PR.

---

Deferred to later packages (owner): browser escalation (4); MarkItDown and Docling (5); job fallback with `wait_seconds` (3); LSH near-duplicates (9); `llms.txt` (not a restriction, ruling).
