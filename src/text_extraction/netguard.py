"""URL validation and the SSRF guard for every outbound fetch.

The guard sits in the network backend, below HTTP: whatever the caller asks for (a first request
or a redirect target), the address about to be connected to is resolved here, checked against an
``AddressPolicy`` and then connected to *by IP*. There is no second lookup between check and
connect, so DNS rebinding cannot swap the address.

``GuardedTransport`` is the one class callers use: ``httpx.AsyncClient(transport=GuardedTransport(
policy))``. Errors a caller has to tell apart:

* ``BlockedAddressError`` (an ``OSError``) is raised as itself, not wrapped in an httpx error:
  it is not an ``httpx.HTTPError``, so ``except httpx.HTTPError`` does not catch it.
* A DNS failure is raised as itself too: ``socket.gaierror`` (an ``OSError``).
* Everything else maps to the matching ``httpx`` exception (``ConnectError``, ``ReadTimeout``, ...).
"""

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterable, AsyncIterator, Callable, Iterable, Iterator
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx

MAX_URL_LENGTH = 2048
# Idle connections are closed after this many seconds.
KEEPALIVE_EXPIRY_SECONDS = 30.0

AddressPolicy = Callable[[IPv4Address | IPv6Address], bool]


class InvalidUrlError(ValueError):
    """The URL is not an acceptable fetch target (shape, scheme, host, userinfo, port, length)."""


class BlockedAddressError(OSError):
    """The target resolves to an address the policy forbids, or the connection type is not allowed.

    The message names the host and the offending address. It is meant for logs; do not echo it to
    callers verbatim.
    """


def validate_url(url: str, *, dns_form: bool = True) -> str:
    """Return ``url`` unchanged when it is a plain http(s) URL, else raise ``InvalidUrlError``.

    With ``dns_form`` (the default, for every fetch) the host must also have a DNS form;
    ``site.urls`` leaves it out, so a link graph keeps a link to a host that has none."""
    if len(url) > MAX_URL_LENGTH:
        raise InvalidUrlError(f"URL is longer than {MAX_URL_LENGTH} characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise InvalidUrlError("URL cannot be parsed") from None
    if parts.scheme not in ("http", "https"):
        raise InvalidUrlError("URL scheme must be http or https")
    if not parts.hostname:
        raise InvalidUrlError("URL has no host")
    if "@" in parts.netloc:
        raise InvalidUrlError("URL must not contain user information")
    if port is not None and not 1 <= port <= 65535:
        raise InvalidUrlError("URL port must be between 1 and 65535")
    if dns_form:
        _check_dns_form(parts.hostname)
    return url


def _check_dns_form(host: str) -> None:
    """Raise ``InvalidUrlError`` for a host name the resolver cannot encode (an empty label, a
    label over 63 characters): its IDNA codec would raise ``UnicodeError`` on the lookup; and
    for one of digits only that is no IPv4 literal."""
    try:
        ip_address(host)
    except ValueError:
        labels = host.removesuffix(".").split(".")
        # All digits but no IPv4 literal (octal, short or one-number forms): the resolver would
        # read it as an address, the HTTP client refuses some of them (audit R7).
        if all(label.isascii() and label.isdigit() for label in labels):
            raise InvalidUrlError("URL host is not a valid IP address") from None
        # A label over 63 characters has no ASCII form (an IDN label only grows when encoded):
        # refused before the encode, which is slow on a long Unicode label.
        if any(not label or len(label) > 63 for label in labels):
            raise InvalidUrlError("URL host is not a valid host name") from None
        try:
            host.encode("idna")
        except UnicodeError:
            raise InvalidUrlError("URL host is not a valid host name") from None


_NAT64 = ip_network("64:ff9b::/96")  # well-known prefix; embeds an IPv4 address in the last 32 bits


def _embedded_ipv4(ip: IPv4Address | IPv6Address) -> IPv4Address | None:
    """The IPv4 address an IPv6 address stands for (mapped, 6to4 or NAT64), if any."""
    if isinstance(ip, IPv4Address):
        return None
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip in _NAT64:
        return IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def is_public_address(ip: IPv4Address | IPv6Address) -> bool:
    """True only for globally routable unicast addresses.

    Private, loopback, link-local (including the 169.254.169.254 metadata address), multicast,
    reserved, unspecified, shared (100.64.0.0/10), documentation and IPv6 unique-local addresses
    are not public. An IPv6 address that embeds an IPv4 address is judged by the embedded one.
    """
    embedded = _embedded_ipv4(ip)
    if embedded is not None:
        return is_public_address(embedded)
    if ip.is_multicast or ip.is_reserved:  # reserved covers IPv4 240/4 and IPv6 ::/8
        return False
    if isinstance(ip, IPv6Address) and ip.is_site_local:  # deprecated fec0::/10
        return False
    return ip.is_global


async def resolve_allowed(
    host: str, port: int, policy: AddressPolicy
) -> list[IPv4Address | IPv6Address]:
    """Resolve ``host`` and return its addresses, all of which the policy allows.

    An IP literal is used as is. The answer is vetted as a whole: one forbidden address blocks
    the lot, so a mixed DNS answer cannot smuggle a private address in. Raises
    ``BlockedAddressError`` for a forbidden address and ``socket.gaierror`` when the name does not
    resolve. Callers connect to the returned addresses, never to the name again.
    """
    addresses: list[IPv4Address | IPv6Address]
    try:
        addresses = [ip_address(host)]
    except ValueError:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = [ip_address(info[4][0]) for info in infos]
    if not addresses:
        raise socket.gaierror(socket.EAI_NONAME, "no address found")
    for address in addresses:
        if not policy(address):
            raise BlockedAddressError(f"connection to {host} blocked: address {address}")
    return addresses


class GuardedBackend(httpcore.AsyncNetworkBackend):
    """Network backend that connects only to addresses the policy allows.

    Resolution happens here; the inner backend is handed a vetted IP, never the name.
    TLS still verifies against the original host name, because httpcore takes that from the URL.
    """

    def __init__(
        self, policy: AddressPolicy, inner: httpcore.AsyncNetworkBackend | None = None
    ) -> None:
        self._policy = policy
        self._inner = inner if inner is not None else httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - fixed by the httpcore interface
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            async with asyncio.timeout(timeout):
                addresses = await resolve_allowed(host, port, self._policy)
        except TimeoutError:
            raise httpcore.ConnectTimeout(f"DNS resolution of {host} timed out") from None

        # Try the vetted addresses in order (the first may be IPv6 on a host without IPv6).
        last_error: Exception = BlockedAddressError(f"no address to connect to for {host}")
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    str(address),
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                last_error = error
        raise last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - fixed by the httpcore interface
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise BlockedAddressError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


# httpcore exception -> httpx exception, most specific first (the first isinstance match wins).
_EXCEPTION_MAP: tuple[tuple[type[Exception], type[httpx.HTTPError]], ...] = (
    (httpcore.ConnectTimeout, httpx.ConnectTimeout),
    (httpcore.ReadTimeout, httpx.ReadTimeout),
    (httpcore.WriteTimeout, httpx.WriteTimeout),
    (httpcore.PoolTimeout, httpx.PoolTimeout),
    (httpcore.TimeoutException, httpx.TimeoutException),
    (httpcore.ConnectError, httpx.ConnectError),
    (httpcore.ReadError, httpx.ReadError),
    (httpcore.WriteError, httpx.WriteError),
    (httpcore.NetworkError, httpx.NetworkError),
    (httpcore.UnsupportedProtocol, httpx.UnsupportedProtocol),
    (httpcore.LocalProtocolError, httpx.LocalProtocolError),
    (httpcore.RemoteProtocolError, httpx.RemoteProtocolError),
    (httpcore.ProtocolError, httpx.ProtocolError),
)


@contextlib.contextmanager
def _map_httpcore_exceptions() -> Iterator[None]:
    try:
        yield
    except Exception as error:
        for core_type, httpx_type in _EXCEPTION_MAP:
            if isinstance(error, core_type):
                raise httpx_type(str(error)) from error
        raise


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream: AsyncIterable[bytes]) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        with _map_httpcore_exceptions():
            async for part in self._stream:
                yield part

    async def aclose(self) -> None:
        close = getattr(self._stream, "aclose", None)
        if close is not None:
            await close()


class GuardedTransport(httpx.AsyncBaseTransport):
    """httpx transport whose connections all pass ``GuardedBackend``.

    No proxy and no environment lookup: the pool connects straight to the checked address.
    """

    def __init__(
        self,
        policy: AddressPolicy,
        *,
        http2: bool = True,
        max_connections: int = 200,
        max_keepalive_connections: int = 50,
        keepalive_expiry: float = KEEPALIVE_EXPIRY_SECONDS,
    ) -> None:
        # httpcore's own defaults (10 connections, idle connections kept forever) would cap a
        # pod at ten fetches in flight.
        self._pool = httpcore.AsyncConnectionPool(
            network_backend=GuardedBackend(policy),
            http2=http2,
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
            keepalive_expiry=keepalive_expiry,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not isinstance(request.stream, httpx.AsyncByteStream):
            raise TypeError("GuardedTransport needs an async request body")
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with _map_httpcore_exceptions():
            core_response = await self._pool.handle_async_request(core_request)
        if not isinstance(core_response.stream, AsyncIterable):  # pragma: no cover - async pool
            raise TypeError("httpcore returned a synchronous response stream")
        return httpx.Response(
            status_code=core_response.status,
            headers=core_response.headers,
            stream=_ResponseStream(core_response.stream),
            extensions=core_response.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()
