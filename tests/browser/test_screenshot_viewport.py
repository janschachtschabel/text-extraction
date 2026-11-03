"""The per-request viewport (package 14): the browser window of a screenshot request.

A local page on pytest-httpserver (127.0.0.1) serves a desktop and a mobile layout by a CSS media
query, and a very tall page. The standard browser (Playwright) and the unblock browser
(Patchright, whose context has ``no_viewport``) both take the viewport of the render options.
"""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from pytest_httpserver import HTTPServer

from tests.browser.conftest import Stack, open_stack, run_pipeline
from tests.browser.site import Site, page
from tests.browser.test_renderer import deadline_in
from tests.browser.test_unblock import UnblockStack, open_unblock_stack
from tests.fakes import FakeArtifacts
from text_extraction.browser_options import ScreenshotOptions, Viewport
from text_extraction.render import Rendered, RenderOptions
from text_extraction.screenshot_image import png_size
from text_extraction.settings import Settings

pytestmark = pytest.mark.browser

SettingsFor = Callable[..., Any]

DESKTOP = "The desktop layout shows the wide navigation of the learning portal."
MOBILE = "The mobile layout shows the narrow menu of the learning portal."
LAYOUT = (
    "<style>#mobile { display: none; } @media (max-width: 600px) "
    "{ #desktop { display: none; } #mobile { display: block; } }</style>"
)
LAYOUT_BODY = f"<p id='desktop'>{DESKTOP}</p><p id='mobile'>{MOBILE}</p>"


@pytest.fixture
def pages(httpserver: HTTPServer) -> HTTPServer:
    httpserver.expect_request("/layout").respond_with_data(
        page(LAYOUT_BODY, head=LAYOUT), content_type="text/html; charset=utf-8"
    )
    httpserver.expect_request("/tall").respond_with_data(
        page("<p>A page much taller than any screenshot.</p><div style='height:20000px'></div>"),
        content_type="text/html; charset=utf-8",
    )
    return httpserver


def scenario(
    settings_for: SettingsFor,
    site: Site,
    pages: HTTPServer,
    body: Callable[[Stack], Awaitable[None]],
    **overrides: Any,
) -> None:
    async def run() -> None:
        async with open_stack(settings_for(**overrides), site, ports=[pages.port]) as stack:
            await body(stack)

    asyncio.run(run())


async def render(stack: Stack, url: str, options: RenderOptions) -> Rendered:
    return await stack.renderer.render(url, _allow, options, deadline=deadline_in(30))


async def _allow(url: str) -> None:
    pass


def test_a_narrow_viewport_gets_the_mobile_layout(
    settings_for: SettingsFor, site: Site, pages: HTTPServer
) -> None:
    async def body(stack: Stack) -> None:
        options = RenderOptions(screenshot=True, full_page=False, viewport=(390, 844))
        rendered = await render(stack, pages.url_for("/layout"), options)

        assert rendered.screenshot is not None
        assert png_size(rendered.screenshot) == (390, 844)
        assert rendered.screenshot_size == (390, 844)
        assert MOBILE in rendered.html
        assert DESKTOP not in rendered.html  # the media query hid it; hidden elements are dropped

        wide = await render(stack, pages.url_for("/layout"), RenderOptions(screenshot=True))
        assert wide.screenshot is not None
        assert png_size(wide.screenshot)[0] == 1920
        assert DESKTOP in wide.html
        assert MOBILE not in wide.html

    scenario(settings_for, site, pages, body)


def test_the_default_viewport_is_not_set_again(
    settings_for: SettingsFor, site: Site, pages: HTTPServer
) -> None:
    async def body(stack: Stack) -> None:
        rendered = await render(stack, pages.url_for("/layout"), RenderOptions(screenshot=True))
        assert rendered.screenshot is not None
        assert png_size(rendered.screenshot) == (1920, 1080)

    scenario(settings_for, site, pages, body)


def test_the_stored_screenshot_follows_viewport_format_and_width(
    settings_for: SettingsFor, site: Site, pages: HTTPServer
) -> None:
    async def body(stack: Stack) -> None:
        artifacts = FakeArtifacts()
        options = ScreenshotOptions(
            viewport=Viewport(width=1200, height=630), format="webp", width=600
        )
        envelope = await run_pipeline(
            stack,
            artifacts=artifacts,
            url=pages.url_for("/layout"),
            screenshot=True,
            full_page=False,
            screenshot_options=options,
        )

        assert envelope.status == "success", envelope.error
        [stored] = artifacts.screenshots
        assert stored.name == "screenshot.webp"
        assert stored.data[:4] == b"RIFF"
        assert stored.data[8:12] == b"WEBP"
        assert (stored.width, stored.height) == (600, 315)

    scenario(settings_for, site, pages, body)


def test_a_wide_viewport_clips_the_full_page_at_the_pixel_cap(
    settings_for: SettingsFor, site: Site, pages: HTTPServer
) -> None:
    async def body(stack: Stack) -> None:
        options = RenderOptions(screenshot=True, viewport=(3840, 1080))
        rendered = await render(stack, pages.url_for("/tall"), options)

        assert rendered.screenshot is not None
        # 16384 x 1920 / 3840 rows: the same number of pixels as a 1920 x 16384 capture.
        assert png_size(rendered.screenshot) == (3840, 8192)
        assert rendered.screenshot_size == (3840, 8192)
        assert "screenshot_clipped" in rendered.warnings

    scenario(settings_for, site, pages, body, screenshot_max_height_px=16384)


def test_a_wide_viewport_does_not_clip_a_page_under_the_cap(
    settings_for: SettingsFor, site: Site, pages: HTTPServer
) -> None:
    async def body(stack: Stack) -> None:
        rendered = await render(
            stack,
            pages.url_for("/layout"),
            RenderOptions(screenshot=True, viewport=(3840, 1080)),
        )

        assert rendered.screenshot is not None
        assert png_size(rendered.screenshot) == (3840, 1080)
        assert "screenshot_clipped" not in rendered.warnings

    scenario(settings_for, site, pages, body)


@pytest.fixture
def unblock_settings() -> Settings:
    return Settings(
        auth_mode="none",
        outbound_min_delay_ms=0,
        browser_enabled=True,
        browser_unblock_enabled=True,
        browser_unblock_channel="chromium",
        browser_unblock_headless=True,
        fetch_allow_private_networks=True,  # R9, as in conftest.py
        browser_idle_wait_seconds=1,
    )


def test_the_unblock_browser_takes_the_viewport_too(
    unblock_settings: Settings,
    site: Site,
    pages: HTTPServer,
    patchright_chromium: Path | None,
    tmp_path: Path,
) -> None:
    async def body(stack: UnblockStack) -> None:
        options = RenderOptions(unblock=True, screenshot=True, viewport=(390, 844))
        rendered = await stack.renderer.render(
            pages.url_for("/layout"), _allow, options, deadline=deadline_in(30)
        )

        assert rendered.screenshot is not None
        assert png_size(rendered.screenshot)[0] == 390
        assert MOBILE in rendered.html
        assert DESKTOP not in rendered.html

    async def run() -> None:
        async with open_unblock_stack(
            unblock_settings, site, patchright_chromium, tmp_path, ports=[pages.port]
        ) as stack:
            await body(stack)

    asyncio.run(run())
