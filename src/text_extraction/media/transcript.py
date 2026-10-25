"""A transcript in the service's content shape, from cues or speech recognition segments.

The content is paragraphs of running text: a new paragraph starts after a pause of 2 seconds or
more between one cue's end and the next cue's start, or after 6 sentences (a sentence ends at
``.``, ``!`` or ``?`` followed by a space or the end; the check is made after each cue, so a cue
is never cut). ``# <title>`` comes first when there is a title; ``## <chapter>`` stands before the
first cue that starts at or after the chapter's start (a chapter with no cue after it is
dropped) and starts a new paragraph.

The Markdown is built so that nothing a speaker says can become markup: ``*``, ``_``, backticks,
brackets, ``<``, ``&`` and backslashes are escaped, and so is a marker at the start of a
paragraph (``#``, ``-``, ``+``, ``>``, ``|``, ``~`` or ``12.``). The formats then come from
``documents.formats.from_markdown`` like those of a document: ``html`` escapes raw HTML,
``text`` has no heading markers.

``Transcription`` is what speech recognition made of a media file (the ASR worker's children):
its segments, the language heard, the duration and the model, for
``Pipeline.transcribed_result``.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from text_extraction.convert import OutputFormat
from text_extraction.documents.formats import from_markdown
from text_extraction.media.captions import Cue
from text_extraction.media.types import MediaKind

__all__ = ["Chapter", "Transcription", "build_transcript"]

PAUSE_SECONDS: Final = 2.0
MAX_SENTENCES: Final = 6
MAX_WORDS: Final = 120

_EPSILON = 1e-6  # a gap of 1.9999999 s computed from rounded times is a gap of 2 s
# A closing quote or bracket may stand between the mark and the space.
_SENTENCE_END: Final = re.compile(r"[.!?]+[\"'\u201d\u2019\u00bb)\]]*(?=\s|$)")
_INLINE_MARKUP: Final = re.compile(r"[\\`*_\[\]<&]")
_BLOCK_MARKER: Final = re.compile(r"^(?:([#>|+~-])|(\d+)([.)]))")


@dataclass(frozen=True, slots=True)
class Chapter:
    start: float
    title: str


@dataclass(frozen=True, slots=True)
class Transcription:
    """Speech recognition of one media file: ``segments`` (start, end and text, in seconds),
    the ``language`` heard (ISO 639-1), the audio's ``duration`` in seconds and the ``model``'s
    name. ``kind`` is what the transcribed bytes were (for the result's ``content_type``),
    ``fetch_ms`` and ``convert_ms`` the time of the download and of the transcription,
    ``upstream_status`` the answer of a downloaded media file (None for an upload or a platform
    video)."""

    segments: list[Cue]
    language: str
    duration: float
    model: str
    kind: MediaKind | None = None
    fetch_ms: int = 0
    convert_ms: int = 0
    upstream_status: int | None = None


def build_transcript(
    cues: Sequence[Cue],
    title: str | None,
    chapters: Sequence[Chapter],
    fmt: OutputFormat,
) -> str:
    """The transcript in ``fmt``; an empty string when there is neither title nor text."""
    blocks: list[str] = []
    heading = _one_line(title)
    if heading:
        blocks.append(f"# {_escape_heading(heading)}")
    pending = sorted((c for c in chapters if _one_line(c.title)), key=lambda c: c.start)
    pending.reverse()  # the earliest chapter last, to pop it

    parts: list[str] = []
    sentences = 0
    words = 0
    previous_end = 0.0

    def close_paragraph() -> None:
        nonlocal sentences, words
        if parts:
            blocks.append(_escape_paragraph(" ".join(parts)))
        parts.clear()
        sentences = 0
        words = 0

    for cue in cues:
        text = _one_line(cue.text)
        if not text:
            continue
        while pending and pending[-1].start <= cue.start:
            close_paragraph()
            blocks.append(f"## {_escape_heading(_one_line(pending.pop().title))}")
        if parts and cue.start - previous_end >= PAUSE_SECONDS - _EPSILON:
            close_paragraph()
        parts.append(text)
        sentences += len(_SENTENCE_END.findall(text))
        words += len(text.split())
        previous_end = cue.end
        if sentences >= MAX_SENTENCES or words >= MAX_WORDS:
            close_paragraph()
    close_paragraph()
    return from_markdown("\n\n".join(blocks), fmt)


def _one_line(text: str | None) -> str:
    return " ".join(text.split()) if text else ""


def _escape_inline(text: str) -> str:
    return _INLINE_MARKUP.sub(lambda m: "\\" + m.group(), text)


def _escape_heading(text: str) -> str:
    return _escape_inline(text).replace("#", "\\#")


def _escape_paragraph(text: str) -> str:
    escaped = _escape_inline(text)
    return _BLOCK_MARKER.sub(
        lambda m: f"\\{m.group(1)}" if m.group(1) else f"{m.group(2)}\\{m.group(3)}", escaped
    )
