"""The guarded HTTP fetcher: every outbound request of the service goes through ``Fetcher``.

What it enforces, for the first request and every redirect hop alike:

* ``validate_url`` on each URL (relative ``Location`` headers are resolved against the current URL);
* the SSRF guard (``GuardedTransport``), with no proxy and no environment lookup;
* one overall deadline for the whole call, redirects included;
* a cap on the *decoded* body size. ``gzip`` and ``deflate`` are inflated here in bounded steps,
  so a compression bomb cannot allocate more than one step beyond the cap. With ``truncate`` the
  body is cut at the cap instead (robots.txt is read from its first 500 KiB, RFC 9309). A
  response declared ``audio/*``, ``video/*`` or ``application/ogg`` is always cut, at
  ``MEDIA_HEAD_BYTES`` (or the cap, if that is lower): a media file is sniffed from its head,
  never refused as ``too_large``, and a declared ``Content-Length`` over the cap does not matter;
* cache validators go only to the origin they came from: once a redirect leaves it, the
  conditional headers are dropped for the rest of the chain.

The caller may watch the chain with ``on_redirect``: it is awaited with each redirect target
before that target is requested, and may raise ``FetchError`` to end the chain there.

``before_request``, a constructor argument, is awaited with the host of every request (each
redirect hop and every caller's, compliance files included) right before it is sent; the outbound
gate waits out its spacing there (``DomainGate.before_request``) and may raise, which ends the
fetch with that error and sends nothing. ``on_request_sent``, also a constructor argument, is
awaited with the host right after the request's headers went out on the wire; the gate counts
its spacing from that real send (``DomainGate.mark_sent``). That hook gets at most half a
second; an error or a timeout of it is logged (host and error type) and does not fail the fetch.

Failures of the target or the network are raised as ``FetchError`` with a code from
``ErrorCode``. A response with status >= 400 is not an error here: it is returned and the
pipeline decides what it means.
"""

import asyncio
import contextlib
import hashlib
import re
import socket
import time
import zlib
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from http.cookiejar import DefaultCookiePolicy
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
import structlog

from text_extraction import __version__
from text_extraction.envelope import ErrorCode
from text_extraction.media.sniff import MEDIA_HEAD_BYTES, is_declared_media
from text_extraction.netguard import (
    AddressPolicy,
    BlockedAddressError,
    GuardedTransport,
    InvalidUrlError,
    is_public_address,
    validate_url,
)
from text_extraction.settings import Settings

USER_AGENT = (
    f"text-extraction/{__version__} (+https://github.com/janschachtschabel/text-extraction)"
)

_log = structlog.get_logger(__name__)

_CHUNK_SIZE = 65536  # largest piece of decoded body handled at once
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# Only encodings we can inflate in bounded steps are advertised; anything else is refused.
_ACCEPT_ENCODING = "gzip, deflate"
_REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
    "Accept-Language": "de,en;q=0.8",
    "Accept-Encoding": _ACCEPT_ENCODING,
}


# A validator is sent back only if it is printable ASCII (no control characters) and this short:
# httpx encodes header values as ASCII, and the value comes from the target site.
_MAX_VALIDATOR_CHARS = 1024
_SENDABLE_VALUE = re.compile(rf"[\x20-\x7e]{{1,{_MAX_VALIDATOR_CHARS}}}")
_CONDITIONAL_HEADERS = ("If-None-Match", "If-Modified-Since")
_DEFAULT_PORTS = {"http": 80, "https": 443}

# Awaited with each redirect target before it is requested; raises ``FetchError`` to stop.
RedirectHook = Callable[[str], Awaitable[None]]
SentHook = Callable[[str], Awaitable[None]]
_SENT_HOOK_TIMEOUT_SECONDS = 0.5  # the hook runs between the headers and the body: keep it short


def _sendable(value: str | None) -> str | None:
    """``value`` if it can go out as a header value unchanged, else None."""
    return value if value is not None and _SENDABLE_VALUE.fullmatch(value) else None


@dataclass(frozen=True)
class Validators:
    """Cache validators of an earlier response, sent back as a conditional request."""

    etag: str | None = None
    last_modified: str | None = None

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> "Validators":
        """The ``ETag`` and ``Last-Modified`` of a response (lower-case names) that can be sent
        back; a value with non-ASCII or control characters, or over 1 KB, is left out."""
        return cls(
            etag=_sendable(headers.get("etag")),
            last_modified=_sendable(headers.get("last-modified")),
        )


@dataclass(frozen=True)
class FetchResult:
    """A completed fetch (any HTTP status).

    ``redirects`` lists the URLs that were followed to, in order, so ``final_url`` is its last
    element when it is not empty. ``raw_sha256`` is the hash of ``content`` as received after
    transfer decoding, before any text extraction. ``not_modified`` is true for a 304 answer,
    which has empty ``content``. ``truncated`` is true when a truncating fetch cut the body at
    its byte limit.
    """

    url: str
    final_url: str
    status: int
    headers: dict[str, str]  # lower-case names
    content: bytes
    content_type: str | None  # media type only, lower-case
    charset: str | None
    elapsed_ms: int
    redirects: list[str]
    not_modified: bool
    raw_sha256: str
    truncated: bool = False


@dataclass
class _Limits:
    """The limits of one ``fetch`` call, and how far it got."""

    max_bytes: int
    truncate: bool
    deadline: float  # seconds for the whole call
    started: float  # time.monotonic() at the start
    # Seconds from the start to the moment the latest request passed ``before_request``.
    cleared_after: float | None = None


class FetchError(Exception):
    """The target could not be fetched. The message is safe to show to API callers.

    ``cleared_after`` (timeouts only) is the seconds from the start of the fetch to the moment
    the request that timed out passed ``before_request``, None when it had not got that far (the
    deadline passed inside the gate's wait, so nothing was sent): the time
    the host had is the call's deadline minus it."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        upstream_status: int | None = None,
        cleared_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.upstream_status = upstream_status
        self.cleared_after = cleared_after


class _Inflater:
    """Incremental gzip or deflate decoder that never yields more than ``_CHUNK_SIZE`` at a time."""

    def __init__(self, *, gzip: bool) -> None:
        self._gzip = gzip
        self._decoder: zlib._Decompress | None = None

    def feed(self, data: bytes) -> Iterator[bytes]:
        if not data:
            return
        if self._decoder is None:
            self._decoder = zlib.decompressobj(self._window_bits(data))
        pending = data
        while True:
            try:
                out = self._decoder.decompress(pending, _CHUNK_SIZE)
            except zlib.error as error:
                raise FetchError(
                    ErrorCode.CONNECTION_ERROR,
                    "The response body could not be decoded.",
                    retryable=True,
                ) from error
            if out:
                yield out
            pending = self._decoder.unconsumed_tail
            # A full chunk may mean more output is still buffered inside the decoder.
            if self._decoder.eof or (not pending and len(out) < _CHUNK_SIZE):
                return

    def _window_bits(self, first: bytes) -> int:
        if self._gzip:
            return zlib.MAX_WBITS | 16
        # HTTP "deflate" is zlib-wrapped, but some servers send a raw deflate stream.
        zlib_header = (
            len(first) >= 2 and first[0] & 0x0F == 8 and ((first[0] << 8) | first[1]) % 31 == 0
        )
        return zlib.MAX_WBITS if zlib_header or len(first) < 2 else -zlib.MAX_WBITS


def _inflaters(content_encoding: str | None) -> list[_Inflater]:
    """Decoders to apply to a body, in the order of application (last-applied encoding first)."""
    names = [part.strip().lower() for part in (content_encoding or "").split(",") if part.strip()]
    inflaters: list[_Inflater] = []
    for name in reversed(names):
        if name in ("gzip", "x-gzip"):
            inflaters.append(_Inflater(gzip=True))
        elif name == "deflate":
            inflaters.append(_Inflater(gzip=False))
        elif name != "identity":
            raise FetchError(
                ErrorCode.UNSUPPORTED_CONTENT_TYPE, "The response uses an unsupported encoding."
            )
    return inflaters


def _decode(inflaters: list[_Inflater], data: bytes) -> Iterator[bytes]:
    if not inflaters:
        yield data
        return
    for part in inflaters[0].feed(data):
        yield from _decode(inflaters[1:], part)


def split_content_type(value: str | None) -> tuple[str | None, str | None]:
    """Media type and charset of a Content-Type header, both lower-case."""
    if not value:
        return None, None
    media_type, _, parameters = value.partition(";")
    charset = None
    for parameter in parameters.split(";"):
        name, _, parameter_value = parameter.partition("=")
        if name.strip().lower() == "charset":
            charset = parameter_value.strip().strip("\"'").lower() or None
    return media_type.strip().lower() or None, charset


_STATUS_KEY = "text_extraction.status"


async def _remember_status(response: httpx.Response) -> None:
    """Response hook: note the status on the request, for ``Fetcher._send``."""
    response.request.extensions[_STATUS_KEY] = response.status_code


def _origin_of(url: str) -> tuple[str, str, int | None]:
    """Scheme, host and port of a validated URL, the port filled in for http and https."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or "").lower(), parts.port or _DEFAULT_PORTS.get(scheme)


def _host_of(url: str) -> str | None:
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def _sent_tracer(hook: SentHook, url: str) -> Callable[[str, dict[str, Any]], Awaitable[None]]:
    """An httpcore ``trace`` callback that calls ``hook`` once the request headers are sent."""
    host = urlsplit(url).hostname or ""

    async def trace(event: str, _info: dict[str, Any]) -> None:
        if not event.endswith(".send_request_headers.complete"):
            return
        try:
            async with asyncio.timeout(_SENT_HOOK_TIMEOUT_SECONDS):
                await hook(host)
        except Exception as error:  # a report must never fail or stall the fetch
            _log.warning("request_sent_hook_failed", host=host, error_type=type(error).__name__)

    return trace


class Fetcher:
    """Guarded HTTP client with redirect, size and time limits. Create once, ``aclose`` last."""

    def __init__(
        self,
        settings: Settings,
        *,
        policy: AddressPolicy | None = None,
        before_request: SentHook | None = None,
        on_request_sent: SentHook | None = None,
    ) -> None:
        self._settings = settings
        if policy is None:
            policy = (
                (lambda _address: True)
                if settings.fetch_allow_private_networks
                else is_public_address
            )
        self._policy = policy
        self._client = httpx.AsyncClient(
            transport=GuardedTransport(
                policy,
                max_connections=settings.outbound_max_connections,
                max_keepalive_connections=settings.outbound_max_keepalive,
            ),
            trust_env=False,
            follow_redirects=False,
            event_hooks={"response": [_remember_status]},
        )
        # The shared client never stores cookies, so nothing leaks from one fetch() to the next.
        # Cookies live in a jar of their own per fetch() call, see ``_fetch``.
        self._client.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=[]))
        self._before_request = before_request
        self._on_request_sent = on_request_sent

    @property
    def policy(self) -> AddressPolicy:
        """The address policy every connection is checked against (the given one or the
        default: public addresses only, any with ``FETCH_ALLOW_PRIVATE_NETWORKS``)."""
        return self._policy

    @property
    def before_request(self) -> SentHook | None:
        """The hook awaited right before each request (see the module docstring)."""
        return self._before_request

    @property
    def on_request_sent(self) -> SentHook | None:
        """The hook awaited once each request's headers are sent (see the module docstring)."""
        return self._on_request_sent

    async def fetch(
        self,
        url: str,
        *,
        validators: Validators | None = None,
        max_bytes: int | None = None,
        timeout_seconds: float | None = None,
        truncate: bool = False,
        on_redirect: RedirectHook | None = None,
    ) -> FetchResult:
        """GET ``url``, following redirects, or raise ``FetchError``.

        ``truncate`` keeps the first ``max_bytes`` of a longer body instead of failing with
        ``too_large``. ``on_redirect`` runs inside the deadline of this call.
        """
        limit = max_bytes if max_bytes is not None else self._settings.fetch_max_bytes
        deadline = (
            timeout_seconds if timeout_seconds is not None else self._settings.fetch_timeout_seconds
        )
        if deadline <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        started = time.monotonic()
        limits = _Limits(limit, truncate, deadline, started)
        try:
            async with asyncio.timeout(deadline):
                return await self._fetch(url, validators, limits, on_redirect)
        except FetchError:
            raise
        except (InvalidUrlError, httpx.InvalidURL) as error:
            raise FetchError(ErrorCode.INVALID_URL, str(error)) from None
        except BlockedAddressError as error:
            # The message names the resolved address: for the log, not for the caller.
            _log.warning("fetch_blocked", host=_host_of(url), detail=str(error))
            raise FetchError(ErrorCode.BLOCKED_URL, "The target address is not allowed.") from None
        except socket.gaierror:
            raise FetchError(
                ErrorCode.DNS_ERROR, "The host name could not be resolved.", retryable=True
            ) from None
        except (TimeoutError, httpx.TimeoutException):
            raise FetchError(
                ErrorCode.TIMEOUT,
                "The fetch did not finish in time.",
                retryable=True,
                cleared_after=limits.cleared_after,
            ) from None
        except (httpx.HTTPError, OSError) as error:
            raise FetchError(
                ErrorCode.CONNECTION_ERROR,
                f"The connection to the target failed ({type(error).__name__}).",
                retryable=True,
            ) from None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _fetch(
        self,
        url: str,
        validators: Validators | None,
        limits: _Limits,
        on_redirect: RedirectHook | None,
    ) -> FetchResult:
        headers = dict(_REQUEST_HEADERS)
        # Validators come from an earlier answer of the target (possibly from the cache): only a
        # value that can be sent unchanged goes out, so no header value can make the request fail.
        if validators is not None and (etag := _sendable(validators.etag)):
            headers["If-None-Match"] = etag
        if validators is not None and (last_modified := _sendable(validators.last_modified)):
            headers["If-Modified-Since"] = last_modified

        current = url
        origin = _origin_of(url)
        redirects: list[str] = []
        cookies = httpx.Cookies()  # lives for this call (one redirect chain) only
        while True:
            validate_url(current)
            if redirects:
                if _origin_of(current) != origin:
                    for name in _CONDITIONAL_HEADERS:  # the validators belong to the first origin
                        headers.pop(name, None)
                if on_redirect is not None:
                    await on_redirect(current)
            request = self._client.build_request(
                "GET", current, headers=headers, timeout=limits.deadline
            )
            cookies.set_cookie_header(request)
            if self._on_request_sent is not None:
                request.extensions["trace"] = _sent_tracer(self._on_request_sent, current)
            limits.cleared_after = None  # a timeout inside the gate's wait sent nothing
            if self._before_request is not None:
                await self._before_request(urlsplit(current).hostname or "")
            limits.cleared_after = time.monotonic() - limits.started
            async with contextlib.aclosing(await self._send(request)) as response:
                cookies.extract_cookies(response)
                status = response.status_code
                location = response.headers.get("location", "").strip()
                if status in _REDIRECT_STATUSES and location:
                    if len(redirects) >= self._settings.fetch_max_redirects:
                        raise FetchError(
                            ErrorCode.TARGET_HTTP_ERROR,
                            "The target redirected too many times.",
                            upstream_status=status,
                        )
                    current = self._redirect_target(current, location, status)
                    redirects.append(current)
                    continue
                content, truncated = (
                    (b"", False) if status == 304 else await self._read_body(response, limits)
                )
                content_type, charset = split_content_type(response.headers.get("content-type"))
                return FetchResult(
                    url=url,
                    final_url=current,
                    status=status,
                    headers={name.lower(): value for name, value in response.headers.items()},
                    content=content,
                    content_type=content_type,
                    charset=charset,
                    elapsed_ms=round((time.monotonic() - limits.started) * 1000),
                    redirects=redirects,
                    not_modified=status == 304,
                    raw_sha256=hashlib.sha256(content).hexdigest(),
                    truncated=truncated,
                )

    async def _send(self, request: httpx.Request) -> httpx.Response:
        try:
            return await self._client.send(request, stream=True)
        except httpx.RemoteProtocolError:
            # httpx prepares the next redirect request itself and fails on an unparsable Location.
            # The response hook ran by then, so a recorded status means the headers were fine.
            status = request.extensions.get(_STATUS_KEY)
            if status is None:
                raise
            raise FetchError(
                ErrorCode.TARGET_HTTP_ERROR,
                "The target sent an invalid redirect location.",
                upstream_status=status,
            ) from None

    @staticmethod
    def _redirect_target(current: str, location: str, status: int) -> str:
        """The absolute URL a ``Location`` points to; a malformed one is the target's fault."""
        try:
            target = urljoin(current, location)
            httpx.URL(target)
        except (ValueError, httpx.InvalidURL):
            raise FetchError(
                ErrorCode.TARGET_HTTP_ERROR,
                "The target sent an invalid redirect location.",
                upstream_status=status,
            ) from None
        return target

    async def _read_body(self, response: httpx.Response, limits: _Limits) -> tuple[bytes, bool]:
        """The decoded body and whether it was cut at the limit."""
        max_bytes = limits.max_bytes
        truncate = limits.truncate
        if is_declared_media(response.headers.get("content-type")):
            max_bytes = min(max_bytes, MEDIA_HEAD_BYTES)
            truncate = True
        declared = response.headers.get("content-length", "")
        if (
            not truncate
            and declared.isdigit()
            and int(declared) > max_bytes
            and not response.headers.get("content-encoding")
        ):
            raise self._too_large(response.status_code)
        body = bytearray()
        async with contextlib.aclosing(self._decoded_chunks(response)) as chunks:
            async for chunk in chunks:
                body += chunk
                if len(body) > max_bytes:
                    if not truncate:
                        raise self._too_large(response.status_code)
                    return bytes(body[:max_bytes]), True
        return bytes(body), False

    async def _decoded_chunks(self, response: httpx.Response) -> AsyncGenerator[bytes]:
        """Decoded body in pieces of at most ``_CHUNK_SIZE`` bytes."""
        inflaters = _inflaters(response.headers.get("content-encoding"))
        async for raw in response.aiter_raw(_CHUNK_SIZE):
            for chunk in _decode(inflaters, raw):
                yield chunk

    @staticmethod
    def _too_large(status: int) -> FetchError:
        return FetchError(
            ErrorCode.TOO_LARGE,
            "The response body is larger than the limit.",
            upstream_status=status,
        )
