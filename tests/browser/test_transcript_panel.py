"""The transcript panel steps of a render (``browser_panel.py``), against a fake watch page."""

import asyncio
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from pytest_httpserver import HTTPServer

from tests.browser.conftest import open_stack
from tests.browser.site import Site
from tests.browser.youtube_site import (
    PANEL_BODY,
    clicks,
    panel_requests,
    serve_watch_site,
)
from text_extraction.browser_panel import TRANSCRIPT_PANEL_UNAVAILABLE
from text_extraction.media.panel import parse_panel
from text_extraction.render import Rendered, RenderOptions

pytestmark = pytest.mark.browser

SettingsFor = Callable[..., Any]


@pytest.fixture(scope="module")
def watch_server() -> Iterator[HTTPServer]:
    server = HTTPServer(host="127.0.0.1", port=0)
    server.start()
    yield server
    server.clear()
    if server.is_running():
        server.stop()


@pytest.fixture
def watch(watch_server: HTTPServer) -> Iterator[HTTPServer]:
    watch_server.clear()
    serve_watch_site(watch_server)
    yield watch_server
    watch_server.clear()


async def allow(url: str) -> None:
    pass


def render(
    settings_for: SettingsFor,
    site: Site,
    server: HTTPServer,
    path: str,
    *,
    transcript_panel: bool = True,
    **overrides: Any,
) -> Rendered:
    async def run() -> Rendered:
        async with open_stack(settings_for(**overrides), site, ports={server.port}) as stack:
            deadline = asyncio.get_running_loop().time() + 40
            return await stack.renderer.render(
                f"http://127.0.0.1:{server.port}{path}",
                allow,
                RenderOptions(transcript_panel=transcript_panel),
                deadline=deadline,
            )

    return asyncio.run(run())


def test_the_new_view_gives_the_get_panel_body(
    settings_for: SettingsFor, site: Site, watch: HTTPServer
) -> None:
    rendered = render(settings_for, site, watch, "/watch/new")

    assert rendered.transcript_panel == PANEL_BODY
    assert rendered.transcript_panel is not None
    assert parse_panel(rendered.transcript_panel, max_bytes=len(PANEL_BODY))
    assert TRANSCRIPT_PANEL_UNAVAILABLE not in rendered.warnings
    # The consent was declined before the transcript button was clicked.
    assert clicks(rendered.html) == ["reject", "expand", "transcript"]
    assert panel_requests(watch) == ["SOCS=x"]


def test_without_the_new_view_nothing_is_clicked(
    settings_for: SettingsFor, site: Site, watch: HTTPServer
) -> None:
    rendered = render(settings_for, site, watch, "/watch/old")

    assert rendered.transcript_panel is None
    assert TRANSCRIPT_PANEL_UNAVAILABLE in rendered.warnings
    assert "expand" not in clicks(rendered.html)
    assert "transcript" not in clicks(rendered.html)
    assert panel_requests(watch) == []


def test_a_failed_get_panel_is_a_warning(
    settings_for: SettingsFor, site: Site, watch: HTTPServer
) -> None:
    rendered = render(settings_for, site, watch, "/watch/failing")

    assert rendered.transcript_panel is None
    assert TRANSCRIPT_PANEL_UNAVAILABLE in rendered.warnings
    assert len(panel_requests(watch)) == 1
    # The render itself succeeded: the page was read.
    assert rendered.responses[-1].status == 200
    assert "Der Wasserkreislauf" in rendered.html


def test_a_body_over_the_cap_is_a_warning(
    settings_for: SettingsFor, site: Site, watch: HTTPServer
) -> None:
    cap = 2048
    assert len(PANEL_BODY) > cap
    rendered = render(settings_for, site, watch, "/watch/new", media_subtitle_max_bytes=cap)

    assert rendered.transcript_panel is None
    assert TRANSCRIPT_PANEL_UNAVAILABLE in rendered.warnings
    assert len(panel_requests(watch)) == 1


def test_a_consent_dialog_that_stays_open_is_a_warning(
    settings_for: SettingsFor, site: Site, watch: HTTPServer
) -> None:
    started = time.monotonic()
    rendered = render(settings_for, site, watch, "/watch/stuck")

    assert rendered.transcript_panel is None
    assert TRANSCRIPT_PANEL_UNAVAILABLE in rendered.warnings
    assert clicks(rendered.html) == ["reject"]
    assert panel_requests(watch) == []
    # The consent step's own cap, not the render's deadline, ended the wait.
    assert time.monotonic() - started < 20


def test_without_the_option_nothing_changes(
    settings_for: SettingsFor, site: Site, watch: HTTPServer
) -> None:
    rendered = render(settings_for, site, watch, "/watch/new", transcript_panel=False)

    assert rendered.transcript_panel is None
    assert TRANSCRIPT_PANEL_UNAVAILABLE not in rendered.warnings
    assert clicks(rendered.html) == []
    assert panel_requests(watch) == []
