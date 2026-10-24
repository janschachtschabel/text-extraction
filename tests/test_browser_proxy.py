import asyncio
import base64
import contextlib
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from ipaddress import IPv4Address, IPv6Address
from typing import Any

import pytest
from prometheus_client import CollectorRegistry

from text_extraction.browser_proxy import EgressProxy, ProxyContext, host_header
from text_extraction.settings import Settings

Addr = IPv4Address | IPv6Address
Reply = tuple[int, dict[str, str], bytes]
Reader = asyncio.StreamReader
Writer = asyncio.StreamWriter


def allow_all(_ip: Addr) -> bool:
    return True


def only_loopback(ip: Addr) -> bool:
    return str(ip) == "127.0.0.1"


async def echo_server() -> asyncio.Server:
    """Echoes raw bytes; to a request that starts with GET it answers 200 with the head as body."""

    async def handle(reader: Reader, writer: Writer) -> None:
        try:
            first = await reader.read(65536)
            if first.startswith(b"GET "):
                while b"\r\n\r\n" not in first:
                    first += await reader.read(65536)
                head = b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(
                    first
                )
                writer.write(head + first)
            else:
                while first:
                    writer.write(first)
                    await writer.drain()
                    first = await reader.read(65536)
        except ConnectionError:
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


class KeepAliveOrigin:
    """Answers the first request it receives with ``response`` and then keeps the connection open,
    ignoring ``Connection: close``; records every byte it receives."""

    def __init__(self, response: bytes) -> None:
        self.response = response
        self.received = b""
        self.closed = asyncio.Event()
        self.server: asyncio.Server | None = None

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return port_of(self.server)

    async def stop(self) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader: Reader, writer: Writer) -> None:
        answered = False
        try:
            while chunk := await reader.read(65536):
                self.received += chunk
                if not answered and b"\r\n\r\n" in self.received:
                    answered = True
                    writer.write(self.response)
                    await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()
            self.closed.set()


def port_of(server: asyncio.Server) -> int:
    return int(server.sockets[0].getsockname()[1])


def basic(context: ProxyContext, password: str | None = None) -> str:
    pair = f"{context.username}:{context.password if password is None else password}"
    return "Basic " + base64.b64encode(pair.encode()).decode()


@contextlib.asynccontextmanager
async def running(
    *,
    policy: Callable[[Addr], bool] | None = None,
    registry: CollectorRegistry | None = None,
    settings: Settings | None = None,
    **kwargs: Any,
) -> AsyncIterator[tuple[EgressProxy, int, CollectorRegistry]]:
    registry = registry or CollectorRegistry()
    proxy = EgressProxy(
        settings or Settings(auth_mode="none"), policy=policy, registry=registry, **kwargs
    )
    echo = await echo_server()
    await proxy.start()
    try:
        yield proxy, port_of(echo), registry
    finally:
        await proxy.stop()
        echo.close()
        await echo.wait_closed()


async def talk(proxy: EgressProxy, raw: bytes) -> tuple[Reader, Writer]:
    host, _, port = proxy.url.removeprefix("http://").partition(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(raw)
    await writer.drain()
    return reader, writer


async def read_head(reader: Reader) -> Reply:
    raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    lines = raw.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        if line:
            name, _, value = line.partition(":")
            headers[name.lower()] = value.strip()
    return int(lines[0].split(" ")[1]), headers, b""


def connect_request(target: str, auth: str | None) -> bytes:
    head = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
    if auth is not None:
        head += f"Proxy-Authorization: {auth}\r\n"
    return (head + "\r\n").encode()


async def open_tunnel(
    proxy: EgressProxy, context: ProxyContext, target: str
) -> tuple[int, Reader, Writer]:
    reader, writer = await talk(proxy, connect_request(target, basic(context)))
    status, _, _ = await read_head(reader)
    return status, reader, writer


def refused(registry: CollectorRegistry, reason: str) -> float:
    value = registry.get_sample_value(
        "text_extraction_browser_proxy_refused_total", {"reason": reason}
    )
    return value or 0.0


def run_scenario(scenario: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(asyncio.wait_for(scenario(), 30))


def fake_getaddrinfo(
    monkeypatch: pytest.MonkeyPatch, answer: list[str] | type[socket.gaierror]
) -> None:
    async def getaddrinfo(host: str, port: int, **_: object) -> list[Any]:
        if isinstance(answer, list):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in answer]
        raise socket.gaierror(socket.EAI_NONAME, "unknown")

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)


def test_connect_tunnel_relays_bytes() -> None:
    async def scenario() -> None:
        async with running(policy=allow_all) as (proxy, echo_port, _):
            context = proxy.open_context()
            status, reader, writer = await open_tunnel(proxy, context, f"127.0.0.1:{echo_port}")
            assert status == 200
            writer.write(b"hello")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(5), 5) == b"hello"
            assert context.bytes == 10  # both directions
            assert context.hosts == frozenset({"127.0.0.1"})
            writer.close()

    run_scenario(scenario)


def test_plain_http_request_is_forwarded_with_pinned_host() -> None:
    async def scenario() -> None:
        async with running(policy=allow_all) as (proxy, echo_port, _):
            context = proxy.open_context()
            request = (
                f"GET http://127.0.0.1:{echo_port}/a/b?q=1 HTTP/1.1\r\n"
                "Host: evil.example\r\n"
                f"Proxy-Authorization: {basic(context)}\r\n"
                "Proxy-Connection: keep-alive\r\n"
                "Connection: keep-alive\r\n"
                "X-Test: 1\r\n\r\n"
            ).encode()
            reader, writer = await talk(proxy, request)
            raw = await asyncio.wait_for(reader.read(), 5)  # until the proxy closes
            head, _, body = raw.partition(b"\r\n\r\n")
            assert head.startswith(b"HTTP/1.1 200")
            text = body.decode()
            assert text.startswith("GET /a/b?q=1 HTTP/1.1\r\n")
            assert f"Host: 127.0.0.1:{echo_port}\r\n" in text
            assert "Connection: close\r\n" in text
            assert "X-Test: 1\r\n" in text
            for dropped in (
                "evil.example",
                "Proxy-Authorization",
                "Proxy-Connection",
                "keep-alive",
            ):
                assert dropped not in text
            assert context.hosts == frozenset({"127.0.0.1"})
            writer.close()

    run_scenario(scenario)


@pytest.mark.parametrize(
    ("host", "port", "expected"),
    [
        ("example.org", 80, "example.org"),
        ("example.org", 8080, "example.org:8080"),
        ("::1", 80, "[::1]"),
        ("2001:db8::1", 8080, "[2001:db8::1]:8080"),
    ],
)
def test_host_header_leaves_port_80_unnamed_and_brackets_ipv6(
    host: str, port: int, expected: str
) -> None:
    assert host_header(host, port) == expected


def test_plain_http_to_an_ipv6_literal_is_checked_not_misparsed() -> None:
    async def scenario() -> None:
        async with running() as (proxy, _, registry):
            context = proxy.open_context()
            request = (
                f"GET http://[::1]:8080/ HTTP/1.1\r\nProxy-Authorization: {basic(context)}\r\n\r\n"
            )
            reader, writer = await talk(proxy, request.encode())
            status, _, _ = await read_head(reader)
            assert status == 403
            assert context.refused == (("::1", "blocked_address"),)
            assert refused(registry, "blocked_address") == 1
            writer.close()

    run_scenario(scenario)


def test_missing_or_unknown_credentials_get_407() -> None:
    async def scenario() -> None:
        async with running(policy=allow_all) as (proxy, echo_port, registry):
            context = proxy.open_context()
            target = f"127.0.0.1:{echo_port}"
            attempts = [
                None,
                basic(context, "wrong"),
                "Basic " + base64.b64encode(b"nobody:" + context.password.encode()).decode(),
                "Basic not-base64!",
                "Bearer " + context.password,
            ]
            for auth in attempts:
                reader, writer = await talk(proxy, connect_request(target, auth))
                status, headers, _ = await read_head(reader)
                assert status == 407, auth
                assert headers["proxy-authenticate"] == 'Basic realm="browser"'
                assert headers["connection"] == "close"
                assert await reader.read() == b""
                writer.close()
            assert refused(registry, "auth") == len(attempts)
            assert context.bytes == 0
            assert context.refused == ()  # nobody was identified, so nothing to record

            context.close()  # a closed context is unknown again
            reader, writer = await talk(proxy, connect_request(target, basic(context)))
            assert (await read_head(reader))[0] == 407
            writer.close()

    run_scenario(scenario)


@pytest.mark.parametrize("target", ["127.0.0.1:80", "[::1]:80", "169.254.169.254:80"])
def test_private_address_is_refused(target: str) -> None:
    async def scenario() -> None:
        async with running() as (proxy, _, registry):  # default policy: public addresses only
            context = proxy.open_context()
            status, reader, writer = await open_tunnel(proxy, context, target)
            assert status == 403
            assert await reader.read() == b""
            assert refused(registry, "blocked_address") == 1
            host = target.rsplit(":", 1)[0].strip("[]")
            assert context.refused == ((host, "blocked_address"),)
            assert context.hosts == frozenset()
            writer.close()

    run_scenario(scenario)


def test_the_default_policy_follows_the_private_network_setting() -> None:
    async def scenario() -> None:
        settings = Settings(auth_mode="none", fetch_allow_private_networks=True)
        async with running(settings=settings) as (proxy, echo_port, _):
            context = proxy.open_context()
            status, _, writer = await open_tunnel(proxy, context, f"127.0.0.1:{echo_port}")
            assert status == 200
            writer.close()

    run_scenario(scenario)


def test_name_resolving_to_private_ip_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async with running() as (proxy, _, registry):
            fake_getaddrinfo(monkeypatch, ["93.184.216.34", "10.0.0.1"])  # mixed answer
            context = proxy.open_context()
            status, _, writer = await open_tunnel(proxy, context, "rebind.example:443")
            assert status == 403
            assert refused(registry, "blocked_address") == 1
            assert context.refused == (("rebind.example", "blocked_address"),)
            writer.close()

    run_scenario(scenario)


def test_name_that_does_not_resolve_is_a_502(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async with running() as (proxy, _, registry):
            fake_getaddrinfo(monkeypatch, socket.gaierror)
            context = proxy.open_context()
            status, _, writer = await open_tunnel(proxy, context, "nowhere.example:443")
            assert status == 502
            assert refused(registry, "dns_error") == 1
            assert context.refused == (("nowhere.example", "dns_error"),)
            writer.close()

    run_scenario(scenario)


def test_unreachable_target_is_a_502() -> None:
    async def scenario() -> None:
        closed = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        dead_port = port_of(closed)
        closed.close()
        await closed.wait_closed()
        async with running(policy=allow_all) as (proxy, _, registry):
            context = proxy.open_context()
            status, _, writer = await open_tunnel(proxy, context, f"127.0.0.1:{dead_port}")
            assert status == 502
            assert refused(registry, "connect_error") == 1
            writer.close()

    run_scenario(scenario)


def test_bytes_are_counted_per_context_and_capped() -> None:
    async def scenario() -> None:
        settings = Settings(auth_mode="none", browser_max_bytes=1_000_000)
        async with running(policy=allow_all, settings=settings) as (proxy, echo_port, registry):
            target = f"127.0.0.1:{echo_port}"
            context, other = proxy.open_context(), proxy.open_context()
            status, reader, writer = await open_tunnel(proxy, context, target)
            assert status == 200

            async def send() -> None:
                with contextlib.suppress(ConnectionError):
                    writer.write(b"x" * 700_000)
                    await writer.drain()

            sender = asyncio.create_task(send())
            received = await asyncio.wait_for(reader.read(), 10)  # ends when the proxy cuts it
            await sender
            assert len(received) < 700_000  # 700 kB out plus the echo passes the 1 MB cap
            assert context.capped
            assert context.bytes > 1_000_000
            writer.close()

            status, _, writer2 = await open_tunnel(proxy, context, target)
            assert status == 403
            assert refused(registry, "bytes_cap") == 1
            assert context.refused == (("127.0.0.1", "bytes_cap"),)
            writer2.close()

            status, _, writer3 = await open_tunnel(proxy, other, target)  # not affected
            assert status == 200
            assert not other.capped
            assert other.bytes == 0
            writer3.close()

    run_scenario(scenario)


def test_hosts_seen_and_refusals_are_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async with running(policy=only_loopback) as (proxy, echo_port, registry):
            fake_getaddrinfo(monkeypatch, ["127.0.0.1"])
            context = proxy.open_context()
            status, _, writer = await open_tunnel(proxy, context, f"Site.Test:{echo_port}")
            assert status == 200
            writer.close()
            for index in range(60):  # more refusals than the context keeps
                status, _, writer = await open_tunnel(proxy, context, f"10.0.0.{index}:80")
                assert status == 403
                writer.close()
            assert context.hosts == frozenset({"site.test"})  # lower case, refused hosts not in
            assert len(context.refused) == 50
            assert context.refused[0] == ("10.0.0.0", "blocked_address")
            assert context.refused[-1] == ("10.0.0.49", "blocked_address")
            assert refused(registry, "blocked_address") == 60  # the metric counts them all

    run_scenario(scenario)


def test_the_host_list_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async with running(policy=allow_all) as (proxy, echo_port, _):
            fake_getaddrinfo(monkeypatch, ["127.0.0.1"])
            context = proxy.open_context()
            for index in range(210):
                status, _, writer = await open_tunnel(proxy, context, f"h{index}.test:{echo_port}")
                assert status == 200
                writer.close()
            assert len(context.hosts) == 200

    run_scenario(scenario)


def test_closing_a_context_ends_its_tunnels() -> None:
    async def scenario() -> None:
        async with running(policy=allow_all) as (proxy, echo_port, _):
            target = f"127.0.0.1:{echo_port}"
            context, other = proxy.open_context(), proxy.open_context()
            _, reader, writer = await open_tunnel(proxy, context, target)
            _, other_reader, other_writer = await open_tunnel(proxy, other, target)
            context.close()
            assert await asyncio.wait_for(reader.read(), 5) == b""  # ended
            other_writer.write(b"still here")
            await other_writer.drain()
            assert await asyncio.wait_for(other_reader.readexactly(10), 5) == b"still here"
            writer.close()
            other_writer.close()

    run_scenario(scenario)


def test_stopping_the_proxy_ends_all_tunnels() -> None:
    async def scenario() -> None:
        async with running(policy=allow_all) as (proxy, echo_port, _):
            context = proxy.open_context()
            _, reader, writer = await open_tunnel(proxy, context, f"127.0.0.1:{echo_port}")
            await proxy.stop()
            assert await asyncio.wait_for(reader.read(), 5) == b""
            writer.close()

    run_scenario(scenario)


def test_oversized_or_slow_request_head_is_refused() -> None:
    async def scenario() -> None:
        async with running(policy=allow_all, head_timeout=0.2) as (proxy, _, registry):
            # no end of head within 64 KiB
            junk = b"GET http://example.org/ HTTP/1.1\r\nX-Junk: " + b"a" * 70_000
            reader, writer = await talk(proxy, junk)
            assert (await read_head(reader))[0] == 400
            writer.close()
            assert refused(registry, "bad_request") == 1

            # a head that never ends
            reader, writer = await talk(proxy, b"CONNECT example.org:443 HTTP/1.1\r\n")
            assert (await read_head(reader))[0] == 400
            writer.close()
            assert refused(registry, "bad_request") == 2

    run_scenario(scenario)


@pytest.mark.parametrize(
    "raw",
    [
        b"GARBAGE\r\n\r\n",
        b"GET /path HTTP/1.1\r\n\r\n",  # origin form: not a proxy request
        b"GET https://example.org/ HTTP/1.1\r\n\r\n",  # https needs CONNECT
        b"GET http://user@example.org/ HTTP/1.1\r\n\r\n",
        b"CONNECT example.org HTTP/1.1\r\n\r\n",  # no port
        b"CONNECT example.org:99999 HTTP/1.1\r\n\r\n",
        b"CONNECT example.org:443 HTTP/2\r\n\r\n",
        b"CONNECT example.org:443 HTTP/1.1\r\nno colon here\r\n\r\n",
        b"POST http://example.org/ HTTP/1.1\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\n",
        b"POST http://example.org/ HTTP/1.1\r\nContent-Length: x\r\n\r\n",
        (
            b"POST http://example.org/ HTTP/1.1\r\nContent-Length: 1\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
        ),
        b"POST http://example.org/ HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n",
        pytest.param(
            b"POST http://example.org/ HTTP/1.1\r\nContent-Length: " + b"9" * 5000 + b"\r\n\r\n",
            id="content-length-5000-digits",
        ),
        pytest.param(
            b"POST http://example.org/ HTTP/1.1\r\nContent-Length: " + b"1" * 19 + b"\r\n\r\n",
            id="content-length-19-digits",
        ),
        b"POST http://example.org/ HTTP/1.1\r\nContent-Length: -1\r\n\r\n",
    ],
)
def test_malformed_requests_are_400(raw: bytes) -> None:
    async def scenario() -> None:
        async with running(policy=allow_all) as (proxy, _, registry):
            context = proxy.open_context()
            # Put a valid credential into the head so that only the shape is at fault.
            head, _, rest = raw.partition(b"\r\n")
            raw_with_auth = (
                head + f"\r\nProxy-Authorization: {basic(context)}".encode() + b"\r\n" + rest
            )
            reader, writer = await talk(proxy, raw_with_auth)
            assert (await read_head(reader))[0] == 400
            writer.close()
            assert refused(registry, "bad_request") == 1

    run_scenario(scenario)


def test_a_tunnel_does_not_outlive_its_time_limit() -> None:
    async def scenario() -> None:
        async with running(policy=allow_all, tunnel_timeout=0.3) as (proxy, echo_port, _):
            context = proxy.open_context()
            _, reader, writer = await open_tunnel(proxy, context, f"127.0.0.1:{echo_port}")
            assert await asyncio.wait_for(reader.read(), 5) == b""  # idle, then cut
            writer.close()

    run_scenario(scenario)


def test_connections_over_the_limit_are_turned_away() -> None:
    async def scenario() -> None:
        async with running(policy=allow_all, max_connections=2) as (proxy, echo_port, _):
            context = proxy.open_context()
            target = f"127.0.0.1:{echo_port}"
            tunnels = [await open_tunnel(proxy, context, target) for _ in range(2)]
            status, _, writer = await open_tunnel(proxy, context, target)
            assert status == 503
            writer.close()
            _, _, first = tunnels[0]
            first.close()  # a slot frees up
            for _ in range(50):
                status, _, writer = await open_tunnel(proxy, context, target)
                writer.close()
                if status == 200:
                    break
                await asyncio.sleep(0.05)
            assert status == 200

    run_scenario(scenario)


def test_two_proxies_can_share_one_registry() -> None:
    async def scenario() -> None:
        registry = CollectorRegistry()
        async with (
            running(registry=registry) as (first, _, _),
            running(registry=registry) as (second, _, _),
        ):
            for proxy in (first, second):
                context = proxy.open_context()
                status, _, writer = await open_tunnel(proxy, context, "127.0.0.1:80")
                assert status == 403
                writer.close()
            assert refused(registry, "blocked_address") == 2

    run_scenario(scenario)


def test_contexts_get_distinct_random_credentials() -> None:
    async def scenario() -> None:
        async with running() as (proxy, _, _):
            contexts = [proxy.open_context() for _ in range(5)]
            assert len({c.username for c in contexts}) == 5
            assert len({c.password for c in contexts}) == 5
            assert all(len(c.password) >= 16 for c in contexts)

    run_scenario(scenario)


KEEP_ALIVE_RESPONSE = (
    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: keep-alive\r\n"
    b"Keep-Alive: timeout=60\r\nProxy-Connection: keep-alive\r\nX-Origin: yes\r\n\r\nok"
)


def test_a_plain_connection_carries_one_request_only() -> None:
    async def scenario() -> None:
        origin = KeepAliveOrigin(KEEP_ALIVE_RESPONSE)
        origin_port = await origin.start()
        try:
            async with running(policy=allow_all) as (proxy, _, _):
                context = proxy.open_context()
                auth = f"Proxy-Authorization: {basic(context)}\r\n"
                first = f"GET http://127.0.0.1:{origin_port}/one HTTP/1.1\r\n{auth}\r\n"
                reader, writer = await talk(proxy, first.encode())
                status, headers, _ = await read_head(reader)
                assert status == 200
                assert headers["connection"] == "close"  # the browser must not reuse the socket
                assert "keep-alive" not in headers
                assert "proxy-connection" not in headers
                assert headers["x-origin"] == "yes"
                assert await asyncio.wait_for(reader.readexactly(2), 5) == b"ok"

                # A client that reuses the connection anyway: the request goes nowhere.
                second = f"GET http://10.0.0.1/secret HTTP/1.1\r\nHost: 10.0.0.1\r\n{auth}\r\n"
                writer.write(second.encode())
                await writer.drain()
                writer.close()
                await asyncio.wait_for(origin.closed.wait(), 5)  # the proxy ended the connection
                received = origin.received.decode()
                assert received.count("GET ") == 1
                assert received.startswith("GET /one HTTP/1.1\r\n")
                assert "10.0.0.1" not in received
                assert "Proxy-Authorization" not in received
        finally:
            await origin.stop()

    run_scenario(scenario)


def test_a_plain_request_body_is_forwarded_counted_and_nothing_beyond_it() -> None:
    async def scenario() -> None:
        origin = KeepAliveOrigin(KEEP_ALIVE_RESPONSE)
        origin_port = await origin.start()
        try:
            async with running(policy=allow_all) as (proxy, _, _):
                context = proxy.open_context()
                head = (
                    f"POST http://127.0.0.1:{origin_port}/form HTTP/1.1\r\n"
                    f"Proxy-Authorization: {basic(context)}\r\nContent-Length: 5\r\n\r\nab"
                )
                reader, writer = await talk(proxy, head.encode())  # part of the body with the head
                writer.write(b"cde" + b"GET http://10.0.0.1/ HTTP/1.1\r\n\r\n")  # and beyond it
                await writer.drain()
                assert (await read_head(reader))[0] == 200
                writer.close()
                await asyncio.wait_for(origin.closed.wait(), 5)
                assert origin.received.endswith(b"\r\n\r\nabcde")
                assert b"10.0.0.1" not in origin.received
                assert context.bytes >= len(origin.received)  # the request, and the response too
        finally:
            await origin.stop()

    run_scenario(scenario)


def test_interim_responses_lose_their_keep_alive_too() -> None:
    async def scenario() -> None:
        interim = b"HTTP/1.1 100 Continue\r\nConnection: keep-alive\r\n\r\n"
        origin = KeepAliveOrigin(interim + KEEP_ALIVE_RESPONSE)
        origin_port = await origin.start()
        try:
            async with running(policy=allow_all) as (proxy, _, _):
                context = proxy.open_context()
                request = (
                    f"GET http://127.0.0.1:{origin_port}/ HTTP/1.1\r\n"
                    f"Proxy-Authorization: {basic(context)}\r\n\r\n"
                )
                reader, writer = await talk(proxy, request.encode())
                for expected in (100, 200):
                    status, headers, _ = await read_head(reader)
                    assert status == expected
                    assert headers["connection"] == "close"
                    assert "keep-alive" not in headers
                writer.close()
        finally:
            await origin.stop()

    run_scenario(scenario)


def test_a_stalled_dns_lookup_is_a_502_dns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async with running(dns_timeout=0.2) as (proxy, _, registry):

            async def stalled(host: str, port: int, **_: object) -> list[Any]:
                await asyncio.sleep(3600)
                return []

            monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", stalled)
            context = proxy.open_context()
            status, _, writer = await open_tunnel(proxy, context, "slow.example:443")
            assert status == 502
            assert refused(registry, "dns_error") == 1
            assert refused(registry, "connect_error") == 0
            assert context.refused == (("slow.example", "dns_error"),)
            writer.close()

    run_scenario(scenario)
