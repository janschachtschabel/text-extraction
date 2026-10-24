"""``text-extraction-browser-worker --check`` with a real Chromium: the image build's proof that
the browser in the image starts."""

from pathlib import Path

import pytest

from text_extraction import browser_worker

pytestmark = pytest.mark.browser


def test_check_launches_chromium_and_exits_0(
    chromium: Path | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("BROWSER_ENABLED", "true")
    monkeypatch.setenv("BROWSER_UNBLOCK_ENABLED", "false")
    if chromium is not None:
        monkeypatch.setenv("BROWSER_EXECUTABLE_PATH", str(chromium))

    with pytest.raises(SystemExit) as exit_info:
        browser_worker.main(["--check"])

    assert exit_info.value.code == 0
