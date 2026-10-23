"""The fast extraction pipeline behind ``POST /v1/extract``.

One request runs these steps (docs/architecture.md, "Extraction pipeline"):

1. ``resolve_mode``: the compliance mode that applies; a refused choice is a 403.
2. ``validate_url``.
3. Result cache: a fresh entry is served as it is (``cache: hit``).
4. Compliance before the fetch (robots.txt, tdmrep.json, ai.txt); a block ends here.
5. The fetch, inside the host's ``DomainGate`` slot, and its outcome reported to the gate. A
   stale cache entry's validators are sent; a 304 or unchanged bytes reuse the stored result
   (``cache: revalidated``).
6. An answer >= 400 ends as ``target_http_error``.
7. HTML and XHTML: decode, compliance after the fetch (headers, meta tags, ``noai``; may block),
   convert, strip domain boilerplate. ``text/plain``: decode, header checks, ``convert_plain``.
   Anything else: ``unsupported_content_type``.
8. Quality verdict, language, metadata, links, fingerprint and cache.

All of it runs under one deadline, ``EXTRACT_TIMEOUT_SECONDS``. Every outcome of the target is
an envelope (HTTP 200) with ``status`` ``success``, ``partial`` or ``failed``; only a refused
compliance mode (403) and a Valkey outage of the outbound gate (503) leave as ``ServiceError``.

Only ``success`` and ``partial`` results are cached and fingerprinted: a bot challenge or an
error page is nothing to serve again, nor to compare the next version of the page with.
CPU-bound steps run in a worker thread; the deadline cannot stop such a thread, but every
step bounds its own cost.
"""

import asyncio
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
from pydantic import BaseModel, Field, ValidationError
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
)
from text_extraction.convert import (
    Link,
    OutputFormat,
    convert_html,
    convert_plain,
    decode_html,
    page_links,
)
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.errors import ServiceError
from text_extraction.fetch import Fetcher, FetchError, FetchResult, Validators
from text_extraction.fingerprint import Fingerprint, FingerprintStore, compute_fingerprint
from text_extraction.metadata import PageMetadata, detect_language, extract_metadata
from text_extraction.netguard import MAX_URL_LENGTH, InvalidUrlError, validate_url
from text_extraction.outbound import CircuitOpen, DomainGate, OutboundBusy, parse_retry_after
from text_extraction.quality import Quality, assess, quality_error
from text_extraction.result_cache import CachedEntry, ResultCache, cache_key
from text_extraction.settings import Settings
from text_extraction.tracing import current_trace_id

_log = structlog.get_logger(__name__)

CacheState = Literal["miss", "hit", "revalidated", "bypass"]

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
_MIN_FETCH_SECONDS = 0.01
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


class ExtractRequest(BaseModel, extra="forbid"):
    """Body of ``POST /v1/extract``."""

    url: str = Field(max_length=MAX_URL_LENGTH, description="The http(s) URL to extract.")
    mode: Literal["fast", "auto"] = Field(
        default="auto",
        description="`fast`: HTTP only. `auto`: also reports whether a browser could do better "
        "(`quality.browser_recommended`); the browser itself arrives in a later release.",
    )
    format: OutputFormat = Field(default="markdown", description="Format of `content`.")
    metadata: bool = Field(default=True, description="Include the page's DOM metadata.")
    links: bool = Field(default=False, description="Include the page's links.")
    compliance: ComplianceMode | None = Field(
        default=None,
        description="`enforce`, `report` or `ignore` (admins only); default: the operator's.",
    )
    cache: bool = Field(
        default=True, description="Use the result cache; `false` neither reads nor stores."
    )


class Timings(BaseModel):
    """Milliseconds spent in this request. ``fetch_ms`` includes the wait for the host's turn;
    ``convert_ms`` covers decoding, conversion and the steps after it."""

    total_ms: int
    compliance_ms: int
    fetch_ms: int
    convert_ms: int


class ExtractResult(BaseModel):
    """What one extraction produced; on failure as much of it as was known."""

    url: str
    final_url: str | None
    upstream_status: int | None  # of the response the content came from
    content_type: str | None
    mode_used: Literal["fast"]
    attempts: int  # fetches of the target in this request
    language: str | None
    word_count: int
    title: str | None
    format: OutputFormat
    content: str | None
    quality: Quality | None
    timings: Timings
    compliance: ComplianceReport
    metadata: PageMetadata | None
    links: list[Link] | None
    fingerprint: Fingerprint | None
    cache: CacheState
    warnings: list[str]


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
    ) -> None:
        self._fetcher = fetcher
        self._gate = gate
        self._checker = checker
        self._cache = cache
        self._fingerprints = fingerprints
        self._settings = settings
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
                _with_page_findings(report, stored.result.compliance), fetched, None
            )
            if (code := blocking_code(run.report)) is not None:
                return run.failed(code, _BLOCK_MESSAGES[code])
            return await self._revalidated(run, stored, fetched)
        if fetched.status >= 400:
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

        started = time.perf_counter()
        text = await asyncio.to_thread(decode_html, fetched.content, fetched.charset)
        run.convert_ms += _ms(started)
        started = time.perf_counter()
        page = text if kind == "html" else None
        run.report = await asyncio.to_thread(self._checker.after_fetch, report, fetched, page)
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
        """Fetch the target in its host's turn and report the outcome to the gate."""
        host = run.host or ""
        started = time.perf_counter()
        try:
            async with self._gate.slot(host, run.report.crawl_delay):
                run.attempts += 1
                try:
                    fetched = await self._fetcher.fetch(
                        run.req.url, validators=validators, timeout_seconds=self._fetch_budget(run)
                    )
                except FetchError as error:
                    await self._record_failure(host, error)
                    raise
                # Recorded while the turn is held, so a 429 pauses the host before the next turn.
                await self._gate.record(
                    host,
                    status=fetched.status,
                    retry_after=parse_retry_after(
                        fetched.headers.get("retry-after"), now=time.time()
                    ),
                )
        finally:
            run.fetch_ms += _ms(started)
        run.final_url = fetched.final_url
        run.upstream_status = fetched.status
        run.content_type = fetched.content_type
        return fetched

    def _fetch_budget(self, run: _Run) -> float:
        budget = self._settings.fetch_timeout_seconds
        when = run.deadline.when() if run.deadline is not None else None
        if when is not None:
            left = when - asyncio.get_running_loop().time() - _FETCH_MARGIN_SECONDS
            budget = min(budget, left)
        return max(budget, _MIN_FETCH_SECONDS)

    async def _record_failure(self, host: str, error: FetchError) -> None:
        """Report a failed fetch to the gate.

        An error with a status (too large, too many redirects) still got an answer: that status
        counts. DNS, connection and timeout errors count as failures of the host. Errors that say
        nothing about the host's health (a redirect to a blocked or invalid URL, an unsupported
        encoding) are not reported.
        """
        if error.upstream_status is not None:
            await self._gate.record(host, status=error.upstream_status, retry_after=None)
        elif error.code in _HOST_FAILURES:
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
        fresh = _validators(fetched)
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
            processed = await asyncio.to_thread(self._process, run.req, fetched, kind, text)
        except Exception as error:  # a page that breaks a converter fails alone
            _log.error("conversion_failed", host=run.host, error_type=type(error).__name__)
            return run.failed(ErrorCode.CONVERSION_FAILED, "The page could not be converted.")
        finally:
            run.convert_ms += _ms(started)
        fingerprint = processed.fingerprint
        if fingerprint is not None:
            fingerprint = await self._fingerprints.compare_and_save(run.req.url, fingerprint)
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
                    validators=_validators(fetched),
                    raw_sha256=fetched.raw_sha256,
                    result=result.model_dump(mode="json"),
                ),
            )
        return envelope

    def _process(
        self, req: ExtractRequest, fetched: FetchResult, kind: Literal["html", "text"], text: str
    ) -> _Processed:
        """Convert and judge the decoded page. CPU-bound: runs in a worker thread."""
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
    settings: Settings, valkey: Redis, fetcher: Fetcher, registry: CollectorRegistry
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
    )


# --- helpers -------------------------------------------------------------------------------------


def _cache_options(req: ExtractRequest, mode: ComplianceMode) -> dict[str, Any]:
    """Every request option that changes the result; the cache key covers them."""
    return {
        "mode": req.mode,
        "format": req.format,
        "metadata": req.metadata,
        "links": req.links,
        "compliance": mode,
    }


def _validators(fetched: FetchResult) -> Validators:
    return Validators(
        etag=fetched.headers.get("etag"), last_modified=fetched.headers.get("last-modified")
    )


def _with_page_findings(report: ComplianceReport, previous: ComplianceReport) -> ComplianceReport:
    """``report`` plus the TDM statement and ``noai`` the stored page carried.

    A 304 has no body and often leaves out headers, but it says the page is the stored one, so
    what that page said about itself still holds. ``after_fetch`` then adds the 304's own
    headers and decides again.
    """
    update: dict[str, object] = {}
    reasons = list(report.reasons)
    source = previous.tdm_source
    carry_tdm = (
        source in ("header", "meta")
        and previous.tdm_reserved is not None
        and report.tdm_reserved is not True
        and (previous.tdm_reserved or report.tdm_reserved is None)
    )
    if carry_tdm:
        update.update(
            tdm_reserved=previous.tdm_reserved, tdm_policy=previous.tdm_policy, tdm_source=source
        )
        if previous.tdm_reserved:
            reasons.append(f"The page reserves text and data mining rights ({source}).")
    if previous.noai and not report.noai:
        update["noai"] = True
        reasons.append("The page asks not to be used for AI (noai or noml).")
    if not update:
        return report
    update["reasons"] = reasons
    return report.model_copy(update=update)


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
