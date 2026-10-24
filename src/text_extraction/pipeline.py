"""The fast extraction pipeline behind ``POST /v1/extract``.

One request runs these steps (docs/architecture.md, "Extraction pipeline"):

1. ``resolve_mode``: the compliance mode that applies; a refused choice is a 403.
2. ``validate_url``.
3. Result cache: a fresh entry is served as it is (``cache: hit``).
4. Compliance before the fetch (robots.txt, tdmrep.json, ai.txt); a block ends here. When the
   origin's files are cached, this takes no request. Otherwise the host's ``DomainGate`` turn is
   taken first and the missing files are fetched in it, one after another, before the page: so
   every request to the host counts against its limits. The fetcher checks every request with
   the gate (``DomainGate.before_request``): the turn's own requests go back to back, and each
   keeps the spacing to the latest request of any other turn.
5. The fetch, inside the host's ``DomainGate`` turn (the one of step 4, if taken), and its
   outcome reported to the gate. A stale cache entry's validators are sent; a 304 or unchanged
   bytes reuse the stored result (``cache: revalidated``). Each redirect target passes the
   compliance checks of step 4 before it is requested; one on another host gives the first
   host's turn back and takes the new host's, in which its missing files are fetched too.
6. Any other final status than 2xx (or a 304 to our validators) ends as ``target_http_error``.
7. HTML and XHTML: decode, compliance after the fetch (headers, meta tags, ``noai``; may block),
   convert, strip domain boilerplate. ``text/plain``: decode, header checks, ``convert_plain``.
   Anything else: ``unsupported_content_type``.
8. Quality verdict, language, metadata, links, fingerprint and cache.

All of it runs under one deadline, ``EXTRACT_TIMEOUT_SECONDS``. Every outcome of the target is
an envelope (HTTP 200) with ``status`` ``success``, ``partial`` or ``failed``; only a refused
compliance mode (403) and a Valkey outage of the outbound gate (503) leave as ``ServiceError``.
A caller that can retry later itself (the job worker) may ask for the gate's refusals
(``OutboundBusy``, ``CircuitOpen``) to be raised instead of answered as envelopes.

Only ``success`` and ``partial`` results are cached and fingerprinted: a bot challenge or an
error page is nothing to serve again, nor to compare the next version of the page with. Requests
with browser secrets (actions, cookies, headers) touch neither; screenshot requests skip the
cache, since their artifact belongs to one job.

With a ``Renderer`` (the browser worker only; docs/architecture.md, "Browser worker"), ``browser``
requests, screenshots, requests with browser options and ``auto`` items with a draft are rendered
instead of fetched, and an ``auto`` request renders when its fast result needs the browser
(``escalation.needs_browser``). A render runs step 4 for the request URL, takes the host's turn and
holds it until the render ends. Each later main-frame navigation is checked before the browser
sends it; the HTTP redirect hops and the document answers are checked when the render returns,
before any content is used. The rendered HTML then goes through step 7 (compliance after the fetch,
conversion) and step 8. An ``auto`` result that will escalate is neither cached nor fingerprinted;
of the fast result (or draft) and the render, only the better one is (``escalation.better``).
Rendered results store no validators, so they are never revalidated. Unblock mode (admins,
``compliance: ignore``, ``BROWSER_UNBLOCK_ENABLED``) uses the unblock renderer and its clearance
cookies.

A body over ``CONVERT_MAX_BYTES`` fails with ``too_large`` before it is decoded. CPU-bound steps
run on the ``ConversionPool`` (``CONVERT_CONCURRENCY`` threads); waiting for a slot counts
against the deadline. The deadline cannot stop a running thread, but every step bounds its
own cost.
"""

import asyncio
import contextlib
import hashlib
import html as html_lib
import math
import re
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

import structlog
from prometheus_client import CollectorRegistry, Counter, Histogram
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from text_extraction.auth import Principal
from text_extraction.boilerplate import load_boilerplate_rules, strip_boilerplate
from text_extraction.browser_options import ArtifactRef
from text_extraction.compliance import (
    ComplianceChecker,
    ComplianceMode,
    ComplianceReport,
    blocking_code,
    resolve_mode,
    with_page_findings,
    with_redirect_findings,
)
from text_extraction.conversion_pool import ConversionPool
from text_extraction.convert import Link, convert_html, convert_plain, decode_html, page_links
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.errors import ServiceError
from text_extraction.escalation import better, needs_browser, unblock_for
from text_extraction.extract_models import CacheState, ExtractRequest, ExtractResult, Timings
from text_extraction.fetch import Fetcher, FetchError, FetchResult, Validators
from text_extraction.fingerprint import Fingerprint, FingerprintStore, compute_fingerprint
from text_extraction.metadata import PageMetadata, detect_language, extract_metadata
from text_extraction.netguard import InvalidUrlError, validate_url
from text_extraction.outbound import (
    CircuitOpen,
    DomainGate,
    OutboundBusy,
    normalize_host,
    parse_retry_after,
)
from text_extraction.pipeline_render import (
    CHALLENGE_STATUSES,
    answers_for_gate,
    as_fetched,
    blocked_resources,
    caller_cookies,
    challenge_not_cleared,
    challenged_by_hosts,
    check_address,
    check_target,
    render_options,
)
from text_extraction.quality import Quality, assess, quality_error
from text_extraction.render import (
    ArtifactSink,
    ArtifactUnavailable,
    ClearanceStore,
    Rendered,
    Renderer,
    RenderOptions,
)
from text_extraction.result_cache import CachedEntry, MemoryPressure, ResultCache, cache_key
from text_extraction.settings import Settings
from text_extraction.tracing import current_trace_id

_log = structlog.get_logger(__name__)

# Media types the fast mode converts, and how.
_KINDS: dict[str, Literal["html", "text"]] = {
    "text/html": "html",
    "application/xhtml+xml": "html",
    "text/plain": "text",
}
# Fetch errors that say the host itself is in trouble; they count towards its circuit breaker.
_HOST_FAILURES = frozenset({ErrorCode.DNS_ERROR, ErrorCode.CONNECTION_ERROR, ErrorCode.TIMEOUT})
# Upstream answers worth retrying later.
_RETRYABLE_STATUSES = frozenset({408, 425, 429})
# The fetch gets the time left before the deadline minus this, so it times out first and the
# timeout is recorded for the host.
_FETCH_MARGIN_SECONDS = 0.25
# With less time than this left when its turn comes, the target is not fetched (or rendered).
_MIN_FETCH_SECONDS = 1.0
# The renderer's deadline is this much before the extraction's, so the render ends in time for
# the conversion and the result after it.
_RENDER_MARGIN_SECONDS = 2.0
# A fetch timeout counts against the host only if the fetch had this share of
# FETCH_TIMEOUT_SECONDS.
_FAIR_BUDGET_SHARE = 0.5
# Only the start of a page is searched for the <html lang> attribute.
_LANG_SCAN_CHARS = 65_536
_MAX_TAG_CHARS = 4096
_LANG_ATTRIBUTE = re.compile(r"""(?:^|\s)(?:xml:)?lang\s*=\s*["']?([^"'\s>]+)""", re.IGNORECASE)
_DURATION_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 3, 5, 7.5, 10, 15, 20, 25, 30)

_BLOCK_MESSAGES = {
    ErrorCode.BLOCKED_BY_ROBOTS: "The site's robots.txt does not allow fetching this URL.",
    ErrorCode.TDM_RESERVED: "The site reserves text and data mining rights for this URL.",
}
_QUALITY_MESSAGES = {
    ErrorCode.BOT_PROTECTION: "The page is a bot challenge, not the content.",
    ErrorCode.LIKELY_ERROR_PAGE: "The page looks like an error page.",
    ErrorCode.LOGIN_REQUIRED: "The page asks for a login.",
    ErrorCode.EMPTY_CONTENT: "No text could be extracted from the page.",
}


@dataclass(frozen=True)
class _Metrics:
    total: Counter
    duration: Histogram


# One set of metrics per registry, so several pipelines (tests, app factories) can share one.
_METRICS: WeakKeyDictionary[CollectorRegistry, _Metrics] = WeakKeyDictionary()


def _metrics_for(registry: CollectorRegistry) -> _Metrics:
    metrics = _METRICS.get(registry)
    if metrics is None:
        metrics = _Metrics(
            total=Counter(
                "text_extraction_extract_total",
                "Extractions by requested mode and outcome (success, partial or the error code).",
                ["mode", "outcome"],
                registry=registry,
            ),
            duration=Histogram(
                "text_extraction_extract_duration_seconds",
                "Duration of extractions by requested mode.",
                ["mode"],
                buckets=_DURATION_BUCKETS,
                registry=registry,
            ),
        )
        _METRICS[registry] = metrics
    return metrics


def _ms(since: float) -> int:
    return round((time.perf_counter() - since) * 1000)


@dataclass
class _Run:
    """What one request has learned so far; a failure at any step reports it."""

    req: ExtractRequest
    report: ComplianceReport
    cache: CacheState
    started: float
    raise_refusals: bool = False
    deadline: asyncio.Timeout | None = None
    host: str | None = None
    final_url: str | None = None
    upstream_status: int | None = None
    content_type: str | None = None
    attempts: int = 0
    compliance_ms: int = 0
    fetch_ms: int = 0
    convert_ms: int = 0
    render_ms: int = 0
    # Set once per request: may it read and write the result cache (no secrets, no screenshot),
    # and fingerprints (no secrets, B6)? Does unblock mode apply (B14)? Is the caller an admin?
    cacheable: bool = True
    private: bool = False
    unblock: bool = False
    admin: bool = False
    draft: Envelope[ExtractResult] | None = None  # the fast result of an ``auto`` item (B5)
    artifacts: ArtifactSink | None = None
    # A render whose result is compared with an earlier one: only the better one is stored.
    comparing: bool = False
    mode_used: Literal["fast", "browser"] = "fast"
    unblocked: bool = False
    warnings: list[str] = field(default_factory=list)  # of the renderer, also on failures
    validators: Validators = field(default_factory=Validators)  # of the fast fetch's answer

    def timings(self) -> Timings:
        return Timings(
            total_ms=_ms(self.started),
            compliance_ms=self.compliance_ms,
            fetch_ms=self.fetch_ms,
            convert_ms=self.convert_ms,
            render_ms=self.render_ms,
        )

    def result(self, **fields: Any) -> ExtractResult:
        values: dict[str, Any] = {
            "url": self.req.url,
            "final_url": self.final_url,
            "upstream_status": self.upstream_status,
            "content_type": self.content_type,
            "mode_used": self.mode_used,
            "unblocked": self.unblocked,
            "attempts": self.attempts,
            "language": None,
            "word_count": 0,
            "title": None,
            "format": self.req.format,
            "content": None,
            "quality": None,
            "timings": self.timings(),
            "compliance": self.report,
            "metadata": None,
            "links": None,
            "fingerprint": None,
            "cache": self.cache,
            "warnings": list(self.warnings),
        }
        values.update(fields)
        return ExtractResult(**values)

    def failed(
        self, code: ErrorCode, message: str, *, retryable: bool = False
    ) -> Envelope[ExtractResult]:
        return Envelope(
            status="failed",
            data=self.result(),
            error=ErrorInfo(code=code, message=message, retryable=retryable),
        )


class _Turn:
    """The outbound gate's turn for the host being fetched; it moves along a redirect chain.

    ``answering`` is the host whose answer the fetch waits for: None while no turn is held (a
    redirect target on another host is being checked, or its turn is being waited for)."""

    def __init__(self, gate: DomainGate) -> None:
        self._gate = gate
        self._held: contextlib.AsyncExitStack | None = None
        self._host: str | None = None  # host of the turn held
        self.answering: str | None = None

    def holds(self, host: str) -> bool:
        """Whether the turn held (if any) is this host's: a redirect that stays on the host
        keeps it, and waits for no further spacing slot or crawl delay."""
        return self._held is not None and normalize_host(host) == self._host

    async def take(self, host: str, crawl_delay: float | None) -> None:
        """Wait for a turn at ``host``; a turn already held for it is kept. A turn at another
        host is given back first, so we never hold two at once."""
        if self.holds(host):
            return
        await self.give_back()
        held = contextlib.AsyncExitStack()
        await held.enter_async_context(self._gate.slot(host, crawl_delay))
        self._held = held
        self._host = normalize_host(host)
        self.answering = self._host

    async def give_back(self) -> None:
        held, self._held, self._host = self._held, None, None
        self.answering = None
        if held is not None:
            await held.aclose()


@dataclass(frozen=True)
class _Stored:
    key: str
    entry: CachedEntry
    result: ExtractResult
    fresh: bool


@dataclass(frozen=True)
class _Processed:
    content: str
    title: str | None
    warnings: list[str]
    quality: Quality
    language: str | None
    metadata: PageMetadata | None
    links: list[Link] | None
    fingerprint: Fingerprint | None  # not yet compared with the previous one


class Pipeline:
    """Runs one extraction from URL to result envelope. Create once per app."""

    def __init__(
        self,
        fetcher: Fetcher,
        gate: DomainGate,
        checker: ComplianceChecker,
        cache: ResultCache,
        fingerprints: FingerprintStore,
        settings: Settings,
        registry: CollectorRegistry,
        pool: ConversionPool | None = None,
        *,
        renderer: Renderer | None = None,
        unblock_renderer: Renderer | None = None,
        clearance: ClearanceStore | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        """``pool`` runs the CPU-bound steps; without one the pipeline makes its own.

        Only the browser worker passes a ``renderer`` (and, with unblock mode enabled, the
        ``unblock_renderer`` and the ``clearance`` store). ``timeout_seconds`` replaces
        ``EXTRACT_TIMEOUT_SECONDS`` as the deadline; an unblock render has
        ``BROWSER_UNBLOCK_TIMEOUT_SECONDS``."""
        self._fetcher = fetcher
        self._gate = gate
        self._checker = checker
        self._cache = cache
        self._fingerprints = fingerprints
        self._settings = settings
        self._pool = pool if pool is not None else ConversionPool(settings.convert_concurrency)
        self._rules = load_boilerplate_rules()
        self._metrics = _metrics_for(registry)
        self._renderer = renderer
        self._unblock_renderer = unblock_renderer
        self._clearance = clearance
        self._timeout_seconds = timeout_seconds
        self._blocked_resources = blocked_resources(settings)

    async def run(
        self,
        req: ExtractRequest,
        principal: Principal,
        *,
        gate_refusals: Literal["fail", "raise"] = "fail",
        artifacts: ArtifactSink | None = None,
        draft: Envelope[ExtractResult] | None = None,
    ) -> Envelope[ExtractResult]:
        """Extract ``req.url``. Raises ``ServiceError`` only for a refused compliance mode (403)
        and an unavailable Valkey (503).

        A refusal of the outbound gate is a ``rate_limited`` (busy) or ``connection_error``
        (paused) envelope, retryable. With ``gate_refusals="raise"`` the ``OutboundBusy`` or
        ``CircuitOpen`` propagates unchanged instead; the metrics count it under the same code.

        With a renderer: ``artifacts`` stores the screenshot of a ``screenshot`` request (it is
        required then), and ``draft`` is the fast result of an ``auto`` item, which the render is
        compared with instead of fetching the page again (B5).
        """
        mode = resolve_mode(req.compliance, principal, self._settings)
        private = not req.secrets().empty()
        cacheable = req.cache and not private and not req.screenshot
        run = _Run(
            req=req,
            report=ComplianceReport(mode=mode, decision="skipped"),  # until it is checked
            cache="miss" if cacheable else "bypass",
            started=time.perf_counter(),
            raise_refusals=gate_refusals == "raise",
            cacheable=cacheable,
            private=private,
            unblock=unblock_for(principal, mode, self._settings),
            admin=principal.role == "admin",
            draft=draft if req.mode == "auto" else None,
            artifacts=artifacts,
        )
        outcome = ErrorCode.INTERNAL_ERROR.value
        try:
            envelope = await self._within_deadline(run)
            if envelope.data is not None:
                envelope.data.timings = run.timings()
            envelope.trace_id = current_trace_id()
            outcome = _outcome(envelope)
            return envelope
        except OutboundBusy:
            outcome = ErrorCode.RATE_LIMITED.value
            raise
        except CircuitOpen:
            outcome = ErrorCode.CONNECTION_ERROR.value
            raise
        finally:
            self._observe(run, outcome)

    # --- steps ---------------------------------------------------------------------------------

    async def _within_deadline(self, run: _Run) -> Envelope[ExtractResult]:
        seconds = self._timeout_seconds or self._settings.extract_timeout_seconds
        chosen = self._renderer_for(run)
        if chosen is not None and chosen[1]:
            seconds = self._settings.browser_unblock_timeout_seconds
        deadline = asyncio.timeout(seconds)
        try:
            async with deadline:
                run.deadline = deadline
                return await self._extract(run)
        except TimeoutError:
            if not deadline.expired():
                raise
            return run.failed(
                ErrorCode.TIMEOUT, "The extraction did not finish in time.", retryable=True
            )

    async def _extract(self, run: _Run) -> Envelope[ExtractResult]:
        req = run.req
        try:
            validate_url(req.url)
        except InvalidUrlError as error:
            return run.failed(ErrorCode.INVALID_URL, str(error))
        run.host = urlsplit(req.url).hostname

        stored = await self._cached(req, run.report.mode) if run.cacheable else None
        if stored is not None and stored.fresh:
            return self._serve_hit(run, stored)
        if stored is not None and stored.result.mode_used == "browser":
            stored = None  # a rendered result has no validators and is never revalidated (B10)
        if self._renders_first(run):
            return await self._rendered(run, earlier=run.draft)
        fast = await self._guarded(run, self._fetch_and_extract(run, stored))
        if self._renderer_for(run) is not None and self._escalates(run, fast):
            return await self._rendered(run, earlier=fast)
        return fast

    async def _guarded(
        self, run: _Run, step: Awaitable[Envelope[ExtractResult]]
    ) -> Envelope[ExtractResult]:
        """``step``'s envelope; a failed fetch or render and a refusal of the gate become
        failed envelopes, and an unavailable gate backend a 503."""
        try:
            return await step
        except FetchError as error:
            run.upstream_status = error.upstream_status
            return run.failed(error.code, error.message, retryable=error.retryable)
        except OutboundBusy as busy:
            if run.raise_refusals:
                raise
            return run.failed(
                ErrorCode.RATE_LIMITED,
                "Too many requests to this site are waiting; "
                f"retry after {_seconds(busy.retry_after)} s.",
                retryable=True,
            )
        except CircuitOpen as paused:
            if run.raise_refusals:
                raise
            return run.failed(
                ErrorCode.CONNECTION_ERROR,
                "Requests to this site are paused after errors or a rate limit; "
                f"retry after {_seconds(paused.retry_after)} s.",
                retryable=True,
            )
        except RedisError as error:
            _log.warning("outbound_gate_unavailable", error_type=type(error).__name__)
            raise ServiceError(
                ErrorCode.INTERNAL_ERROR,
                "The outbound state backend is unavailable. Try again later.",
                503,
                retryable=True,
            ) from None

    async def _cached(self, req: ExtractRequest, mode: ComplianceMode) -> _Stored | None:
        key = cache_key(req.url, _cache_options(req, mode))
        found = await self._cache.get(key)
        if found is None:
            return None
        entry, fresh = found
        try:
            result = ExtractResult.model_validate(entry.result)
        except ValidationError:
            return None  # another shape: extract again and overwrite it
        return _Stored(key, entry, result, fresh)

    def _serve_hit(self, run: _Run, stored: _Stored) -> Envelope[ExtractResult]:
        run.cache = "hit"
        run.report = stored.result.compliance
        result = stored.result.model_copy(
            update={"url": run.req.url, "attempts": 0, "cache": "hit"}
        )
        return self._envelope(result)

    async def _fetch_and_extract(
        self, run: _Run, stored: _Stored | None
    ) -> Envelope[ExtractResult]:
        req = run.req
        turn = _Turn(self._gate)
        try:
            run.report = await self._check_origin(run, req.url, turn)
            if (code := blocking_code(run.report)) is not None:
                return run.failed(code, _BLOCK_MESSAGES[code])
            fetched = await self._fetch(run, stored.entry.validators if stored else None, turn)
        finally:
            await turn.give_back()  # `_fetch` gives it back as soon as the fetch ends
        if fetched.not_modified:
            if stored is None:
                return run.failed(
                    ErrorCode.TARGET_HTTP_ERROR,
                    "The target answered 304 Not Modified to an unconditional request.",
                )
            # No body: the page is the stored one, so its own statements still hold.
            run.report = self._checker.after_fetch(
                with_page_findings(run.report, stored.result.compliance), fetched, None
            )
            if (code := blocking_code(run.report)) is not None:
                return run.failed(code, _BLOCK_MESSAGES[code])
            return await self._revalidated(run, stored, fetched)
        if not 200 <= fetched.status < 300:  # also a 3xx the fetcher could not follow
            failed = run.failed(
                ErrorCode.TARGET_HTTP_ERROR,
                f"The target answered with HTTP status {fetched.status}.",
                retryable=fetched.status in _RETRYABLE_STATUSES or fetched.status >= 500,
            )
            if failed.data is not None and self._assesses_challenge(run, fetched):
                failed.data.quality = await self._challenge_quality(run, fetched)
            return failed
        kind = _KINDS.get(fetched.content_type or "")
        if kind is None:
            return run.failed(
                ErrorCode.UNSUPPORTED_CONTENT_TYPE,
                f"Content type {fetched.content_type or 'unknown'} is not supported in fast mode.",
            )

        limit = self._settings.convert_max_bytes
        if len(fetched.content) > limit:
            return run.failed(
                ErrorCode.TOO_LARGE,
                f"The page has {len(fetched.content)} bytes, more than the {limit} bytes "
                "that are converted.",
            )

        started = time.perf_counter()
        text = await self._pool.run(decode_html, fetched.content, fetched.charset)
        run.convert_ms += _ms(started)
        started = time.perf_counter()
        page = text if kind == "html" else None
        run.report = await self._pool.run(self._checker.after_fetch, run.report, fetched, page)
        run.compliance_ms += _ms(started)
        if (code := blocking_code(run.report)) is not None:
            return run.failed(code, _BLOCK_MESSAGES[code])
        if (
            stored is not None
            and 200 <= fetched.status < 300
            and fetched.raw_sha256 == stored.entry.raw_sha256
        ):
            return await self._revalidated(run, stored, fetched)
        return await self._extract_content(run, fetched, kind, text)

    def _assesses_challenge(self, run: _Run, fetched: FetchResult) -> bool:
        """Whether a failed answer's body is judged as a possible challenge: only in unblock
        mode, where ``auto`` escalates a challenge answer (B5, B14)."""
        return (
            fetched.status in CHALLENGE_STATUSES
            and run.unblock
            and run.req.mode == "auto"
            and self._settings.browser_enabled
            and _KINDS.get(fetched.content_type or "") == "html"
            and len(fetched.content) <= self._settings.convert_max_bytes
        )

    async def _challenge_quality(self, run: _Run, fetched: FetchResult) -> Quality | None:
        """The quality facts of a failed answer's body, with ``challenge_status`` set, when the
        body is a bot challenge; None otherwise."""
        started = time.perf_counter()
        try:
            text = await self._pool.run(decode_html, fetched.content, fetched.charset)
            quality = await self._pool.run(assess, text, "")
        finally:
            run.convert_ms += _ms(started)
        if not quality.bot_challenge:
            return None
        return quality.model_copy(update={"challenge_status": True})

    async def _check_origin(self, run: _Run, url: str, turn: _Turn) -> ComplianceReport:
        """``before_fetch`` for ``url``. Answered from the cache when it can be; otherwise in the
        turn of ``url``'s host, taken here (or kept, when ``turn`` holds it already), so the
        origin's files are fetched under the host's limits. The wait for the turn counts as
        fetch time, the checks as compliance time."""
        mode = run.report.mode
        started = time.perf_counter()
        report = await self._checker.cached_before_fetch(url, mode)
        if report is None:
            host = urlsplit(url).hostname or ""
            waited = time.perf_counter()
            # The origin's Crawl-delay is not known yet; the request after this turn keeps it.
            await turn.take(host, None)
            run.fetch_ms += _ms(waited)
            started += time.perf_counter() - waited
            report = await self._checker.before_fetch(url, mode)
        run.compliance_ms += _ms(started)
        return report

    async def _fetch(self, run: _Run, validators: Validators | None, turn: _Turn) -> FetchResult:
        """Fetch the target in its host's turn and report the outcome to the gate.

        A redirect target is checked against the compliance files of its origin before it is
        requested (a block ends the chain). A target on another host waits for that host's turn
        and the previous host's turn is given back first; one on the same host keeps the turn.
        Missing files of the target's origin are fetched in the target's turn.
        The outcome is recorded for the host that gave it, judged against the time that host's
        request actually had.
        """
        checked = {run.req.url}
        started = time.perf_counter()
        hop_checks = 0.0  # seconds of compliance checks inside the fetch
        budget = 0.0  # seconds the request at `turn.answering` has, for `_record_failure`
        chain_budget = 0.0  # the fetcher's deadline for the whole redirect chain ...
        chain_started = started  # ... counted from here

        async def on_redirect(url: str) -> None:
            nonlocal hop_checks, budget
            host = urlsplit(url).hostname or ""
            if not turn.holds(host):
                turn.answering = None  # until the target's turn is taken, no answer is its host's
            if url not in checked:
                checked.add(url)
                check_started = time.perf_counter()
                compliance_before = run.compliance_ms
                fetch_before = run.fetch_ms
                hop = await self._check_origin(run, url, turn)
                # Counted below from hop_checks instead: the fetch time is what remains, and a
                # wait for the target's turn is part of it.
                waited = (run.fetch_ms - fetch_before) / 1000
                hop_checks += time.perf_counter() - check_started - waited
                run.compliance_ms, run.fetch_ms = compliance_before, fetch_before
                run.report = with_redirect_findings(run.report, hop)
                if (code := blocking_code(run.report)) is not None:
                    raise FetchError(code, _BLOCK_MESSAGES[code])
            await turn.take(host, run.report.crawl_delay)
            # The hop starts now. It gets what is left of the fetcher's deadline for the whole
            # chain (hook time included), capped as any fetch is by the request's own deadline.
            budget = min(
                self._fetch_budget(run), chain_budget - (time.perf_counter() - chain_started)
            )

        try:
            await turn.take(run.host or "", run.report.crawl_delay)
            budget = self._fetch_budget(run)
            if budget < _MIN_FETCH_SECONDS:
                # Not the host's fault: the turn came too late. Nothing is fetched or recorded.
                raise FetchError(
                    ErrorCode.TIMEOUT,
                    "Too little time was left to fetch the target.",
                    retryable=True,
                )
            run.attempts += 1
            chain_budget, chain_started = budget, time.perf_counter()
            try:
                fetched = await self._fetcher.fetch(
                    run.req.url,
                    validators=validators,
                    timeout_seconds=budget,  # the whole chain's budget; a hop gets what is left
                    on_redirect=on_redirect,
                )
            except FetchError as error:
                gate_timeout = error.code == ErrorCode.TIMEOUT and error.cleared_after is None
                if turn.answering is not None and not gate_timeout:  # nothing sent: not the host's
                    if error.cleared_after is not None:
                        # The gate's wait before the send is not time the host had.
                        budget = min(budget, chain_budget - error.cleared_after)
                    await self._record_failure(turn.answering, error, budget)
                raise
            # Recorded while the turn is held, so a 429 pauses the host before the next turn.
            if turn.answering is not None:
                await self._gate.record(
                    turn.answering,
                    status=fetched.status,
                    retry_after=parse_retry_after(
                        fetched.headers.get("retry-after"), now=time.time()
                    ),
                )
        finally:
            await turn.give_back()
            run.compliance_ms += round(hop_checks * 1000)
            run.fetch_ms += max(_ms(started) - round(hop_checks * 1000), 0)
        run.final_url = fetched.final_url
        run.upstream_status = fetched.status
        run.content_type = fetched.content_type
        return fetched

    def _fetch_budget(self, run: _Run) -> float:
        """Seconds the fetch may take: ``FETCH_TIMEOUT_SECONDS``, or less when the deadline is
        nearer (minus a margin, so the fetch times out first and the outcome is recorded)."""
        budget = self._settings.fetch_timeout_seconds
        when = run.deadline.when() if run.deadline is not None else None
        if when is not None:
            left = when - asyncio.get_running_loop().time() - _FETCH_MARGIN_SECONDS
            budget = min(budget, left)
        return budget

    async def _record_failure(self, host: str, error: FetchError, budget: float) -> None:
        """Report a failed fetch to the gate.

        An error with a status (too large, too many redirects) still got an answer: that status
        counts. DNS and connection errors count as failures of the host. A timeout counts only
        when the request to this host had at least half of ``FETCH_TIMEOUT_SECONDS`` (``budget``,
        the time left once the gate let the request go): one cut short by the extraction's own
        deadline or by a long wait at the gate says little about the host, and a redirect target
        only gets what the chain left over. Errors
        that say nothing about the host's health (a redirect to a blocked or invalid URL, an
        unsupported encoding) are not reported.
        """
        fair_budget = budget >= self._settings.fetch_timeout_seconds * _FAIR_BUDGET_SHARE
        if error.upstream_status is not None:
            await self._gate.record(host, status=error.upstream_status, retry_after=None)
        elif error.code in _HOST_FAILURES and (error.code != ErrorCode.TIMEOUT or fair_budget):
            await self._gate.record(host, status=None, retry_after=None)

    async def _revalidated(
        self, run: _Run, stored: _Stored, fetched: FetchResult
    ) -> Envelope[ExtractResult]:
        """The stored result again, after the site said it has not changed."""
        run.cache = "revalidated"
        fingerprint = stored.result.fingerprint
        if fingerprint is not None:
            fingerprint = fingerprint.model_copy(
                update={"changed": False, "similarity_to_previous": 1.0}
            )
        result = stored.result.model_copy(
            update={
                "url": run.req.url,
                "attempts": run.attempts,
                "compliance": run.report,
                "fingerprint": fingerprint,
                "cache": "revalidated",
            }
        )
        # Not compared again, so renew the baseline the next real extraction compares with.
        await self._fingerprints.touch(run.req.url, variant=run.req.format)
        fresh = Validators.from_headers(fetched.headers)
        validators = Validators(
            etag=fresh.etag or stored.entry.validators.etag,
            last_modified=fresh.last_modified or stored.entry.validators.last_modified,
        )
        await self._cache.put(
            stored.key,
            CachedEntry(
                stored_at=time.time(),
                validators=validators,
                raw_sha256=stored.entry.raw_sha256,
                result=result.model_dump(mode="json"),
            ),
        )
        return self._envelope(result)

    async def _extract_content(
        self,
        run: _Run,
        fetched: FetchResult,
        kind: Literal["html", "text"],
        text: str,
    ) -> Envelope[ExtractResult]:
        processed = await self._converted(run, fetched, kind, text)
        if isinstance(processed, Envelope):
            return processed
        run.validators = Validators.from_headers(fetched.headers)
        envelope = self._envelope(
            run.result(
                language=processed.language,
                word_count=processed.quality.word_count,
                title=processed.title,
                content=processed.content,
                quality=processed.quality,
                metadata=processed.metadata,
                links=processed.links,
                fingerprint=processed.fingerprint,
                warnings=processed.warnings,
            )
        )
        if self._escalates(run, envelope):
            # B5: the browser's result, or the better of both, is stored instead of this one.
            return envelope
        return await self._finish(run, envelope, run.validators)

    async def _converted(
        self, run: _Run, fetched: FetchResult, kind: Literal["html", "text"], text: str
    ) -> _Processed | Envelope[ExtractResult]:
        """``_process`` on the conversion pool, or the failed envelope of a page that broke it."""
        started = time.perf_counter()
        try:
            return await self._pool.run(self._process, run.req, fetched, kind, text)
        except Exception as error:  # a page that breaks a converter fails alone
            # Traceback without locals (see logging.py); the host only, never the URL.
            _log.error(
                "conversion_failed", host=run.host, error_type=type(error).__name__, exc_info=True
            )
            return run.failed(ErrorCode.CONVERSION_FAILED, "The page could not be converted.")
        finally:
            run.convert_ms += _ms(started)

    def _escalates(self, run: _Run, envelope: Envelope[ExtractResult]) -> bool:
        """Whether this fast result of an ``auto`` request goes on to the browser (B5)."""
        return (
            run.req.mode == "auto"
            and self._settings.browser_enabled
            and needs_browser(envelope, unblock=run.unblock)
        )

    async def _finish(
        self, run: _Run, envelope: Envelope[ExtractResult], validators: Validators
    ) -> Envelope[ExtractResult]:
        """Compare the result's fingerprint with the previous one and cache the result: the last
        step of an answer that is kept. A failed result is not cached, and a request with
        secrets touches neither the cache nor the fingerprints (B6). The cache entry's
        ``raw_sha256`` is the fingerprint's: the fetched bytes, or the rendered HTML."""
        result = envelope.data
        if result is None:
            return envelope
        if result.fingerprint is not None and not run.private:
            fingerprint = await self._fingerprints.compare_and_save(
                run.req.url, result.fingerprint, variant=run.req.format
            )
            result = result.model_copy(update={"fingerprint": fingerprint})
            envelope = envelope.model_copy(update={"data": result})
        if run.cacheable and envelope.status != "failed" and result.fingerprint is not None:
            await self._cache.put(
                cache_key(run.req.url, _cache_options(run.req, run.report.mode)),
                CachedEntry(
                    stored_at=time.time(),
                    validators=validators,
                    raw_sha256=result.fingerprint.raw_sha256,
                    result=result.model_dump(mode="json"),
                ),
            )
        return envelope

    # --- the render path (browser worker only) -------------------------------------------------

    def _renderer_for(self, run: _Run) -> tuple[Renderer, bool] | None:
        """The renderer for this request and whether it is the unblock one; None when it is not
        rendered at all (``mode: fast``, or a pipeline without a renderer)."""
        if run.req.mode == "fast":
            return None
        if run.unblock and self._unblock_renderer is not None:
            return self._unblock_renderer, True
        if self._renderer is not None:
            return self._renderer, False
        return None

    def _renders_first(self, run: _Run) -> bool:
        """``browser``, a screenshot, browser options (actions, cookies, headers: the fast fetch
        could not use them) and an ``auto`` item with a draft render without a fast fetch;
        ``auto`` otherwise renders only when its fast result needs it."""
        req = run.req
        wants = req.mode == "browser" or req.screenshot or run.private or run.draft is not None
        return wants and self._renderer_for(run) is not None

    async def _rendered(
        self, run: _Run, *, earlier: Envelope[ExtractResult] | None
    ) -> Envelope[ExtractResult]:
        """Render the page; with an ``earlier`` result (the draft or this run's fast result),
        keep the better of both and store only that one. A screenshot is never compared: its
        rendered result carries the artifact."""
        if run.req.screenshot:
            earlier = None
        run.comparing = earlier is not None
        rendered = await self._guarded(run, self._render_and_extract(run))
        if earlier is None:
            return rendered
        winner = better(earlier, rendered)
        if winner.data is not None:
            # Every fetch of the target for this item counts, the draft's too.
            drafted = run.draft.data.attempts if run.draft and run.draft.data else 0
            data = winner.data.model_copy(update={"attempts": run.attempts + drafted})
            winner = winner.model_copy(update={"data": data})
        # This run's fast result keeps its validators; a draft (fetched elsewhere) or a render
        # has none, and a run that took no fast step has none either.
        fast = winner.data is not None and winner.data.mode_used == "fast"
        return await self._finish(run, winner, run.validators if fast else Validators())

    async def _render_and_extract(self, run: _Run) -> Envelope[ExtractResult]:
        """Render the page in its host's turn and extract it (B2):

        1. the request URL's origin is checked, then the host's turn is taken and held;
        2. the renderer loads the page, and asks before each later main-frame navigation;
        3. every document answer and redirect hop is checked when the render returns, before
           any content is used, and its status is reported to the gate; then the turn is given
           back, on every path.
        """
        chosen = self._renderer_for(run)
        if chosen is None:  # pragma: no cover - only called when there is one
            raise RuntimeError("no renderer")
        renderer, unblock = chosen
        req = run.req
        if req.screenshot and run.artifacts is None:
            raise ValueError("a screenshot request needs an artifact sink")
        # What a fast step before learned is in its own result; the render starts afresh.
        run.mode_used, run.unblocked = "browser", unblock
        run.report = ComplianceReport(mode=run.report.mode, decision="skipped")
        run.final_url = run.upstream_status = run.content_type = None
        turn = _Turn(self._gate)
        checked = {req.url}
        try:
            run.report = await self._check_origin(run, req.url, turn)
            if (code := blocking_code(run.report)) is not None:
                return run.failed(code, _BLOCK_MESSAGES[code])
            options = await self._render_options(run, unblock)
            rendered = await self._render(run, renderer, options, turn, checked)
            refused = await self._check_answers(run, rendered, turn, checked)
            if refused is not None:
                return refused
        finally:
            await turn.give_back()
        return await self._extract_rendered(run, rendered, unblock)

    async def _render_options(self, run: _Run, unblock: bool) -> RenderOptions:
        """The request's browser options; an unblock render first adds the clearance cookies
        stored for the host (B14), so the caller's own cookies of the same name win."""
        host = run.host or ""
        cookies = caller_cookies(run.req, host)
        if unblock and self._clearance is not None:
            cookies = [*await self._clearance.get(host), *cookies]
        return render_options(
            run.req, cookies, admin=run.admin, unblock=unblock, blocked=self._blocked_resources
        )

    async def _render(
        self,
        run: _Run,
        renderer: Renderer,
        options: RenderOptions,
        turn: _Turn,
        checked: set[str],
    ) -> Rendered:
        """Take the host's turn and render. Each later main-frame navigation is checked before
        it is sent: its URL, its origin's compliance files (a block refuses it) and the turn of
        its host, which moves along with that host's own crawl delay. The checks count as render
        time. A failed render is reported to the gate for the host whose turn is held, as a
        failed fetch is: a status the renderer saw, or a timeout or connection error."""
        host = normalize_host(run.host or "")
        crawl_delays = {host: run.report.crawl_delay}  # by host, as the turns are
        await turn.take(host, crawl_delays[host])
        loop = asyncio.get_running_loop()
        when = run.deadline.when() if run.deadline is not None else None
        if when is None:  # pragma: no cover - the run always has a deadline
            when = loop.time() + self._settings.browser_timeout_seconds
        deadline = when - _RENDER_MARGIN_SECONDS
        if deadline - loop.time() < _MIN_FETCH_SECONDS:
            raise FetchError(
                ErrorCode.TIMEOUT, "Too little time was left to render the target.", retryable=True
            )

        async def check(url: str) -> None:
            check_target(url)
            target = urlsplit(url).hostname or ""
            check_address(target, self._settings)
            if url not in checked:
                checked.add(url)
                compliance_ms, fetch_ms = run.compliance_ms, run.fetch_ms
                hop = await self._check_origin(run, url, turn)
                run.compliance_ms, run.fetch_ms = compliance_ms, fetch_ms
                run.report = with_redirect_findings(run.report, hop)
                if (code := blocking_code(run.report)) is not None:
                    raise FetchError(code, _BLOCK_MESSAGES[code])
                crawl_delays[normalize_host(target)] = hop.crawl_delay
            await turn.take(target, crawl_delays.get(normalize_host(target)))

        started = time.perf_counter()
        budget = deadline - loop.time()  # what the render had, for `_record_failure`
        try:
            return await renderer.render(run.req.url, check, options, deadline=deadline)
        except FetchError as error:
            # Recorded while the turn is held, so a 429 pauses the host before the next turn.
            if turn.answering is not None:
                await self._record_failure(turn.answering, error, budget)
            raise
        finally:
            run.render_ms += _ms(started)

    async def _check_answers(
        self, run: _Run, rendered: Rendered, turn: _Turn, checked: set[str]
    ) -> Envelope[ExtractResult] | None:
        """Report each document answer to the gate (but a cleared challenge's, B14), then check
        every URL the render arrived at that was not checked before it was sent (HTTP redirect
        hops, which the browser follows unasked): with the cached verdicts, or with the
        origin's files fetched in its host's turn. A block discards the render: its failed
        envelope is returned."""
        run.attempts += len(rendered.responses)
        for answer in answers_for_gate(rendered, unblock=run.unblocked):
            await self._gate.record(
                urlsplit(answer.url).hostname or "",
                status=answer.status,
                retry_after=parse_retry_after(answer.headers.get("retry-after"), now=time.time()),
            )
        for answer in rendered.responses:
            for url in (*answer.redirects, answer.url):
                if url in checked:
                    continue
                checked.add(url)
                check_target(url)
                check_address(urlsplit(url).hostname or "", self._settings)
                run.report = with_redirect_findings(
                    run.report, await self._check_origin(run, url, turn)
                )
                if (code := blocking_code(run.report)) is not None:
                    return run.failed(code, _BLOCK_MESSAGES[code])
        return None

    async def _extract_rendered(
        self, run: _Run, rendered: Rendered, unblock: bool
    ) -> Envelope[ExtractResult]:
        """Judge the last document answer as fast mode would, then convert the rendered HTML."""
        req = run.req
        run.warnings = list(rendered.warnings)
        if not rendered.responses:
            return run.failed(
                ErrorCode.INTERNAL_ERROR, "The browser reported no document.", retryable=True
            )
        last = rendered.responses[-1]
        run.final_url = rendered.final_url
        run.upstream_status = last.status
        run.content_type = last.content_type
        not_cleared = challenge_not_cleared(rendered, unblock=unblock)
        if not_cleared and not 200 <= last.status < 300:
            return run.failed(ErrorCode.BOT_PROTECTION, _QUALITY_MESSAGES[ErrorCode.BOT_PROTECTION])
        if not 200 <= last.status < 300:
            return run.failed(
                ErrorCode.TARGET_HTTP_ERROR,
                f"The target answered with HTTP status {last.status}.",
                retryable=last.status in _RETRYABLE_STATUSES or last.status >= 500,
            )
        if _KINDS.get(last.content_type or "") != "html":  # a download, text, an image ...
            return run.failed(
                ErrorCode.UNSUPPORTED_CONTENT_TYPE,
                f"Content type {last.content_type or 'unknown'} is not supported in browser mode.",
            )
        content = rendered.html.encode("utf-8", "surrogatepass")
        limit = self._settings.convert_max_bytes
        if len(content) > limit:
            return run.failed(
                ErrorCode.TOO_LARGE,
                f"The rendered page has {len(content)} bytes, more than the {limit} bytes "
                "that are converted.",
            )
        fetched = as_fetched(req.url, rendered, content, run.render_ms)
        started = time.perf_counter()
        run.report = await self._pool.run(
            self._checker.after_fetch, run.report, fetched, rendered.html
        )
        run.compliance_ms += _ms(started)
        if (code := blocking_code(run.report)) is not None:
            return run.failed(code, _BLOCK_MESSAGES[code])

        processed = await self._converted(run, fetched, "html", rendered.html)
        if isinstance(processed, Envelope):
            return processed
        quality, fingerprint = processed.quality, processed.fingerprint
        if not_cleared or challenged_by_hosts(quality, rendered):
            quality = quality.model_copy(
                update={"bot_challenge": True, "browser_recommended": True}
            )
            fingerprint = None
        screenshot = await self._store_screenshot(run, rendered) if req.screenshot else None
        if isinstance(screenshot, Envelope):
            return screenshot
        result = run.result(
            language=processed.language,
            word_count=quality.word_count,
            title=processed.title,
            content=processed.content,
            quality=quality,
            metadata=processed.metadata,
            links=processed.links,
            fingerprint=fingerprint,
            warnings=[*processed.warnings, *rendered.warnings],
            screenshot=screenshot,
        )
        # B7: a stored screenshot is the answer; the page's quality is reported, not judged.
        envelope = Envelope(status="success", data=result) if screenshot else self._envelope(result)
        if (
            unblock
            and self._clearance is not None
            and rendered.cookies
            and not quality.bot_challenge
        ):
            await self._clearance.put(run.host or "", list(rendered.cookies))
        if run.comparing:
            return envelope
        return await self._finish(run, envelope, Validators())  # B10: never revalidated

    async def _store_screenshot(
        self, run: _Run, rendered: Rendered
    ) -> ArtifactRef | Envelope[ExtractResult]:
        """Store the render's screenshot (B7): its reference, or the failed envelope."""
        png, size = rendered.screenshot, rendered.screenshot_size
        if png is None or size is None or run.artifacts is None:
            return run.failed(
                ErrorCode.INTERNAL_ERROR, "The browser took no screenshot.", retryable=True
            )
        limit = self._settings.screenshot_max_bytes
        if len(png) > limit:
            return run.failed(
                ErrorCode.TOO_LARGE,
                f"The screenshot has {len(png)} bytes, more than the {limit} bytes that are kept.",
            )
        try:
            return await run.artifacts.put_screenshot(png, *size)
        except ArtifactUnavailable:
            return run.failed(
                ErrorCode.INTERNAL_ERROR,
                "The screenshot could not be stored now. Try again later.",
                retryable=True,
            )

    def _process(
        self, req: ExtractRequest, fetched: FetchResult, kind: Literal["html", "text"], text: str
    ) -> _Processed:
        """Convert and judge the decoded page. CPU-bound: runs on the conversion pool."""
        final_url = fetched.final_url
        metadata: PageMetadata | None = None
        links: list[Link] | None = None
        html_lang: str | None = None
        if kind == "html":
            conversion = convert_html(text, final_url, req.format)
            host = urlsplit(final_url).hostname or ""
            content = strip_boilerplate(host, conversion.content, self._rules)
            quality = assess(text, content)
            html_lang = _html_lang(text)
            if req.metadata:
                metadata = extract_metadata(text, final_url, fetched.headers)
            if req.links:
                links = page_links(text, final_url)
        else:
            conversion = convert_plain(text)
            content = conversion.content
            if req.format == "html":
                content = f"<pre>{html_lib.escape(content)}</pre>" if content else ""
            quality = assess(None, content)
            if req.links:
                links = []
        language = detect_language(html_lang, fetched.headers.get("content-language"), content)
        verdict = quality_error(quality, self._settings.quality_min_words)
        failed = verdict is not None and verdict[0] == "failed"
        return _Processed(
            content=content,
            title=conversion.title,
            warnings=list(conversion.warnings),
            quality=quality,
            language=language,
            metadata=metadata,
            links=links,
            fingerprint=None if failed else compute_fingerprint(fetched.raw_sha256, content),
        )

    def _envelope(self, result: ExtractResult) -> Envelope[ExtractResult]:
        """The envelope for a finished result: the quality verdict decides the status."""
        min_words = self._settings.quality_min_words
        verdict = quality_error(result.quality, min_words) if result.quality else None
        if verdict is None:
            return Envelope(status="success", data=result)
        status, code = verdict
        message = _QUALITY_MESSAGES.get(
            code,
            f"The extracted text has {result.word_count} words, fewer than {min_words}.",
        )
        return Envelope(status=status, data=result, error=ErrorInfo(code=code, message=message))

    def _observe(self, run: _Run, outcome: str) -> None:
        """Metrics and the one log line of the request. No URL: host and path hash only."""
        seconds = time.perf_counter() - run.started
        mode = run.req.mode
        self._metrics.total.labels(mode=mode, outcome=outcome).inc()
        self._metrics.duration.labels(mode=mode).observe(seconds)
        _log.info(
            "extract_done",
            host=run.host,
            path_hash=_path_hash(run.req.url) if run.host else None,
            mode=mode,
            outcome=outcome,
            cache=run.cache,
            upstream_status=run.upstream_status,
            total_ms=round(seconds * 1000),
        )


def create_outbound(settings: Settings, valkey: Redis) -> tuple[DomainGate, Fetcher]:
    """The per-host gate in ``valkey`` and a fetcher that announces every request it sends
    (pages, redirect hops, compliance files) to that gate and reports its send, so each request
    keeps the spacing to the other turns' real starts. Pass both to ``create_pipeline``;
    ``aclose`` the fetcher last."""
    gate = DomainGate(valkey, settings)
    return gate, Fetcher(
        settings, before_request=gate.before_request, on_request_sent=gate.mark_sent
    )


def create_pipeline(
    settings: Settings,
    valkey: Redis,
    fetcher: Fetcher,
    registry: CollectorRegistry,
    pool: ConversionPool | None = None,
    *,
    gate: DomainGate | None = None,
    memory_guard: MemoryPressure | None = None,
    renderer: Renderer | None = None,
    unblock_renderer: Renderer | None = None,
    clearance: ClearanceStore | None = None,
    timeout_seconds: float | None = None,
) -> Pipeline:
    """A pipeline whose gate, compliance checks, cache and fingerprints all live in ``valkey``.

    ``gate`` is the one ``fetcher`` reports its sends to (``create_outbound`` makes the pair);
    without it a new gate is made, which then counts its spacing from the turn starts only.
    With ``memory_guard`` the result cache skips its writes while Valkey's memory is high.
    The browser options go to ``Pipeline`` unchanged."""
    if gate is None:
        gate = DomainGate(valkey, settings)
    return Pipeline(
        fetcher,
        gate,
        ComplianceChecker(fetcher, valkey, settings),
        ResultCache(valkey, settings, guard=memory_guard),
        FingerprintStore(valkey, settings),
        settings,
        registry,
        pool,
        renderer=renderer,
        unblock_renderer=unblock_renderer,
        clearance=clearance,
        timeout_seconds=timeout_seconds,
    )


# --- helpers -------------------------------------------------------------------------------------


def _cache_options(req: ExtractRequest, mode: ComplianceMode) -> dict[str, Any]:
    """Every request option that changes the result; the cache key covers them.

    ``mode`` keeps the results of each mode apart: ``browser`` renders, and ``auto`` keeps a
    rendered result when the fast one needed the browser. The browser options that are not
    here (actions, cookies, headers, a screenshot) keep a request out of the cache altogether.
    """
    return {
        "mode": req.mode,
        "format": req.format,
        "metadata": req.metadata,
        "links": req.links,
        "compliance": mode,
    }


def _html_lang(html: str) -> str | None:
    """The ``lang`` attribute of the first ``<html>`` tag near the start of the page."""
    head = html[:_LANG_SCAN_CHARS]
    lowered = head.lower()
    start = lowered.find("<html")
    while start >= 0:
        after = lowered[start + 5 : start + 6]
        if after in ("", ">", "/") or after.isspace():
            end = head.find(">", start, start + _MAX_TAG_CHARS)
            tag = head[start + 5 : end if end >= 0 else start + _MAX_TAG_CHARS]
            match = _LANG_ATTRIBUTE.search(tag)
            return match.group(1) if match else None
        start = lowered.find("<html", start + 5)
    return None


def _path_hash(url: str) -> str:
    """First 12 hex characters of the SHA-256 of path and query: lets logs group requests
    without showing the URL, whose query can carry secrets."""
    parts = urlsplit(url)
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    return hashlib.sha256(target.encode()).hexdigest()[:12]


def _outcome(envelope: Envelope[ExtractResult]) -> str:
    """Metric label: ``success``, ``partial`` or the error code of a failure."""
    if envelope.status == "failed" and envelope.error is not None:
        return envelope.error.code.value
    return envelope.status


def _seconds(value: float) -> int:
    return max(math.ceil(value), 1)
