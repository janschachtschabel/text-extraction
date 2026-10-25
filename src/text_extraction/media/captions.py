"""Caption tracks to cues.

WebVTT, SRT, TTML and YouTube's ``json3`` become a list of :class:`Cue` ``(start, end, text)``
with times in seconds. The track is untrusted input from a platform or a page:

* A body over ``max_bytes`` is refused before it is decoded.
* The bytes are decoded by their byte order mark, else by the declared ``charset``, else as
  UTF-8, else by charset-normalizer (windows-1252 when it has no opinion); this never fails.
* TTML goes through ``defusedxml``: a DTD, an entity declaration or an external reference
  (the classic XML attacks) refuses the track. Any namespace prefix works (``tt:p``, ``p``).
* A track that is not what its format says (HTML where WebVTT should be, XML that is not TTML,
  broken JSON) raises :class:`CaptionError`, so the caller can try the next tier. A track
  without cues is an empty list, not an error.

Cleaning keeps the spoken record: tags (``<c>``, ``<i>``, ``<v Name>``, ``<lang>``, ``<ruby>``,
timing tags, SRT's ``{\\an8}``) and entities are removed and whitespace collapses, but a sound
label such as ``[Musik]`` stays a cue. Only the rolling repetition of automatic captions is
dropped (YouTube shows two lines at a time and repeats the first line of each cue as the last
line of the one before): a cue's leading lines that repeat the previous cue's last lines, when
the cue has further lines after them; a cue that is only such a repeat goes when it lasts under
``TRANSITION_SECONDS`` (YouTube's transition cues), else it is a real repeat ("Nein." twice)
and stays. A dropped cue lengthens the cue it repeats, so the pauses between cues stay true.
TTML times are absolute: the offsets of parent elements, and frame and tick offset times, are
not supported (a ``<p>`` whose ``begin`` we cannot read is skipped).

Light enough for the parent process: no media library is imported.
"""

import codecs
import html
import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Final, Literal
from xml.etree.ElementTree import Element, ParseError

from charset_normalizer import from_bytes
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as parse_xml

__all__ = ["CaptionError", "CaptionFormat", "Cue", "parse_captions"]

CaptionFormat = Literal["vtt", "srt", "ttml", "json3"]

DEFAULT_MAX_BYTES: Final = 5_000_000  # MEDIA_SUBTITLE_MAX_BYTES
TRANSITION_SECONDS: Final = 0.1
# Rolling captions repeat one or two lines; a longer overlap is not looked for (the search
# would be quadratic in the lines of a hostile cue).
MAX_REPEATED_LINES: Final = 4
MAX_TTML_DEPTH: Final = 64  # of elements around the paragraphs; real files nest 5 or 6 deep

_BOMS: Final = (
    (codecs.BOM_UTF32_LE, "utf-32"),  # before UTF-16 LE, whose BOM is a prefix of this one
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)

_TIME: Final = r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
_TIMING: Final = re.compile(rf"^\s*{_TIME}\s*-->\s*{_TIME}")
_VTT_HEADER: Final = re.compile(r"WEBVTT(?:[ \t\n]|$)")
_VTT_SKIPPED_BLOCK: Final = re.compile(r"(?:NOTE|STYLE|REGION)(?:[ \t]|$)")
_VTT_BLOCKS: Final = re.compile(r"\n{2,}")  # an empty line; a line of blanks is cue text
_SRT_BLOCKS: Final = re.compile(r"\n[ \t]*\n")
# A tag starts with a letter or a slash and a letter (so "3 < 5" is text), or is a timing tag.
_TAG: Final = re.compile(r"</?[A-Za-z][^<>]*>|<\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3}>")
_ASS_TAG: Final = re.compile(r"\{\\[^{}]*\}")
_CLOCK: Final = re.compile(r"^(\d+):(\d{2}):(\d{2}(?:\.\d+)?)(?::(\d+))?$")
_OFFSET: Final = re.compile(r"^(\d+(?:\.\d+)?)(ms|h|m|s)$")
_OFFSET_SECONDS: Final = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}
_BREAK: Final = ""  # stands for <br/> while a TTML paragraph's text is joined


class CaptionError(Exception):
    """The track cannot be read: too large, or not what its format says."""


@dataclass(frozen=True, slots=True)
class Cue:
    """One piece of the spoken record: caption cue or speech recognition segment."""

    start: float
    end: float
    text: str


# A cue before cleaning: start, end, lines of text.
_Raw = tuple[float, float, list[str]]


def parse_captions(
    body: bytes,
    fmt: CaptionFormat,
    charset: str | None = None,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> list[Cue]:
    """The cues of a caption track, in track order. Raises :class:`CaptionError`."""
    if len(body) > max_bytes:
        raise CaptionError(f"Caption track is too large ({len(body)} bytes, limit {max_bytes}).")
    text = _decode(body, charset)
    if not text.strip():
        return []
    match fmt:
        case "vtt":
            return _to_cues(_vtt(text))
        case "srt":
            return _to_cues(_srt(text))
        case "ttml":
            return _to_cues(_ttml(text))
        case "json3":
            return _to_cues(_json3(text))
    raise ValueError(f"Unknown caption format: {fmt!r}")


# --- bytes to text -----------------------------------------------------------------------------


def _decode(body: bytes, charset: str | None) -> str:
    for bom, codec in _BOMS:
        if body.startswith(bom):
            return body.decode(codec, errors="replace").removeprefix("﻿")
    candidates = ([charset.strip().strip("\"'")] if charset else []) + ["utf-8"]
    for label in candidates:
        try:
            return body.decode(label)
        except (UnicodeDecodeError, LookupError, ValueError):
            continue
    best = from_bytes(body).best()
    if best is not None:
        return str(best)
    return body.decode("cp1252", errors="replace")


def _newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


# --- WebVTT and SRT ----------------------------------------------------------------------------


def _seconds(match: re.Match[str], offset: int) -> float:
    hours, minutes, seconds, fraction = match.groups()[offset : offset + 4]
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + float(f"0.{fraction}")


def _vtt(text: str) -> Iterator[_Raw]:
    text = _newlines(text).lstrip()
    if not _VTT_HEADER.match(text):
        raise CaptionError("The track is not WebVTT (it does not start with WEBVTT).")
    blocks = _VTT_BLOCKS.split(text)[1:]  # the first block is the header
    for block in blocks:
        if _VTT_SKIPPED_BLOCK.match(block):
            continue
        yield from _block_cue(block, markup=True, ass=False)


def _srt(text: str) -> Iterator[_Raw]:
    for block in _SRT_BLOCKS.split(_newlines(text).strip()):
        yield from _block_cue(block, markup=True, ass=True)


def _block_cue(block: str, *, markup: bool, ass: bool) -> Iterator[_Raw]:
    lines = block.split("\n")
    for index, line in enumerate(lines[:2]):  # an identifier line may come first
        timing = _TIMING.match(line)
        if timing:
            body = lines[index + 1 :]
            if ass:
                body = [_ASS_TAG.sub("", line) for line in body]
            yield _seconds(timing, 0), _seconds(timing, 4), [_markup_text(line) for line in body]
            return


def _markup_text(line: str) -> str:
    """A cue line without tags and entities, whitespace collapsed."""
    return _collapse(html.unescape(_TAG.sub("", line)))


def _collapse(text: str) -> str:
    return " ".join(text.split())


# --- TTML --------------------------------------------------------------------------------------


def _local(tag: object) -> str:
    return tag.rpartition("}")[2] if isinstance(tag, str) else ""


def _ttml(text: str) -> Iterator[_Raw]:
    try:
        root = parse_xml(text, forbid_dtd=True)
    except (ParseError, DefusedXmlException, ValueError) as exc:
        raise CaptionError(f"The TTML track cannot be parsed: {type(exc).__name__}.") from exc
    if _local(root.tag) != "tt":
        raise CaptionError("The track is not TTML (its root element is not <tt>).")
    frame_rate = _frame_rate(root)
    for paragraph in _paragraphs(root):
        begin = _ttml_time(paragraph.get("begin"), frame_rate)
        if begin is None:
            continue
        end = _ttml_time(paragraph.get("end"), frame_rate)
        if end is None:
            duration = _ttml_time(paragraph.get("dur"), frame_rate)
            end = begin + (duration or 0.0)
        yield begin, end, _ttml_lines(paragraph)


def _paragraphs(root: Element) -> Iterator[Element]:
    """The ``<p>`` elements in document order. A ``<p>`` inside a ``<p>`` is invalid TTML and no
    cue of its own: its text belongs to the outer one, so nothing is read twice."""
    stack = [(root, 0)]
    while stack:
        element, depth = stack.pop()
        if _local(element.tag) == "p":
            yield element
            continue
        if depth > MAX_TTML_DEPTH:
            raise CaptionError("The TTML track is nested too deeply.")
        stack.extend((child, depth + 1) for child in reversed(element))


def _ttml_lines(paragraph: Element) -> list[str]:
    for element in paragraph.iter():
        if _local(element.tag) == "br":
            element.tail = _BREAK + (element.tail or "")
    return [_collapse(line) for line in "".join(paragraph.itertext()).split(_BREAK)]


def _frame_rate(root: Element) -> float:
    for name, value in root.attrib.items():
        if _local(name) == "frameRate":
            try:
                return float(value) or 30.0
            except ValueError:
                break
    return 30.0


def _ttml_time(value: str | None, frame_rate: float) -> float | None:
    """A TTML clock time (``00:01:02.5``, ``00:01:02:12`` with frames) or offset time (``2.5s``,
    ``300ms``, ``1m``) in seconds; None when absent or in a form we do not read."""
    if value is None:
        return None
    value = value.strip()
    if clock := _CLOCK.match(value):
        hours, minutes, seconds, frames = clock.groups()
        return (
            int(hours) * 3600 + int(minutes) * 60 + float(seconds) + int(frames or 0) / frame_rate
        )
    if offset := _OFFSET.match(value):
        return float(offset.group(1)) * _OFFSET_SECONDS[offset.group(2)]
    return None


# --- YouTube json3 -----------------------------------------------------------------------------


def _json3(text: str) -> Iterator[_Raw]:
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise CaptionError("The json3 track is not valid JSON.") from exc
    if not isinstance(data, dict):
        raise CaptionError("The json3 track is not a JSON object.")
    events = data.get("events", [])
    if not isinstance(events, list):
        raise CaptionError("The json3 track has no list of events.")
    for event in events:
        if not isinstance(event, dict):
            continue
        start, segs = _number(event.get("tStartMs")), event.get("segs")
        if start is None or not isinstance(segs, list):
            continue  # window definitions and the like carry no text
        end = start + (_number(event.get("dDurationMs")) or 0.0)
        pieces = (seg.get("utf8") for seg in segs if isinstance(seg, dict))
        text_ = "".join(piece for piece in pieces if isinstance(piece, str))
        yield start / 1000, end / 1000, [_collapse(line) for line in text_.split("\n")]


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


# --- cleaning ----------------------------------------------------------------------------------


def _to_cues(raw: Iterable[_Raw]) -> list[Cue]:
    """Cues from cleaned lines, without the rolling repetition of automatic captions."""
    cues: list[Cue] = []
    previous: list[str] = []  # the lines of the previous cue, as it was written
    for start, end, lines in raw:
        lines = [line for line in lines if line]
        if not lines:
            continue
        repeated = max(
            (
                n
                for n in range(1, min(len(lines), len(previous), MAX_REPEATED_LINES) + 1)
                if lines[:n] == previous[-n:]
            ),
            default=0,
        )
        previous = lines
        if repeated < len(lines):
            cues.append(Cue(start, end, " ".join(lines[repeated:])))
        elif cues and end - start < TRANSITION_SECONDS:
            last = cues[-1]
            cues[-1] = Cue(last.start, max(last.end, end), last.text)
        else:
            cues.append(Cue(start, end, " ".join(lines)))
    return cues
