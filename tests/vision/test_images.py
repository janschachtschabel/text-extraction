"""Image bytes for the vision call (``vision/images.py``): sniffing by magic bytes, the data URL,
the cleaned description and how it is appended to an image upload's content (rulings V-S8,
V-S9)."""

import base64

import pytest

from text_extraction.vision.images import (
    append_description,
    clean_description,
    data_url,
    is_svg,
    sniff_image,
)
from text_extraction.vision.types import DESCRIPTION_MAX_CHARS


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, "image/png"),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 8, "image/jpeg"),
        (b"GIF87a" + b"\x00" * 8, "image/gif"),
        (b"GIF89a" + b"\x00" * 8, "image/gif"),
        (b"RIFF\x24\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', None),
        (b"<!doctype html><html></html>", None),
        (b"%PDF-1.7\n", None),
        (b"II*\x00\x08\x00\x00\x00", None),  # TIFF
        (b"BM" + b"\x00" * 12, None),  # BMP
        (b"RIFF\x24\x00\x00\x00WAVEfmt ", None),
        (b"RIFF", None),
        (b"", None),
    ],
)
def test_sniff_image_by_magic_bytes(body: bytes, expected: str | None) -> None:
    assert sniff_image(body) == expected


@pytest.mark.parametrize(
    "body",
    [
        b'<svg xmlns="http://www.w3.org/2000/svg"></svg>',
        b"<svg>",
        b"<svg/>",
        b"\n  \t<svg\nwidth='1'></svg>",
        b'<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg"/>',
        b'\xef\xbb\xbf<svg xmlns="http://www.w3.org/2000/svg"/>',
        b'\xef\xbb\xbf<?xml version="1.0"?><svg/>',
        b'<?xml version="1.0"?>\n<!-- made by hand -->\n'
        b'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" '
        b'"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">\n<svg/>',
        b"<!-- a --><!-- b --><svg/>",
        b'<svg:svg xmlns:svg="http://www.w3.org/2000/svg"/>',
        b'<x:svg xmlns:x="http://www.w3.org/2000/svg"></x:svg>',
    ],
)
def test_is_svg_true(body: bytes) -> None:
    assert is_svg(body) is True


@pytest.mark.parametrize(
    "body",
    [
        b'<!doctype html><html><body><svg xmlns="http://www.w3.org/2000/svg"></svg></body></html>',
        b"<html><svg></svg></html>",
        b"<svgfoo></svgfoo>",
        b"<svg-icon></svg-icon>",
        b"<x:svgfoo/>",
        b"<!-- unterminated <svg/>",
        b"<?xml version='1.0'?><html/>",
        b"<!DOCTYPE html><svg/>",
        b"text before <svg/>",
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 8,
        b"%PDF-1.7\n",
        b"<svg",
        b"",
        b"   ",
    ],
)
def test_is_svg_false(body: bytes) -> None:
    assert is_svg(body) is False


def test_is_svg_reads_only_the_first_4096_bytes() -> None:
    padding = b"<!--" + b" " * 5000 + b"-->"
    assert is_svg(padding + b"<svg/>") is False
    assert is_svg(b"<svg/>" + b"\xff" * 5000) is True


def test_sniff_image_still_ignores_svg() -> None:
    assert sniff_image(b"<svg/>") is None


def test_data_url_is_base64_of_the_bytes() -> None:
    body = b"\x89PNG\r\n\x1a\n\x00\x01\xfe\xff"

    url = data_url(body, "image/png")

    assert url == "data:image/png;base64," + base64.b64encode(body).decode("ascii")


def test_clean_description_collapses_cuts_at_word_and_drops_empty() -> None:
    assert clean_description("  A  red\n\tbicycle \r\n leaning on a wall.  ") == (
        "A red bicycle leaning on a wall."
    )

    cut = clean_description("word " * 80)  # 400 characters
    assert cut is not None
    assert len(cut) <= DESCRIPTION_MAX_CHARS
    assert cut.endswith("word")
    assert not cut.endswith(" ")

    exact = "a" * 299 + " " + "b" * 50
    assert clean_description(exact) == "a" * 299

    assert clean_description("x" * 400) == "x" * DESCRIPTION_MAX_CHARS  # no space to cut at
    assert clean_description("y" * 300) == "y" * 300

    assert clean_description("  ") is None
    assert clean_description("") is None
    assert clean_description(None) is None


def test_append_description_per_format() -> None:
    assert append_description("Text", "A cat.", "markdown") == "Text\n\n> Image description: A cat."
    assert append_description("Text", "A cat.", "text") == "Text\n\nImage description: A cat."
    assert (
        append_description("<p>Text</p>", "A <b>cat</b> & a dog.", "html")
        == "<p>Text</p>\n<p>Image description: A &lt;b&gt;cat&lt;/b&gt; &amp; a dog.</p>"
    )


def test_append_description_to_empty_content_has_no_leading_break() -> None:
    assert append_description("", "A cat.", "markdown") == "> Image description: A cat."
    assert append_description("", "A cat.", "text") == "Image description: A cat."
    assert append_description("", "A <cat>", "html") == "<p>Image description: A &lt;cat&gt;</p>"
