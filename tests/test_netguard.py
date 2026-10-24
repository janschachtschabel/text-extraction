import asyncio
import socket
from collections.abc import Callable, Coroutine, Iterable
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Any

import httpcore
import httpx
import pytest
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from text_extraction.netguard import (
    BlockedAddressError,
    GuardedBackend,
    GuardedTransport,
    InvalidUrlError,
    is_public_address,
    resolve_allowed,
    validate_url,
)

LOOPBACK = IPv4Address("127.0.0.1")
Addr = IPv4Address | IPv6Address
AddrInfo = list[tuple[int, int, int, str, tuple[Any, ...]]]


def allow_loopback_only(ip: Addr) -> bool:
    return ip == LOOPBACK


def addrinfo(*addresses: str) -> AddrInfo:
    """The shape of ``loop.getaddrinfo`` results for stream sockets."""
    result: AddrInfo = []
    for address in addresses:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        result.append((family, socket.SOCK_STREAM, 6, "", (address, 80)))
    return result


def fake_getaddrinfo(
    monkeypatch: pytest.MonkeyPatch, loop: asyncio.AbstractEventLoop, answer: AddrInfo
) -> None:
    async def getaddrinfo(host: str, port: int, **_: object) -> AddrInfo:
        return answer

    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)


class RecordingStream(httpcore.AsyncNetworkStream):
    pass


class RecordingBackend(httpcore.AsyncNetworkBackend):
    """Inner backend that records the host it is asked to connect to."""

    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.connected: list[tuple[str, int]] = []
        self._fail_on = fail_on or set()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - fixed by the httpcore interface
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connected.append((host, port))
        if host in self._fail_on:
            raise httpcore.ConnectError(f"cannot connect to {host}")
        return RecordingStream()


def run[T](test: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(test())


# --- validate_url ---


@pytest.mark.parametrize(
    "url", ["http://example.org/a?b=c", "https://example.org:8443/", "HTTP://x.org"]
)
def test_validate_url_accepts_http_and_https(url: str) -> None:
    assert validate_url(url) == url


def test_validate_url_accepts_a_url_of_exactly_2048_characters() -> None:
    url = "http://x.org/" + "a" * (2048 - len("http://x.org/"))

    assert validate_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "ftp://x",
        "http://",
        "http://user:pw@x.org/",
        "http://user@x.org/",
        "http://x.org/" + "a" * 2040,
        "http://x.org:0/",
        "http://x.org:65536/",
        "http://x.org:abc/",
        "javascript:alert(1)",
        "//x.org/",
        "",
    ],
)
def test_validate_url_rejects(url: str) -> None:
    with pytest.raises(InvalidUrlError):
        validate_url(url)


def test_the_2049_character_url_of_the_brief_is_rejected() -> None:
    url = "http://x.org/" + "a" * (2049 - len("http://x.org/"))
    assert len(url) == 2049

    with pytest.raises(InvalidUrlError):
        validate_url(url)


def test_invalid_url_error_is_a_value_error() -> None:
    assert issubclass(InvalidUrlError, ValueError)


# --- is_public_address ---


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "127.0.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",  # noqa: S104
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "2002:7f00:1::",
        "224.0.0.1",
        "ff02::1",
        "240.0.0.1",
        "192.0.2.1",
        "::",
        "64:ff9b::7f00:1",
        "::ffff:10.0.0.1",
        "2002:a9fe:a9fe::",
        "::7f00:1",
        "::a00:1",
        "fec0::1",
    ],
)
def test_private_addresses_are_not_public(address: str) -> None:
    assert is_public_address(ip_address(address)) is False


@pytest.mark.parametrize(
    "address",
    ["93.184.216.34", "2606:2800:220:1::", "::ffff:93.184.216.34", "2002:5db8:d822::"],
)
def test_public_addresses_are_public(address: str) -> None:
    assert is_public_address(ip_address(address)) is True


# --- GuardedBackend ---


def test_backend_refuses_loopback_literal() -> None:
    inner = RecordingBackend()
    backend = GuardedBackend(is_public_address, inner)

    with pytest.raises(BlockedAddressError):
        run(lambda: backend.connect_tcp("127.0.0.1", 80))
    assert inner.connected == []


def test_backend_refuses_ipv6_literal() -> None:
    inner = RecordingBackend()
    backend = GuardedBackend(is_public_address, inner)

    with pytest.raises(BlockedAddressError):
        run(lambda: backend.connect_tcp("::1", 80))
    assert inner.connected == []


def test_backend_refuses_mixed_dns_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    inner = RecordingBackend()
    backend = GuardedBackend(is_public_address, inner)

    async def scenario() -> None:
        fake_getaddrinfo(
            monkeypatch, asyncio.get_running_loop(), addrinfo("93.184.216.34", "10.0.0.1")
        )
        await backend.connect_tcp("rebind.example", 80)

    with pytest.raises(BlockedAddressError):
        run(scenario)
    assert inner.connected == []


def test_backend_connects_to_the_resolved_address_not_the_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inner = RecordingBackend()
    backend = GuardedBackend(is_public_address, inner)

    async def scenario() -> None:
        fake_getaddrinfo(
            monkeypatch, asyncio.get_running_loop(), addrinfo("93.184.216.34", "93.184.216.35")
        )
        await backend.connect_tcp("example.org", 8080)

    run(scenario)

    assert inner.connected == [("93.184.216.34", 8080)]


def test_backend_falls_back_to_the_next_vetted_address(monkeypatch: pytest.MonkeyPatch) -> None:
    inner = RecordingBackend(fail_on={"2606:2800:220:1::"})
    backend = GuardedBackend(is_public_address, inner)

    async def scenario() -> None:
        fake_getaddrinfo(
            monkeypatch,
            asyncio.get_running_loop(),
            addrinfo("2606:2800:220:1::", "93.184.216.34"),
        )
        await backend.connect_tcp("example.org", 80)

    run(scenario)

    assert inner.connected == [("2606:2800:220:1::", 80), ("93.184.216.34", 80)]


def test_backend_reraises_the_last_connect_error_when_all_addresses_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inner = RecordingBackend(fail_on={"93.184.216.34", "93.184.216.35"})
    backend = GuardedBackend(is_public_address, inner)

    async def scenario() -> None:
        fake_getaddrinfo(
            monkeypatch, asyncio.get_running_loop(), addrinfo("93.184.216.34", "93.184.216.35")
        )
        await backend.connect_tcp("example.org", 80)

    with pytest.raises(httpcore.ConnectError, match=r"93\.184\.216\.35"):
        run(scenario)


def test_backend_connects_to_an_allowed_literal() -> None:
    inner = RecordingBackend()
    backend = GuardedBackend(allow_loopback_only, inner)

    run(lambda: backend.connect_tcp("127.0.0.1", 8080))

    assert inner.connected == [("127.0.0.1", 8080)]


def test_backend_dns_failure_surfaces_as_gaierror(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = GuardedBackend(is_public_address, RecordingBackend())

    async def scenario() -> None:
        async def failing(host: str, port: int, **_: object) -> AddrInfo:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", failing)
        await backend.connect_tcp("nope.invalid", 80)

    with pytest.raises(socket.gaierror):
        run(scenario)


def test_backend_dns_timeout_is_a_connect_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = GuardedBackend(is_public_address, RecordingBackend())

    async def scenario() -> None:
        async def slow(host: str, port: int, **_: object) -> AddrInfo:
            await asyncio.sleep(5)
            return []

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", slow)
        await backend.connect_tcp("slow.example", 80, timeout=0.05)

    with pytest.raises(httpcore.ConnectTimeout):
        run(scenario)


def test_backend_refuses_unix_sockets() -> None:
    backend = GuardedBackend(allow_loopback_only, RecordingBackend())

    with pytest.raises(BlockedAddressError):
        run(lambda: backend.connect_unix_socket("/var/run/docker.sock"))


def test_blocked_address_error_is_an_os_error() -> None:
    assert issubclass(BlockedAddressError, OSError)


def test_backend_connects_when_policy_allows(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/ok").respond_with_data("hello")
    url = httpserver.url_for("/ok")

    async def scenario() -> httpx.Response:
        async with httpx.AsyncClient(transport=GuardedTransport(allow_loopback_only)) as client:
            return await client.get(url)

    response = run(scenario)

    assert response.status_code == 200
    assert response.text == "hello"


# --- GuardedTransport ---


def test_transport_blocks_a_private_target_and_raises_the_blocked_error(
    httpserver: HTTPServer,
) -> None:
    url = httpserver.url_for("/")

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=GuardedTransport(is_public_address)) as client:
            await client.get(url)

    with pytest.raises(BlockedAddressError):
        run(scenario)


def test_transport_blocks_a_redirect_to_a_private_address(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/go").respond_with_data(
        "", status=302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
    )
    url = httpserver.url_for("/go")

    async def scenario() -> None:
        transport = GuardedTransport(allow_loopback_only)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            await client.get(url)

    with pytest.raises(BlockedAddressError):
        run(scenario)


def test_transport_streams_the_response_body(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/big").respond_with_data(b"x" * 100_000)
    url = httpserver.url_for("/big")

    async def scenario() -> int:
        transport = GuardedTransport(allow_loopback_only)
        async with (
            httpx.AsyncClient(transport=transport) as client,
            client.stream("GET", url) as response,
        ):
            return sum([len(chunk) async for chunk in response.aiter_bytes()])

    assert run(scenario) == 100_000


def test_transport_sends_method_headers_and_body(httpserver: HTTPServer) -> None:
    seen: dict[str, str] = {}

    def handler(request: Request) -> Response:
        seen["method"] = request.method
        seen["header"] = request.headers.get("X-Probe", "")
        seen["body"] = request.get_data(as_text=True)
        return Response("done")

    httpserver.expect_request("/echo").respond_with_handler(handler)
    url = httpserver.url_for("/echo")

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=GuardedTransport(allow_loopback_only)) as client:
            await client.post(url, content=b"payload", headers={"X-Probe": "1"})

    run(scenario)

    assert seen == {"method": "POST", "header": "1", "body": "payload"}


def test_transport_maps_a_read_timeout_to_the_httpx_exception() -> None:
    with socket.socket() as silent:  # accepts connections (backlog) but never answers
        silent.bind(("127.0.0.1", 0))
        silent.listen()
        url = f"http://127.0.0.1:{silent.getsockname()[1]}/"

        async def scenario() -> None:
            async with httpx.AsyncClient(
                transport=GuardedTransport(allow_loopback_only), timeout=0.1
            ) as client:
                await client.get(url)

        with pytest.raises(httpx.ReadTimeout):
            run(scenario)


def test_transport_maps_a_refused_connection_to_connect_error() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=GuardedTransport(allow_loopback_only)) as client:
            await client.get(f"http://127.0.0.1:{port}/")

    with pytest.raises(httpx.ConnectError):
        run(scenario)


def test_transport_lets_a_dns_failure_surface_as_gaierror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async def failing(host: str, port: int, **_: object) -> AddrInfo:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", failing)
        async with httpx.AsyncClient(transport=GuardedTransport(is_public_address)) as client:
            await client.get("http://nope.invalid/")

    with pytest.raises(socket.gaierror):
        run(scenario)


# --- resolve_allowed ---


def test_resolve_allowed_returns_the_vetted_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> list[Addr]:
        fake_getaddrinfo(
            monkeypatch, asyncio.get_running_loop(), addrinfo("93.184.216.34", "2606:2800:220:1::1")
        )
        return await resolve_allowed("example.org", 443, is_public_address)

    assert run(scenario) == [ip_address("93.184.216.34"), ip_address("2606:2800:220:1::1")]


def test_resolve_allowed_uses_an_ip_literal_as_is() -> None:
    assert run(lambda: resolve_allowed("93.184.216.34", 80, is_public_address)) == [
        IPv4Address("93.184.216.34")
    ]
    with pytest.raises(BlockedAddressError):
        run(lambda: resolve_allowed("169.254.169.254", 80, is_public_address))


def test_resolve_allowed_blocks_a_mixed_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        fake_getaddrinfo(
            monkeypatch, asyncio.get_running_loop(), addrinfo("93.184.216.34", "10.0.0.1")
        )
        await resolve_allowed("rebind.example", 80, is_public_address)

    with pytest.raises(BlockedAddressError):
        run(scenario)


def test_resolve_allowed_passes_dns_failures_on(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        fake_getaddrinfo(monkeypatch, asyncio.get_running_loop(), [])
        await resolve_allowed("empty.example", 80, is_public_address)

    with pytest.raises(socket.gaierror):
        run(scenario)
