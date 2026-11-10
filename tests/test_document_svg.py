"""SVG as a document kind (package 22, ``documents/svg.py``): the text of ``<text>``,
``<flowRoot>`` and ``<desc>`` elements, parsed with defusedxml and never rendered. The files here
are synthetic."""

from typing import Any

import pytest

from tests.fakes import ORIGIN, PARAGRAPH, fetched
from tests.test_pipeline_documents import Harness, data_of
from text_extraction.documents.sniff import media_type, sniff
from text_extraction.documents.svg import SvgUnreadable, svg_text
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.extract_models import ExtractResult

URL = f"{ORIGIN}/files/schema.svg"
NS = 'xmlns="http://www.w3.org/2000/svg"'
FIRST = "Die Sonne erwärmt das Wasser der Meere."
SECOND = "Der Wasserdampf steigt auf und bildet Wolken."


def svg(body: str, head: str = "") -> bytes:
    return f'<?xml version="1.0" encoding="UTF-8"?>{head}<svg {NS}>{body}</svg>'.encode()


def drawing(text: str = PARAGRAPH * 2) -> bytes:
    return svg(f'<title>Wasserkreislauf</title><path d="M0 0L9 9"/><text x="1" y="2">{text}</text>')


# --- svg_text ---


def test_two_text_elements_give_two_paragraphs_in_document_order() -> None:
    body = svg(f'<text x="1">{FIRST}</text><g><text x="2">{SECOND}</text></g>')

    title, text = svg_text(body, 10_000)

    assert title is None
    assert text == f"{FIRST}\n\n{SECOND}"


def test_title_flow_root_desc_and_tspan_lines() -> None:
    body = svg(
        "<title>Wasserkreislauf</title><desc>Ein Schema.</desc>"
        '<text><tspan x="0" y="1">Erste Zeile</tspan><tspan x="0" y="2">zweite Zeile</tspan></text>'
        "<flowRoot><flowPara>Fließtext im Rahmen</flowPara></flowRoot>"
    )

    title, text = svg_text(body, 10_000)

    assert title == "Wasserkreislauf"
    assert text == "Ein Schema.\n\nErste Zeile zweite Zeile\n\nFließtext im Rahmen"


def test_prefixed_svg_namespace_and_inkscape_attributes() -> None:
    body = (
        b'<svg:svg xmlns:svg="http://www.w3.org/2000/svg" xmlns:inkscape="http://x/y">'
        b'<svg:text inkscape:label="l"><svg:tspan>Hallo Welt</svg:tspan></svg:text></svg:svg>'
    )

    assert svg_text(body, 10_000) == (None, "Hallo Welt")


def test_svg_with_paths_only_has_no_text() -> None:
    assert svg_text(svg('<path d="M0 0L9 9"/><circle r="3"/>'), 10_000) == (None, "")


def test_text_is_cut_at_max_chars() -> None:
    _title, text = svg_text(svg(f"<text>{'wort ' * 100}</text>"), 50)

    assert len(text) <= 50


def test_text_in_a_script_or_style_is_not_text() -> None:
    body = svg("<style>.a { fill: red }</style><script>alert(1)</script><text>Sichtbar</text>")

    assert svg_text(body, 10_000) == (None, "Sichtbar")


def test_doctype_without_entities_is_read() -> None:
    head = '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">'

    assert svg_text(svg("<text>Hallo</text>", head), 10_000) == (None, "Hallo")


XXE = svg(
    "<text>&xxe;</text>",
    '<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>',
)
LAUGHS = svg(
    "<text>&lol3;</text>",
    '<!DOCTYPE svg [<!ENTITY lol "lol"><!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
    '<!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">'
    '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>',
)
INTERNAL = svg("<text>&a;</text>", '<!DOCTYPE svg [<!ENTITY a "x">]>')


@pytest.mark.parametrize("body", [XXE, LAUGHS, INTERNAL], ids=["external", "laughs", "internal"])
def test_entities_are_refused_not_expanded(body: bytes) -> None:
    with pytest.raises(SvgUnreadable):
        svg_text(body, 10_000)


@pytest.mark.parametrize(
    "body",
    [b"<svg", b"", b"not xml at all", b"<svg xmlns='http://www.w3.org/2000/svg'><g></svg>"],
)
def test_broken_xml_is_unreadable(body: bytes) -> None:
    with pytest.raises(SvgUnreadable):
        svg_text(body, 10_000)


def test_deep_nesting_is_unreadable_or_read_but_never_crashes() -> None:
    depth = 200_000
    body = (
        f"<svg {NS}>".encode() + b"<g>" * depth + b"<text>tief</text>" + b"</g>" * depth + b"</svg>"
    )

    try:
        result = svg_text(body, 10_000)
    except SvgUnreadable:
        return
    assert result == (None, "tief")


def test_recursion_error_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RecursionError

    monkeypatch.setattr("text_extraction.documents.svg.parse_xml", boom)

    with pytest.raises(SvgUnreadable):
        svg_text(svg("<text>x</text>"), 10_000)


# --- sniffing ---


def test_svg_media_type() -> None:
    assert media_type("svg") == "image/svg+xml"


@pytest.mark.parametrize(
    "declared", ["image/svg+xml", "text/plain", "application/octet-stream", None]
)
def test_svg_bytes_sniff_as_svg(declared: str | None) -> None:
    assert sniff(drawing(), declared, "schema.svg") == "svg"


def test_svg_without_xml_declaration_and_with_a_bom() -> None:
    assert sniff(f"<svg {NS}><text>x</text></svg>".encode(), "image/svg+xml", None) == "svg"
    assert sniff(b"\xef\xbb\xbf" + drawing(), None, None) == "svg"


def test_svg_with_an_entity_subset_still_sniffs_as_svg() -> None:
    assert sniff(XXE, "image/svg+xml", None) == "svg"
    assert sniff(LAUGHS, "image/svg+xml", None) == "svg"


def test_html_stays_html() -> None:
    page = b"<!doctype html><html><body><svg><text>x</text></svg></body></html>"

    assert sniff(page, "text/html", None) == "html"
    assert sniff(page, "image/svg+xml", None) == "html"
    assert sniff(b"<?xml version='1.0'?><html><body></body></html>", None, None) is None


def test_declared_svg_that_is_no_svg_is_not_supported() -> None:
    assert sniff(b"just some words", "image/svg+xml", None) is None


# --- the pipeline ---


def harness(body: bytes, **overrides: Any) -> Harness:
    return Harness(None, {URL: fetched(URL, body, content_type="image/svg+xml")}, **overrides)


def extract(h: Harness) -> Envelope[ExtractResult]:
    return h.run(url=URL, cache=False)


def test_svg_url_gives_its_text() -> None:
    envelope = extract(harness(drawing()))

    assert envelope.status == "success", envelope.error
    data = data_of(envelope)
    assert data.content_type == "image/svg+xml"
    assert data.title == "Wasserkreislauf"
    assert (data.content or "").startswith(PARAGRAPH[:40])
    assert data.document is not None
    assert (data.document.kind, data.document.converter) == ("svg", "svg")
    assert data.word_count > 0
    assert data.fingerprint is not None


@pytest.mark.parametrize("fmt", ["markdown", "text", "html"])
def test_svg_in_each_output_format(fmt: str) -> None:
    envelope = extract_with(harness(drawing()), format=fmt)

    assert envelope.status == "success", envelope.error
    assert "Wasserkreislauf" not in (data_of(envelope).content or "") or fmt != "text"
    assert (data_of(envelope).content or "").strip()


def extract_with(h: Harness, **request: Any) -> Envelope[ExtractResult]:
    return h.run(url=URL, cache=False, **request)


def test_svg_without_text_answers_like_any_document_without_text() -> None:
    envelope = extract(harness(svg('<path d="M0 0L9 9"/>')))

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.EMPTY_CONTENT  # as a PDF without text


def test_svg_with_few_words_is_content_too_short() -> None:
    envelope = extract(harness(drawing("Nur drei Worte.")))

    assert envelope.status == "partial"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.CONTENT_TOO_SHORT


@pytest.mark.parametrize("body", [XXE, LAUGHS], ids=["external", "laughs"])
def test_malicious_svg_is_conversion_failed(body: bytes) -> None:
    envelope = extract(harness(body))

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.CONVERSION_FAILED
    assert envelope.error.retryable is False
    assert "passwd" not in envelope.model_dump_json()


def test_deeply_nested_svg_answers_without_a_crash() -> None:
    depth = 200_000
    body = (
        f"<svg {NS}>".encode() + b"<g>" * depth + b"<text>tief</text>" + b"</g>" * depth + b"</svg>"
    )

    envelope = extract(harness(body))

    assert envelope.status in {"failed", "partial", "success"}
    if envelope.status == "failed":
        assert envelope.error is not None
        assert envelope.error.code in {ErrorCode.CONVERSION_FAILED, ErrorCode.EMPTY_CONTENT}


def test_svg_over_document_max_bytes_is_refused_by_the_size_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[bytes] = []
    monkeypatch.setattr(
        "text_extraction.documents.svg.parse_xml", lambda data, **_kw: called.append(data)
    )
    body = svg("<text>" + "wort " * 1000 + "</text>")
    h = harness(body, document_max_bytes=1024)

    envelope = extract(h)

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.TOO_LARGE
    assert called == []
