import re

import pytest
from pydantic import SecretStr

from text_extraction.pii.replace import Replacer, hash_key
from text_extraction.pii.types import Span
from text_extraction.settings import Settings

KEY = b"k" * 32


def span(start: int, end: int, type_: str = "PERSON", score: float = 0.85) -> Span:
    return Span(start=start, end=end, type=type_, score=score)  # type: ignore[arg-type]


def test_mask_redact_hash_placeholders() -> None:
    person = span(0, 4)

    assert Replacer("mask", KEY).placeholder(person, "Anna") == "[PERSON]"
    assert Replacer("mask", KEY).placeholder(span(0, 1, "EMAIL_ADDRESS"), "a") == "[EMAIL_ADDRESS]"
    assert Replacer("redact", KEY).placeholder(person, "Anna") == "[REDACTED]"
    assert re.fullmatch(
        r"\[PERSON:[0-9a-f]{12}\]", Replacer("hash", KEY).placeholder(person, "Anna")
    )


def test_annotate_has_no_placeholder_and_leaves_the_text() -> None:
    replacer = Replacer("annotate", KEY)

    assert replacer.apply("Anna was here", [span(0, 4)]) == "Anna was here"
    with pytest.raises(ValueError, match="annotate"):
        replacer.placeholder(span(0, 4), "Anna")


def test_hash_equal_values_equal_across_case_and_spaces() -> None:
    replacer = Replacer("hash", KEY)
    person = span(0, 4)

    base = replacer.placeholder(person, "Anna Maria Schmidt")

    assert replacer.placeholder(person, "ANNA  maria\tSCHMIDT") == base
    assert replacer.placeholder(person, "  anna\nmaria schmidt ") == base
    assert replacer.placeholder(person, "Anna Maria Schmitt") != base
    # The type is part of the hashed text.
    assert replacer.placeholder(span(0, 4, "LOCATION"), "Anna Maria Schmidt") != base


def test_hash_differs_by_key() -> None:
    person = span(0, 4)

    one = Replacer("hash", b"a" * 32).placeholder(person, "Anna")
    two = Replacer("hash", b"b" * 32).placeholder(person, "Anna")

    assert one != two
    assert Replacer("hash", b"a" * 32).placeholder(person, "Anna") == one


def test_hash_is_the_documented_hmac() -> None:
    import hashlib
    import hmac

    digest = hmac.new(KEY, b"EMAIL_ADDRESS:a@b.de", hashlib.sha256).hexdigest()[:12]

    assert Replacer("hash", KEY).placeholder(span(0, 6, "EMAIL_ADDRESS"), "A@B.de") == (
        f"[EMAIL_ADDRESS:{digest}]"
    )


def test_apply_keeps_text_between_spans_and_code_point_offsets() -> None:
    text = "😀 Anna wrote to a@b.de, 👍 bye"
    start = text.index("Anna")
    email = text.index("a@b.de")
    spans = [span(start, start + 4), span(email, email + 6, "EMAIL_ADDRESS")]

    assert (
        Replacer("mask", KEY).apply(text, spans) == "😀 [PERSON] wrote to [EMAIL_ADDRESS], 👍 bye"
    )
    assert Replacer("redact", KEY).apply(text, spans) == "😀 [REDACTED] wrote to [REDACTED], 👍 bye"
    hashed = Replacer("hash", KEY).apply(text, spans)
    assert re.fullmatch(
        r"😀 \[PERSON:[0-9a-f]{12}\] wrote to \[EMAIL_ADDRESS:[0-9a-f]{12}\], 👍 bye", hashed
    )
    assert Replacer("mask", KEY).apply(text, []) == text


def test_apply_adjacent_spans() -> None:
    assert Replacer("mask", KEY).apply("AnnaBert!", [span(0, 4), span(4, 8, "LOCATION")]) == (
        "[PERSON][LOCATION]!"
    )


def test_apply_hashes_the_value_of_each_span() -> None:
    replacer = Replacer("hash", KEY)
    text = "Anna and ANNA"

    out = replacer.apply(text, [span(0, 4), span(9, 13)])

    first, second = re.findall(r"\[PERSON:[0-9a-f]{12}\]", out)
    assert first == second


def test_hash_key_service_key_when_set() -> None:
    settings = Settings(auth_mode="none", pii_hash_key=SecretStr("s" * 40))

    assert hash_key(settings) == (b"s" * 40, "service")
    assert hash_key(settings) == hash_key(settings)


def test_hash_key_random_per_call_without_a_service_key() -> None:
    settings = Settings(auth_mode="none")

    first, scope = hash_key(settings)
    second, _ = hash_key(settings)

    assert scope == "result"
    assert len(first) == 32
    assert first != second
