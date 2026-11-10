"""The attachments of a thin page (package 22, ``pipeline_attachments.py``): a page with fewer
than ``ATTACHMENTS_BELOW_WORDS`` words gets the text of at most ``ATTACHMENTS_MAX`` of its linked
documents appended, each fetched as the page is (compliance, host turn, limits, deadline).

The pages are synthetic. One test converts a real DOCX in the module's MarkItDown child; the
others answer each conversion with a fake pool."""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from tests.fakes import ORIGIN, PAGE, PARAGRAPH, FakeRenderer, Redirect, fetched, rendered
from tests.test_pipeline_documents import (
    PNG,
    USER,
    FakePool,
    Harness,
    Loop,
    corpus,
    data_of,
    reply,
)
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractRequest, ExtractResult
from text_extraction.fetch import FetchResult, Validators
from text_extraction.pipeline_documents import OcrNeeded
from text_extraction.quality import count_words

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
BLATT = f"{ORIGIN}/files/blatt.docx"
OTHER = "https://files.example.net"
# About twenty words of page text.
THIN_TEXT = (
    "Dieses Arbeitsblatt zum Wasserkreislauf erklärt Verdunstung, Kondensation und "
    "Niederschlag. Die Lernenden bearbeiten die Aufgaben allein oder zu zweit im Unterricht."
)
ATTACHMENT_TEXT = "# Wasserkreislauf\n\n" + PARAGRAPH * 2


def page(text: str = THIN_TEXT, links: str = "") -> str:
    return (
        '<!doctype html><html lang="de"><head><title>Wasserkreislauf</title></head><body>'
        f"<main><h1>Wasserkreislauf</h1><p>{text}</p>{links}</main></body></html>"
    )


def link(url: str, label: str) -> str:
    return f'<p><a href="{url}">{label}</a></p>'


def pdf_link(number: int) -> str:
    return link(f"/files/blatt-{number}.pdf", f"Blatt {number}")


def pdf_url(number: int) -> str:
    return f"{ORIGIN}/files/blatt-{number}.pdf"


def pdf_answer(url: str) -> FetchResult:
    return fetched(url, corpus("text.pdf"), content_type="application/pdf")


def fake_harness(answers: dict[str, Any], **overrides: Any) -> Harness:
    return Harness(None, answers, pool=FakePool(reply(content=ATTACHMENT_TEXT)), **overrides)


def fetched_urls(harness: Harness) -> list[str]:
    return [url for url, _options in harness.fetcher.calls]


def run(harness: Harness, **request: Any) -> Envelope[ExtractResult]:
    request.setdefault("url", PAGE)
    request.setdefault("cache", False)
    return harness.run(**request)


def without_attachments(answers: dict[str, Any], **request: Any) -> ExtractResult:
    """The page's result when attachments are switched off."""
    return data_of(run(fake_harness(answers, attachments_max=0), **request))


@pytest.fixture(scope="module")
def loop() -> Iterator[Loop]:
    instance = Loop()
    try:
        yield instance
    finally:
        instance.close()


# --- appended ---


def test_thin_page_gets_its_docx_appended(loop: Loop) -> None:
    answers: dict[str, Any] = {
        PAGE: fetched(PAGE, page(links=link("/files/blatt.docx", "blatt.docx"))),
        BLATT: fetched(BLATT, corpus("lesson.docx"), content_type=DOCX),
    }
    harness = Harness(loop, answers)
    alone = without_attachments(answers)

    envelope = run(harness)

    assert envelope.status == "success", envelope.error
    data = data_of(envelope)
    content = data.content or ""
    assert content.startswith(alone.content or "")
    assert "Wasserkreislauf" in content.split("## blatt.docx")[0]
    assert "\n\n## blatt.docx\n\n" in content
    assert "Photosynthese" in content.split("## blatt.docx")[1]
    assert data.warnings.count("attachment_read") == 1
    assert "attachment_failed" not in data.warnings
    assert alone.word_count < harness.settings.quality_min_words <= data.word_count
    assert data.quality is not None
    assert data.word_count == data.quality.word_count == count_words(content)
    assert data.fingerprint is not None
    assert data.mode_used == "fast"
    assert data.url == PAGE
    assert data.final_url == PAGE
    assert data.content_type == "text/html"
    assert data.document is None
    # The attachment was fetched as any document: within DOCUMENT_MAX_BYTES, in a turn of its own.
    (options,) = [options for url, options in harness.fetcher.calls if url == BLATT]
    assert options["max_bytes"] == harness.settings.document_max_bytes
    assert harness.gate.held() == []


@pytest.mark.parametrize("fmt", ["text", "html"])
def test_attachment_heading_in_each_format(loop: Loop, fmt: str) -> None:
    answers: dict[str, Any] = {
        PAGE: fetched(PAGE, page(links=link("/files/blatt.docx", "blatt.docx"))),
        BLATT: fetched(BLATT, corpus("lesson.docx"), content_type=DOCX),
    }

    data = data_of(run(Harness(loop, answers), format=fmt))

    content = data.content or ""
    assert "Photosynthese" in content
    if fmt == "text":
        assert "\n\nblatt.docx\n\n" in content
        assert "##" not in content
    else:
        assert "<h2>blatt.docx</h2>" in content
        assert content.count("<body") == content.count("</body>") <= 1
        assert content.index("<h2>blatt.docx</h2>") < content.index("Photosynthese")
    assert data.warnings.count("attachment_read") == 1


def test_label_is_escaped_in_html() -> None:
    answers: dict[str, Any] = {
        PAGE: fetched(PAGE, page(links=link("/files/blatt-1.pdf", "Blatt &lt;b&gt;1"))),
        pdf_url(1): pdf_answer(pdf_url(1)),
    }

    content = data_of(run(fake_harness(answers), format="html")).content or ""

    assert "<h2>Blatt &lt;b&gt;1</h2>" in content


def test_drupal_file_entity_is_followed() -> None:
    entity = (
        '{"type":"file--file","id":"1","attributes":{"filename":"wasser.pdf",'
        '"uri":{"value":"public://wasser.pdf","url":"/sites/default/files/wasser.pdf"},'
        '"filemime":"application/pdf","filesize":1000}}'
    )
    url = f"{ORIGIN}/sites/default/files/wasser.pdf"
    html = page().replace("</body>", f'<script type="application/json">{entity}</script></body>')
    harness = fake_harness({PAGE: fetched(PAGE, html), url: pdf_answer(url)})

    data = data_of(run(harness))

    assert url in fetched_urls(harness)
    assert "## wasser.pdf" in (data.content or "")
    assert data.warnings.count("attachment_read") == 1


# --- not followed ---


def test_page_with_enough_words_fetches_nothing() -> None:
    harness = fake_harness(
        {
            PAGE: fetched(PAGE, page(PARAGRAPH * 7, links=pdf_link(1))),
            pdf_url(1): pdf_answer(pdf_url(1)),
        }
    )

    data = data_of(run(harness))

    assert data.word_count >= harness.settings.attachments_below_words
    assert pdf_url(1) not in fetched_urls(harness)
    assert "attachment_read" not in data.warnings


def test_thin_page_with_twenty_pdf_links_fetches_two() -> None:
    links = "".join(pdf_link(number) for number in range(1, 21))
    answers: dict[str, Any] = {PAGE: fetched(PAGE, page(links=links))}
    answers |= {pdf_url(number): pdf_answer(pdf_url(number)) for number in range(1, 21)}
    # The link labels are page text too (twice: the list of files is kept): 100 words.
    harness = fake_harness(answers, attachments_below_words=200)

    data = data_of(run(harness))

    followed = [url for url in fetched_urls(harness) if url.endswith(".pdf")]
    assert followed == [pdf_url(1), pdf_url(2)]
    assert data.warnings.count("attachment_read") == 2
    content = data.content or ""
    assert content.index("## Blatt 1") < content.index("## Blatt 2")


def test_attachments_max_zero_fetches_nothing() -> None:
    harness = fake_harness(
        {PAGE: fetched(PAGE, page(links=pdf_link(1))), pdf_url(1): pdf_answer(pdf_url(1))},
        attachments_max=0,
    )

    data = data_of(run(harness))

    assert fetched_urls(harness).count(pdf_url(1)) == 0
    assert "attachment_read" not in data.warnings
    assert "attachment_failed" not in data.warnings


def test_media_and_archives_are_not_fetched() -> None:
    links = (
        link("/files/film.mp4", "Film")
        + link("/files/paket.zip", "Paket")
        + link("/files/text.odt", "Text")
        + pdf_link(1)
    )
    harness = fake_harness(
        {PAGE: fetched(PAGE, page(links=links)), pdf_url(1): pdf_answer(pdf_url(1))},
        attachments_max=1,
    )

    data = data_of(run(harness))

    assert [url for url in fetched_urls(harness) if "/files/" in url] == [pdf_url(1)]
    assert data.warnings.count("attachment_read") == 1


@pytest.mark.parametrize("bad", ["over_limit", "robots", "not_found", "image", "html"])
def test_failed_attachment_leaves_the_page_as_it_was(bad: str) -> None:
    url = pdf_url(1)
    answer: Any = pdf_answer(url)
    target = url
    request: dict[str, Any] = {}
    if bad == "over_limit":
        answer = fetched(url, b"%PDF-1.4\n" + b"x" * 4096, content_type="application/pdf")
    elif bad == "robots":
        target = f"{OTHER}/files/blatt-1.pdf"
        answer = pdf_answer(target)
        request["compliance"] = "enforce"
    elif bad == "not_found":
        answer = fetched(url, "Not found", status=404)
    elif bad == "image":
        answer = fetched(url, PNG, content_type="image/png")
    else:
        answer = fetched(url, page(PARAGRAPH * 3))
    links = link(target, "Blatt 1")
    answers: dict[str, Any] = {
        PAGE: fetched(PAGE, page(links=links)),
        target: answer,
        f"{OTHER}/robots.txt": fetched(f"{OTHER}/robots.txt", "User-agent: *\nDisallow: /"),
    }
    harness = fake_harness(answers, document_max_bytes=2048)
    alone = without_attachments(answers, **request)

    envelope = run(harness, **request)

    data = data_of(envelope)
    assert data.content == alone.content
    assert data.word_count == alone.word_count
    assert data.warnings.count("attachment_failed") == 1
    assert "attachment_read" not in data.warnings
    assert envelope.status == "partial"
    assert data.upstream_status == 200  # the page's, not the attachment's
    assert data.final_url == PAGE
    assert data.compliance.decision == "allowed"  # the page's report
    if bad == "robots":
        assert target not in fetched_urls(harness)  # blocked before its request
    if bad == "over_limit":
        (options,) = [
            options for fetched_url, options in harness.fetcher.calls if fetched_url == url
        ]
        assert options["max_bytes"] == 2048
    assert harness.gate.held() == []


def test_declared_size_over_the_limit_is_not_fetched() -> None:
    entity = (
        '{"type":"file--file","id":"1","attributes":{"filename":"gross.pdf",'
        '"uri":{"value":"public://gross.pdf","url":"/files/gross.pdf"},'
        '"filemime":"application/pdf","filesize":50000000}}'
    )
    url = f"{ORIGIN}/files/gross.pdf"
    html = page().replace("</body>", f'<script type="application/json">{entity}</script></body>')
    harness = fake_harness({PAGE: fetched(PAGE, html), url: pdf_answer(url)})

    data = data_of(run(harness))

    assert url not in fetched_urls(harness)
    assert data.warnings.count("attachment_failed") == 1


def test_redirect_to_another_host_takes_that_hosts_turn() -> None:
    moved = "https://api.example.org/files/blatt-1.pdf"
    harness = fake_harness(
        {
            PAGE: fetched(PAGE, page(links=pdf_link(1))),
            pdf_url(1): Redirect(moved),
            moved: pdf_answer(moved),
        }
    )

    data = data_of(run(harness))

    assert data.warnings.count("attachment_read") == 1
    takes = [host for event, host in harness.gate.events if event == "take"]
    assert takes[-2:] == ["example.org", "api.example.org"]
    assert "https://api.example.org/robots.txt" in fetched_urls(harness)
    assert harness.gate.held() == []


def test_attachment_that_needs_ocr_appends_its_text_layer() -> None:
    harness = Harness(
        None,
        {PAGE: fetched(PAGE, page(links=pdf_link(1))), pdf_url(1): pdf_answer(pdf_url(1))},
        pool=FakePool(reply(content=ATTACHMENT_TEXT, needs_ocr=True)),
        docling_enabled=True,
    )
    handed: list[OcrNeeded] = []

    async def handoff(
        needed: OcrNeeded, without: Envelope[ExtractResult]
    ) -> Envelope[ExtractResult]:
        handed.append(needed)
        return without

    envelope = harness.run(handoff, url=PAGE, cache=False)

    assert handed == []
    data = data_of(envelope)
    assert "Photosynthesis" in (data.content or "")
    assert data.warnings.count("attachment_read") == 1
    assert data.document is None


def test_slow_attachment_leaves_the_page_its_answer() -> None:
    url = pdf_url(1)

    async def slow(_validators: Validators | None) -> FetchResult:
        await asyncio.sleep(10)
        return pdf_answer(url)

    harness = fake_harness(
        {PAGE: fetched(PAGE, page(links=pdf_link(1))), url: slow}, extract_timeout_seconds=3
    )

    envelope = run(harness)

    assert envelope.status == "partial", envelope.error
    assert data_of(envelope).warnings.count("attachment_failed") == 1
    assert harness.gate.held() == []


# --- browser ---


def test_rendered_thin_page_gets_its_attachments() -> None:
    renderer = FakeRenderer(rendered(page(links=pdf_link(1))))
    harness = Harness(
        None,
        {pdf_url(1): pdf_answer(pdf_url(1))},
        pool=FakePool(reply(content=ATTACHMENT_TEXT)),
        renderer=renderer,
        browser_enabled=True,
    )

    envelope = run(harness, mode="browser")

    assert envelope.status == "success", envelope.error
    data = data_of(envelope)
    assert data.mode_used == "browser"
    assert "## Blatt 1" in (data.content or "")
    assert data.warnings.count("attachment_read") == 1
    assert harness.gate.held() == []


def test_a_bug_in_the_candidates_keeps_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_html: str, _url: str) -> list[Any]:
        raise RuntimeError("bug")

    monkeypatch.setattr("text_extraction.pipeline_attachments.attachment_candidates", broken)
    answers: dict[str, Any] = {PAGE: fetched(PAGE, page(links=pdf_link(1)))}
    harness = fake_harness(answers)

    envelope = run(harness)

    assert envelope.status == "partial"
    assert data_of(envelope).content == without_attachments(answers).content
    assert pdf_url(1) not in fetched_urls(harness)


def test_site_crawl_page_fetches_no_attachment() -> None:
    harness = fake_harness(
        {PAGE: fetched(PAGE, page(links=pdf_link(1))), pdf_url(1): pdf_answer(pdf_url(1))}
    )

    work = harness.pipeline.run(ExtractRequest(url=PAGE, cache=False), USER, site_links=True)
    data = data_of(asyncio.run(work))

    assert pdf_url(1) not in fetched_urls(harness)
    assert "attachment_read" not in data.warnings
