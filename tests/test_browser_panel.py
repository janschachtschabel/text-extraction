"""Decoding the page's own ``get_panel`` request body (``browser_panel.decode_request_body``)."""

import gzip
import json

import pytest

from text_extraction.browser_panel import decode_request_body

BODY = json.dumps({"context": {}, "panelId": "PAmodern_transcript_view"}).encode()
LIMIT = 1024 * 1024


@pytest.mark.parametrize("encoding", ["", "identity"])
def test_a_plain_body_is_given_as_it_is(encoding: str) -> None:
    assert decode_request_body(BODY, encoding) == BODY


def test_a_gzip_body_is_gunzipped() -> None:
    assert decode_request_body(gzip.compress(BODY), "gzip") == BODY


def test_a_gzip_body_of_exactly_the_limit_is_gunzipped() -> None:
    body = b"x" * LIMIT
    assert decode_request_body(gzip.compress(body), "gzip") == body


@pytest.mark.parametrize(
    ("body", "encoding"),
    [
        (None, ""),  # no body to read
        (BODY, "br"),  # an encoding not decoded here
        (b"not gzip at all", "gzip"),  # broken
        (gzip.compress(BODY)[:-12], "gzip"),  # cut off
        (gzip.compress(b"x" * (LIMIT + 1)), "gzip"),  # over the limit, decompressed
    ],
)
def test_a_body_that_cannot_be_decoded_is_none(body: bytes | None, encoding: str) -> None:
    assert decode_request_body(body, encoding) is None
