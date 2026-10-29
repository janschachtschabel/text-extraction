import random
import time
from itertools import pairwise

import pytest

from text_extraction.pii.spans import resolve, segments
from text_extraction.pii.types import Span


def span(start: int, end: int, type_: str = "PERSON", score: float = 0.85) -> Span:
    return Span(start=start, end=end, type=type_, score=score)  # type: ignore[arg-type]


def test_contained_span_dropped() -> None:
    text = "Contact Anna Maria Schmidt today"
    outer = span(8, 26, "PERSON", 0.85)
    inner = span(8, 12, "PERSON", 0.85)  # equal score: the contained one goes
    weaker = span(13, 18, "LOCATION", 0.4)

    assert resolve([inner, weaker, outer], text) == [outer]
    # A span of higher score inside a weaker one is kept, the weaker keeps its outer parts.
    stronger = span(13, 18, "LOCATION", 0.95)
    assert resolve([outer, stronger], text) == [span(8, 12), stronger, span(19, 26)]


def test_equal_spans_keep_one() -> None:
    text = "Anna Schmidt"

    assert len(resolve([span(0, 12), span(0, 12)], text)) == 1
    assert len(resolve([span(0, 12, "PERSON"), span(0, 12, "LOCATION")], text)) == 1


def test_partial_overlap_cut_to_outside_part() -> None:
    """N7: the NER span "IBAN DE89 3704" against the IBAN keeps the IBAN whole."""
    text = "IBAN DE89 3704 0044 0532 0130 00 bitte"
    iban = span(5, 32, "IBAN_CODE", 1.0)
    ner = span(0, 14, "PERSON", 0.85)

    assert resolve([ner, iban], text) == [span(0, 4, "PERSON", 0.85), iban]
    assert text[0:4] == "IBAN"


def test_partial_overlap_equal_scores_keeps_the_longer_then_the_earlier() -> None:
    text = "abcdefghij klmnop"
    longer = span(3, 12, "PERSON", 0.5)
    shorter = span(0, 6, "LOCATION", 0.5)
    assert resolve([shorter, longer], text) == [span(0, 3, "LOCATION", 0.5), longer]

    earlier = span(0, 6, "PERSON", 0.5)
    later = span(3, 9, "LOCATION", 0.5)
    assert resolve([later, earlier], text) == [earlier, span(6, 9, "LOCATION", 0.5)]


def test_lower_span_around_a_higher_one_keeps_both_outer_parts() -> None:
    text = "Dr. Anna Schmidt aus Bremen"
    outer = span(0, 16, "PERSON", 0.6)
    inner = span(4, 8, "ORGANIZATION", 0.9)

    assert resolve([outer, inner], text) == [
        span(0, 3, "PERSON", 0.6),
        inner,
        span(9, 16, "PERSON", 0.6),
    ]


def test_cut_without_letter_or_digit_dropped() -> None:
    text = "tel +49 30 123456, -- x"
    phone = span(4, 17, "PHONE_NUMBER", 0.75)
    ner = span(0, 20, "PERSON", 0.4)  # outside the phone: "tel " and ", --"

    resolved = resolve([ner, phone], text)

    assert resolved == [span(0, 3, "PERSON", 0.4), phone]  # "tel" has letters, ", --" has none
    assert resolve([phone, span(3, 17, "PERSON", 0.4)], text) == [phone]  # only the blank is left


def test_output_is_sorted_and_not_overlapping() -> None:
    text = "x" * 100
    spans = [span(50, 60), span(0, 10), span(5, 20, "LOCATION", 0.6), span(55, 80, "URL", 0.3)]

    resolved = resolve(spans, text)

    assert [s.start for s in resolved] == sorted(s.start for s in resolved)
    assert all(a.end <= b.start for a, b in pairwise(resolved))


def test_empty_and_out_of_range_spans_are_dropped_or_clamped() -> None:
    text = "Anna"

    assert resolve([span(2, 2), span(3, 1)], text) == []
    assert resolve([span(0, 99)], text) == [span(0, 4)]
    assert resolve([], text) == []


def test_segments_cut_at_blank_line_then_line_then_space() -> None:
    blank = "aaaa bbbb\nccc\n\ndddd eeee\nffff"
    assert segments(blank, 20, overlap=0) == [(0, "aaaa bbbb\nccc\n\n"), (15, "dddd eeee\nffff")]

    line = "aaaa bbbb\nccccccccc dddd eeee"
    assert segments(line, 20, overlap=0) == [(0, "aaaa bbbb\n"), (10, "ccccccccc dddd eeee")]

    space = "aaaa bbbb cccc dddd eeee"
    assert segments(space, 12, overlap=0) == [(0, "aaaa bbbb "), (10, "cccc dddd "), (20, "eeee")]


def test_segments_hard_cut_without_a_separator() -> None:
    assert segments("x" * 25, 10, overlap=0) == [(0, "x" * 10), (10, "x" * 10), (20, "x" * 5)]


def test_segments_short_text_is_one_piece() -> None:
    assert segments("short", 20, overlap=5) == [(0, "short")]
    assert segments("", 20, overlap=5) == []
    assert segments("a" * 20, 20, overlap=5) == [(0, "a" * 20)]


def test_segments_overlap_starts_the_next_piece_before_the_cut_at_a_word() -> None:
    """A piece after the first starts up to ``overlap`` characters before the cut, after the first
    blank there: an entity with spaces across the cut (``DE89 3704 0044 ...``) is whole in the
    next piece. The cut itself still prefers a blank line, a line break, a space."""
    text = "aaaa bbbb cccc dddd eeee ffff"

    assert segments(text, 12, overlap=6) == [
        (0, "aaaa bbbb "),  # cut after the last space of the window
        (5, "bbbb cccc "),  # from the first word start of the 6 characters before the cut
        (10, "cccc dddd "),
        (15, "dddd eeee "),
        (20, "eeee ffff"),
    ]
    line = "aaaa bbbb\ncccc dddd eeee"
    assert segments(line, 14, overlap=5)[:2] == [(0, "aaaa bbbb\n"), (5, "bbbb\ncccc ")]


def test_segments_overlap_without_a_blank_starts_exactly_overlap_before_the_cut() -> None:
    assert segments("x" * 25, 10, overlap=3) == [
        (0, "x" * 10),
        (7, "x" * 10),
        (14, "x" * 10),
        (21, "x" * 4),
    ]


def test_segments_overlap_an_entity_with_spaces_across_a_cut_is_whole_in_one_piece() -> None:
    iban = "DE89 3704 0044 0532 0130 00"
    head = "wort " * 3998  # 19,990 characters: the IBAN starts 10 before the window's end
    text = f"{head}{iban} und mehr " + "wort " * 100

    pieces = segments(text)

    assert len(pieces) == 2
    first, second = pieces
    assert iban not in first[1]  # cut inside the IBAN, at its last space in the window
    assert iban in second[1]
    assert second[0] + second[1].index(iban) == text.index(iban)
    assert len(first[1]) <= 20_000
    assert len(second[1]) <= 20_000


@pytest.mark.parametrize("overlap", [-1, 20, 25])
def test_segments_overlap_must_be_below_the_piece_size(overlap: int) -> None:
    with pytest.raises(ValueError, match="overlap"):
        segments("abc def", 20, overlap=overlap)


def test_segments_default_overlap_is_500() -> None:
    from text_extraction.pii.spans import SEGMENT_OVERLAP

    assert SEGMENT_OVERLAP == 500
    pieces = segments("x" * 45_000)
    assert [offset for offset, _ in pieces] == [0, 19_500, 39_000]


@pytest.mark.parametrize(
    "text",
    [
        "word " * 5000,
        "x" * 12345,
        "line one\n\nline two\nline three " * 700,
        "\n" * 300 + "a" + " " * 300,
        "😀 emoji and ümlauts " * 900,
    ],
    ids=["words", "no space", "mixed", "separators only", "non-ascii"],
)
def test_segments_offsets_reassemble_text(text: str) -> None:
    pieces = segments(text, 1000, overlap=0)

    assert all(0 < len(piece) <= 1000 for _, piece in pieces)
    assert "".join(piece for _, piece in pieces) == text
    assert all(text[offset : offset + len(piece)] == piece for offset, piece in pieces)
    assert [offset for offset, _ in pieces] == sorted({offset for offset, _ in pieces})


@pytest.mark.parametrize(
    "text",
    [
        "word " * 5000,
        "x" * 12345,
        "line one\n\nline two\nline three " * 700,
        "\n" * 300 + "a" + " " * 300,
        "😀 emoji and ümlauts " * 900,
        "a" + " " * 3000 + "b",
    ],
    ids=["words", "no space", "mixed", "separators only", "non-ascii", "one long blank"],
)
def test_segments_with_overlap_cover_the_text_and_overlap_each_cut(text: str) -> None:
    """With an overlap the pieces still cover every character, in order, each at its offset, and
    each piece after the first starts at most ``overlap`` characters before the previous end."""
    pieces = segments(text, 1000, overlap=100)

    assert all(0 < len(piece) <= 1000 for _, piece in pieces)
    assert all(text[offset : offset + len(piece)] == piece for offset, piece in pieces)
    assert pieces[0][0] == 0
    assert pieces[-1][0] + len(pieces[-1][1]) == len(text)
    for (offset, piece), (next_offset, _) in pairwise(pieces):
        end = offset + len(piece)
        assert offset < next_offset <= end
        assert end - next_offset <= 100
    assert len(pieces) <= 2 * len(text) // 900 + 1  # the overlap costs a tenth, never quadratic


def test_segments_default_size_is_20000() -> None:
    pieces = segments("x" * 45_000)

    assert [len(piece) for _, piece in pieces] == [20_000, 20_000, 6_000]


def test_segments_rejects_a_size_below_one() -> None:
    with pytest.raises(ValueError, match="max_chars"):
        segments("abc", 0)


def _reference_resolve(spans: list[Span], text: str) -> list[Span]:
    """The same rules, written the slow obvious way: scan every accepted span."""
    accepted: list[Span] = []
    ordered = sorted(spans, key=lambda s: (-s.score, s.start - s.end, s.start, s.type))
    for candidate in ordered:
        if candidate.end <= candidate.start:
            continue
        taken = sorted(
            (o for o in accepted if o.start < candidate.end and candidate.start < o.end),
            key=lambda o: o.start,
        )
        if not taken:
            accepted.append(candidate)
            continue
        parts, position = [], candidate.start
        for other in taken:
            if other.start > position:
                parts.append((position, other.start))
            position = max(position, other.end)
        if position < candidate.end:
            parts.append((position, candidate.end))
        for start, end in parts:
            piece = text[start:end]
            if not any(char.isalnum() for char in piece):
                continue
            start += len(piece) - len(piece.lstrip())
            accepted.append(
                Span(start, start + len(piece.strip()), candidate.type, candidate.score)
            )
    return sorted(accepted, key=lambda s: (s.start, s.end))


def _many_spans(count: int, seed: int) -> tuple[str, list[Span]]:
    """``count`` spans over a text of words: mostly disjoint, some contained in or overlapping
    their neighbour, with a few distinct scores."""
    rng = random.Random(seed)  # noqa: S311 - test data, not a secret
    text = "word " * (count * 3)
    spans = []
    for index in range(count):
        start = index * 14 + rng.randrange(0, 4)
        length = rng.choice([4, 4, 4, 9, 20])  # 20 reaches into the next span or two
        spans.append(
            Span(start, min(start + length, len(text)), "PERSON", rng.choice([0.4, 0.6, 0.85]))
        )
    return text, spans


def test_resolve_matches_a_brute_force_reference() -> None:
    text, spans = _many_spans(400, seed=7)

    assert resolve(spans, text) == _reference_resolve(spans, text)


def test_resolve_many_spans_is_fast() -> None:
    text, spans = _many_spans(50_000, seed=11)

    started = time.perf_counter()
    resolved = resolve(spans, text)
    elapsed = time.perf_counter() - started

    assert len(resolved) > 40_000
    assert all(a.end <= b.start for a, b in pairwise(resolved))
    assert elapsed < 2.0
