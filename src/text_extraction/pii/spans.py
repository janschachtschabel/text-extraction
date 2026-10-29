"""Spans: segmenting long text for the analyser and settling overlaps between its findings.

Pure and free of Presidio and spaCy. The analyser's cost grows faster than linearly with the
length of a text (its context step), so long text goes in segments of at most 20,000
characters; in the spike 175,000 characters took 11.3 s in one piece and 3.8 s in segments.

Segments overlap by 500 characters: a cut at a space may fall inside an entity that holds
spaces (an IBAN or a card number in groups, a phone number, a name of two words), which neither
piece would hold whole. 500 characters hold the longest of them (an IBAN in groups is at most
42 characters, a name or a phone number far less) with a few sentences of context for the NER
and the phone recognizer's context words, and cost 2.5 % more text to analyse. The same entity
found in both pieces has the same offsets in the text, or overlaps, which ``resolve`` settles.
"""

import re
from bisect import bisect_left, bisect_right, insort
from collections.abc import Iterable

from text_extraction.pii.types import Span

SEGMENT_CHARS = 20_000
SEGMENT_OVERLAP = 500  # see the module docstring

# Where a segment is cut, best first: after a blank line, after a line break, after a space.
_SEPARATORS = ("\n\n", "\n", " ")
_BLANK = re.compile(r"\s")


def segments(
    text: str, max_chars: int = SEGMENT_CHARS, overlap: int = SEGMENT_OVERLAP
) -> list[tuple[int, str]]:
    """``text`` in pieces of at most ``max_chars`` characters, as ``(offset, piece)``; a span's
    offset in the text is the piece's offset plus its own.

    A piece ends after the last blank line inside its window, else after the last line break,
    else after the last space, else it is cut at ``max_chars``; a separator counts only past the
    first ``overlap`` characters of the window, so every piece moves on. The next piece starts
    up to ``overlap`` characters before that end, at the first word start there (right after a
    blank), else exactly ``overlap`` characters before it: an entity across the cut is whole in
    the next piece. With ``overlap`` 0 the pieces in order are the text again.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be at least 1")
    if not 0 <= overlap < max_chars:
        raise ValueError("overlap must be at least 0 and below max_chars")
    pieces: list[tuple[int, str]] = []
    start = 0
    while start < len(text):
        window_end = start + max_chars
        if window_end >= len(text):
            pieces.append((start, text[start:]))
            break
        cut = window_end
        for separator in _SEPARATORS:
            found = text.rfind(separator, start + overlap, window_end)
            if found != -1:
                cut = found + len(separator)
                break
        pieces.append((start, text[start:cut]))
        start = _next_start(text, cut, overlap)
    return pieces


def _next_start(text: str, cut: int, overlap: int) -> int:
    """The first word start in the ``overlap`` characters before ``cut``, else their start."""
    if overlap == 0:
        return cut
    earliest = cut - overlap  # past the previous piece's start: ``segments`` cuts after it
    blank = _BLANK.search(text, earliest - 1, cut)
    return earliest if blank is None else blank.end()


def resolve(spans: Iterable[Span], text: str) -> list[Span]:
    """The spans without overlaps, sorted by start.

    A span inside another of equal or higher score is dropped (of two equal spans one stays).
    Where two spans overlap partly, the one of lower score is cut to its part outside the other,
    and the part is dropped when it holds no letter or digit; a span that surrounds a better
    one keeps both of its outer parts. On equal scores the longer span wins, then the earlier,
    and stays whole. A cut part loses the blanks at its ends: the replacement must not eat the
    space between words. Offsets past the text are clamped, empty spans are dropped.
    """
    # Disjoint and sorted by start, hence also by end: the spans that overlap a candidate are one
    # run of neighbours, which bisection finds (many thousands of spans must stay cheap).
    accepted: list[Span] = []
    for candidate in sorted(
        (_clamped(span, len(text)) for span in spans),
        key=lambda span: (-span.score, span.start - span.end, span.start, span.type),
    ):
        if candidate.end <= candidate.start:
            continue
        first = bisect_right(accepted, candidate.start, key=_end)
        last = bisect_left(accepted, candidate.end, key=_start)
        taken = accepted[first:last]
        if not taken:
            accepted.insert(first, candidate)
            continue
        for start, end in _outside(candidate, taken):
            piece = text[start:end]
            stripped = piece.strip()
            if not any(char.isalnum() for char in stripped):
                continue
            start += len(piece) - len(piece.lstrip())
            insort(
                accepted,
                Span(start, start + len(stripped), candidate.type, candidate.score),
                key=_start,
            )
    return accepted


def _start(span: Span) -> int:
    return span.start


def _end(span: Span) -> int:
    return span.end


def _clamped(span: Span, length: int) -> Span:
    return Span(max(span.start, 0), min(span.end, length), span.type, span.score)


def _outside(span: Span, taken: list[Span]) -> list[tuple[int, int]]:
    """The parts of ``span`` that none of ``taken`` covers, left to right."""
    parts: list[tuple[int, int]] = []
    position = span.start
    for other in sorted(taken, key=lambda span: span.start):
        if other.start > position:
            parts.append((position, other.start))
        position = max(position, other.end)
    if position < span.end:
        parts.append((position, span.end))
    return parts
