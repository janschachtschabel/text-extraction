"""The temporary profiles of unblock mode's launcher (B14): a profile a crashed browser still
writes to, even after its removal, is removed again in the background for a bounded window,
and ``stop`` removes what is left."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from text_extraction import browser_unblock
from text_extraction.browser import PageProxy
from text_extraction.browser_unblock import PatchrightLauncher
from text_extraction.render import RenderOptions
from text_extraction.settings import Settings

PROXY = PageProxy("http://127.0.0.1:1", "page", "secret")
REMOVED = browser_unblock._removed


class StillWritten:
    """Stands in for removing a profile: the browser writes to it again ``times`` times."""

    def __init__(self, times: float) -> None:
        self.times = times
        self.calls = 0

    def __call__(self, profile: Path) -> bool:
        self.calls += 1
        if self.calls <= self.times:
            return False
        return REMOVED(profile)


@pytest.fixture
def launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PatchrightLauncher:
    monkeypatch.setattr(browser_unblock, "_REMOVE_PAUSE_SECONDS", 0.0)
    monkeypatch.setattr(browser_unblock, "_LATER_FIRST_PAUSE_SECONDS", 0.01)
    monkeypatch.setattr(browser_unblock, "_LATER_SECONDS", 0.5)
    launcher = PatchrightLauncher(
        Settings(auth_mode="none", browser_enabled=True), profile_root=tmp_path
    )
    launcher.user_agent = "Chrome"  # no browser to ask

    async def launch(_profile: Path, _proxy: PageProxy) -> Any:
        return object()

    monkeypatch.setattr(launcher, "_launch", launch)
    return launcher


def remover(monkeypatch: pytest.MonkeyPatch, times: float) -> StillWritten:
    fake = StillWritten(times)
    monkeypatch.setattr(browser_unblock, "_removed", fake)
    return fake


async def until(condition: Callable[[], bool], seconds: float = 2.0) -> bool:
    for _ in range(int(seconds / 0.01)):
        if condition():
            return True
        await asyncio.sleep(0.01)
    return condition()


def others() -> set["asyncio.Task[Any]"]:
    return asyncio.all_tasks() - {asyncio.current_task()}


def entries(root: Path) -> list[Path]:
    return list(root.iterdir())


def empty(root: Path) -> bool:
    return not entries(root)


def make_cache_index(profile: Path) -> None:
    index = profile / "Default" / "Cache" / "Cache_Data" / "index-dir"
    index.mkdir(parents=True)
    (index / "the-real-index").write_bytes(b"index")


def test_profile_still_written_to_is_removed_later_without_waiting(
    launcher: PatchrightLauncher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = remover(monkeypatch, browser_unblock._REMOVE_ATTEMPTS + 2)

    async def main() -> None:
        _, context = await launcher.open(None, PROXY, RenderOptions())
        await launcher.release(context)
        assert fake.calls == browser_unblock._REMOVE_ATTEMPTS  # the release did not wait
        assert not empty(tmp_path)
        assert await until(lambda: empty(tmp_path))
        assert await until(lambda: not others())  # the window is bounded
        await launcher.stop()
        assert empty(tmp_path)

    asyncio.run(main())


def test_profile_written_again_after_its_removal_is_removed_again(
    launcher: PatchrightLauncher, tmp_path: Path
) -> None:
    async def main() -> None:
        _, context = await launcher.open(None, PROXY, RenderOptions())
        (profile,) = entries(tmp_path)
        await launcher.release(context)
        assert empty(tmp_path)
        # A process of the crashed browser that was still ending writes to it once more.
        await asyncio.to_thread(make_cache_index, profile)
        assert await until(lambda: empty(tmp_path))
        assert await until(lambda: not others())
        await launcher.stop()

    asyncio.run(main())


def test_profile_left_after_the_window_is_removed_by_stop(
    launcher: PatchrightLauncher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = remover(monkeypatch, float("inf"))

    async def main() -> None:
        _, context = await launcher.open(None, PROXY, RenderOptions())
        with capture_logs() as logs:
            await launcher.release(context)
            assert await until(lambda: not others())  # the window is bounded
        assert not empty(tmp_path)
        assert [entry["event"] for entry in logs] == ["browser_profile_left"]
        assert str(tmp_path) not in str(logs)
        fake.times = 0
        await launcher.stop()
        assert empty(tmp_path)

    asyncio.run(main())


def test_stop_ends_the_retries_and_removes_the_profiles(
    launcher: PatchrightLauncher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser_unblock, "_LATER_SECONDS", 60.0)
    fake = remover(monkeypatch, float("inf"))

    async def main() -> None:
        for _ in range(3):
            _, context = await launcher.open(None, PROXY, RenderOptions())
            await launcher.release(context)
        assert len(others()) == 1  # one sweep, however many profiles
        fake.times = 0
        async with asyncio.timeout(2):
            await launcher.stop()
        assert not others()
        assert empty(tmp_path)

    asyncio.run(main())


def test_profile_written_again_and_left_is_removed_by_stop(
    launcher: PatchrightLauncher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = remover(monkeypatch, 0)

    async def main() -> None:
        _, context = await launcher.open(None, PROXY, RenderOptions())
        (profile,) = entries(tmp_path)
        await launcher.release(context)
        await asyncio.to_thread(make_cache_index, profile)
        fake.times = float("inf")
        assert await until(lambda: not others())
        assert not empty(tmp_path)
        fake.times = 0
        await launcher.stop()
        assert empty(tmp_path)

    asyncio.run(main())
