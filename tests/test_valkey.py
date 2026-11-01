"""Helpers of ``valkey.py``."""

from text_extraction.valkey import reply_text


def test_reply_text_decodes_bytes_and_keeps_text() -> None:
    assert reply_text(b"gr\xc3\xbc\xc3\x9fe") == "grüße"
    assert reply_text("plain") == "plain"
