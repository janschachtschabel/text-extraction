"""The fast extraction pipeline behind ``POST /v1/extract``.

One request runs these steps (docs/architecture.md, "Extraction pipeline"):

1. ``resolve_mode``: the compliance mode that applies; a refused choice is a 403.
2. ``validate_url``.
3. Result cache: a fresh entry is served as it is (``cache: hit``).
4. Compliance before the fetch (robots.txt, tdmrep.json, ai.txt); a block ends here.
5. The fetch, inside the host's ``DomainGate`` slot, and its outcome reported to the gate. A
   stale cache entry's validators are sent; a 304 or unchanged bytes reuse the stored result
   (``cache: revalidated``). Each redirect target passes the compliance checks of step 4 before
   it is requested, and one on another host waits for that host's slot instead.
6. Any other final status than 2xx (or a 304 to our validators) ends as ``target_http_error``.
7. HTML and XHTML: decode, compliance after the fetch (headers, meta tags, ``noai``; may block),
   convert, strip domain boilerplate. ``text/plain``: decode, header checks, ``convert_plain``.
   Anything else: ``unsupported_content_type``.
8. Quality verdict, language, metadata, links, fingerprint and cache.

All of it runs under one deadline, ``EXTRACT_TIMEOUT_SECONDS``. Every outcome of the target is
an envelope (HTTP 200) with ``status`` ``success``, ``partial`` or ``failed``; only a refused
compliance mode (403) and a Valkey outage of the outbound gate (503) leave as ``ServiceError``.

Only ``success`` and ``partial`` results are cached and fingerprinted: a bot challenge or an
error page is nothing to serve again, nor to compare the next version of the page with.
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
from dataclasses import dataclass
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
from text_extraction.quality import Quality, assess, quality_error
from text_extraction.result_cache import CachedEntry, ResultCache, cache_key
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
# With less time than this left when its turn comes, the target is not fetched at all.
_MIN_FETCH_SECONDS = 1.0
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
    deadline: asyncio.Timeout | None = None
    host: str | None = None
    final_url: str | None = None
    upstream_status: int | None = None
    content_type: str | None = None
    attempts: int = 0
    compliance_ms: int = 0
    fetch_ms: int = 0
    convert_ms: int = 0

    def timings(self) -> Timings:
        return Timings(
            total_ms=_ms(self.started),
            compliance_ms=self.compliance_ms,
            fetch_ms=self.fetch_ms,
            convert_ms=self.convert_ms,
        )

    def result(self, **fields: Any) -> ExtractResult:
        values: dict[str, Any] = {
            "url": self.req.url,
            "final_url": self.final_url,
            "upstream_status": self.upstream_status,
            "content_type": self.content_type,
            "mode_used": "fast",
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
            "warnings": [],
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
    ) -> None:
        """``pool`` runs the CPU-bound steps; without one the pipeline makes its own."""
        self._fetcher = fetcher
        self._gate = gate
        self._checker = checker
        self._cache = cache
        self._fingerprints = fingerprints
        self._settings = settings
        self._pool = pool if pool is not None else ConversionPool(settings.convert_concurrency)
        self._rules = load_boilerplate_rules()
        self._metrics = _metrics_for(registry)

    async def run(self, req: ExtractRequest, principal: Principal) -> Envelope[ExtractResult]:
        """Extract ``req.url``. Raises ``ServiceError`` only for a refused compliance mode (403)
        and an unavailable Valkey (503)."""
        mode = resolve_mode(req.compliance, principal, self._settings)
        run = _Run(
            req=req,
            report=ComplianceReport(mode=mode, decision="skipped"),  # until it is checked
            cache="miss" if req.cache else "bypass",
            started=time.perf_counter(),
        )
        outcome = ErrorCode.INTERNAL_ERROR.value
        try:
            envelope = await self._within_deadline(run)
            if envelope.data is not None:
                envelope.data.timings = run.timings()
            envelope.trace_id = current_trace_id()
            outcome = _outcome(envelope)
            return envelope
        finally:
            self._observe(run, outcome)

    # --- steps ---------------------------------------------------------------------------------

    async def _within_deadline(self, run: _Run) -> Envelope[ExtractResult]:
        deadline = asyncio.timeout(self._settings.extract_timeout_seconds)
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

        stored = await self._cached(req, run.report.mode) if req.cache else None
        if stored is not None and stored.fresh:
            return self._serve_hit(run, stored)
        try:
            return await self._fetch_and_extract(run, stored)
        except FetchError as error:
            run.upstream_status = error.upstream_status
            return run.failed(error.code, error.message, retryable=error.retryable)
        except OutboundBusy as busy:
            return run.failed(
                ErrorCode.RATE_LIMITED,
                "Too many requests to this site are waiting; "
                f"retry after {_seconds(busy.retry_after)} s.",
                retryable=True,
            )
        except CircuitOpen as paused:
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
        started = time.perf_counter()
        report = await self._checker.before_fetch(req.url, run.report.mode)
        run.compliance_ms += _ms(started)
        run.report = report
        if (code := blocking_code(report)) is not None:
            return run.failed(code, _BLOCK_MESSAGES[code])

        fetched = await self._fetch(run, stored.entry.validators if stored else None)
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
            return run.failed(
                ErrorCode.TARGET_HTTP_ERROR,
                f"The target answered with HTTP status {fetched.status}.",
                retryable=fetched.status in _RETRYABLE_STATUSES or fetched.status >= 500,
            )
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
        return await self._extract_content(run, stored, fetched, kind, text)

    async def _fetch(self, run: _Run, validators: Validators | None) -> FetchResult:
        """Fetch the target in its host's turn and report the outcome to the gate.

        A redirect target is checked against the compliance files of its origin before it is
        requested (a block ends the chain). A target on another host waits for that host's turn
        and the previous host's turn is given back first; one on the same host keeps the turn.
        The outcome is recorded for the host that gave it, judged against the time that host's
        request actually had.
        """
        turn = _Turn(self._gate)
        checked = {run.req.url}
        started = time.perf_counter()
        hop_checks = 0.0  # seconds of compliance checks inside the fetch
        budget = 0.0  # seconds the request at `turn.answering` has, for `_record_failure`

        async def on_redirect(url: str) -> None:
            nonlocal hop_checks, budget
            host = urlsplit(url).hostname or ""
            if not turn.holds(host):
                turn.answering = None  # until the target's turn is taken, no answer is its host's
            if url not in checked:
                checked.add(url)
                check_started = time.perf_counter()
                hop = await self._checker.before_fetch(url, run.report.mode)
                hop_checks += time.perf_counter() - check_started
                run.report = with_redirect_findings(run.report, hop)
                if (code := blocking_code(run.report)) is not None:
                    raise FetchError(code, _BLOCK_MESSAGES[code])
            await turn.take(host, run.report.crawl_delay)
            # The hop starts now, so this is its budget, whatever the whole chain started with.
            budget = self._fetch_budget(run)

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
            try:
                fetched = await self._fetcher.fetch(
                    run.req.url,
                    validators=validators,
                    timeout_seconds=budget,  # the whole chain's budget; a hop gets what is left
                    on_redirect=on_redirect,
                )
            except FetchError as error:
                if turn.answering is not None:
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
        the time left when its turn came): one cut short by the extraction's own deadline says
        little about the host, and a redirect target only gets what the chain left over. Errors
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
        stored: _Stored | None,
        fetched: FetchResult,
        kind: Literal["html", "text"],
        text: str,
    ) -> Envelope[ExtractResult]:
        started = time.perf_counter()
        try:
            processed = await self._pool.run(self._process, run.req, fetched, kind, text)
        except Exception as error:  # a page that breaks a converter fails alone
            # Traceback without locals (see logging.py); the host only, never the URL.
            _log.error(
                "conversion_failed", host=run.host, error_type=type(error).__name__, exc_info=True
            )
            return run.failed(ErrorCode.CONVERSION_FAILED, "The page could not be converted.")
        finally:
            run.convert_ms += _ms(started)
        fingerprint = processed.fingerprint
        if fingerprint is not None:
            fingerprint = await self._fingerprints.compare_and_save(
                run.req.url, fingerprint, variant=run.req.format
            )
        result = run.result(
            language=processed.language,
            word_count=processed.quality.word_count,
            title=processed.title,
            content=processed.content,
            quality=processed.quality,
            metadata=processed.metadata,
            links=processed.links,
            fingerprint=fingerprint,
            warnings=processed.warnings,
        )
        envelope = self._envelope(result)
        if run.req.cache and envelope.status != "failed":
            options = _cache_options(run.req, run.report.mode)
            key = stored.key if stored else cache_key(run.req.url, options)
            await self._cache.put(
                key,
                CachedEntry(
                    stored_at=time.time(),
                    validators=Validators.from_headers(fetched.headers),
                    raw_sha256=fetched.raw_sha256,
                    result=result.model_dump(mode="json"),
                ),
            )
        return envelope

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


def create_pipeline(
    settings: Settings,
    valkey: Redis,
    fetcher: Fetcher,
    registry: CollectorRegistry,
    pool: ConversionPool | None = None,
) -> Pipeline:
    """A pipeline whose gate, compliance checks, cache and fingerprints all live in ``valkey``."""
    return Pipeline(
        fetcher,
        DomainGate(valkey, settings),
        ComplianceChecker(fetcher, valkey, settings),
        ResultCache(valkey, settings),
        FingerprintStore(valkey, settings),
        settings,
        registry,
        pool,
    )


# --- helpers -------------------------------------------------------------------------------------


def _cache_options(req: ExtractRequest, mode: ComplianceMode) -> dict[str, Any]:
    """Every request option that changes the result; the cache key covers them.

    ``mode`` gives the same result in both modes today (``quality.browser_recommended`` is
    reported in both, and nothing escalates), but ``auto`` will escalate to the browser in
    package 4, so its results are kept apart already.
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
