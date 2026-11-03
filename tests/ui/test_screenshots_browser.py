"""The screenshot helper (package 15, ``pytest -m browser``): one full-page PNG per state of the
playground, language and width, named by position and state."""

from pathlib import Path

import pytest
from PIL import Image

from tests.ui.screenshots import STATES, capture, main

pytestmark = pytest.mark.browser


def test_capture_writes_every_state(tmp_path: Path) -> None:
    written = capture(tmp_path, languages=["en"], widths=[390])
    assert [path.name for path in written] == [
        f"{n:02d}-{state}-en-390.png" for n, state in enumerate(STATES, start=1)
    ]
    for path in written:
        with Image.open(path) as image:
            assert image.format == "PNG"
            assert image.width == 390


def test_main_takes_out_lang_and_width(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--out", str(tmp_path), "--lang", "de", "--width", "1440"]) == 0
    printed = capsys.readouterr().out.split()
    assert printed == [str(tmp_path / f"{n:02d}-{s}-de-1440.png") for n, s in enumerate(STATES, 1)]
