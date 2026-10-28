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
    assert segments(blank, 20) == [(0, "aaaa bbbb\nccc\n\n"), (15, "dddd eeee\nffff")]

    line = "aaaa bbbb\nccccccccc dddd eeee"
    assert segments(line, 20) == [(0, "aaaa bbbb\n"), (10, "ccccccccc dddd eeee")]

    space = "aaaa bbbb cccc dddd eeee"
    assert segments(space, 12) == [(0, "aaaa bbbb "), (10, "cccc dddd "), (20, "eeee")]


def test_segments_hard_cut_without_a_separator() -> None:
    assert segments("x" * 25, 10) == [(0, "x" * 10), (10, "x" * 10), (20, "x" * 5)]


def test_segments_short_text_is_one_piece() -> None:
    assert segments("short", 20) == [(0, "short")]
    assert segments("", 20) == []
    assert segments("a" * 20, 20) == [(0, "a" * 20)]


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
    pieces = segments(text, 1000)

    assert all(0 < len(piece) <= 1000 for _, piece in pieces)
    assert "".join(piece for _, piece in pieces) == text
    assert all(text[offset : offset + len(piece)] == piece for offset, piece in pieces)
    assert [offset for offset, _ in pieces] == sorted({offset for offset, _ in pieces})


def test_segments_default_size_is_20000() -> None:
    pieces = segments("x" * 45_000)

    assert [len(piece) for _, piece in pieces] == [20_000, 20_000, 5_000]


def test_segments_rejects_a_size_below_one() -> None:
    with pytest.raises(ValueError, match="max_chars"):
        segments("abc", 0)
