"""The SSRF-guarded egress proxy every browser is launched behind.

A browser runs page JavaScript, redirects and sub-requests the service cannot see one by one, so
the guard sits below it: the browser's only way out is this local proxy (``127.0.0.1``, random
port). It answers HTTP/1.1 ``CONNECT host:port`` (HTTPS and anything else tunnelled) and
absolute-form plain ``http://`` requests. For each connection it resolves the host once with
``resolve_allowed`` (the same check as the fast mode's ``GuardedBackend``) and dials the first
allowed address that answers, so a DNS answer cannot change between check and connect. TLS stays
end to end: the proxy relays bytes and never looks inside a tunnel.

A plain request is the only one of its connection: only its head and the body its
``Content-Length`` declares go to the origin (a chunked or otherwise ambiguous request is
refused), and the response head tells the browser to close the connection.

One proxy serves one browser process (or the media children of a process, ``media.pool``).
Every browser page gets its own ``ProxyContext`` from ``open_context()`` with random credentials,
which the browser sends as ``Proxy-Authorization``. The context counts the bytes of its page
(both directions) up to its cap (``BROWSER_MAX_BYTES``, or what ``open_context`` is given), the
hosts it reached and the requests that were refused, and closing it ends its tunnels. Requests
without the credentials of an open context get ``407``.

Refusals answer ``403`` (forbidden address, byte cap), ``502`` (DNS or connect failure), ``400``
(malformed request), ``407`` (credentials) or ``503`` (too many connections), always with
``Connection: close``. They are counted in ``text_extraction_browser_proxy_refused_total`` and
logged with the host only, never a URL. A request without any credentials is not a refusal: a
browser sends its first request so and its credentials only after the ``407`` challenge, so that
answer is neither counted nor logged. Wrong credentials count as ``auth``.
"""

import asyncio
import base64
import contextlib
import hmac
import secrets
import socket
from collections.abc import Callable, Coroutine
from ipaddress import IPv4Address, IPv6Address
from typing import Any, Final
from weakref import WeakKeyDictionary

import structlog
from prometheus_client import CollectorRegistry, Counter

from text_extraction.netguard import BlockedAddressError, is_public_address, resolve_allowed
from text_extraction.settings import Settings

_log = structlog.get_logger(__name__)

MAX_HEAD_BYTES: Final = 64 * 1024  # a request head larger than this is refused
HEAD_TIMEOUT_SECONDS: Final = 15.0  # the head must be complete this long after the connection
MAX_CONNECTIONS: Final = 512  # open connections over all contexts
MAX_HOSTS: Final = 200  # hosts a context remembers
MAX_REFUSALS: Final = 50  # refusals a context remembers
CONNECT_TIMEOUT_SECONDS: Final = 10.0  # one address
DNS_TIMEOUT_SECONDS: Final = 10.0  # the lookup of one host
TUNNEL_GRACE_SECONDS: Final = 5.0  # added to the longest render deadline
_CHUNK_BYTES: Final = 64 * 1024
_SEND_TIMEOUT_SECONDS: Final = 2.0  # a client that does not read its refusal is dropped

type _Address = IPv4Address | IPv6Address
type _Policy = Callable[[_Address], bool]

_STATUS_PHRASES: Final = {
    400: "Bad Request",
    403: "Forbidden",
    407: "Proxy Authentication Required",
    502: "Bad Gateway",
    503: "Service Unavailable",
}
_BAD_HOST_CHARS: Final = frozenset(' /\\?#@[]"<>')
# The hop-by-hop and proxy headers that are not forwarded to the target of a plain request.
_NOT_FORWARDED: Final = frozenset(
    {"host", "connection", "proxy-connection", "proxy-authorization", "keep-alive"}
)
# The same in a response head: the browser must see "Connection: close" and no keep-alive.
_NOT_RETURNED: Final = frozenset({b"connection", b"proxy-connection", b"keep-alive"})


def host_header(host: str, port: int) -> str:
    """The ``Host`` header of a forwarded plain request: port 80 goes unnamed, as clients send it
    (a server that builds absolute redirects from ``Host`` would otherwise answer
    ``https://host:80/``), and an IPv6 literal is bracketed."""
    name = f"[{host}]" if ":" in host else host
    return name if port == 80 else f"{name}:{port}"


class ProxyContext:
    """What one browser page did through the proxy; created by ``EgressProxy.open_context``."""

    def __init__(self, max_bytes: int, on_close: Callable[["ProxyContext"], None]) -> None:
        self.username = secrets.token_hex(8)
        self.password = secrets.token_urlsafe(24)
        self.bytes = 0  # relayed in both directions
        self.capped = False  # the byte limit was passed; new connections are refused
        self._max_bytes = max_bytes
        self._on_close = on_close
        self._hosts: set[str] = set()
        self._refused: list[tuple[str, str]] = []
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def max_bytes(self) -> int:
        """The context's byte cap."""
        return self._max_bytes

    @property
    def hosts(self) -> frozenset[str]:
        """Hosts the context connected to, lower case (at most 200)."""
        return frozenset(self._hosts)

    @property
    def refused(self) -> tuple[tuple[str, str], ...]:
        """``(host, reason)`` of the requests that were refused, oldest first (at most 50)."""
        return tuple(self._refused)

    def close(self) -> None:
        """Forget the credentials and end every tunnel of this context."""
        self._on_close(self)
        for task in list(self._tasks):
            task.cancel()

    def _count(self, size: int) -> bool:
        """Count relayed bytes; False once the limit is passed (then ``capped`` is set)."""
        self.bytes += size
        if self.bytes > self._max_bytes:
            self.capped = True
        return not self.capped

    def _saw(self, host: str) -> None:
        if len(self._hosts) < MAX_HOSTS:
            self._hosts.add(host)

    def _refuse(self, host: str, reason: str) -> None:
        if len(self._refused) < MAX_REFUSALS:
            self._refused.append((host, reason))


class _Refusal(Exception):  # noqa: N818 - not an error, an answer
    def __init__(
        self,
        status: int,
        reason: str,
        host: str | None = None,
        *,
        challenge: bool = False,
        quiet: bool = False,
    ) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.host = host
        self.challenge = challenge
        self.quiet = quiet  # answered, but not counted or logged


class _Request:
    """A parsed proxy request: a ``CONNECT`` (``path`` is None) or a plain ``http://`` one."""

    def __init__(
        self,
        method: str,
        host: str,
        port: int,
        path: str | None,
        headers: list[str],
        authorization: str | None,
        rest: bytes,
        body_limit: int = 0,
    ) -> None:
        self.method = method
        self.host = host
        self.port = port
        self.path = path
        self.headers = headers  # the header lines as received
        self.authorization = authorization
        self.rest = rest  # bytes that arrived behind the head
        # Bytes of request body a plain request may send: its Content-Length.
        self.body_limit = body_limit


def _split_authority(authority: str, default_port: int | None) -> tuple[str, int]:
    """``host``, ``host:port``, ``[v6]`` or ``[v6]:port`` to a lower-case host and a port."""
    if authority.startswith("["):
        host, closing, tail = authority[1:].partition("]")
        if not closing or (tail and not tail.startswith(":")):
            raise _Refusal(400, "bad_request")
        try:
            IPv6Address(host)
        except ValueError:
            raise _Refusal(400, "bad_request") from None
        port_text = tail[1:]
    else:
        host, _, port_text = authority.partition(":")
        if not host or not host.isascii() or not host.isprintable() or set(host) & _BAD_HOST_CHARS:
            raise _Refusal(400, "bad_request")
    if port_text:
        if not (port_text.isascii() and port_text.isdecimal()) or not 1 <= int(port_text) <= 65535:
            raise _Refusal(400, "bad_request")
        return host.lower(), int(port_text)
    if default_port is None:
        raise _Refusal(400, "bad_request")
    return host.lower(), default_port


def _parse_head(head: bytes, rest: bytes) -> _Request:
    text = head.decode("latin-1")
    lines = text.split("\r\n")
    if any("\r" in line or "\n" in line for line in lines):  # no header smuggling
        raise _Refusal(400, "bad_request")
    parts = lines[0].split(" ")
    if len(parts) != 3 or parts[2] not in ("HTTP/1.0", "HTTP/1.1"):
        raise _Refusal(400, "bad_request")
    method, target, _ = parts
    if not (method.isascii() and method.isalpha() and method.isupper()):
        raise _Refusal(400, "bad_request")

    headers: list[str] = []
    authorization: str | None = None
    lengths: list[str] = []
    chunked = False
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        if not colon or not name or not name.isascii() or not name.isprintable() or " " in name:
            raise _Refusal(400, "bad_request")
        headers.append(line)
        if name.lower() == "proxy-authorization" and authorization is None:
            authorization = value.strip()
        elif name.lower() == "content-length":
            lengths.append(value.strip())
        elif name.lower() == "transfer-encoding":
            chunked = True

    if method == "CONNECT":
        host, port = _split_authority(target, None)
        return _Request(method, host, port, None, headers, authorization, rest)
    if target[:7].lower() != "http://":  # not a proxy request, or https without CONNECT
        raise _Refusal(400, "bad_request")
    remainder = target[7:]
    end = next((i for i, char in enumerate(remainder) if char in "/?#"), len(remainder))
    authority, path = remainder[:end], remainder[end:].split("#", 1)[0]
    host, port = _split_authority(authority, 80)
    if not path.startswith("/"):
        path = "/" + path
    # One request per connection (see EgressProxy._tunnel): only the head and the declared body
    # are passed on, so the framing must be unambiguous. A body without a Content-Length (chunked)
    # has no end the proxy could find, and browsers frame plain HTTP/1.1 uploads with a length.
    if chunked or len(lengths) > 1:
        raise _Refusal(400, "bad_request")
    if lengths and not (lengths[0].isascii() and lengths[0].isdecimal() and len(lengths[0]) <= 18):
        raise _Refusal(400, "bad_request")
    body_limit = int(lengths[0]) if lengths else 0
    return _Request(method, host, port, path, headers, authorization, rest, body_limit)


class _Metrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        self.refused = Counter(
            "text_extraction_browser_proxy_refused_total",
            "Requests the browser egress proxy refused, by reason.",
            ["reason"],
            registry=registry,
        )


_METRICS: WeakKeyDictionary[CollectorRegistry, _Metrics] = WeakKeyDictionary()


def _metrics_for(registry: CollectorRegistry) -> _Metrics:
    metrics = _METRICS.get(registry)
    if metrics is None:
        metrics = _Metrics(registry)
        _METRICS[registry] = metrics
    return metrics


def tunnel_lifetime(settings: Settings) -> float:
    """Seconds a tunnel or plain connection may stay open: the longest render deadline
    (``BROWSER_TIMEOUT_SECONDS`` or ``BROWSER_UNBLOCK_TIMEOUT_SECONDS``) plus a grace."""
    deadline = max(settings.browser_timeout_seconds, settings.browser_unblock_timeout_seconds)
    return deadline + TUNNEL_GRACE_SECONDS


class EgressProxy:
    """The local proxy of one browser process; see the module docstring.

    ``policy`` decides which addresses may be dialed: by default only public ones, or any when
    ``FETCH_ALLOW_PRIVATE_NETWORKS`` is on (the rule of the fast mode's ``Fetcher``). The timing
    and size keywords exist for tests.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        policy: _Policy | None = None,
        registry: CollectorRegistry,
        head_timeout: float = HEAD_TIMEOUT_SECONDS,
        tunnel_timeout: float | None = None,
        dns_timeout: float = DNS_TIMEOUT_SECONDS,
        max_connections: int = MAX_CONNECTIONS,
    ) -> None:
        if policy is None:
            policy = (
                (lambda _address: True)
                if settings.fetch_allow_private_networks
                else is_public_address
            )
        self._policy = policy
        self._max_bytes = settings.browser_max_bytes
        self._head_timeout = head_timeout
        self._tunnel_timeout = (
            tunnel_timeout if tunnel_timeout is not None else tunnel_lifetime(settings)
        )
        self._dns_timeout = dns_timeout
        self._max_connections = max_connections
        self._metrics = _metrics_for(registry)
        self._server: asyncio.Server | None = None
        self._contexts: dict[str, ProxyContext] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def tunnel_timeout(self) -> float:
        """Seconds a tunnel or plain connection may stay open, from its start."""
        return self._tunnel_timeout

    async def start(self) -> None:
        """Listen on ``127.0.0.1`` at a free port."""
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def stop(self) -> None:
        """Stop listening, end every tunnel and forget every context."""
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        for context in list(self._contexts.values()):
            context.close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(2):
                await server.wait_closed()

    @property
    def url(self) -> str:
        """The proxy URL for the browser's launch options (without credentials)."""
        if self._server is None:
            raise RuntimeError("the proxy is not running")
        sock = self._server.sockets[0]
        return f"http://127.0.0.1:{sock.getsockname()[1]}"

    def open_context(self, max_bytes: int | None = None) -> ProxyContext:
        """New credentials, new counters: one per browser page, or one per media child call.
        ``max_bytes`` caps the context's bytes (default ``BROWSER_MAX_BYTES``)."""
        context = ProxyContext(self._max_bytes if max_bytes is None else max_bytes, self._forget)
        self._contexts[context.username] = context
        return context

    def _forget(self, context: ProxyContext) -> None:
        self._contexts.pop(context.username, None)

    # --- one connection ---

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is None or len(self._tasks) >= self._max_connections:
            _log.warning("browser_proxy_connection_limit", limit=self._max_connections)
            await self._send(writer, 503)
            writer.close()
            return
        self._tasks.add(task)
        context: ProxyContext | None = None
        try:
            request = await self._read_request(reader)
            context = self._authenticate(request.authorization)
            context._tasks.add(task)
            if context.capped:
                raise _Refusal(403, "bytes_cap", request.host)
            await self._tunnel(context, request, reader, writer)
        except _Refusal as refusal:
            if not refusal.quiet:
                self._record(context, refusal)
            await self._send(writer, refusal.status, refusal.challenge)
        except OSError:  # the browser went away, or the tunnel ended
            pass
        finally:
            self._tasks.discard(task)
            if context is not None:
                context._tasks.discard(task)
            writer.close()

    async def _read_request(self, reader: asyncio.StreamReader) -> _Request:
        buffer = b""
        try:
            async with asyncio.timeout(self._head_timeout):
                while (end := buffer.find(b"\r\n\r\n")) < 0:
                    if len(buffer) > MAX_HEAD_BYTES:
                        raise _Refusal(400, "bad_request")
                    chunk = await reader.read(_CHUNK_BYTES)
                    if not chunk:
                        if not buffer:  # a connection opened and dropped, as browsers do
                            raise ConnectionResetError
                        raise _Refusal(400, "bad_request")
                    buffer += chunk
        except TimeoutError:
            raise _Refusal(400, "bad_request") from None
        if end + 4 > MAX_HEAD_BYTES:
            raise _Refusal(400, "bad_request")
        return _parse_head(buffer[:end], buffer[end + 4 :])

    def _authenticate(self, authorization: str | None) -> ProxyContext:
        if authorization is None:  # the browser asks for the challenge first: not a refusal
            raise _Refusal(407, "auth", challenge=True, quiet=True)
        refusal = _Refusal(407, "auth", challenge=True)
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "basic":
            raise refusal
        try:
            decoded = base64.b64decode(token.strip(), validate=True).decode("utf-8")
        except ValueError:
            raise refusal from None
        username, colon, password = decoded.partition(":")
        context = self._contexts.get(username)
        if (
            not colon
            or context is None
            or not hmac.compare_digest(password.encode(), context.password.encode())
        ):
            raise refusal
        return context

    async def _tunnel(
        self,
        context: ProxyContext,
        request: _Request,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        established = False
        remote: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(self._tunnel_timeout):
                remote_reader, remote = await self._dial(request.host, request.port)
                context._saw(request.host)
                remaining = 0
                if request.path is None:
                    writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    first = request.rest
                else:
                    kept = [
                        line
                        for line in request.headers
                        if line.partition(":")[0].lower() not in _NOT_FORWARDED
                    ]
                    # The connection and the Host header are pinned to the target the guard
                    # checked, whatever the client sent.
                    kept += [
                        f"Host: {host_header(request.host, request.port)}",
                        "Connection: close",
                    ]
                    head = f"{request.method} {request.path} HTTP/1.1\r\n" + "\r\n".join(kept)
                    body = request.rest
                    body = body[: request.body_limit]
                    remaining = request.body_limit - len(body)
                    first = head.encode("latin-1") + b"\r\n\r\n" + body
                established = True
                if first:
                    if not context._count(len(first)):
                        return
                    remote.write(first)
                await writer.drain()
                await remote.drain()
                if request.path is None:
                    await self._relay(
                        self._pump(context, reader, remote),
                        self._pump(context, remote_reader, writer),
                    )
                else:
                    # One request per connection: whatever the client sends after the request's
                    # body is dropped, and the browser is told not to reuse the connection.
                    await self._relay(
                        self._pump_request(context, reader, remote, remaining),
                        self._pump_response(context, remote_reader, writer),
                    )
        except TimeoutError:
            if not established:
                raise _Refusal(502, "connect_error", request.host) from None
        finally:
            if remote is not None:
                remote.close()

    async def _dial(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            async with asyncio.timeout(self._dns_timeout):
                addresses = await resolve_allowed(host, port, self._policy)
        except BlockedAddressError:
            raise _Refusal(403, "blocked_address", host) from None
        except (socket.gaierror, UnicodeError, TimeoutError):
            raise _Refusal(502, "dns_error", host) from None
        for address in addresses:  # the first that answers; IPv6 may be unreachable here
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT_SECONDS):
                    return await asyncio.open_connection(str(address), port)
            except (OSError, TimeoutError):
                continue
        raise _Refusal(502, "connect_error", host)

    @staticmethod
    async def _relay(
        client_to_remote: Coroutine[Any, Any, None], remote_to_client: Coroutine[Any, Any, None]
    ) -> None:
        """Run both directions until either side ends or the context passes its byte limit."""
        pumps = [asyncio.create_task(client_to_remote), asyncio.create_task(remote_to_client)]
        try:
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)

    @staticmethod
    async def _pump(
        context: ProxyContext, source: asyncio.StreamReader, sink: asyncio.StreamWriter
    ) -> None:
        while chunk := await source.read(_CHUNK_BYTES):
            if not context._count(len(chunk)):  # past the limit: this chunk is not passed on
                return
            sink.write(chunk)
            await sink.drain()

    @staticmethod
    async def _pump_request(
        context: ProxyContext,
        source: asyncio.StreamReader,
        sink: asyncio.StreamWriter,
        remaining: int,
    ) -> None:
        """Pass on the rest of a plain request's body (``remaining`` bytes).

        Then the client's stream is only read, so that its end is noticed, and dropped.
        """
        while chunk := await source.read(_CHUNK_BYTES):
            chunk, remaining = chunk[:remaining], remaining - len(chunk[:remaining])
            if chunk:
                if not context._count(len(chunk)):
                    return
                sink.write(chunk)
                await sink.drain()

    @classmethod
    async def _pump_response(
        cls, context: ProxyContext, source: asyncio.StreamReader, sink: asyncio.StreamWriter
    ) -> None:
        """Relay a plain response with ``Connection: close`` in place of its connection headers."""
        buffer = b""
        while True:  # interim 1xx heads come before the final one
            while (end := buffer.find(b"\r\n\r\n")) < 0:
                if len(buffer) > MAX_HEAD_BYTES:
                    return
                chunk = await source.read(_CHUNK_BYTES)
                if not chunk:  # the origin ended mid-head: pass on what it sent
                    if buffer and context._count(len(buffer)):
                        sink.write(buffer)
                        await sink.drain()
                    return
                buffer += chunk
            lines = buffer[:end].split(b"\r\n")
            buffer = buffer[end + 4 :]
            kept = [
                line
                for line in lines[1:]
                if line.partition(b":")[0].strip().lower() not in _NOT_RETURNED
            ]
            head = b"\r\n".join([lines[0], *kept, b"Connection: close"]) + b"\r\n\r\n"
            if not context._count(len(head)):
                return
            sink.write(head)
            status = lines[0].split(b" ")[1:2]
            if not (status and status[0].startswith(b"1") and status[0] != b"101"):
                break
        if buffer:
            if not context._count(len(buffer)):
                return
            sink.write(buffer)
        await sink.drain()
        await cls._pump(context, source, sink)

    # --- refusals ---

    def _record(self, context: ProxyContext | None, refusal: _Refusal) -> None:
        self._metrics.refused.labels(reason=refusal.reason).inc()
        if context is not None and refusal.host is not None:
            context._refuse(refusal.host, refusal.reason)
        _log.info("browser_proxy_refused", host=refusal.host, reason=refusal.reason)

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, status: int, challenge: bool = False) -> None:
        lines = [f"HTTP/1.1 {status} {_STATUS_PHRASES[status]}", "Content-Length: 0"]
        if challenge:
            lines.append('Proxy-Authenticate: Basic realm="browser"')
        lines.append("Connection: close")
        with contextlib.suppress(OSError, TimeoutError):
            async with asyncio.timeout(_SEND_TIMEOUT_SECONDS):
                writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
                await writer.drain()
