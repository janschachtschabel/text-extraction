"""Fetching a page's images for the vision call (ruling V-S6).

``ImageFetcher.get(url, mode, timeout=...)`` fetches one image the way the pipeline fetches a
page, and answers its bytes or why it was skipped; it never raises for the target's faults:

* the SSRF guard: ``validate_url``, then the host's addresses against the address policy before
  anything is requested (a refused address gets no request, not even for its robots.txt); the
  fetcher's guard checks every connection again, and every redirect hop gets the same checks;
* the compliance checks of the job's mode: from the cache when they can be, else
  ``before_fetch`` inside the host's turn; a redirect target's origin is checked the same way
  before it is requested, and a block ends the chain; ``after_fetch`` adds the image's own
  ``tdm-reservation`` and ``noai`` headers. Only ``enforce`` blocks;
* the outbound gate: the fetch runs in the host's turn (``slot(host, crawl_delay)``), the
  outcome is recorded while the turn is held, and a busy or paused host skips the image
  (``busy``) instead of deferring the work;
* the fetcher's ``USER_AGENT`` and nothing of the caller (no cookies, headers or actions), at
  most ``LLM_VISION_MAX_IMAGE_BYTES``, within ``timeout`` seconds for the whole call.

The type comes from the body's magic bytes (``sniff_image``), never from ``Content-Type``.
Logs carry the host and the reason only, never the URL.
"""

import asyncio
import contextlib
import socket
import time
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address
from typing import Final, Literal, Protocol
from urllib.parse import urlsplit

import structlog
from redis.asyncio import Redis

from text_extraction.compliance import (
    ComplianceChecker,
    ComplianceMode,
    ComplianceReport,
    blocking_code,
    with_redirect_findings,
)
from text_extraction.envelope import ErrorCode
from text_extraction.fetch import Fetcher, FetchError, FetchResult
from text_extraction.netguard import (
    AddressPolicy,
    BlockedAddressError,
    InvalidUrlError,
    is_public_address,
    resolve_allowed,
    validate_url,
)
from text_extraction.outbound import (
    CircuitOpen,
    DomainGate,
    OutboundBusy,
    normalize_host,
    parse_retry_after,
)
from text_extraction.settings import Settings
from text_extraction.vision.images import sniff_image
from text_extraction.vision.types import VisionMediaType

__all__ = [
    "ImageBytes",
    "ImageFetcher",
    "ImageSkipped",
    "ImageSource",
    "SkipReason",
    "create_image_fetcher",
]

_log = structlog.get_logger(__name__)

SkipReason = Literal[
    "blocked", "refused", "too_large", "unsupported", "http_error", "error", "busy"
]

_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
# Failures that say something about the host's health (as the pipeline counts them).
_HOST_FAILURES: Final = frozenset(
    {ErrorCode.DNS_ERROR, ErrorCode.CONNECTION_ERROR, ErrorCode.TIMEOUT}
)
# A timeout counts against the host only if the request had this share of FETCH_TIMEOUT_SECONDS.
_FAIR_BUDGET_SHARE: Final = 0.5


@dataclass(frozen=True)
class ImageBytes:
    """An image as fetched: its bytes and the type its magic bytes say."""

    data: bytes
    media_type: VisionMediaType

    def __repr__(self) -> str:  # never the bytes in a log or a traceback
        return f"ImageBytes(media_type={self.media_type!r}, size={len(self.data)})"


@dataclass(frozen=True)
class ImageSkipped:
    """An image that was not fetched or is not one the vision endpoint takes."""

    reason: SkipReason


class ImageSource(Protocol):
    """Where the vision step gets a page's images from."""

    async def get(
        self,
        url: str,
        mode: ComplianceMode,
        *,
        timeout: float,  # noqa: ASYNC109 - fixed by the plan's interface
    ) -> ImageBytes | ImageSkipped: ...


class _BlockedRedirectError(FetchError):
    """A redirect target the compliance checks block: ends the chain."""


class _Turn:
    """The gate's turn for the host being fetched; it moves along a redirect chain (as the
    pipeline's). ``answering`` is the host whose answer the fetch waits for: None while no turn
    is held."""

    def __init__(self, gate: DomainGate) -> None:
        self._gate = gate
        self._held: contextlib.AsyncExitStack | None = None
        self._host: str | None = None
        self.answering: str | None = None

    def holds(self, host: str) -> bool:
        return self._held is not None and normalize_host(host) == self._host

    async def take(self, host: str, crawl_delay: float | None) -> None:
        """Wait for a turn at ``host``; one already held there is kept, one at another host is
        given back first."""
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


class ImageFetcher:
    """Fetches page images (see the module docstring). ``aclose`` closes its ``fetcher``.

    ``policy`` is the address policy of ``fetcher`` (default: as ``Fetcher``'s), used to refuse
    a target before anything is requested from its host."""

    def __init__(
        self,
        fetcher: Fetcher,
        gate: DomainGate,
        checker: ComplianceChecker,
        settings: Settings,
        *,
        policy: AddressPolicy | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._gate = gate
        self._checker = checker
        self._settings = settings
        self._policy = policy if policy is not None else _default_policy(settings)

    async def aclose(self) -> None:
        await self._fetcher.aclose()

    async def get(
        self,
        url: str,
        mode: ComplianceMode,
        *,
        timeout: float,  # noqa: ASYNC109 - fixed by the plan's interface
    ) -> ImageBytes | ImageSkipped:
        """The image at ``url``, or why it was skipped, within ``timeout`` seconds."""
        host = _host_of(url)
        try:
            outcome = await self._get(url, mode, timeout)
        except (OutboundBusy, CircuitOpen):
            outcome = ImageSkipped("busy")
        if isinstance(outcome, ImageSkipped):
            _log.info("image_skipped", host=host, reason=outcome.reason)
        return outcome

    async def _get(
        self, url: str, mode: ComplianceMode, seconds: float
    ) -> ImageBytes | ImageSkipped:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        try:
            await self._check_address(url)
        except FetchError as error:
            return ImageSkipped(_reason_of(error))
        turn = _Turn(self._gate)
        try:
            try:
                async with asyncio.timeout_at(deadline):
                    report = await self._check_origin(url, mode, turn)
                    if blocking_code(report) is not None:
                        return ImageSkipped("blocked")
                    await turn.take(_host_of(url), report.crawl_delay)
            except TimeoutError:
                return ImageSkipped("error")
            budget = deadline - loop.time()
            if budget <= 0:
                return ImageSkipped("error")
            fetched, report = await self._fetch(url, mode, report, turn, budget)
        except FetchError as error:
            return ImageSkipped(_reason_of(error))
        finally:
            await turn.give_back()
        if blocking_code(self._checker.after_fetch(report, fetched, None)) is not None:
            return ImageSkipped("blocked")
        if fetched.status >= 400:
            return ImageSkipped("http_error")
        if fetched.truncated:  # a body declared as audio or video is cut, not refused
            return ImageSkipped("too_large")
        media_type = sniff_image(fetched.content)
        if media_type is None:
            return ImageSkipped("unsupported")
        return ImageBytes(data=fetched.content, media_type=media_type)

    async def _fetch(
        self,
        url: str,
        mode: ComplianceMode,
        report: ComplianceReport,
        turn: _Turn,
        budget: float,
    ) -> tuple[FetchResult, ComplianceReport]:
        """Fetch ``url`` in the turn held for its host; the outcome is recorded for the host
        that gave it while its turn is held. Raises ``FetchError``."""
        checked = {url}

        async def on_redirect(target: str) -> None:
            nonlocal report
            await self._check_address(target)
            host = _host_of(target)
            if not turn.holds(host):
                turn.answering = None
            if target not in checked:
                checked.add(target)
                hop = await self._check_origin(target, mode, turn)
                report = with_redirect_findings(report, hop)
                code = blocking_code(report)
                if code is not None:
                    raise _BlockedRedirectError(code, "The image's redirect target is blocked.")
            await turn.take(host, report.crawl_delay)

        try:
            fetched = await self._fetcher.fetch(
                url,
                max_bytes=self._settings.llm_vision_max_image_bytes,
                timeout_seconds=budget,
                on_redirect=on_redirect,
            )
        except FetchError as error:
            gate_timeout = error.code == ErrorCode.TIMEOUT and error.cleared_after is None
            if (
                turn.answering is not None
                and not gate_timeout
                and not isinstance(error, _BlockedRedirectError)
            ):
                # The gate's wait before the send is not time the host had.
                had = budget - (error.cleared_after or 0.0)
                await self._record_failure(turn.answering, error, had)
            raise
        if turn.answering is not None:
            await self._gate.record(
                turn.answering,
                status=fetched.status,
                retry_after=parse_retry_after(fetched.headers.get("retry-after"), now=time.time()),
            )
        return fetched, report

    async def _check_origin(self, url: str, mode: ComplianceMode, turn: _Turn) -> ComplianceReport:
        """``before_fetch`` for ``url``: from the cache when it can be, else in the turn of
        ``url``'s host, taken here (or kept), so the origin's files are fetched under the host's
        limits. The Crawl-delay is not known yet; the image fetched next in the turn keeps it."""
        report = await self._checker.cached_before_fetch(url, mode)
        if report is None:
            await turn.take(_host_of(url), None)
            report = await self._checker.before_fetch(url, mode)
        return report

    async def _check_address(self, url: str) -> None:
        """``url`` must be a plain http(s) URL whose host resolves to allowed addresses only.
        Raises ``FetchError`` (``invalid_url``, ``blocked_url``, ``dns_error``)."""
        try:
            validate_url(url)
            parts = urlsplit(url)
            port = parts.port or _DEFAULT_PORTS[parts.scheme]
            await resolve_allowed(parts.hostname or "", port, self._policy)
        except InvalidUrlError as error:
            raise FetchError(ErrorCode.INVALID_URL, str(error)) from None
        except BlockedAddressError:
            raise FetchError(ErrorCode.BLOCKED_URL, "The target address is not allowed.") from None
        except (socket.gaierror, UnicodeError):
            raise FetchError(
                ErrorCode.DNS_ERROR, "The host name could not be resolved.", retryable=True
            ) from None

    async def _record_failure(self, host: str, error: FetchError, had: float) -> None:
        """Report a failed fetch to the gate, as the pipeline does: an answer's status counts,
        DNS and connection errors count as failures of the host, a timeout only when the request
        had a fair share of ``FETCH_TIMEOUT_SECONDS``."""
        fair = had >= self._settings.fetch_timeout_seconds * _FAIR_BUDGET_SHARE
        if error.upstream_status is not None:
            await self._gate.record(host, status=error.upstream_status, retry_after=None)
        elif error.code in _HOST_FAILURES and (error.code != ErrorCode.TIMEOUT or fair):
            await self._gate.record(host, status=None, retry_after=None)


def create_image_fetcher(
    settings: Settings, valkey: Redis, *, policy: AddressPolicy | None = None
) -> ImageFetcher:
    """An image fetcher with a gate and compliance cache in ``valkey`` and a fetcher of its own
    that announces every request to that gate (as ``pipeline.create_outbound`` builds them).
    ``aclose`` it last."""
    gate = DomainGate(valkey, settings)
    fetcher = Fetcher(
        settings, policy=policy, before_request=gate.before_request, on_request_sent=gate.mark_sent
    )
    checker = ComplianceChecker(fetcher, valkey, settings)
    return ImageFetcher(fetcher, gate, checker, settings, policy=policy)


def _default_policy(settings: Settings) -> AddressPolicy:
    """The address policy ``Fetcher`` uses without one."""
    if settings.fetch_allow_private_networks:
        return _allow_all
    return is_public_address


def _allow_all(_address: IPv4Address | IPv6Address) -> bool:
    return True


def _reason_of(error: FetchError) -> SkipReason:
    if isinstance(error, _BlockedRedirectError):
        return "blocked"
    if error.code == ErrorCode.TOO_LARGE:
        return "too_large"
    if error.code in (ErrorCode.BLOCKED_URL, ErrorCode.INVALID_URL):
        return "refused"
    return "error"


def _host_of(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""
