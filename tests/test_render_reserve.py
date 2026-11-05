"""The time a render keeps free after its page work (``_Render.run``), without a browser: what
reading the page needs, more for a screenshot, and more again for its encode (audit A1)."""

import asyncio
from typing import Any, cast

import pytest

from text_extraction.browser_render import _Render, render_page
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
