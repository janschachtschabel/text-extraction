"""Speech recognition in bounded windows (``media/asr_windows.py``), with a fake model: the
audio is cut at its quietest moment near each window's end, every window reaches the model as a
1-D float32 array (never a path) no longer than the window, and the segments come back on the
whole file's clock. Nothing here imports faster-whisper or PyAV."""

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

import numpy as np
import pytest

from text_extraction.media.asr_windows import (
    SAMPLE_RATE,
    SEARCH_SECONDS,
    WINDOW_SECONDS,
    transcribe_windows,
    window_bounds,
)


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Info:
    language: str
    duration: float


class FakeModel:
    """faster-whisper's ``WhisperModel.transcribe``: one segment per window (its first and last
    second), the language it is told or ``detected``; records what it got."""

    def __init__(self, detected: str = "de", silent: set[int] | None = None) -> None:
        self.detected = detected
        self.silent = silent or set()  # windows without speech
        self.calls: list[dict[str, Any]] = []

    def transcribe(self, audio: Any, **kwargs: Any) -> tuple[Iterable[Segment], Info]:
        index = len(self.calls)
        self.calls.append({"audio": audio, **kwargs})
        seconds = len(audio) / SAMPLE_RATE
        segments = [] if index in self.silent else [Segment(0.0, seconds, f" window {index} ")]
        return iter(segments), Info(kwargs.get("language") or self.detected, seconds)


def speech(seconds: float, pauses: list[float]) -> np.ndarray[Any, Any]:
    """Loud noise with half-second silences at ``pauses`` (seconds)."""
    audio = np.random.default_rng(0).uniform(-0.5, 0.5, int(seconds * SAMPLE_RATE))
    for at in pauses:
        start = int(at * SAMPLE_RATE)
        audio[start : start + SAMPLE_RATE // 2] = 0.0
    return audio.astype(np.float32)


def test_short_audio_is_one_window() -> None:
    audio = speech(30, [])

    assert window_bounds(audio) == [(0, len(audio))]


def test_windows_are_cut_in_a_pause_near_their_end() -> None:
    audio = speech(250, pauses=[50.0, 95.0, 185.0])

    bounds = window_bounds(audio, window_seconds=100, search_seconds=20)

    assert bounds[0][0] == 0
    assert all(a[1] == b[0] for a, b in pairwise(bounds))
    cuts = [end / SAMPLE_RATE for _start, end in bounds[:-1]]
    assert 95.0 < cuts[0] < 95.5  # in the pause at 95 s, not at 100 s
    assert 185.0 < cuts[1] < 185.5
    assert bounds[-1][1] == len(audio)
    assert all(end - start <= 100 * SAMPLE_RATE for start, end in bounds)


def test_without_a_pause_the_window_still_ends_in_time() -> None:
    audio = np.full(250 * SAMPLE_RATE, 0.3, dtype=np.float32)

    bounds = window_bounds(audio, window_seconds=100, search_seconds=20)

    assert all(end - start <= 100 * SAMPLE_RATE for start, end in bounds)
    assert bounds[0][0] == 0
    assert bounds[-1][1] == len(audio)
    assert all(a[1] == b[0] for a, b in pairwise(bounds))


def test_the_default_window_bounds_memory_for_long_media() -> None:
    """Two hours (``MEDIA_MAX_DURATION_SECONDS``) reach the model in windows of at most
    ``WINDOW_SECONDS``: faster-whisper builds the features of a whole call at once, so its
    memory follows the window, not the file."""
    audio = np.zeros(2 * 3600 * SAMPLE_RATE, dtype=np.float32)
    model = FakeModel()

    transcribe_windows(model, audio, language="de")

    assert WINDOW_SECONDS <= 20 * 60
    assert len(model.calls) >= 2 * 3600 // WINDOW_SECONDS
    for call in model.calls:
        window = call["audio"]
        assert isinstance(window, np.ndarray)  # never a path
        assert window.dtype == np.float32
        assert window.ndim == 1
        assert len(window) <= WINDOW_SECONDS * SAMPLE_RATE
        assert np.shares_memory(window, audio)  # a view: no copy of the file per window
        assert (call["vad_filter"], call["condition_on_previous_text"]) == (True, False)
    assert SEARCH_SECONDS < WINDOW_SECONDS


def test_segments_are_on_the_files_clock() -> None:
    audio = speech(250, pauses=[95.0, 170.0])
    model = FakeModel()

    reply = transcribe_windows(model, audio, language="de", window_seconds=100, search_seconds=20)

    bounds = window_bounds(audio, window_seconds=100, search_seconds=20)
    assert [segment[2] for segment in reply["segments"]] == ["window 0", "window 1", "window 2"]
    for (start, end), segment in zip(bounds, reply["segments"], strict=True):
        assert segment[0] == pytest.approx(start / SAMPLE_RATE, abs=0.001)
        assert segment[1] == pytest.approx(end / SAMPLE_RATE, abs=0.001)
    assert reply["duration"] == pytest.approx(250.0)
    assert reply["language"] == "de"


def test_the_language_heard_first_holds_for_the_later_windows() -> None:
    audio = speech(250, pauses=[])
    model = FakeModel(detected="en", silent={0})

    reply = transcribe_windows(model, audio, language=None, window_seconds=100, search_seconds=20)

    # The first window had no speech: its detection does not count; the second one's does.
    assert [call["language"] for call in model.calls] == [None, None, "en"]
    assert reply["language"] == "en"
    assert [segment[2] for segment in reply["segments"]] == ["window 1", "window 2"]


def test_no_speech_at_all() -> None:
    audio = speech(30, [])
    model = FakeModel(silent={0})

    reply = transcribe_windows(model, audio, language="de")

    assert reply["segments"] == []
    assert reply["language"] == "de"
