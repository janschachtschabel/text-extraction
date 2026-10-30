"""Fetching a page's images for the vision call (ruling V-S6): SSRF guard, compliance checks of
the job's mode, the outbound gate's turns and the service's user agent."""

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager
from ipaddress import IPv4Address, IPv6Address
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from pytest_httpserver import HTTPServer
from structlog.testing import capture_logs
from werkzeug import Response

from text_extraction.compliance import ComplianceChecker, ComplianceMode
from text_extraction.fetch import USER_AGENT, Fetcher
from text_extraction.outbound import CircuitOpen, DomainGate, OutboundBusy
from text_extraction.settings import Settings
from text_extraction.vision.fetch import (
    ImageBytes,
    ImageFetcher,
    ImageSkipped,
    create_image_fetcher,
)

LOOPBACK = IPv4Address("127.0.0.1")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
TIMEOUT = 5.0


def only_loopback(ip: IPv4Address | IPv6Address) -> bool:
    return ip == LOOPBACK


def make_settings(**overrides: Any) -> Settings:
    # No spacing between turns, so the tests do not sleep; the crawl delay asked for is recorded.
    values: dict[str, Any] = {
        "auth_mode": "none",
        "outbound_min_delay_ms": 0,
        "outbound_max_crawl_delay_seconds": 0,
    }
    values.update(overrides)
    return Settings(**values)


class RecordingGate(DomainGate):
    """The real gate, noting each turn taken and each outcome recorded."""

    def __init__(self, valkey: FakeAsyncRedis, settings: Settings) -> None:
        super().__init__(valkey, settings)
        self.slots: list[tuple[str, float | None]] = []
        self.records: list[tuple[str, int | None]] = []

    @asynccontextmanager
    async def slot(self, host: str, crawl_delay: float | None = None) -> AsyncIterator[None]:
        self.slots.append((host, crawl_delay))
        async with super().slot(host, crawl_delay):
            yield

    async def record(self, host: str, *, status: int | None, retry_after: float | None) -> None:
        self.records.append((host, status))
        await super().record(host, status=status, retry_after=retry_after)


class RefusingGate(RecordingGate):
    """A gate whose turns are never free."""

    def __init__(self, valkey: FakeAsyncRedis, settings: Settings, error: Exception) -> None:
        super().__init__(valkey, settings)
        self.error = error

    @asynccontextmanager
    async def slot(self, host: str, crawl_delay: float | None = None) -> AsyncIterator[None]:
        self.slots.append((host, crawl_delay))
        raise self.error
        yield  # pragma: no cover - makes this a generator


def make_fetcher(
    settings: Settings | None = None,
    gate_of: Callable[[FakeAsyncRedis, Settings], RecordingGate] = RecordingGate,
) -> tuple[ImageFetcher, RecordingGate]:
    settings = settings or make_settings()
    valkey = FakeAsyncRedis()
    gate = gate_of(valkey, settings)
    fetcher = Fetcher(
        settings,
        policy=only_loopback,
        before_request=gate.before_request,
        on_request_sent=gate.mark_sent,
    )
    checker = ComplianceChecker(fetcher, valkey, settings)
    return ImageFetcher(fetcher, gate, checker, settings, policy=only_loopback), gate


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


def get(
    url: str,
    mode: ComplianceMode = "enforce",
    *,
    settings: Settings | None = None,
    timeout: float = TIMEOUT,
) -> ImageBytes | ImageSkipped:
    async def scenario() -> ImageBytes | ImageSkipped:
        images, _ = make_fetcher(settings)
        try:
            return await images.get(url, mode, timeout=timeout)
        finally:
            await images.aclose()

    return run(scenario)


def no_robots(server: HTTPServer) -> None:
    server.expect_request("/robots.txt").respond_with_data("", status=404)


def requested_paths(server: HTTPServer) -> list[str]:
    return [request.path for request, _ in server.log]


@pytest.fixture
def other_server() -> Iterator[HTTPServer]:
    """A second origin (same host, another port)."""
    server = HTTPServer(host="127.0.0.1", port=0)
    server.start()
    try:
        yield server
    finally:
        server.clear()
        if server.is_running():
            server.stop()


def test_png_is_fetched_and_sniffed(httpserver: HTTPServer) -> None:
    no_robots(httpserver)
    httpserver.expect_request("/a.png").respond_with_data(PNG, content_type="image/png")
    url = httpserver.url_for("/a.png")

    async def scenario() -> ImageBytes | ImageSkipped:
        images = create_image_fetcher(make_settings(), FakeAsyncRedis(), policy=only_loopback)
        try:
            return await images.get(url, "enforce", timeout=TIMEOUT)
        finally:
            await images.aclose()

    image = run(scenario)

    assert image == ImageBytes(data=PNG, media_type="image/png")
    request = next(request for request, _ in httpserver.log if request.path == "/a.png")
    assert request.headers.get("User-Agent") == USER_AGENT
    assert "Cookie" not in request.headers


def test_type_comes_from_the_body_not_the_header(httpserver: HTTPServer) -> None:
    no_robots(httpserver)
    httpserver.expect_request("/a.png").respond_with_data(JPEG, content_type="image/png")

    image = get(httpserver.url_for("/a.png"))

    assert image == ImageBytes(data=JPEG, media_type="image/jpeg")


def test_declared_png_that_is_html_skipped(httpserver: HTTPServer) -> None:
    no_robots(httpserver)
    httpserver.expect_request("/a.png").respond_with_data(
        "<html><body>not an image</body></html>", content_type="image/png"
    )

    assert get(httpserver.url_for("/a.png")) == ImageSkipped(reason="unsupported")


def test_svg_skipped(httpserver: HTTPServer) -> None:
    no_robots(httpserver)
    httpserver.expect_request("/a.svg").respond_with_data(
        '<svg xmlns="http://www.w3.org/2000/svg"></svg>', content_type="image/svg+xml"
    )

    assert get(httpserver.url_for("/a.svg")) == ImageSkipped(reason="unsupported")


def test_body_over_cap_skipped(httpserver: HTTPServer) -> None:
    no_robots(httpserver)
    httpserver.expect_request("/big.png").respond_with_data(
        PNG + b"\x00" * 50_000, content_type="image/png"
    )
    settings = make_settings(llm_vision_max_image_bytes=10_000)

    assert get(httpserver.url_for("/big.png"), settings=settings) == ImageSkipped(
        reason="too_large"
    )


def test_body_declared_as_media_is_not_cut_to_an_image(httpserver: HTTPServer) -> None:
    # The fetcher cuts a declared video at the cap instead of refusing it; a cut image is no image.
    no_robots(httpserver)
    httpserver.expect_request("/big.png").respond_with_data(
        PNG + b"\x00" * 50_000, content_type="video/mp4"
    )
    settings = make_settings(llm_vision_max_image_bytes=10_000)

    assert get(httpserver.url_for("/big.png"), settings=settings) == ImageSkipped(
        reason="too_large"
    )


def test_http_404_skipped_and_recorded(httpserver: HTTPServer) -> None:
    no_robots(httpserver)
    httpserver.expect_request("/gone.png").respond_with_data("missing", status=404)
    url = httpserver.url_for("/gone.png")

    async def scenario() -> tuple[ImageBytes | ImageSkipped, RecordingGate]:
        images, gate = make_fetcher()
        try:
            return await images.get(url, "enforce", timeout=TIMEOUT), gate
        finally:
            await images.aclose()

    image, gate = run(scenario)

    assert image == ImageSkipped(reason="http_error")
    assert gate.records == [("127.0.0.1", 404)]


def test_robots_disallowed_not_requested_in_enforce(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/robots.txt").respond_with_data(
        "User-agent: *\nDisallow: /private/\n", content_type="text/plain"
    )
    httpserver.expect_request("/private/a.png").respond_with_data(PNG, content_type="image/png")

    image = get(httpserver.url_for("/private/a.png"))

    assert image == ImageSkipped(reason="blocked")
    paths = requested_paths(httpserver)
    assert "/robots.txt" in paths
    assert "/private/a.png" not in paths


def test_robots_disallowed_fetched_in_report_mode(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/robots.txt").respond_with_data(
        "User-agent: *\nDisallow: /private/\n", content_type="text/plain"
    )
    httpserver.expect_request("/private/a.png").respond_with_data(PNG, content_type="image/png")

    image = get(httpserver.url_for("/private/a.png"), "report")

    assert image == ImageBytes(data=PNG, media_type="image/png")


def test_ignore_mode_reads_no_compliance_file(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/a.png").respond_with_data(PNG, content_type="image/png")

    image = get(httpserver.url_for("/a.png"), "ignore")

    assert image == ImageBytes(data=PNG, media_type="image/png")
    assert requested_paths(httpserver) == ["/a.png"]


def test_noai_header_blocks_in_enforce(httpserver: HTTPServer) -> None:
    no_robots(httpserver)
    httpserver.expect_request("/a.png").respond_with_data(
        PNG, content_type="image/png", headers={"X-Robots-Tag": "noai"}
    )

    assert get(httpserver.url_for("/a.png")) == ImageSkipped(reason="blocked")


def test_tdm_reservation_header_is_only_reported_in_report_mode(httpserver: HTTPServer) -> None:
    no_robots(httpserver)
    httpserver.expect_request("/a.png").respond_with_data(
        PNG, content_type="image/png", headers={"tdm-reservation": "1"}
    )

    image = get(httpserver.url_for("/a.png"), "report")

    assert image == ImageBytes(data=PNG, media_type="image/png")


def test_private_address_refused() -> None:
    assert get("http://169.254.169.254/a.png") == ImageSkipped(reason="refused")


def test_invalid_url_refused() -> None:
    assert get("file:///etc/passwd") == ImageSkipped(reason="refused")


def test_redirect_to_private_address_refused(httpserver: HTTPServer) -> None:
    no_robots(httpserver)
    httpserver.expect_request("/a.png").respond_with_data(
        "", status=302, headers={"Location": "http://10.0.0.1/a.png"}
    )

    assert get(httpserver.url_for("/a.png")) == ImageSkipped(reason="refused")


def test_redirect_to_disallowed_origin_blocked(
    httpserver: HTTPServer, other_server: HTTPServer
) -> None:
    no_robots(httpserver)
    httpserver.expect_request("/a.png").respond_with_data(
        "", status=302, headers={"Location": other_server.url_for("/private/a.png")}
    )
    other_server.expect_request("/robots.txt").respond_with_data(
        "User-agent: *\nDisallow: /private/\n", content_type="text/plain"
    )
    other_server.expect_request("/private/a.png").respond_with_data(PNG, content_type="image/png")

    image = get(httpserver.url_for("/a.png"))

    assert image == ImageSkipped(reason="blocked")
    assert "/robots.txt" in requested_paths(other_server)
    assert "/private/a.png" not in requested_paths(other_server)


def test_redirect_to_allowed_origin_is_followed(
    httpserver: HTTPServer, other_server: HTTPServer
) -> None:
    no_robots(httpserver)
    httpserver.expect_request("/a.png").respond_with_data(
        "", status=302, headers={"Location": other_server.url_for("/b.png")}
    )
    no_robots(other_server)
    other_server.expect_request("/b.png").respond_with_data(PNG, content_type="image/png")

    assert get(httpserver.url_for("/a.png")) == ImageBytes(data=PNG, media_type="image/png")


def test_images_of_one_host_take_its_turns(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/robots.txt").respond_with_data(
        "User-agent: *\nCrawl-delay: 2\n", content_type="text/plain"
    )
    for name in ("a", "b", "c"):
        httpserver.expect_request(f"/{name}.png").respond_with_data(PNG, content_type="image/png")
    urls = [httpserver.url_for(f"/{name}.png") for name in ("a", "b", "c")]

    async def scenario() -> tuple[list[ImageBytes | ImageSkipped], RecordingGate]:
        images, gate = make_fetcher()
        try:
            return [await images.get(url, "enforce", timeout=TIMEOUT) for url in urls], gate
        finally:
            await images.aclose()

    results, gate = run(scenario)

    assert results == [ImageBytes(data=PNG, media_type="image/png")] * 3
    # One turn per image. The first image's turn was taken to read robots.txt, before its
    # Crawl-delay was known, and the image followed in it (as the pipeline does for a page); the
    # others take the host's turn with the Crawl-delay.
    assert gate.slots == [("127.0.0.1", None), ("127.0.0.1", 2.0), ("127.0.0.1", 2.0)]
    assert requested_paths(httpserver).count("/robots.txt") == 1
    assert gate.records == [("127.0.0.1", 200)] * 3


@pytest.mark.parametrize("error", [OutboundBusy(3.0), CircuitOpen(30.0)])
def test_gate_busy_is_skipped_not_raised(httpserver: HTTPServer, error: Exception) -> None:
    httpserver.expect_request("/a.png").respond_with_data(PNG, content_type="image/png")
    url = httpserver.url_for("/a.png")

    async def scenario() -> ImageBytes | ImageSkipped:
        images, _ = make_fetcher(
            gate_of=lambda valkey, settings: RefusingGate(valkey, settings, error)
        )
        try:
            return await images.get(url, "enforce", timeout=TIMEOUT)
        finally:
            await images.aclose()

    assert run(scenario) == ImageSkipped(reason="busy")
    assert requested_paths(httpserver) == []


def test_timeout_is_error(httpserver: HTTPServer) -> None:
    def drip() -> Iterator[bytes]:
        yield PNG
        for _ in range(20):
            time.sleep(0.1)
            yield b"\x00"

    no_robots(httpserver)
    httpserver.expect_request("/slow.png").respond_with_handler(
        lambda _: Response(drip(), content_type="image/png")
    )
    started = time.monotonic()

    image = get(httpserver.url_for("/slow.png"), timeout=0.5)

    assert image == ImageSkipped(reason="error")
    assert time.monotonic() - started < 1.5


def test_skip_is_logged_with_host_and_reason_only(httpserver: HTTPServer) -> None:
    no_robots(httpserver)
    httpserver.expect_request("/secret-name.png").respond_with_data("missing", status=404)

    with capture_logs() as logs:
        get(httpserver.url_for("/secret-name.png"))

    skipped = [entry for entry in logs if entry["event"] == "image_skipped"]
    assert skipped == [
        {"event": "image_skipped", "host": "127.0.0.1", "reason": "http_error", "log_level": "info"}
    ]
    assert not any("secret-name" in repr(entry) for entry in logs)
