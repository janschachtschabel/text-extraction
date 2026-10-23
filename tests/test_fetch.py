import asyncio
import gzip
import hashlib
import socket
import threading
import time
import zlib
from collections.abc import Callable, Coroutine, Iterator
from ipaddress import IPv4Address, IPv6Address
from typing import Any

import pytest
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

import text_extraction
from text_extraction.envelope import ErrorCode
from text_extraction.fetch import USER_AGENT, Fetcher, FetchError, FetchResult, Validators
from text_extraction.settings import Settings

Addr = IPv4Address | IPv6Address
LOOPBACK = IPv4Address("127.0.0.1")


def allow_loopback_only(ip: Addr) -> bool:
    return ip == LOOPBACK


def make_fetcher(**settings: Any) -> Fetcher:
    return Fetcher(Settings(auth_mode="none", **settings), policy=allow_loopback_only)


def run[T](test: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(test())


def fetch(url: str, *, fetcher: Fetcher | None = None, **kwargs: Any) -> FetchResult:
    async def scenario() -> FetchResult:
        client = fetcher or make_fetcher()
        try:
            return await client.fetch(url, **kwargs)
        finally:
            await client.aclose()

    return run(scenario)


def fetch_error(url: str, **kwargs: Any) -> FetchError:
    with pytest.raises(FetchError) as info:
        fetch(url, **kwargs)
    return info.value


def test_fetch_returns_body_status_and_content_type(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/page").respond_with_data(
        "<p>Hällo</p>", content_type="Text/HTML; charset=UTF-8", headers={"X-Thing": "a"}
    )
    url = httpserver.url_for("/page")

    result = fetch(url)

    assert result.url == url
    assert result.final_url == url
    assert result.status == 200
    assert result.content == "<p>Hällo</p>".encode()
    assert result.content_type == "text/html"
    assert result.charset == "utf-8"
    assert result.headers["x-thing"] == "a"
    assert result.redirects == []
    assert result.not_modified is False
    assert result.raw_sha256 == hashlib.sha256(result.content).hexdigest()
    assert result.elapsed_ms >= 0


def test_missing_content_type_gives_none(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/bare").respond_with_data("x", content_type="")

    result = fetch(httpserver.url_for("/bare"))

    assert result.content_type is None
    assert result.charset is None


def test_fetch_follows_redirect_and_records_chain(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/a").respond_with_data("", 301, headers={"Location": "/b"})
    httpserver.expect_request("/b").respond_with_data(
        "", 302, headers={"Location": httpserver.url_for("/c")}
    )
    httpserver.expect_request("/c").respond_with_data("done")

    result = fetch(httpserver.url_for("/a"))

    assert result.status == 200
    assert result.content == b"done"
    assert result.url == httpserver.url_for("/a")
    assert result.final_url == httpserver.url_for("/c")
    assert result.redirects == [httpserver.url_for("/b"), httpserver.url_for("/c")]


def test_redirect_to_disallowed_address_is_blocked(httpserver: HTTPServer) -> None:
    target = f"http://127.0.0.2:{httpserver.port}/secret"
    httpserver.expect_request("/go").respond_with_data("", 302, headers={"Location": target})

    error = fetch_error(httpserver.url_for("/go"))

    assert error.code is ErrorCode.BLOCKED_URL
    assert error.retryable is False
    assert "127.0.0.2" not in error.message


def test_blocked_first_hop_does_not_echo_the_address(httpserver: HTTPServer) -> None:
    fetcher = Fetcher(Settings(auth_mode="none"))  # default policy: public addresses only

    error = fetch_error(httpserver.url_for("/"), fetcher=fetcher)

    assert error.code is ErrorCode.BLOCKED_URL
    assert "127.0.0.1" not in error.message


def test_default_policy_allows_private_networks_only_when_configured(
    httpserver: HTTPServer,
) -> None:
    httpserver.expect_request("/ok").respond_with_data("ok")
    fetcher = Fetcher(Settings(auth_mode="none", fetch_allow_private_networks=True))

    assert fetch(httpserver.url_for("/ok"), fetcher=fetcher).content == b"ok"


def test_redirect_to_invalid_scheme_is_invalid_url(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/go").respond_with_data(
        "", 302, headers={"Location": "file:///etc/passwd"}
    )

    error = fetch_error(httpserver.url_for("/go"))

    assert error.code is ErrorCode.INVALID_URL
    assert error.retryable is False


def test_invalid_url_is_rejected_before_any_request() -> None:
    error = fetch_error("ftp://example.org/file")

    assert error.code is ErrorCode.INVALID_URL
    assert error.retryable is False


def test_redirect_without_location_returns_the_response(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/odd").respond_with_data("moved", 302)

    result = fetch(httpserver.url_for("/odd"))

    assert result.status == 302
    assert result.content == b"moved"
    assert result.redirects == []


def test_too_many_redirects(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/loop").respond_with_data("", 302, headers={"Location": "/loop"})

    error = fetch_error(httpserver.url_for("/loop"), fetcher=make_fetcher(fetch_max_redirects=2))

    assert error.code is ErrorCode.TARGET_HTTP_ERROR
    assert error.retryable is False
    assert error.upstream_status == 302
    assert len(httpserver.log) == 3  # the first request plus two followed redirects


def test_zero_redirects_allowed_fails_on_first_redirect(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/a").respond_with_data("", 301, headers={"Location": "/b"})

    error = fetch_error(httpserver.url_for("/a"), fetcher=make_fetcher(fetch_max_redirects=0))

    assert error.code is ErrorCode.TARGET_HTTP_ERROR
    assert error.upstream_status == 301


def test_body_over_cap_is_too_large(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/big").respond_with_data(b"x" * 2_000_000)

    error = fetch_error(httpserver.url_for("/big"), max_bytes=1_000_000)

    assert error.code is ErrorCode.TOO_LARGE
    assert error.retryable is False


def test_body_exactly_at_cap_is_accepted(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/edge").respond_with_data(b"x" * 5000)

    assert len(fetch(httpserver.url_for("/edge"), max_bytes=5000).content) == 5000


def test_streamed_body_without_content_length_is_capped(httpserver: HTTPServer) -> None:
    def handler(_: Request) -> Response:
        return Response((b"x" * 1000 for _ in range(2000)), content_type="text/plain")

    httpserver.expect_request("/chunked").respond_with_handler(handler)

    error = fetch_error(httpserver.url_for("/chunked"), max_bytes=100_000)

    assert error.code is ErrorCode.TOO_LARGE


def test_gzip_body_is_decoded(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/gz").respond_with_data(
        gzip.compress(b"hello gzip"), headers={"Content-Encoding": "gzip"}
    )

    result = fetch(httpserver.url_for("/gz"))

    assert result.content == b"hello gzip"
    assert result.headers["content-encoding"] == "gzip"


def test_gzip_bomb_is_too_large(httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch) -> None:
    # Depends on the private Fetcher._decoded_chunks (every decoded piece passes through it):
    # if that method is renamed or split, update the spy below.
    bomb = gzip.compress(b"\0" * 50_000_000, compresslevel=9)
    assert len(bomb) < 100_000
    httpserver.expect_request("/bomb").respond_with_data(bomb, headers={"Content-Encoding": "gzip"})
    seen: list[int] = []
    original = Fetcher._decoded_chunks

    async def counting(self: Fetcher, response: Any) -> Any:
        async for chunk in original(self, response):
            seen.append(len(chunk))
            yield chunk

    monkeypatch.setattr(Fetcher, "_decoded_chunks", counting)

    error = fetch_error(httpserver.url_for("/bomb"), max_bytes=1_000_000)

    assert error.code is ErrorCode.TOO_LARGE
    assert sum(seen) < 1_000_000 + 65536


def test_deflate_body_is_decoded(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/df").respond_with_data(
        zlib.compress(b"hello deflate"), headers={"Content-Encoding": "deflate"}
    )

    assert fetch(httpserver.url_for("/df")).content == b"hello deflate"


def test_corrupt_gzip_is_a_connection_error(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/bad").respond_with_data(
        b"not gzip at all", headers={"Content-Encoding": "gzip"}
    )

    error = fetch_error(httpserver.url_for("/bad"))

    assert error.code is ErrorCode.CONNECTION_ERROR
    assert error.retryable is True


def test_unknown_content_encoding_is_unsupported(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/br").respond_with_data(b"zzz", headers={"Content-Encoding": "br"})

    error = fetch_error(httpserver.url_for("/br"))

    assert error.code is ErrorCode.UNSUPPORTED_CONTENT_TYPE
    assert error.retryable is False


def test_slow_drip_hits_overall_deadline(httpserver: HTTPServer) -> None:
    def drip() -> Iterator[bytes]:
        for _ in range(50):
            yield b"x"
            time.sleep(0.2)

    httpserver.expect_request("/drip").respond_with_handler(
        lambda _: Response(drip(), content_type="text/plain")
    )
    started = time.monotonic()

    error = fetch_error(httpserver.url_for("/drip"), timeout_seconds=1)

    assert error.code is ErrorCode.TIMEOUT
    assert error.retryable is True
    assert time.monotonic() - started < 2


def test_http_error_status_is_returned_not_raised(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/missing").respond_with_data("nope", 404)

    result = fetch(httpserver.url_for("/missing"))

    assert result.status == 404
    assert result.content == b"nope"


def test_conditional_request_304(httpserver: HTTPServer) -> None:
    seen: dict[str, str | None] = {}

    def handler(request: Request) -> Response:
        seen["etag"] = request.headers.get("If-None-Match")
        seen["modified"] = request.headers.get("If-Modified-Since")
        return Response(status=304, headers={"ETag": '"v1"'})

    httpserver.expect_request("/c").respond_with_handler(handler)
    validators = Validators(etag='"v1"', last_modified="Wed, 21 Oct 2026 07:28:00 GMT")

    result = fetch(httpserver.url_for("/c"), validators=validators)

    assert result.status == 304
    assert result.not_modified is True
    assert result.content == b""
    assert seen == {"etag": '"v1"', "modified": "Wed, 21 Oct 2026 07:28:00 GMT"}


def test_no_validators_sends_no_conditional_headers(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/plain").respond_with_data("x")

    fetch(httpserver.url_for("/plain"))

    headers = httpserver.log[0][0].headers
    assert "If-None-Match" not in headers
    assert "If-Modified-Since" not in headers


def test_dns_failure_is_dns_error() -> None:
    fetcher = Fetcher(Settings(auth_mode="none"))  # default policy, real resolver

    error = fetch_error("http://no-such-host.invalid/", fetcher=fetcher)

    assert error.code is ErrorCode.DNS_ERROR
    assert error.retryable is True


def test_connection_refused_is_connection_error() -> None:
    with socket.socket() as probe:  # a port that was free a moment ago and has no listener
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    error = fetch_error(f"http://127.0.0.1:{port}/")

    assert error.code is ErrorCode.CONNECTION_ERROR
    assert error.retryable is True


def test_user_agent_header_sent(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/ua").respond_with_data("x")

    fetch(httpserver.url_for("/ua"))

    request = httpserver.log[0][0]
    assert request.headers["User-Agent"] == USER_AGENT
    assert USER_AGENT.startswith(f"text-extraction/{text_extraction.__version__} (+https://")
    assert request.headers["Accept"] == "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"
    assert request.headers["Accept-Language"] == "de,en;q=0.8"


def test_no_client_trace_headers_forwarded(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/t").respond_with_data("x")

    fetch(httpserver.url_for("/t"))

    names = {name.lower() for name, _ in httpserver.log[0][0].headers}
    assert "x-client-trace-id" not in names
    assert not {n for n in names if n == "b3" or n.startswith("x-b3-")}


def test_proxy_environment_is_ignored(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpserver.expect_request("/direct").respond_with_data("direct")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")

    assert fetch(httpserver.url_for("/direct")).content == b"direct"


def test_timeout_argument_overrides_the_setting() -> None:
    with socket.socket() as silent:  # accepts connections (backlog) but never answers
        silent.bind(("127.0.0.1", 0))
        silent.listen()
        url = f"http://127.0.0.1:{silent.getsockname()[1]}/"
        started = time.monotonic()

        error = fetch_error(
            url, fetcher=make_fetcher(fetch_timeout_seconds=30), timeout_seconds=0.5
        )

    assert error.code is ErrorCode.TIMEOUT
    assert time.monotonic() - started < 5


def test_cookie_from_one_fetch_is_not_sent_on_the_next(httpserver: HTTPServer) -> None:
    httpserver.expect_ordered_request("/set").respond_with_data(
        "x", headers={"Set-Cookie": "session=abc; Path=/"}
    )
    httpserver.expect_ordered_request("/set").respond_with_data("y")

    async def scenario() -> None:
        fetcher = make_fetcher()
        try:
            await fetcher.fetch(httpserver.url_for("/set"))
            await fetcher.fetch(httpserver.url_for("/set"))
        finally:
            await fetcher.aclose()

    run(scenario)

    assert "Cookie" not in httpserver.log[0][0].headers
    assert "Cookie" not in httpserver.log[1][0].headers


def test_cookie_set_on_a_redirect_is_sent_to_the_target_in_the_same_fetch(
    httpserver: HTTPServer,
) -> None:
    httpserver.expect_request("/login").respond_with_data(
        "", 302, headers={"Location": "/home", "Set-Cookie": "session=abc; Path=/"}
    )
    httpserver.expect_request("/home").respond_with_data("welcome")

    result = fetch(httpserver.url_for("/login"))

    assert result.content == b"welcome"
    assert httpserver.log[1][0].headers["Cookie"] == "session=abc"


def test_malformed_redirect_location_is_a_target_error() -> None:
    # Raw socket: werkzeug refuses to send a Location it cannot parse.
    reply = b"HTTP/1.1 302 Found\r\nLocation: http://[::1/x\r\nContent-Length: 0\r\n\r\n"
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()

        def answer_once() -> None:
            connection, _ = server.accept()
            with connection:
                connection.recv(65536)
                connection.sendall(reply)

        thread = threading.Thread(target=answer_once)
        thread.start()
        try:
            error = fetch_error(f"http://127.0.0.1:{server.getsockname()[1]}/bad")
        finally:
            thread.join(5)

    assert error.code is ErrorCode.TARGET_HTTP_ERROR
    assert error.retryable is False
    assert error.upstream_status == 302


def test_accept_encoding_offers_only_what_the_fetcher_can_inflate(
    httpserver: HTTPServer,
) -> None:
    httpserver.expect_request("/enc").respond_with_data("x")

    fetch(httpserver.url_for("/enc"))

    assert httpserver.log[0][0].headers["Accept-Encoding"] == "gzip, deflate"


def test_zero_timeout_is_rejected_not_replaced_by_the_default() -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        fetch("http://127.0.0.1:1/", timeout_seconds=0)
