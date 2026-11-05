"""The time a render keeps free after its page work (``_Render.run``), without a browser: what
reading the page needs, more for a screenshot, and more again for its encode (audit A1), at
most a third of the render time for the encode."""

import asyncio
from typing import Any, cast

import pytest

from text_extraction.browser_render import _Render, encode_reserve_within, render_page
from text_extraction.render import RenderOptions
from text_extraction.settings import Settings


class _StopError(Exception):
    """Ends the stubbed render after its first page step."""


async def allow(_url: str) -> None:
    return None


@pytest.mark.parametrize(
    ("options", "reserve"),
    [
        (RenderOptions(), 1.0),
        (RenderOptions(encode_reserve=10.0), 1.0),  # no screenshot, nothing to encode
        (RenderOptions(screenshot=True), 3.0),
        (RenderOptions(screenshot=True, encode_reserve=10.0), 13.0),
    ],
)
def test_page_work_stops_the_reserve_before_the_deadline(
    monkeypatch: pytest.MonkeyPatch, options: RenderOptions, reserve: float
) -> None:
    seen: list[float] = []

    async def install(self: _Render) -> None:
        return None

    async def set_viewport(self: _Render) -> None:
        return None

    async def goto(self: _Render, kept: float) -> bool:
        seen.append(kept)
        raise _StopError

    monkeypatch.setattr(_Render, "install", install)
    monkeypatch.setattr(_Render, "_set_viewport", set_viewport)
    monkeypatch.setattr(_Render, "_goto", goto)

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        with pytest.raises(_StopError):
            await render_page(
                cast(Any, None),
                "https://example.org/",
                allow,
                options,
                deadline=loop.time() + 60,
                settings=Settings(auth_mode="none"),
            )

    asyncio.run(scenario())
    assert seen == [reserve]


@pytest.mark.parametrize(
    ("wanted", "render_seconds", "kept"),
    [
        (10.0, 60.0, 10.0),  # a third is more than wanted
        (10.0, 30.0, 10.0),
        (10.0, 15.0, 5.0),  # a short render keeps a third of its time
        (10.0, 3.0, 1.0),
        (0.0, 3.0, 0.0),  # nothing to encode
        (10.0, -1.0, 0.0),  # no time left: nothing to keep
    ],
)
def test_the_encode_reserve_is_at_most_a_third_of_the_render(
    wanted: float, render_seconds: float, kept: float
) -> None:
    assert encode_reserve_within(wanted, render_seconds) == pytest.approx(kept)


class _Page:
    """The page of a render: ``goto`` is the first thing it is asked for that matters here."""

    def __init__(self) -> None:
        self.timeouts: list[float] = []

    async def goto(
        self,
        _url: str,
        *,
        wait_until: str,
        timeout: float,  # noqa: ASYNC109 - Playwright's Page.goto
    ) -> None:
        self.timeouts.append(timeout)
        raise _StopError


def test_a_short_render_with_an_encoded_screenshot_reaches_goto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # BROWSER_TIMEOUT_SECONDS may be 5: the reserve of 3 + 10 s would leave no time to navigate.
    async def install(self: _Render) -> None:
        return None

    async def set_viewport(self: _Render) -> None:
        return None

    monkeypatch.setattr(_Render, "install", install)
    monkeypatch.setattr(_Render, "_set_viewport", set_viewport)
    page = _Page()

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        with pytest.raises(_StopError):
            await render_page(
                cast(Any, page),
                "https://example.org/",
                allow,
                RenderOptions(screenshot=True, encode_reserve=10.0),
                deadline=loop.time() + 10,
                settings=Settings(auth_mode="none"),
            )

    asyncio.run(scenario())
    # 10 s less 3 s for the screenshot and a third (about 3.3 s) for the encode.
    assert len(page.timeouts) == 1
    assert 3_000 < page.timeouts[0] <= 3_700
