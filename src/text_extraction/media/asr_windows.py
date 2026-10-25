"""Speech recognition of long audio in bounded windows (used by ``media/asr_child.py``).

faster-whisper 1.2.1's ``WhisperModel.transcribe`` copies the audio it gets (the VAD collects
the speech into a new array) and computes the log-mel features of all of it at once: its
memory grows with the audio, about 3.4 GiB for an hour of speech, past the child's
``ASR_MEMORY_LIMIT_MB`` next to the model. So the decoded audio (16 kHz mono float32) is
handed over in windows of at most ``WINDOW_SECONDS`` (10 minutes, about 0.6 GiB of features
and copies), each a view of the decoded array, never a copy and never a path.

Where a window ends: at the quietest 20 ms of the last ``SEARCH_SECONDS`` (30 s) before
``WINDOW_SECONDS``, by mean energy. Speech has a pause between words or sentences every few
seconds, so the cut falls between words, and no word is lost or heard twice. That is why the
windows neither overlap nor need a dedupe: matching the overlapping text of two windows would be
a fuzzy guess that can drop or double words itself, while a cut in a pause leaves nothing to
match. Only 30 s without any pause (continuous music, for example) gives a hard cut at the
quietest moment found. Each window then runs with faster-whisper's own VAD filter and without
the previous text as its prompt (``condition_on_previous_text=False``, as for one call), and its
segments are moved by the window's start onto the file's clock.

The language: the requested one, else the one Whisper detects in the first window that has
speech, kept for the later windows (a window of music or silence does not decide it).
"""

from collections.abc import Iterable
from typing import Any, Final, Protocol

import numpy as np

__all__ = [
    "FRAME_SECONDS",
    "SAMPLE_RATE",
    "SEARCH_SECONDS",
    "WINDOW_SECONDS",
    "SpeechModel",
    "transcribe_windows",
    "window_bounds",
]

SAMPLE_RATE: Final = 16_000
WINDOW_SECONDS: Final = 600
SEARCH_SECONDS: Final = 30
FRAME_SECONDS: Final = 0.02

type Audio = np.ndarray[Any, np.dtype[np.float32]]


class SpeechModel(Protocol):
    """The part of faster-whisper's ``WhisperModel`` used here (a fake in tests)."""

    def transcribe(self, audio: Audio, **kwargs: Any) -> tuple[Iterable[Any], Any]: ...


def window_bounds(
    audio: Audio,
    *,
    window_seconds: float = WINDOW_SECONDS,
    search_seconds: float = SEARCH_SECONDS,
) -> list[tuple[int, int]]:
    """``(start, end)`` sample indices of the windows: contiguous, covering ``audio``, each at
    most ``window_seconds`` long and ending at the quietest frame of its last
    ``search_seconds`` (the file's end for the last one)."""
    size = len(audio)
    window = int(window_seconds * SAMPLE_RATE)
    search = min(int(search_seconds * SAMPLE_RATE), window - 1)
    frame = max(int(FRAME_SECONDS * SAMPLE_RATE), 1)
    bounds: list[tuple[int, int]] = []
    start = 0
    while size - start > window:
        low = start + window - search
        frames = search // frame
        region = audio[low : low + frames * frame].reshape(frames, frame)
        energy = np.square(region, dtype=np.float64).mean(axis=1)
        quietest = int(np.argmin(energy))
        cut = low + quietest * frame + frame // 2
        bounds.append((start, cut))
        start = cut
    bounds.append((start, size))
    return bounds


def transcribe_windows(
    model: SpeechModel,
    audio: Audio,
    *,
    language: str | None,
    window_seconds: float = WINDOW_SECONDS,
    search_seconds: float = SEARCH_SECONDS,
) -> dict[str, Any]:
    """The ``transcribe`` reply of a child: the segments of all windows as ``[start, end,
    text]`` in the file's seconds (empty texts dropped), the language and the duration."""
    segments: list[list[Any]] = []
    chosen = language
    first: str | None = None
    for start, end in window_bounds(
        audio, window_seconds=window_seconds, search_seconds=search_seconds
    ):
        found, info = model.transcribe(
            audio[start:end],
            language=chosen,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        offset = start / SAMPLE_RATE
        spoken = [
            [
                round(float(segment.start) + offset, 3),
                round(float(segment.end) + offset, 3),
                segment.text.strip(),
            ]
            for segment in found
        ]
        spoken = [segment for segment in spoken if segment[2]]
        first = first or info.language
        if chosen is None and spoken:
            chosen = info.language
        segments.extend(spoken)
    return {
        "segments": segments,
        "language": chosen or first or "",
        "duration": len(audio) / SAMPLE_RATE,
    }
