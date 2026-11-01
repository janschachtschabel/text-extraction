"""Preview images (ruling P10-S16, D7 a): the server fetches a result's images through the
vision ``ImageFetcher`` (SSRF guard on every hop, the caller's compliance mode, the outbound
gate, sniffed types, ``UI_IMAGE_MAX_BYTES``) and inlines them as ``data:`` URIs."""

import asyncio
import base64
import socket
import time
from typing import Any, get_args

import httpcore
import lxml.html
import pytest
from prometheus_client import CollectorRegistry
from pytest_httpserver import HTTPServer

from tests.ui.conftest import run, running
from text_extraction.compliance import ComplianceMode
from text_extraction.ratelimit import RateDecision, RateLimiter
from text_extraction.ui.previews import (
    OUTCOMES,
    SHOWN,
    SKIP_REASONS,
    TIME,
    Preview,
    load_previews,
)
from text_extraction.vision.fetch import ImageBytes, ImageFetcher, ImageSkipped, SkipReason

PUBLIC_HOST = "images.example.org"
PUBLIC_ADDRESS = "93.184.216.34"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x02" * 200
JPEG_2MB = b"\xff\xd8\xff\xe0" + b"\x00" * (2_000_000 - 4)
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>'
PRIVATE = "http://127.0.0.1:6379/"


class CountingLimiter(RateLimiter):
    """The real limiter, counting its hits."""

    hits = 0

    async def hit(self, principal_name: str, limit: int, units: int = 1) -> RateDecision:
        CountingLimiter.hits += units
        return await super().hit(principal_name, limit, units)


@pytest.fixture
def connects(monkeypatch: pytest.MonkeyPatch, httpserver: HTTPServer) -> list[str]:
    """A resolver stub that maps ``PUBLIC_HOST`` to a public address, and a network backend that
    connects that address to the test server; every address a connection was asked for is
    recorded (the SSRF guard hands the backend vetted addresses only)."""
    seen: list[str] = []
    real_getaddrinfo = asyncio.base_events.BaseEventLoop.getaddrinfo
    real_connect = httpcore.AnyIOBackend.connect_tcp

    async def getaddrinfo(self: Any, host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        if host == PUBLIC_HOST:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_ADDRESS, port))]
        return await real_getaddrinfo(self, host, port, *args, **kwargs)

    async def connect_tcp(self: Any, host: str, port: int, **kwargs: Any) -> Any:
        seen.append(host)
        target = "127.0.0.1" if host == PUBLIC_ADDRESS else host
        return await real_connect(self, target, port, **kwargs)

    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect_tcp)
    return seen


def public(httpserver: HTTPServer, path: str) -> str:
    return f"http://{PUBLIC_HOST}:{httpserver.port}{path}"


def test_previews_guarded(httpserver: HTTPServer, connects: list[str]) -> None:
    for name in ("/robots.txt", "/.well-known/tdmrep.json", "/ai.txt"):
        httpserver.expect_request(name).respond_with_data("", status=404)
    httpserver.expect_request("/a.png").respond_with_data(PNG, content_type="image/png")
    httpserver.expect_request("/b.svg").respond_with_data(SVG, content_type="image/svg+xml")
    httpserver.expect_request("/c.jpg").respond_with_data(JPEG_2MB, content_type="image/jpeg")
    httpserver.expect_request("/d.png").respond_with_data(
        "", status=302, headers={"Location": "http://169.254.169.254/latest"}
    )
    urls = [
        public(httpserver, "/a.png"),
        public(httpserver, "/b.svg"),
        public(httpserver, "/c.jpg"),
        public(httpserver, "/d.png"),
        PRIVATE,
    ]
    alts = ["A <b>png</b>", "An svg", "A jpeg", "A redirect", "Valkey"]
    registry = CollectorRegistry()

    with running(
        registry=registry, fetch_allow_private_networks=False, ui_image_max_bytes=1_000_000
    ) as client:
        client.app.state.rate_limiter = CountingLimiter(  # type: ignore[attr-defined]
            client.app.state.valkey,  # type: ignore[attr-defined]
            10_000,
        )
        CountingLimiter.hits = 0
        answer = run(client, "images", url=urls, alt=alts, compliance="enforce")

    assert answer.status_code == 200
    assert CountingLimiter.hits == len(urls)  # M4: one hit per image fetched
    root = lxml.html.fragment_fromstring(answer.text, create_parent="div")
    figures = root.xpath("//figure[@class='preview']")
    assert [figure.get("data-url") for figure in figures] == urls
    images = root.xpath("//figure[@class='preview']//img")
    assert [(image.get("src"), image.get("alt")) for image in images] == [
        ("data:image/png;base64," + base64.b64encode(PNG).decode(), "A <b>png</b>")
    ]
    skipped = root.xpath("//figure[@class='preview']//*[@class='skipped']")
    assert [node.get("data-reason") for node in skipped] == [
        "unsupported",
        "too_large",
        "refused",
        "refused",
    ]
    assert [node.text_content() for node in skipped] == [
        "not a supported image",
        "too large",
        "address not allowed",
        "address not allowed",
    ]
    assert "<b>png</b>" not in answer.text

    # No connection to anything but the public address (the redirect target and the private
    # URL were refused before a request).
    assert connects
    assert set(connects) == {PUBLIC_ADDRESS}
    images_asked = sorted(
        {request.path for request, _ in httpserver.log}
        - {"/robots.txt", "/.well-known/tdmrep.json", "/ai.txt"}
    )
    assert images_asked == ["/a.png", "/b.svg", "/c.jpg", "/d.png"]

    def counted(outcome: str) -> float | None:
        return registry.get_sample_value(
            "text_extraction_ui_image_previews_total", {"outcome": outcome}
        )

    assert (counted("shown"), counted("unsupported"), counted("too_large"), counted("refused")) == (
        1.0,
        1.0,
        1.0,
        2.0,
    )


def test_previews_need_a_key_and_a_mode_the_key_may_choose(connects: list[str]) -> None:
    with running() as client:
        html = run(client, "images", auth=False, url=[PRIVATE], alt=["x"]).text
        assert "unauthorized" in html
        html = run(client, "images", url=[PRIVATE], alt=["x"], compliance="ignore").text
        assert "forbidden" in html
        assert "Only administrators may switch the compliance checks off." in html
    assert connects == []


class FakeSource:
    """An image source that answers a PNG for every URL ending in ``.png`` and notes the
    calls and how many ran at once."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ComplianceMode, float, int | None]] = []
        self.running = 0
        self.most = 0

    async def get(
        self,
        url: str,
        mode: ComplianceMode,
        *,
        timeout: float,  # noqa: ASYNC109 - the ImageFetcher's interface
        max_bytes: int | None = None,
    ) -> ImageBytes | ImageSkipped:
        self.calls.append((url, mode, timeout, max_bytes))
        self.running += 1
        self.most = max(self.most, self.running)
        await asyncio.sleep(0.01)
        self.running -= 1
        if url.endswith(".png"):
            return ImageBytes(data=PNG, media_type="image/png")
        return ImageSkipped("http_error")


def test_load_previews_bounds() -> None:
    source = FakeSource()
    urls = [f"https://example.org/{index}.png" for index in range(25)]
    urls.insert(1, urls[0])  # a duplicate is fetched once
    urls.append("https://example.org/x.gif")

    previews = asyncio.run(
        load_previews(urls, fetcher=source, mode="report", max_bytes=123_456)  # type: ignore[arg-type]
    )

    assert len(previews) == 20
    assert [preview.url for preview in previews] == [
        f"https://example.org/{index}.png" for index in range(20)
    ]
    assert previews[0] == Preview(
        url=urls[0],
        data_uri="data:image/png;base64," + base64.b64encode(PNG).decode(),
        skipped=None,
    )
    assert len(source.calls) == 20
    assert {call[1:] for call in source.calls} == {("report", 10.0, 123_456)}
    assert source.most == 4


def test_load_previews_reports_skips() -> None:
    source = FakeSource()
    previews = asyncio.run(
        load_previews(
            ["https://example.org/a.gif"],
            fetcher=source,  # type: ignore[arg-type]
            mode="enforce",
            max_bytes=10_000,
        )
    )

    assert previews == [
        Preview(url="https://example.org/a.gif", data_uri=None, skipped="http_error")
    ]


def test_ui_images_fetcher_is_an_image_fetcher() -> None:
    with running() as client:
        assert isinstance(client.app.state.ui_images, ImageFetcher)  # type: ignore[attr-defined]
    with running(ui_enabled=False) as client:
        assert client.app.state.ui_images is None  # type: ignore[attr-defined]


class FlakySource(FakeSource):
    """Raises an unexpected error for ``boom`` URLs, and takes ``delay`` seconds per image."""

    def __init__(self, delay: float = 0.01) -> None:
        super().__init__()
        self.delay = delay

    async def get(
        self,
        url: str,
        mode: ComplianceMode,
        *,
        timeout: float,  # noqa: ASYNC109 - the ImageFetcher's interface
        max_bytes: int | None = None,
    ) -> ImageBytes | ImageSkipped:
        self.calls.append((url, mode, timeout, max_bytes))
        await asyncio.sleep(self.delay)
        if "boom" in url:
            raise RuntimeError("the gate's Valkey is gone")
        return ImageBytes(data=PNG, media_type="image/png")


def test_one_failing_image_fails_alone() -> None:
    source = FlakySource()
    urls = [
        "https://example.org/a.png",
        "https://example.org/boom.png",
        "https://example.org/c.png",
    ]

    previews = asyncio.run(
        load_previews(urls, fetcher=source, mode="enforce", max_bytes=10_000)  # type: ignore[arg-type]
    )

    assert [preview.skipped for preview in previews] == [None, "error", None]


def test_previews_stop_at_the_deadline() -> None:
    source = FlakySource(delay=0.3)
    urls = [f"https://example.org/{index}.png" for index in range(8)]

    started = time.monotonic()
    previews = asyncio.run(
        load_previews(
            urls,
            fetcher=source,  # type: ignore[arg-type]
            mode="enforce",
            max_bytes=10_000,
            deadline_seconds=0.5,
        )
    )

    assert time.monotonic() - started < 1.0
    assert [preview.skipped for preview in previews] == [None] * 4 + ["time"] * 4
    assert all(call[2] <= 0.5 for call in source.calls)  # each fetch has at most what is left


def test_preview_view_survives_an_unexpected_error() -> None:
    registry = CollectorRegistry()
    with running(registry=registry) as client:
        state = client.app.state  # type: ignore[attr-defined]
        state.ui_images, real = FlakySource(), state.ui_images
        answer = run(
            client,
            "images",
            url=["https://example.org/a.png", "https://example.org/boom.png"],
            alt=["a", "b"],
        )
        state.ui_images = real  # the lifespan closes it

    assert answer.status_code == 200
    root = lxml.html.fragment_fromstring(answer.text, create_parent="div")
    assert len(root.xpath("//figure[@class='preview']//img")) == 1
    skipped = [" ".join(node.text_content().split()) for node in root.xpath("//figure/p")]
    assert skipped == ["skipped (error)"]
    assert (
        registry.get_sample_value("text_extraction_ui_image_previews_total", {"outcome": "error"})
        == 1.0
    )


def test_every_skip_reason_has_a_text() -> None:
    assert set(SKIP_REASONS) == set(OUTCOMES) - {SHOWN}
    assert set(get_args(SkipReason)) | {TIME} == set(SKIP_REASONS)
