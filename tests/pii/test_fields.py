from typing import Any

import pytest

from text_extraction.compliance import ComplianceReport
from text_extraction.convert import Link
from text_extraction.documents.types import DocumentInfo
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.extract_models import ExtractResult, Timings
from text_extraction.fingerprint import Fingerprint
from text_extraction.licenses import License
from text_extraction.media.types import MediaInfo
from text_extraction.metadata import Image, PageMetadata
from text_extraction.pii.fields import (
    FAIL_CLOSED_MESSAGE,
    apply_pii,
    fail_closed,
    needs_pii,
    text_fields,
)
from text_extraction.pii.types import PiiOptions, PiiReport, Span
from text_extraction.quality import Quality

KEY = b"k" * 32
ALL: list[Any] = ["PERSON", "EMAIL_ADDRESS"]


def span(start: int, end: int, type_: str = "PERSON", score: float = 0.9) -> Span:
    return Span(start=start, end=end, type=type_, score=score)  # type: ignore[arg-type]


def where(text: str, value: str, type_: str = "PERSON") -> Span:
    start = text.index(value)
    return span(start, start + len(value), type_)


def quality() -> Quality:
    return Quality(
        visible_chars=100,
        word_count=20,
        script_count=0,
        js_heavy=False,
        bot_challenge=False,
        soft_404=False,
        login_wall=False,
        browser_recommended=False,
    )


def make_result(**changes: Any) -> ExtractResult:
    """A result with a value in every field that N5 names, and URLs, dates and licences that it
    leaves alone."""
    values: dict[str, Any] = {
        "url": "https://example.org/page",
        "final_url": "https://example.org/final",
        "upstream_status": 200,
        "content_type": "text/html",
        "mode_used": "fast",
        "attempts": 1,
        "language": "de",
        "word_count": 20,
        "title": "Anna Schmidt privat",
        "format": "markdown",
        "content": "Anna Schmidt wohnt in Bremen.",
        "quality": quality(),
        "timings": Timings(total_ms=5, compliance_ms=1, fetch_ms=2, convert_ms=2),
        "compliance": ComplianceReport(mode="report"),
        "metadata": PageMetadata(
            title="Seite von Anna",
            description="Kontakt: anna@example.org",
            authors=["Anna Schmidt", "Bob Meier"],
            published="2026-01-01",
            modified="2026-02-01",
            site_name="Annas Blog",
            canonical_url="https://example.org/canonical",
            language="de",
            keywords=["Mathe", "Anna"],
            license=License(id="CC0-1.0", url=None, label="CC0", source="meta", confidence="high"),
            lrmi={
                "name": "Bruchrechnen",
                "educationalAlignment": [
                    {"targetName": "Brüche", "targetUrl": "https://example.org/t"},
                    {"targetName": "Anna"},
                ],
                "typicalAgeRange": 12,
                "author": {"name": "Anna Schmidt"},
            },
            images=[Image(url="https://example.org/a.png", alt="Foto von Anna")],
        ),
        "links": [
            Link(url="https://example.org/a", text="Anna", kind="external"),
            Link(url="mailto:anna@example.org", text="Mail", kind="external"),
            Link(url="tel:+4930123456", text="Anruf", kind="external"),
            Link(url="https://example.org/b", text=" ", kind="external"),
        ],
        "fingerprint": Fingerprint(raw_sha256="a" * 64, text_sha256="b" * 64, minhash="AAAA"),
        "cache": "miss",
        "warnings": ["a_warning"],
        "document": DocumentInfo(
            kind="pdf",
            converter="markitdown",
            pages=1,
            pages_converted=1,
            ocr=False,
            file_name="Zeugnis Anna.pdf",
        ),
        "media": MediaInfo(
            platform="youtube", transcript_source="manual_subtitles", uploader="Anna S."
        ),
    }
    values.update(changes)
    return ExtractResult(**values)


def names(fields: list[Any]) -> list[str]:
    return [field.name for field in fields]


def test_fields_cover_n5_and_skip_urls_dates_licences() -> None:
    fields = text_fields(make_result(), ALL)

    assert names(fields) == [
        "content",
        "title",
        "metadata.title",
        "metadata.description",
        "metadata.authors[0]",
        "metadata.authors[1]",
        "metadata.keywords[0]",
        "metadata.keywords[1]",
        "metadata.site_name",
        "metadata.images[0].alt",
        "metadata.lrmi.name",
        "metadata.lrmi.educationalAlignment.0.targetName",
        "metadata.lrmi.educationalAlignment.0.targetUrl",
        "metadata.lrmi.educationalAlignment.1.targetName",
        "metadata.lrmi.author.name",
        "links[0].text",
        "links[1].text",
        "links[1].url",
        "links[2].text",
        "links[2].url",
        "media.uploader",
        "document.file_name",
    ]
    texts = [field.text for field in fields]
    assert "mailto:anna@example.org" in texts
    assert "tel:+4930123456" in texts
    assert "https://example.org/a" not in texts  # an https link's URL stays out
    assert "https://example.org/a.png" not in texts  # so does an image URL
    assert "https://example.org/canonical" not in texts
    assert "2026-01-01" not in texts
    assert all(text.strip() for text in texts)  # blank strings are skipped


def test_fields_skip_what_is_missing() -> None:
    result = make_result(
        title=None, content=None, metadata=None, links=None, document=None, media=None
    )

    assert text_fields(result, ALL) == []


def test_authors_and_uploader_whole_person() -> None:
    fields = {field.name: field for field in text_fields(make_result(), ["PERSON"])}

    assert fields["metadata.authors[0]"].whole == "PERSON"
    assert fields["metadata.authors[1]"].whole == "PERSON"
    assert fields["media.uploader"].whole == "PERSON"
    assert fields["metadata.keywords[1]"].whole is None
    assert fields["title"].whole is None


def test_authors_untouched_without_person() -> None:
    fields = {field.name: field for field in text_fields(make_result(), ["EMAIL_ADDRESS"])}

    # Plain fields: the analyser reads them like any other.
    assert fields["metadata.authors[0]"].whole is None
    assert fields["media.uploader"].whole is None
    assert fields["metadata.authors[0]"].text == "Anna Schmidt"


def test_lrmi_string_leaves_dotted_names() -> None:
    fields = text_fields(make_result(), ALL)
    lrmi = {field.name: field.text for field in fields if field.name.startswith("metadata.lrmi")}

    assert lrmi["metadata.lrmi.educationalAlignment.1.targetName"] == "Anna"
    assert lrmi["metadata.lrmi.author.name"] == "Anna Schmidt"
    assert not any("typicalAgeRange" in name for name in lrmi)  # not a string


def test_lrmi_keys_with_dots_keep_names_unique() -> None:
    metadata = PageMetadata(lrmi={"a.b": "Anna", "a": {"b": "Bob"}})

    fields = text_fields(make_result(metadata=metadata), ALL)
    lrmi = [field.name for field in fields if field.name.startswith("metadata.lrmi")]

    assert len(set(lrmi)) == len(lrmi) == 2


def test_html_content_field_is_its_text() -> None:
    result = make_result(format="html", content="<p><b>Anna</b> Schmidt</p><p>kam</p>")

    assert text_fields(result, ALL)[0].text == "Anna Schmidt kam"


def test_blank_content_is_not_a_field() -> None:
    assert "content" not in names(text_fields(make_result(content="  \n"), ALL))
    assert "content" not in names(text_fields(make_result(format="html", content="<p> </p>"), ALL))


def apply(
    result: ExtractResult,
    spans: dict[str, list[Span]],
    mode: str = "mask",
    *,
    max_entities: int = 1000,
    entities: list[Any] | None = None,
) -> tuple[ExtractResult, list[str]]:
    options = PiiOptions(mode=mode, entities=entities)  # type: ignore[arg-type]
    return apply_pii(
        result,
        options,
        spans,
        language="de",
        key=KEY,
        hash_scope="result",
        max_entities=max_entities,
        entities=entities if entities is not None else ["PERSON"],
    )


def test_apply_mask_on_content_title_metadata_links() -> None:
    result = make_result()
    content = result.content or ""
    spans = {
        "content": [where(content, "Anna Schmidt")],
        "title": [where(result.title or "", "Anna Schmidt")],
        "metadata.description": [
            where("Kontakt: anna@example.org", "anna@example.org", "EMAIL_ADDRESS")
        ],
        "metadata.keywords[1]": [span(0, 4)],
        "metadata.site_name": [span(0, 4)],
        "metadata.images[0].alt": [where("Foto von Anna", "Anna")],
        "metadata.lrmi.educationalAlignment.1.targetName": [span(0, 4)],
        "metadata.lrmi.author.name": [span(0, 12)],
        "links[0].text": [span(0, 4)],
        "links[1].url": [span(7, 23, "EMAIL_ADDRESS")],
        "links[2].url": [span(4, 15, "PHONE_NUMBER")],
        "document.file_name": [where("Zeugnis Anna.pdf", "Anna")],
    }

    processed, warnings = apply(result, spans)

    assert warnings == []
    assert processed.content == "[PERSON] wohnt in Bremen."
    assert processed.title == "[PERSON] privat"
    metadata = processed.metadata
    assert metadata is not None
    assert metadata.description == "Kontakt: [EMAIL_ADDRESS]"
    assert metadata.authors == ["[PERSON]", "[PERSON]"]  # whole fields, no spans needed
    assert metadata.keywords == ["Mathe", "[PERSON]"]
    assert metadata.site_name == "[PERSON]s Blog"
    assert metadata.images[0].alt == "Foto von [PERSON]"
    assert metadata.images[0].url == "https://example.org/a.png"
    assert metadata.lrmi["educationalAlignment"][1]["targetName"] == "[PERSON]"
    assert metadata.lrmi["educationalAlignment"][0]["targetName"] == "Brüche"
    assert metadata.lrmi["author"]["name"] == "[PERSON]"
    assert metadata.lrmi["typicalAgeRange"] == 12
    assert metadata.canonical_url == "https://example.org/canonical"
    assert metadata.published == "2026-01-01"
    assert metadata.license == (result.metadata.license if result.metadata else None)
    links = processed.links
    assert links is not None
    assert [link.text for link in links] == ["[PERSON]", "Mail", "Anruf", " "]
    assert [link.url for link in links] == [
        "https://example.org/a",
        "mailto:[EMAIL_ADDRESS]",
        "tel:[PHONE_NUMBER]",
        "https://example.org/b",
    ]
    assert [link.kind for link in links] == ["external"] * 4
    assert processed.document is not None
    assert processed.document.file_name == "Zeugnis [PERSON].pdf"
    assert processed.media is not None
    assert processed.media.uploader == "[PERSON]"
    assert processed.media.platform == "youtube"
    assert processed.url == result.url
    assert processed.final_url == result.final_url
    assert processed.pii == PiiReport(mode="mask", language="de", entities=processed.pii.entities)  # type: ignore[union-attr]
    # The input result is not changed.
    assert result.content == "Anna Schmidt wohnt in Bremen."
    assert result.pii is None


def test_apply_report_counts_resolved_spans_per_type() -> None:
    result = make_result()
    spans = {
        "content": [span(0, 4), span(0, 12), span(18, 24, "LOCATION")],  # the first is inside
        "metadata.description": [
            where("Kontakt: anna@example.org", "anna@example.org", "EMAIL_ADDRESS")
        ],
    }

    processed, _ = apply(result, spans)

    assert processed.pii is not None
    # Two authors and the uploader are whole persons, plus the one in the content.
    assert processed.pii.entities == {"PERSON": 4, "LOCATION": 1, "EMAIL_ADDRESS": 1}
    assert processed.pii.items is None
    assert processed.pii.hash_scope is None
    assert processed.pii.language == "de"


def test_apply_ignores_child_spans_for_whole_fields() -> None:
    spans = {"metadata.authors[0]": [span(0, 4)], "media.uploader": [span(0, 2, "LOCATION")]}

    processed, _ = apply(make_result(), spans)

    assert processed.metadata is not None
    assert processed.metadata.authors[0] == "[PERSON]"
    assert processed.media is not None
    assert processed.media.uploader == "[PERSON]"
    assert processed.pii is not None
    assert "LOCATION" not in processed.pii.entities


def test_apply_without_person_sends_authors_through_the_spans() -> None:
    spans = {"metadata.authors[0]": [span(0, 4, "EMAIL_ADDRESS")]}

    processed, _ = apply(make_result(), spans, entities=["EMAIL_ADDRESS"])

    assert processed.metadata is not None
    assert processed.metadata.authors == ["[EMAIL_ADDRESS] Schmidt", "Bob Meier"]
    assert processed.media is not None
    assert processed.media.uploader == "Anna S."


def test_apply_whole_span_excludes_blanks_at_the_ends() -> None:
    metadata = PageMetadata(authors=["  Anna Schmidt "])

    processed, _ = apply(make_result(metadata=metadata), {})

    assert processed.metadata is not None
    assert processed.metadata.authors == ["  [PERSON] "]


def test_apply_redact_and_hash() -> None:
    spans = {"content": [span(0, 12)]}

    redacted, _ = apply(make_result(), spans, "redact")
    hashed_options = PiiOptions(mode="hash", entities=["PERSON"])
    hashed, _ = apply_pii(
        make_result(),
        hashed_options,
        spans,
        language="en",
        key=KEY,
        hash_scope="service",
        max_entities=10,
        entities=["PERSON"],
    )

    assert redacted.content == "[REDACTED] wohnt in Bremen."
    assert redacted.pii is not None
    assert redacted.pii.hash_scope is None
    assert hashed.content is not None
    assert hashed.content.startswith("[PERSON:")
    assert hashed.metadata is not None
    assert hashed.metadata.authors[0].startswith("[PERSON:")
    assert hashed.pii is not None
    assert hashed.pii.hash_scope == "service"
    assert hashed.pii.language == "en"


def test_apply_overlapping_child_spans_are_resolved() -> None:
    spans = {"content": [span(0, 12, score=0.6), span(5, 12, "LOCATION", 0.9)]}

    processed, _ = apply(make_result(), spans)

    assert processed.content == "[PERSON] [LOCATION] wohnt in Bremen."


def test_annotate_items_offsets_and_truncation() -> None:
    content = "x" * 3001
    spans = {"content": [span(index * 3, index * 3 + 1) for index in range(1001)]}
    result = make_result(
        content=content, title=None, metadata=None, links=None, media=None, document=None
    )

    processed, warnings = apply(result, spans, "annotate", max_entities=1000)

    assert warnings == ["pii_entities_truncated"]
    assert processed.pii is not None
    assert processed.pii.items is not None
    assert len(processed.pii.items) == 1000
    assert processed.pii.entities == {"PERSON": 1001}
    assert processed.content == content
    first, last = processed.pii.items[0], processed.pii.items[-1]
    assert (first.field, first.type, first.start, first.end, first.text) == (
        "content",
        "PERSON",
        0,
        1,
        "x",
    )
    assert (last.start, last.end) == (2997, 2998)


def test_annotate_items_in_field_then_offset_order_and_strings_unchanged() -> None:
    result = make_result()
    spans = {
        "title": [where(result.title or "", "privat", "LOCATION")],
        "content": [where(result.content or "", "Bremen", "LOCATION"), span(0, 12)],
    }

    processed, warnings = apply(result, spans, "annotate")

    assert warnings == []
    assert processed.pii is not None
    assert processed.pii.items is not None
    assert [(item.field, item.start, item.text) for item in processed.pii.items] == [
        ("content", 0, "Anna Schmidt"),
        ("content", 22, "Bremen"),
        ("title", 13, "privat"),
        ("metadata.authors[0]", 0, "Anna Schmidt"),
        ("metadata.authors[1]", 0, "Bob Meier"),
        ("media.uploader", 0, "Anna S."),
    ]
    assert processed.pii.items[0].score == 0.9
    assert processed.pii.items[3].score == 1.0
    assert processed.content == result.content
    assert processed.title == result.title
    assert processed.metadata == result.metadata
    assert processed.links == result.links
    assert processed.media == result.media
    assert processed.document == result.document


def test_annotate_with_html_is_refused() -> None:
    result = make_result(format="html", content="<p>Anna</p>")

    with pytest.raises(ValueError, match="annotate"):
        apply(result, {}, "annotate")


def test_apply_html_content() -> None:
    html = '<p><b>Anna</b> Schmidt kam.</p><img alt="Anna Schmidt" src="https://example.org/a.png">'
    result = make_result(format="html", content=html, title=None, metadata=None, media=None)
    text = text_fields(result, ["PERSON"])[0].text

    processed, _ = apply(result, {"content": [where(text, "Anna Schmidt")]})

    assert processed.content == (
        '<p><b>[PERSON]</b> kam.</p><img alt="Anna Schmidt" src="https://example.org/a.png">'
    )


def test_apply_without_the_entities_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="entities"):
        apply_pii(  # type: ignore[call-arg]
            make_result(),
            PiiOptions(mode="mask"),
            {},
            language="de",
            key=KEY,
            hash_scope="result",
            max_entities=10,
        )


COMMENT_HTML = "<p>Anna kam.</p><!-- Bob Meier --><?php echo 'Carl' ?>"


@pytest.mark.parametrize("mode", ["mask", "redact", "hash"])
def test_apply_html_drops_comments_with_a_replacement_in_the_text(mode: str) -> None:
    result = make_result(format="html", content=COMMENT_HTML, title=None, metadata=None, media=None)
    text = text_fields(result, ["PERSON"])[0].text

    processed, _ = apply(result, {"content": [where(text, "Anna")]}, mode)

    assert processed.content is not None
    assert processed.content.startswith("<p>")
    assert processed.content.endswith(" kam.</p>")
    for leaked in ("Anna", "Bob", "Carl", "<!--", "<?"):
        assert leaked not in processed.content


def test_apply_html_drops_comments_without_a_span() -> None:
    result = make_result(format="html", content=COMMENT_HTML, title=None, metadata=None, media=None)

    processed, _ = apply(result, {})

    assert processed.content == "<p>Anna kam.</p>"


def test_apply_html_with_only_a_comment_drops_it() -> None:
    result = make_result(
        format="html", content="<!-- Bob Meier -->", title=None, metadata=None, media=None
    )

    processed, _ = apply(result, {})

    assert processed.content == ""


def test_apply_annotate_leaves_markdown_alone_and_html_is_refused() -> None:
    result = make_result(content="Anna <!-- Bob Meier -->")

    processed, _ = apply(result, {"content": [span(0, 4)]}, "annotate")

    assert processed.content == "Anna <!-- Bob Meier -->"


def test_apply_html_without_spans_is_unchanged() -> None:
    html = "<p>Anna <br/> kam  &nbsp; </p>"
    result = make_result(format="html", content=html, title=None, metadata=None, media=None)

    processed, _ = apply(result, {})

    assert processed.content == html


def test_apply_keeps_status_quality_fingerprint_word_count() -> None:
    result = make_result()

    processed, _ = apply(result, {"content": [span(0, 12)]})

    assert processed.word_count == result.word_count
    assert processed.quality == result.quality
    assert processed.language == result.language
    assert processed.fingerprint == result.fingerprint
    assert processed.timings == result.timings
    assert processed.timings.pii_ms == 0
    assert processed.warnings == result.warnings
    assert processed.compliance == result.compliance
    assert processed.upstream_status == 200
    assert processed.cache == "miss"


def envelope(result: ExtractResult | None, status: str = "success") -> Envelope[ExtractResult]:
    return Envelope[ExtractResult](status=status, data=result)  # type: ignore[arg-type]


def test_needs_pii_true_for_unprocessed_result_with_text() -> None:
    assert needs_pii(envelope(make_result()), PiiOptions(mode="mask"))


def test_needs_pii_true_for_html_that_holds_only_a_comment() -> None:
    result = make_result(
        format="html",
        content="<!-- Bob Meier -->",
        title=None,
        metadata=None,
        links=None,
        document=None,
        media=None,
    )

    assert needs_pii(envelope(result), PiiOptions(mode="mask"))
    assert fail_closed(envelope(result), PiiOptions(mode="mask")).data.content is None  # type: ignore[union-attr]


def test_needs_pii_false_for_processed_or_empty_failure() -> None:
    options = PiiOptions(mode="mask")
    processed = make_result(pii=PiiReport(mode="mask", language="de", entities={}))
    empty = make_result(
        title=None, content=None, metadata=None, links=None, document=None, media=None
    )

    assert not needs_pii(envelope(make_result()), None)
    assert not needs_pii(envelope(None, "failed"), options)
    assert not needs_pii(envelope(processed), options)
    assert not needs_pii(envelope(empty, "failed"), options)
    # Only blank strings and URLs: nothing N5 reads.
    nothing = make_result(
        title=" ",
        content="",
        metadata=PageMetadata(published="2026-01-01", canonical_url="https://example.org/"),
        links=[Link(url="https://example.org/", text="", kind="external")],
        document=None,
        media=None,
    )
    assert not needs_pii(envelope(nothing), options)


def test_fail_closed_withholds_content_and_metadata() -> None:
    result = make_result(upstream_status=203)
    original = Envelope[ExtractResult](status="success", data=result, trace_id="trace-1")

    closed = fail_closed(original, PiiOptions(mode="mask"))

    assert closed.status == "failed"
    assert closed.error == ErrorInfo(
        code=ErrorCode.PII_FAILED,
        message="PII handling did not run; the content is withheld.",
        retryable=True,
    )
    assert closed.error is not None
    assert closed.error.message == FAIL_CLOSED_MESSAGE
    assert closed.trace_id == "trace-1"
    data = closed.data
    assert data is not None
    assert data.content is None
    assert data.title is None
    assert data.metadata is None
    assert data.links is None
    assert data.media is not None
    assert data.media.uploader is None
    assert data.document is not None
    assert data.document.file_name is None
    # What is kept.
    assert data.upstream_status == 203
    assert data.compliance == result.compliance
    assert data.timings == result.timings
    assert data.url == result.url
    assert data.quality == result.quality
    assert data.word_count == result.word_count
    assert data.media.platform == "youtube"
    assert data.document.kind == "pdf"
    assert data.warnings == result.warnings
    # Nothing of the withheld text is left anywhere in the answer.
    dump = closed.model_dump_json()
    for value in ("Anna", "Schmidt", "anna@example.org", "Bremen"):
        assert value not in dump
    # The input is not changed.
    assert original.status == "success"
    assert result.content is not None


def test_fail_closed_replaces_the_error_of_a_target_failure_that_has_text() -> None:
    failure = Envelope[ExtractResult](
        status="failed",
        data=make_result(),
        error=ErrorInfo(code=ErrorCode.LIKELY_ERROR_PAGE, message="Error page", retryable=False),
    )

    closed = fail_closed(failure, PiiOptions(mode="redact"))

    assert closed.error is not None
    assert closed.error.code == ErrorCode.PII_FAILED
    assert closed.data is not None
    assert closed.data.content is None


def test_fail_closed_passes_processed_and_no_pii() -> None:
    processed = envelope(make_result(pii=PiiReport(mode="mask", language="de", entities={})))
    unprocessed = envelope(make_result())
    failure = envelope(None, "failed")
    empty = envelope(
        make_result(title=None, content=None, metadata=None, links=None, document=None, media=None),
        "failed",
    )

    assert fail_closed(processed, PiiOptions(mode="mask")) is processed
    assert fail_closed(unprocessed, None) is unprocessed
    assert fail_closed(failure, PiiOptions(mode="mask")) is failure
    assert fail_closed(empty, PiiOptions(mode="mask")) is empty


# --- the final review's M3 and M4 at the level of a result ---------------------------------------


def test_apply_html_with_an_xml_declaration_masks_the_text() -> None:
    html = '<?xml version="1.0" encoding="UTF-8"?><p>Anna Schmidt kam.</p>'
    result = make_result(format="html", content=html, title=None, metadata=None, media=None)
    text = text_fields(result, ["PERSON"])[0].text

    processed, _ = apply(result, {"content": [where(text, "Anna Schmidt")]})

    assert processed.content == "<p>[PERSON] kam.</p>"


def test_html_that_does_not_parse_owes_the_step_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lxml.html

    from text_extraction.pii.html import HtmlParseFailed

    def failing(*_args: object, **_kwargs: object) -> None:
        raise ValueError("no")

    monkeypatch.setattr(lxml.html, "document_fromstring", failing)
    result = make_result(format="html", content="<p>Anna Schmidt</p>", title=None, metadata=None)
    options = PiiOptions(mode="mask")

    assert needs_pii(envelope(result), options)
    with pytest.raises(HtmlParseFailed):
        text_fields(result, ["PERSON"])
    held = fail_closed(envelope(result), options)
    assert held.status == "failed"
    assert held.data is not None
    assert held.data.content is None


def test_needs_pii_true_for_html_with_only_a_script_or_an_attribute() -> None:
    for html in ("<script>var a = 'Bob Meier';</script>", '<span aria-label="Bob Meier"></span>'):
        result = make_result(
            format="html",
            content=html,
            title=None,
            metadata=None,
            links=None,
            document=None,
            media=None,
        )
        assert needs_pii(envelope(result), PiiOptions(mode="mask")), html
        processed, _ = apply(result, {})
        assert processed.content is not None
        assert "Bob" not in processed.content


@pytest.mark.parametrize("mode", ["mask", "redact", "hash"])
def test_apply_html_drops_script_style_and_text_bearing_attributes(mode: str) -> None:
    html = (
        '<p data-x="Bob Meier" class="c">Anna kam.</p><script>var b = "Bob Meier";</script>'
        '<input value="Bob Meier" placeholder="Carl"><iframe srcdoc="Carl"></iframe>'
    )
    result = make_result(format="html", content=html, title=None, metadata=None, media=None)
    text = text_fields(result, ["PERSON"])[0].text

    processed, _ = apply(result, {"content": [where(text, "Anna")]}, mode)

    assert processed.content is not None
    assert processed.content.startswith('<p class="c">')
    for leaked in ("Anna", "Bob", "Carl", "<script"):
        assert leaked not in processed.content


def test_apply_hash_without_an_entity_reports_no_scope() -> None:
    """The final review's M6: no key signed anything when nothing was hashed."""
    result = make_result(media=None)
    found, _ = apply(result, {"content": [where(result.content or "", "Anna Schmidt")]}, "hash")
    nothing, _ = apply(result, {}, "hash", entities=["EMAIL_ADDRESS"])

    assert found.pii is not None
    assert found.pii.hash_scope == "result"
    assert nothing.pii is not None
    assert (nothing.pii.entities, nothing.pii.hash_scope) == ({}, None)
