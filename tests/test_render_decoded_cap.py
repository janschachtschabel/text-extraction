"""The decoded-bytes cap of a render (audit B3) without a browser: ``render_page`` turns its own
cancel into ``too_large``, lets a caller's cancel through, and ignores bytes after the render."""

import asyncio
from typing import Any, cast

import pytest

from text_extraction.browser_render import _Render, render_page
from text_extraction.envelope import ErrorCode
from text_extraction.fetch import FetchError
from text_extraction.render import RenderOptions
from text_extraction.settings import Settings

RENDERED = object()


async def allow(_url: str) -> None:
    return None


def stub(monkeypatch: pytest.MonkeyPatch, run: Any) -> list[_Render]:
    """Replace the browser steps of ``_Render``; returns the renders made."""
    made: list[_Render] = []

    async def install(self: _Render) -> None:
        made.append(self)

    monkeypatch.setattr(_Render, "install", install)
    monkeypatch.setattr(_Render, "run", run)
    return made


async def rendered() -> object:
    settings = Settings(auth_mode="none")
    loop = asyncio.get_running_loop()
    return await render_page(
        cast(Any, None),
        "https://example.org/",
        allow,
        RenderOptions(),
        deadline=loop.time() + 5,
        settings=settings,
    )


def test_the_cap_ends_the_render_with_too_large(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run(self: _Render, deadline: float) -> object:
        self.over_cap()  # the page's bytes passed the cap while it loaded
        await asyncio.sleep(10)
        return RENDERED

    stub(monkeypatch, run)

    async def scenario() -> None:
        with pytest.raises(FetchError) as caught:
            await rendered()
        assert caught.value.code is ErrorCode.TOO_LARGE
        task = asyncio.current_task()
        assert task is not None
        assert task.cancelling() == 0  # the render's own cancel is taken back

    asyncio.run(scenario())


def test_a_callers_cancel_at_the_same_time_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run(self: _Render, deadline: float) -> object:
        self.over_cap()
        task = asyncio.current_task()
        assert task is not None
        task.cancel()  # the caller cancels too
        await asyncio.sleep(10)
        return RENDERED

    stub(monkeypatch, run)

    async def scenario() -> None:
        inner = asyncio.ensure_future(rendered())
        with pytest.raises(asyncio.CancelledError):
            await inner

    asyncio.run(scenario())


def test_bytes_after_the_render_change_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run(self: _Render, deadline: float) -> object:
        return RENDERED

    made = stub(monkeypatch, run)

    async def scenario() -> None:
        assert await rendered() is RENDERED
        (render,) = made
        render.decoded = render.decoded_cap + 1
        render.over_cap()  # a late chunk of the page
        assert render.stopping is None
        assert render.fault is None
        task = asyncio.current_task()
        assert task is not None
        assert task.cancelling() == 0
        await asyncio.sleep(0)  # no cancel reaches the caller

    asyncio.run(scenario())
