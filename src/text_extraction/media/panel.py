"""YouTube's new transcript view to cues.

The body is the page's own ``POST /youtubei/v1/get_panel`` answer, read by the browser render
when the watch page offers the new view (``PAmodern_transcript_view``). It is untrusted input:

* A body over ``max_bytes`` is refused before it is decoded.
* Invalid JSON (also too deeply nested JSON) and an answer without the segment list raise
  :class:`PanelError`; no other exception escapes. A segment list without segments is an empty
  list, not an error.

The segments are the ``transcriptSegmentViewModel`` entries under
``content.engagementPanelSectionListRenderer.content.sectionListRenderer.contents[*]
.itemSectionRenderer.contents[*].macroMarkersPanelItemViewModel.item.timelineItemViewModel
.contentItems[0]``, their text in ``simpleText``. The times come from the parallel list
``frameworkUpdates.entityBatchUpdate.mutations[*].payload.timedMarkersListSyncEntity
.timedListData.sections[0].timedSyncDataList`` (``videoTimeMs`` and ``endTimeMs``, strings of
milliseconds), paired by index when both lists have the same length and every entry is a number.
Otherwise a segment starts at its ``timestamp`` (``m:ss`` or ``h:mm:ss``) and ends where the next
one starts (the last one ends at its start); a segment whose timestamp cannot be read is skipped.
Segments without text are skipped after the pairing, so they do not shift the times.

Light enough for the parent process: no media library is imported.
"""

import json
import re
from typing import Any, Final

from text_extraction.media.captions import Cue

__all__ = ["TRANSCRIPT_PANEL_UNAVAILABLE", "PanelError", "parse_panel"]

# The warning of a render that could not read the transcript view (P13-S3), and of the answer
# that is then the video's details (P13-S5). Here, not in ``browser_panel.py``, so that the
# pipeline names it without importing the browser.
TRANSCRIPT_PANEL_UNAVAILABLE: Final = "transcript_panel_unavailable"

_TIMESTAMP: Final = re.compile(r"(?:(\d{1,4}):)?(\d{1,2}):(\d{2})", re.ASCII)
# Digits of a time in milliseconds: years of video, and far below Python's limit for int().
_MAX_MS_DIGITS: Final = 15


class PanelError(Exception):
    """The answer cannot be read: too large, not JSON, or without the segment list."""


def parse_panel(body: bytes, *, max_bytes: int) -> list[Cue]:
    """The cues of a ``get_panel`` answer, in panel order. Raises :class:`PanelError`."""
    if len(body) > max_bytes:
        raise PanelError(f"Transcript panel answer is too large ({len(body)} bytes).")
    try:
        answer = json.loads(body)
    except (ValueError, RecursionError) as error:  # UnicodeDecodeError is a ValueError
        raise PanelError("Transcript panel answer is not JSON.") from error
    segments = _segments(answer)
    times = _synced_times(answer, len(segments)) or _timestamp_times(segments)
    cues = []
    for index, (start, end) in times.items():
        text = segments[index].get("simpleText")
        if isinstance(text, str) and (cleaned := " ".join(text.split())):
            cues.append(Cue(start=start, end=end, text=cleaned))
    return cues


def _get(node: Any, *path: str | int) -> Any:
    """The value at ``path`` below ``node``, or None where a key or index is missing."""
    for step in path:
        if isinstance(step, str) and isinstance(node, dict):
            node = node.get(step)
        elif isinstance(step, int) and isinstance(node, list) and -len(node) <= step < len(node):
            node = node[step]
        else:
            return None
    return node


def _segments(answer: Any) -> list[dict[str, Any]]:
    sections = _get(
        answer, "content", "engagementPanelSectionListRenderer", "content", "sectionListRenderer"
    )
    contents = _get(sections, "contents")
    if not isinstance(contents, list):
        raise PanelError("Transcript panel answer has no segment list.")
    segments = []
    for section in contents:
        items = _get(section, "itemSectionRenderer", "contents")
        for item in items if isinstance(items, list) else []:
            segment = _get(
                item,
                "macroMarkersPanelItemViewModel",
                "item",
                "timelineItemViewModel",
                "contentItems",
                0,
                "transcriptSegmentViewModel",
            )
            if isinstance(segment, dict):
                segments.append(segment)
    return segments


def _synced_times(answer: Any, count: int) -> dict[int, tuple[float, float]]:
    """Start and end of each segment from the sync list, or empty when it does not fit."""
    mutations = _get(answer, "frameworkUpdates", "entityBatchUpdate", "mutations")
    for mutation in mutations if isinstance(mutations, list) else []:
        entity = _get(mutation, "payload", "timedMarkersListSyncEntity")
        if entity is None:
            continue
        timed = _get(entity, "timedListData", "sections", 0, "timedSyncDataList")
        if not isinstance(timed, list) or len(timed) != count:
            return {}
        times = {}
        for index, entry in enumerate(timed):
            start = _milliseconds(_get(entry, "videoTimeMs"))
            end = _milliseconds(_get(entry, "endTimeMs"))
            if start is None or end is None:
                return {}
            times[index] = (start, max(start, end))
        return times
    return {}


def _timestamp_times(segments: list[dict[str, Any]]) -> dict[int, tuple[float, float]]:
    """Start of each segment from its timestamp, end at the next segment's start."""
    starts = [
        (index, start)
        for index, segment in enumerate(segments)
        if (start := _clock(segment.get("timestamp"))) is not None
    ]
    times = {}
    for position, (index, start) in enumerate(starts):
        following = starts[position + 1][1] if position + 1 < len(starts) else start
        times[index] = (start, max(start, following))
    return times


def _milliseconds(value: Any) -> float | None:
    if (
        isinstance(value, str)
        and value.isascii()
        and value.isdigit()
        and len(value) <= _MAX_MS_DIGITS
    ):
        return int(value) / 1000
    return None


def _clock(value: Any) -> float | None:
    match = _TIMESTAMP.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    hours, minutes, seconds = match.groups()
    return float(int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds))
