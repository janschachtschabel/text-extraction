"""The Xvfb display the browser worker runs headful Chrome on (B14): a fake binary, and the
real one where it is installed."""

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from text_extraction.browser_unblock import Xvfb

# Stands in for Xvfb: tells the display number on the -displayfd descriptor, then runs until
# it is terminated (and notes its arguments). Bash: plain sh redirects descriptors 0-9 only.
FAKE = """#!/bin/bash
echo "$@" > "{log}"
while [ $# -gt 0 ]; do
  if [ "$1" = "-displayfd" ]; then fd="$2"; fi
  shift
done
echo 99 >&"$fd"
trap 'exit 0' TERM
while true; do sleep 0.1; done
"""
SILENT = """#!/bin/sh
exit 3
"""


def fake_binary(tmp_path: Path, script: str) -> Path:
    binary = tmp_path / "Xvfb"
    binary.write_text(script.format(log=tmp_path / "args"))
    binary.chmod(0o755)
    return binary


def test_start_sets_display_and_stop_restores_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPLAY", ":5")
    xvfb = Xvfb(binary=str(fake_binary(tmp_path, FAKE)))

    async def scenario() -> None:
        await xvfb.start(":99")
        assert os.environ["DISPLAY"] == ":99"
        args = (tmp_path / "args").read_text().split()
        assert args[0] == ":99"
        assert "-nolisten" in args
        assert "1920x1080x24" in args
        assert xvfb.running
        await xvfb.stop()
        assert not xvfb.running
        assert os.environ["DISPLAY"] == ":5"
        await xvfb.stop()  # twice is harmless

    asyncio.run(scenario())


def test_a_server_that_does_not_come_up_fails_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    xvfb = Xvfb(binary=str(fake_binary(tmp_path, SILENT)))

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="Xvfb"):
            await xvfb.start(":99")
        assert "DISPLAY" not in os.environ
        assert not xvfb.running

    asyncio.run(scenario())


@pytest.mark.skipif(shutil.which("Xvfb") is None, reason="Xvfb is not installed")
def test_real_xvfb_starts_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    xvfb = Xvfb()

    async def scenario() -> None:
        await xvfb.start(":98")
        assert os.environ["DISPLAY"] == ":98"
        await xvfb.stop()
        assert "DISPLAY" not in os.environ

    asyncio.run(scenario())
