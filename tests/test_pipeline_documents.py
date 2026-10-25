"""Documents fetched by URL: the pipeline's conversion branch (``pipeline_documents.py``).

The conversions run through one real ``DocumentPool`` child for the module (its start imports
MarkItDown, a few seconds), on an event loop of its own; the pipelines of the tests run on that
loop too. The error mapping and the wiring use a fake pool.
"""

import asyncio
import io
import json
import time
import zipfile
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis
from prometheus_client import CollectorRegistry
from pytest_httpserver import HTTPServer
from structlog.testing import capture_logs

from tests.fakes import (
    ORIGIN,
    PAGE,
    PARAGRAPH,
    Answer,
    FakeFetcher,
    FakeRenderer,
    RecordingGate,
    fetched,
    rendered,
    response,
)
from text_extraction import browser_worker, worker
from text_extraction.app import create_public_app
from text_extraction.auth import Principal
from text_extraction.compliance import ComplianceChecker, ComplianceReport
from text_extraction.conversion_pool import ConversionPool
from text_extraction.documents.pool import (
    ConversionCrashed,
    ConversionError,
    ConversionOutOfMemory,
    ConversionTimeout,
    DocumentPool,
)
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.escalation import needs_browser
from text_extraction.extract_models import ExtractRequest, ExtractResult
from text_extraction.fetch import Fetcher, FetchResult, Validators
from text_extraction.fingerprint import FingerprintStore
from text_extraction.outbound import DomainGate
from text_extraction.pipeline import Pipeline, create_pipeline
from text_extraction.pipeline_documents import (
    MARKITDOWN_TARGET,
    OCR_UNAVAILABLE_WARNING,
    DocumentSteps,
    OcrNeeded,
    document_input,
)
from text_extraction.render import Renderer
from text_extraction.result_cache import ResultCache
from text_extraction.settings import Settings

CORPUS = Path(__file__).parent / "documents" / "corpus"
GOLDEN = Path(__file__).parent / "documents" / "golden" / "package4_answers.json"
USER = Principal(name="alice", role="user", rate_limit_per_minute=None)
DOC = "https://example.org/files/lesson.pdf?token=secret-token"
ARTICLE_HTML = "<html><body><p>" + PARAGRAPH * 3 + "</p></body></html>"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 64


def corpus(name: str) -> bytes:
    return (CORPUS / name).read_bytes()


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"auth_mode": "none", "outbound_min_delay_ms": 0}
    values.update(overrides)
    return Settings(**values)


def data_of(envelope: Envelope[ExtractResult]) -> ExtractResult:
    assert envelope.data is not None
    return envelope.data


class Loop:
    """An event loop for the module, with one started pool of MarkItDown children."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.pool = DocumentPool(1, 2048, target=MARKITDOWN_TARGET)
        self.loop.run_until_complete(self.pool.start())

    def run[T](self, work: Coroutine[Any, Any, T]) -> T:
        return self.loop.run_until_complete(work)

    def close(self) -> None:
        self.loop.run_until_complete(self.pool.shutdown())
        self.loop.close()


@pytest.fixture(scope="module")
def loop() -> Iterator[Loop]:
    instance = Loop()
    try:
        yield instance
    finally:
        instance.close()


class FakePool:
    """A document pool that answers each call with ``answer`` (or raises it)."""

    def __init__(self, answer: object = None) -> None:
        self.answer = answer
        self.started = 0
        self.calls: list[tuple[str, dict[str, Any], float]] = []

    async def start(self) -> None:
        self.started += 1

    async def run(self, function_name: str, payload: dict[str, Any], deadline: float) -> Any:
        self.calls.append((function_name, payload, deadline))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    async def shutdown(self) -> None:
        pass


def reply(**fields: Any) -> dict[str, Any]:
    """A well-formed reply of the child's ``markitdown`` function."""
    values: dict[str, Any] = {
        "content": "# Lesson\n\n" + PARAGRAPH * 3,
        "title": "Lesson",
        "warnings": [],
        "pages": 1,
        "pages_converted": 1,
        "needs_ocr": False,
        "metadata": None,
    }
    values.update(fields)
    return values


class Harness:
    """A pipeline around a fake fetcher. With the module's ``loop`` it converts in the real
    child, on that loop; with a fake ``pool`` each run gets a loop of its own."""

    def __init__(
        self,
        loop: Loop | None,
        answers: dict[str, Answer],
        *,
        pool: object | None = None,
        renderer: FakeRenderer | None = None,
        **overrides: Any,
    ) -> None:
        self.loop = loop
        self.registry = CollectorRegistry()
        self.fetcher = FakeFetcher(answers)
        self.valkey = FakeAsyncRedis()
        self.settings = make_settings(**overrides)
        fetcher = cast(Fetcher, self.fetcher)
        self.pool = pool if pool is not None else (loop.pool if loop is not None else None)
        self.gate = RecordingGate(DomainGate(self.valkey, self.settings))
        self.fetcher.before_request = self.gate.before_request
        self.fetcher.on_request_sent = self.gate.mark_sent
        self.pipeline = Pipeline(
            fetcher,
            cast(DomainGate, self.gate),
            ComplianceChecker(fetcher, self.valkey, self.settings),
            ResultCache(self.valkey, self.settings),
            FingerprintStore(self.valkey, self.settings),
            self.settings,
            self.registry,
            renderer=cast(Renderer | None, renderer),
            document_pool=cast(DocumentPool, self.pool),
        )

    def metric(self, name: str, **labels: str) -> float:
        return self.registry.get_sample_value(name, labels) or 0.0

    def _run[T](self, work: Coroutine[Any, Any, T]) -> T:
        return asyncio.run(work) if self.loop is None else self.loop.run(work)

    def run(
        self,
        handoff: Callable[[OcrNeeded], Awaitable[Envelope[ExtractResult]]] | None = None,
        *,
        draft: Envelope[ExtractResult] | None = None,
        **request: Any,
    ) -> Envelope[ExtractResult]:
        request.setdefault("url", DOC)
        work = self.pipeline.run(ExtractRequest(**request), USER, draft=draft, ocr_handoff=handoff)
        return self._run(work)

    def keys(self, pattern: str) -> list[bytes]:
        async def scan() -> list[bytes]:
            return [key async for key in self.valkey.scan_iter(pattern)]

        return self._run(scan())


def pdf(body: bytes | None = None, **options: Any) -> FetchResult:
    options.setdefault("content_type", "application/pdf")
    return fetched(DOC, corpus("text.pdf") if body is None else body, **options)


# --- conversion ---


def test_pdf_url_converts(loop: Loop, httpserver: HTTPServer) -> None:
    for path in ("/robots.txt", "/ai.txt", "/.well-known/tdmrep.json"):
        httpserver.expect_request(path).respond_with_data("", status=404)
    httpserver.expect_request("/files/lesson.pdf").respond_with_data(
        corpus("text.pdf"), content_type="application/pdf"
    )
    settings = make_settings(fetch_allow_private_networks=True)

    async def scenario() -> Envelope[ExtractResult]:
        fetcher = Fetcher(settings)
        pipeline = create_pipeline(
            settings, FakeAsyncRedis(), fetcher, CollectorRegistry(), document_pool=loop.pool
        )
        try:
            url = httpserver.url_for("/files/lesson.pdf")
            return await pipeline.run(ExtractRequest(url=url), USER)
        finally:
            await fetcher.aclose()

    envelope = loop.run(scenario())

    assert envelope.status == "success", envelope.error
    data = data_of(envelope)
    assert data.content_type == "application/pdf"
    assert data.mode_used == "fast"
    assert data.upstream_status == 200
    assert data.attempts == 1
    assert data.content is not None
    assert "Photosynthese" in data.content
    assert data.document is not None
    assert data.document.kind == "pdf"
    assert data.document.converter == "markitdown"
    assert data.document.pages == data.document.pages_converted == 3
    assert data.document.ocr is False
    assert data.fingerprint is not None
    assert data.quality is not None
    assert data.word_count == data.quality.word_count > 0
    assert data.compliance.decision == "allowed"


@pytest.mark.parametrize("content_type", ["application/octet-stream", "text/plain", None])
def test_pdf_with_another_or_no_type_converts(loop: Loop, content_type: str | None) -> None:
    harness = Harness(loop, {DOC: pdf(content_type=content_type)})

    envelope = harness.run()

    assert envelope.status == "success", envelope.error
    assert data_of(envelope).content_type == "application/pdf"  # the sniffed type (F7)
    assert data_of(envelope).document is not None


def test_octet_stream_pdf_converts(loop: Loop) -> None:
    harness = Harness(loop, {DOC: pdf(content_type="application/octet-stream")})

    assert harness.run().status == "success"


def test_text_plain_pdf_converts(loop: Loop) -> None:
    harness = Harness(loop, {DOC: pdf(content_type="text/plain")})

    envelope = harness.run()

    assert data_of(envelope).document is not None
    assert data_of(envelope).content_type == "application/pdf"


def test_docx_converts_with_metadata_and_formats(loop: Loop) -> None:
    docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    harness = Harness(
        loop, {DOC: fetched(DOC, corpus("lesson.docx"), content_type=docx)}, quality_min_words=0
    )

    markdown, text, html = (harness.run(format=fmt) for fmt in ("markdown", "text", "html"))

    assert data_of(markdown).document is not None
    assert data_of(markdown).document.kind == "docx"  # type: ignore[union-attr]
    assert "|" in (data_of(markdown).content or "")
    assert "|" not in (data_of(text).content or "")
    assert (data_of(html).content or "").startswith("<html><body>")
    assert data_of(markdown).links is None
    assert data_of(harness.run(format="markdown", links=True, cache=False)).links == []


def test_document_metadata_on_request(loop: Loop) -> None:
    harness = Harness(loop, {DOC: pdf(corpus("xmp-cc-by-sa.pdf"))}, quality_min_words=0)

    with_metadata, without = harness.run(), harness.run(metadata=False)

    metadata = data_of(with_metadata).metadata
    assert metadata is not None
    assert metadata.license is not None
    assert metadata.license.id == "CC-BY-SA-4.0"
    assert data_of(without).metadata is None


def test_csv_gets_the_header_charset(loop: Loop) -> None:
    body = "Имя;Размер\nЕль;30\n".encode("koi8-r")  # noqa: RUF001 - misread without the header's charset
    answer = replace(fetched(DOC, body, content_type="text/csv"), charset="koi8-r")
    harness = Harness(loop, {DOC: answer}, quality_min_words=0)

    envelope = harness.run()

    assert data_of(envelope).document is not None
    assert "Размер" in (data_of(envelope).content or "")


@pytest.mark.parametrize("content_type", ["application/pdf", "application/octet-stream", None])
def test_html_body_under_a_document_type_converts_as_html(content_type: str | None) -> None:
    """Detection follows the bytes: a "PDF" whose body is an HTML page is that page."""
    harness = Harness(
        None,
        {DOC: fetched(DOC, ARTICLE_HTML, content_type=content_type)},
        pool=FakePool(AssertionError("not a document")),
    )

    envelope = harness.run()

    assert envelope.status == "success", envelope.error
    data = data_of(envelope)
    assert data.content_type == "text/html"  # the sniffed type, as for documents (F7)
    assert data.document is None
    assert "Photosynthesis is the process" in (data.content or "")
    assert data.mode_used == "fast"


def test_html_page_answer_unchanged() -> None:
    """The package-4 answers of an HTML page, a text page and an unsupported type, recorded
    before documents were added, are unchanged (apart from the timings)."""
    golden = json.loads(GOLDEN.read_text())
    cases: dict[str, tuple[FetchResult, dict[str, Any]]] = {
        "article": (fetched(), {"links": True}),
        "plain": (
            fetched(body=PARAGRAPH * 5, content_type="text/plain"),
            {"format": "html", "links": True},
        ),
        "unsupported": (fetched(body=b"GIF89a....", content_type="image/gif"), {}),
    }
    for name, (answer, request) in cases.items():
        harness = Harness(None, {PAGE: answer}, pool=FakePool(AssertionError("no document")))

        envelope = harness.run(url=PAGE, **request)

        dumped = envelope.model_dump(mode="json")
        dumped["data"].pop("timings")
        dumped.pop("trace_id")
        assert dumped == golden[name], name


# --- limits, compliance, cache ---


def test_document_over_document_max_bytes_too_large() -> None:
    pool = FakePool(AssertionError("not converted"))
    harness = Harness(None, {DOC: pdf()}, pool=pool, document_max_bytes=1024)

    envelope = harness.run()

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.TOO_LARGE
    assert envelope.error.retryable is False
    assert "1024" in envelope.error.message
    assert pool.calls == []


def test_document_limit_is_not_convert_max_bytes() -> None:
    pool = FakePool(reply())
    harness = Harness(None, {DOC: pdf()}, pool=pool, convert_max_bytes=1024)

    assert harness.run().status == "success"
    assert len(pool.calls) == 1


def test_x_robots_tag_noai_blocks_pdf() -> None:
    pool = FakePool(reply())
    harness = Harness(None, {DOC: pdf(headers={"x-robots-tag": "noai"})}, pool=pool)

    enforced, reported = harness.run(), harness.run(compliance="report")

    assert enforced.status == "failed"
    assert enforced.error is not None
    assert enforced.error.code == ErrorCode.TDM_RESERVED
    assert data_of(enforced).compliance.noai is True
    assert len(pool.calls) == 1  # only the reported one converted
    assert reported.status == "success"
    assert data_of(reported).compliance.decision == "reported"


def test_pdf_cached_and_revalidated() -> None:
    pool = FakePool(reply())
    harness = Harness(None, {DOC: pdf()}, pool=pool)

    first, second = harness.run(), harness.run()

    assert data_of(first).cache == "miss"
    assert data_of(second).cache == "hit"
    assert data_of(second).document == data_of(first).document
    assert len(pool.calls) == 1

    async def answer(validators: Validators | None) -> FetchResult:
        if validators is not None and validators.etag == '"v1"':
            return pdf(b"", status=304, headers={"etag": '"v1"'})
        return pdf(headers={"etag": '"v1"'})

    pool = FakePool(reply())
    harness = Harness(None, {DOC: answer}, pool=pool, result_cache_ttl_seconds=0)

    first, second = harness.run(), harness.run()

    assert data_of(second).cache == "revalidated"
    assert data_of(second).content == data_of(first).content
    assert data_of(second).document == data_of(first).document
    assert len(pool.calls) == 1


def test_same_bytes_revalidate_a_document() -> None:
    pool = FakePool(reply())
    harness = Harness(None, {DOC: pdf()}, pool=pool, result_cache_ttl_seconds=0)

    _, second = harness.run(), harness.run()

    assert data_of(second).cache == "revalidated"
    assert len(pool.calls) == 1


def test_ocr_option_in_cache_key() -> None:
    pool = FakePool(reply())
    harness = Harness(None, {DOC: pdf()}, pool=pool)

    auto, never, again = harness.run(), harness.run(ocr="never"), harness.run(ocr="never")

    assert data_of(auto).cache == "miss"
    assert data_of(never).cache == "miss"
    assert data_of(again).cache == "hit"
    assert len(pool.calls) == 2


def test_payload_sent_to_the_child() -> None:
    pool = FakePool(reply())
    harness = Harness(None, {DOC: pdf(headers={"content-language": "de"})}, pool=pool)

    started = time.monotonic()
    harness.run(format="text", metadata=False)

    ((name, payload, deadline),) = pool.calls
    assert name == "markitdown"
    assert payload["body"] == corpus("text.pdf")
    assert payload["kind"] == "pdf"
    assert payload["format"] == "text"
    assert payload["base"] == DOC
    assert payload["content_language"] == "de"
    assert payload["charset"] == "utf-8"
    assert payload["max_pages"] == harness.settings.document_max_pages
    assert payload["max_uncompressed"] == harness.settings.document_max_uncompressed_bytes
    # The language comes with the metadata, so the child always reads it.
    assert payload["metadata"] is True
    assert pool.started == 1  # started on first use: no pool start without a document
    # Before the extraction's own deadline (asyncio's loop clock is the monotonic one).
    assert deadline < started + harness.settings.extract_timeout_seconds


# --- OCR ---


def test_scan_without_docling_answers_text_layer(loop: Loop) -> None:
    harness = Harness(loop, {DOC: pdf(corpus("scan.pdf"))})

    envelope = harness.run()

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.EMPTY_CONTENT
    data = data_of(envelope)
    assert OCR_UNAVAILABLE_WARNING in data.warnings
    assert data.document is not None
    assert data.document.ocr is False


def test_scan_with_ocr_never_has_no_ocr_warning() -> None:
    harness = Harness(None, {DOC: pdf()}, pool=FakePool(reply(needs_ocr=True)))

    assert OCR_UNAVAILABLE_WARNING not in data_of(harness.run(ocr="never")).warnings


@pytest.mark.parametrize("docling", [False, True])
def test_image_without_docling_unsupported(docling: bool) -> None:
    pool = FakePool(AssertionError("not converted"))
    harness = Harness(
        None, {DOC: fetched(DOC, PNG, content_type="image/png")}, pool=pool, docling_enabled=docling
    )

    envelope = harness.run()

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.UNSUPPORTED_CONTENT_TYPE
    assert envelope.error.message == "OCR is not enabled on this service."
    assert data_of(envelope).content_type == "image/png"
    assert pool.calls == []


def test_image_with_ocr_never_unsupported() -> None:
    harness = Harness(
        None,
        {DOC: fetched(DOC, PNG, content_type="image/png")},
        pool=FakePool(AssertionError("not converted")),
        docling_enabled=True,
    )

    envelope = harness.run(ocr="never")

    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.UNSUPPORTED_CONTENT_TYPE
    assert "ocr: never" in envelope.error.message


def test_scan_with_docling_returns_ocr_needed() -> None:
    pool = FakePool(reply(content="", needs_ocr=True))
    harness = Harness(
        None,
        {DOC: pdf(headers={"etag": '"v1"', "content-language": "de"})},
        pool=pool,
        docling_enabled=True,
    )
    handed: list[OcrNeeded] = []

    async def handoff(needed: OcrNeeded) -> Envelope[ExtractResult]:
        handed.append(needed)
        assert needed.draft is not None
        return needed.draft

    envelope = harness.run(handoff)

    (needed,) = handed
    assert needed.draft is not None
    draft = data_of(needed.draft)
    assert draft.document is not None
    assert draft.document.converter == "markitdown"
    assert OCR_UNAVAILABLE_WARNING not in draft.warnings  # the fallback adds it, not the draft
    meta = needed.input.meta
    assert needed.input.body == corpus("text.pdf")
    assert meta.kind == "pdf"
    assert meta.content_type == "application/pdf"
    assert meta.file_name is None
    assert meta.final_url == DOC
    assert meta.upstream_status == 200
    assert meta.etag == '"v1"'
    assert meta.content_language == "de"
    assert meta.raw_sha256 == pdf().raw_sha256
    assert meta.needs_ocr is True
    assert meta.compliance is not None
    assert meta.compliance.decision == "allowed"
    assert json.loads(meta.model_dump_json())["kind"] == "pdf"  # stored as JSON (F12)
    # The handoff's answer is returned as it is: the OCR pool stores what it keeps.
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.EMPTY_CONTENT
    assert OCR_UNAVAILABLE_WARNING not in data_of(envelope).warnings
    assert harness.keys("res:*") == []
    assert harness.keys("fp:*") == []


def test_image_with_docling_hands_over_without_draft() -> None:
    harness = Harness(
        None,
        {DOC: fetched(DOC, PNG, content_type="image/png")},
        pool=FakePool(AssertionError("not converted")),
        docling_enabled=True,
    )
    handed: list[OcrNeeded] = []

    async def handoff(needed: OcrNeeded) -> Envelope[ExtractResult]:
        handed.append(needed)
        return Envelope(status="success", data=None)

    harness.run(handoff)

    (needed,) = handed
    assert needed.draft is None
    assert needed.input.meta.kind == "png"
    assert needed.input.meta.content_type == "image/png"


def test_ocr_always_hands_over_a_text_pdf() -> None:
    harness = Harness(None, {DOC: pdf()}, pool=FakePool(reply()), docling_enabled=True)
    handed: list[OcrNeeded] = []

    async def handoff(needed: OcrNeeded) -> Envelope[ExtractResult]:
        handed.append(needed)
        assert needed.draft is not None
        return needed.draft

    assert harness.run(handoff, ocr="always").status == "success"
    assert len(handed) == 1
    assert harness.run(handoff, ocr="auto", cache=False).status == "success"
    assert len(handed) == 1  # a good text layer stays with MarkItDown


def test_without_a_handoff_the_draft_answers_with_ocr_unavailable() -> None:
    harness = Harness(
        None, {DOC: pdf()}, pool=FakePool(reply(needs_ocr=True)), docling_enabled=True
    )

    first, second = harness.run(), harness.run()

    assert first.status == "success"
    assert OCR_UNAVAILABLE_WARNING in data_of(first).warnings
    assert data_of(second).cache == "hit"  # cached as final (F10)


def test_document_never_escalates(loop: Loop) -> None:
    harness = Harness(loop, {DOC: pdf(corpus("scan.pdf"))})
    envelope = harness.run(mode="auto")
    data = data_of(envelope)
    assert data.quality is not None
    recommended = data.model_copy(
        update={"quality": data.quality.model_copy(update={"browser_recommended": True})}
    )
    challenged = envelope.model_copy(update={"data": recommended})

    assert needs_browser(challenged, unblock=False) is False
    assert needs_browser(challenged, unblock=True) is False


# --- conversion failures ---


@pytest.mark.parametrize(
    ("raised", "code", "retryable", "message"),
    [
        (ConversionError("DocumentTooLarge", "x"), ErrorCode.TOO_LARGE, False, None),
        (ConversionError("DocumentUnreadable", "x"), ErrorCode.CONVERSION_FAILED, False, None),
        (ConversionError("PdfUnreadable", "x"), ErrorCode.CONVERSION_FAILED, False, None),
        (
            ConversionError("PasswordProtected", "x"),
            ErrorCode.CONVERSION_FAILED,
            False,
            "The PDF is password-protected.",
        ),
        (ConversionTimeout("late"), ErrorCode.TIMEOUT, True, None),
        (ConversionOutOfMemory("big"), ErrorCode.TOO_LARGE, False, None),
    ],
)
def test_document_failures(
    raised: Exception, code: ErrorCode, retryable: bool, message: str | None
) -> None:
    harness = Harness(None, {DOC: pdf()}, pool=FakePool(raised))

    with capture_logs() as logs:
        envelope = harness.run()

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == code
    assert envelope.error.retryable is retryable
    if message is not None:
        assert envelope.error.message == message
    assert data_of(envelope).document is None
    assert data_of(envelope).content_type == "application/pdf"
    # The document's own trouble: nothing of ours to log beyond the request's line.
    assert [line["event"] for line in logs] == ["extract_done"]


@pytest.mark.parametrize(
    ("answer", "error_type"),
    [
        (ConversionError("KeyError", "the secret-token document said so"), "KeyError"),
        (ConversionCrashed("died"), "ConversionCrashed"),
        ({"content": "x"}, "ValueError"),  # a malformed reply
        (reply(metadata={"title": 7, "licenses": "no"}), "ValidationError"),
        (LookupError("no function"), "LookupError"),
    ],
)
def test_our_own_faults_are_logged_by_type_only(answer: object, error_type: str) -> None:
    harness = Harness(None, {DOC: pdf()}, pool=FakePool(answer))

    with capture_logs() as logs:
        envelope = harness.run()

    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.CONVERSION_FAILED
    assert envelope.error.message == "The document could not be converted."
    (line,) = [line for line in logs if line["event"] == "document_conversion_failed"]
    assert line["error_type"] == error_type
    assert line["kind"] == "pdf"
    assert line["host"] == "example.org"
    assert "exc_info" not in line
    assert "secret-token" not in repr(logs)


def test_a_pool_that_is_shut_down_is_retryable() -> None:
    harness = Harness(None, {DOC: pdf()}, pool=FakePool(RuntimeError("shut down")))

    with capture_logs() as logs:
        envelope = harness.run()

    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.INTERNAL_ERROR
    assert envelope.error.retryable is True
    assert [line["error_type"] for line in logs if line.get("error_type")] == ["RuntimeError"]


def test_container_bomb_through_the_child(loop: Loop) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types><Override PartName="/word/document.xml" ContentType="x"/></Types>',
        )
        archive.writestr("word/document.xml", b"\0" * 300_000_000)
    docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    harness = Harness(loop, {DOC: fetched(DOC, buffer.getvalue(), content_type=docx)})

    envelope = harness.run()

    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.TOO_LARGE


def test_password_protected_pdf_through_the_child(loop: Loop) -> None:
    harness = Harness(loop, {DOC: pdf(corpus("encrypted.pdf"))})

    envelope = harness.run()

    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.CONVERSION_FAILED
    assert envelope.error.message == "The PDF is password-protected."


# --- wiring ---


def test_text_plain_with_a_csv_path_stays_plain_text() -> None:
    """The extension breaks no tie for a declared text/plain body (the package-2 answer)."""
    url = "https://example.org/data/table.csv"
    body = "Name,Size\n" + PARAGRAPH * 3
    harness = Harness(
        None,
        {url: fetched(url, body, content_type="text/plain")},
        pool=FakePool(AssertionError("no document")),
    )

    envelope = harness.run(url=url)

    assert envelope.status == "success"
    data = data_of(envelope)
    assert data.document is None
    assert data.content_type == "text/plain"
    csv = Harness(None, {url: fetched(url, body, content_type="text/csv")}, pool=FakePool(reply()))
    assert data_of(csv.run(url=url)).document is not None  # declared CSV still converts


# --- the OCR handoff and its deadline ---


def test_handoff_runs_after_the_extraction_deadline() -> None:
    """The handoff waits for OCR after the fast phase (F12): not within its deadline."""
    harness = Harness(
        None,
        {DOC: pdf()},
        pool=FakePool(reply(needs_ocr=True)),
        docling_enabled=True,
        extract_timeout_seconds=2,
    )

    async def handoff(needed: OcrNeeded) -> Envelope[ExtractResult]:
        await asyncio.sleep(2.5)  # longer than the whole extraction may take
        assert needed.draft is not None
        assert needed.draft.data is not None
        data = needed.draft.data.model_copy(update={"title": "From OCR"})
        return needed.draft.model_copy(update={"data": data})

    started = time.monotonic()
    envelope = harness.run(handoff)

    assert time.monotonic() - started >= 2.5
    assert envelope.status == "success"
    data = data_of(envelope)
    assert data.title == "From OCR"
    assert data.timings.total_ms >= 2500  # the wait is part of the request
    assert harness.keys("res:*") == []  # nothing stored: the OCR pool keeps what it keeps
    assert harness.metric("text_extraction_extract_total", mode="auto", outcome="success") == 1


# --- metrics (F19) ---


def test_conversion_metrics() -> None:
    harness = Harness(None, {DOC: pdf()}, pool=FakePool(reply(pages=12, pages_converted=5)))

    harness.run()

    labels = {"converter": "markitdown", "kind": "pdf"}
    total = "text_extraction_document_conversions_total"
    assert harness.metric(total, **labels, outcome="success") == 1
    assert harness.metric("text_extraction_document_pages_total", converter="markitdown") == 5
    seconds = "text_extraction_document_conversion_seconds_count"
    assert harness.metric(seconds, converter="markitdown") == 1


@pytest.mark.parametrize(
    ("raised", "outcome"),
    [
        (ConversionError("DocumentTooLarge", "x"), "too_large"),
        (ConversionTimeout("late"), "timeout"),
        (ConversionCrashed("died"), "conversion_failed"),
    ],
)
def test_conversion_metrics_of_failures(raised: Exception, outcome: str) -> None:
    harness = Harness(None, {DOC: pdf()}, pool=FakePool(raised))

    harness.run()

    total = "text_extraction_document_conversions_total"
    assert harness.metric(total, converter="markitdown", kind="pdf", outcome=outcome) == 1
    assert harness.metric("text_extraction_document_pages_total", converter="markitdown") == 0


def test_images_are_not_counted_as_conversions() -> None:
    harness = Harness(None, {DOC: fetched(DOC, PNG, content_type="image/png")}, pool=FakePool())

    harness.run()

    total = "text_extraction_document_conversions_total"
    assert harness.metric(total, converter="markitdown", kind="png", outcome="success") == 0


class _Run:
    """The run state ``DocumentSteps`` needs, for calls without a pipeline."""

    def __init__(self, req: ExtractRequest) -> None:
        self.req = req
        self.host: str | None = "example.org"
        self.convert_ms = 0
        self.warnings: list[str] = []

    def result(self, **fields: Any) -> ExtractResult:
        raise AssertionError("no result expected")

    def failed(
        self, code: ErrorCode, message: str, *, retryable: bool = False
    ) -> Envelope[ExtractResult]:
        from text_extraction.envelope import ErrorInfo

        error = ErrorInfo(code=code, message=message, retryable=retryable)
        return Envelope(status="failed", error=error)


def test_a_shut_down_conversion_pool_while_judging_is_retryable() -> None:
    threads = ConversionPool(1)
    threads.shutdown()
    steps = DocumentSteps(
        cast(DocumentPool, FakePool(reply())),
        threads,
        make_settings(),
        lambda result: Envelope(status="success", data=result),
        CollectorRegistry(),
    )
    report = ComplianceReport(mode="enforce", decision="allowed")
    document = document_input(pdf(), "pdf", report, 1)

    async def extract() -> Any:
        loop = asyncio.get_running_loop()
        return await steps.extract(_Run(ExtractRequest(url=DOC)), document, loop.time() + 10)

    with capture_logs() as logs:
        envelope = asyncio.run(extract())

    assert isinstance(envelope, Envelope)
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.INTERNAL_ERROR
    assert envelope.error.retryable is True
    assert [line["error_type"] for line in logs] == ["RuntimeError"]


# --- a render that ends on a file (F18) ---


BROWSER = {"browser_enabled": True}
SHEET = f"{ORIGIN}/files/sheet.pdf"


def test_render_ending_on_a_document_fetches_it_directly() -> None:
    renderer = FakeRenderer(
        rendered("", url=DOC, responses=[response(DOC, content_type="application/pdf")])
    )
    pool = FakePool(reply())
    harness = Harness(None, {DOC: pdf()}, pool=pool, renderer=renderer, **BROWSER)

    envelope = harness.run(mode="browser")

    assert envelope.status == "success", envelope.error
    data = data_of(envelope)
    assert data.mode_used == "fast"
    assert data.url == DOC
    assert data.final_url == DOC
    assert data.content_type == "application/pdf"
    assert "document_fetched_directly" in data.warnings
    assert data.document is not None
    assert data.attempts == 2  # the render's answer and the fetch
    assert harness.fetcher.calls
    assert harness.fetcher.calls[-1][0] == DOC
    assert data.compliance.decision == "allowed"
    # The fetch took a turn of its own, after the render's was given back.
    takes = [event for event in harness.gate.events if event[0] == "take"]
    assert len(takes) == 2
    assert harness.gate.held() == []
    assert len(pool.calls) == 1
    assert harness.keys("res:*") != []  # the result is final, cached as any document


def test_render_download_url_is_fetched_directly() -> None:
    page = response(PAGE)
    result = replace(rendered("", responses=[page]), download_url=SHEET)
    harness = Harness(
        None,
        {SHEET: fetched(SHEET, corpus("text.pdf"), content_type="application/octet-stream")},
        pool=FakePool(reply()),
        renderer=FakeRenderer(result),
        **BROWSER,
    )

    envelope = harness.run(mode="browser", url=PAGE)

    data = data_of(envelope)
    assert envelope.status == "success", envelope.error
    assert data.url == PAGE
    assert data.final_url == SHEET
    assert data.content_type == "application/pdf"
    assert "document_fetched_directly" in data.warnings
    # Fetched from the document's own URL, so its validators would not fit the page's.
    (entry,) = harness.keys("res:*")
    assert entry


def test_a_download_without_responses_is_fetched_directly() -> None:
    result = replace(rendered("", responses=[]), download_url=SHEET)
    harness = Harness(
        None,
        {SHEET: fetched(SHEET, corpus("text.pdf"), content_type="application/pdf")},
        pool=FakePool(reply()),
        renderer=FakeRenderer(result),
        **BROWSER,
    )

    envelope = harness.run(mode="browser", url=PAGE)

    assert envelope.status == "success", envelope.error
    assert data_of(envelope).final_url == SHEET


@pytest.mark.parametrize(
    "secrets",
    [
        {"cookies": [{"name": "session", "value": "s"}]},
        {"headers": {"X-Token": "t"}},
    ],
)
def test_with_caller_secrets_a_render_ending_on_a_document_stays_unsupported(
    secrets: dict[str, Any],
) -> None:
    renderer = FakeRenderer(
        rendered("", url=DOC, responses=[response(DOC, content_type="application/pdf")])
    )
    harness = Harness(
        None,
        {DOC: pdf()},
        pool=FakePool(AssertionError("not converted")),
        renderer=renderer,
        **BROWSER,
    )

    envelope = harness.run(mode="browser", **secrets)

    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.UNSUPPORTED_CONTENT_TYPE
    assert data_of(envelope).mode_used == "browser"
    assert DOC not in [url for url, _ in harness.fetcher.calls]  # no fetch with or without them


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://10.0.0.7/files/sheet.pdf", ErrorCode.BLOCKED_URL),
        ("http://127.0.0.1:8080/sheet.pdf", ErrorCode.BLOCKED_URL),
        ("blob:https://example.org/0b7d", ErrorCode.UNSUPPORTED_CONTENT_TYPE),
    ],
)
def test_a_direct_fetch_passes_the_ssrf_guard(url: str, code: ErrorCode) -> None:
    result = replace(rendered("", responses=[response(PAGE)]), download_url=url)
    harness = Harness(
        None,
        {},
        pool=FakePool(AssertionError("not converted")),
        renderer=FakeRenderer(result),
        **BROWSER,
    )

    envelope = harness.run(mode="browser", url=PAGE)

    assert envelope.error is not None
    assert envelope.error.code == code
    assert url not in [called for called, _ in harness.fetcher.calls]


def test_a_direct_fetch_that_is_no_document_is_unsupported() -> None:
    result = replace(rendered("", responses=[response(PAGE)]), download_url=SHEET)
    harness = Harness(
        None,
        {SHEET: fetched(SHEET, ARTICLE_HTML, content_type="text/html")},
        pool=FakePool(AssertionError("not converted")),
        renderer=FakeRenderer(result),
        **BROWSER,
    )

    envelope = harness.run(mode="browser", url=PAGE)

    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.UNSUPPORTED_CONTENT_TYPE
    assert "document_fetched_directly" in data_of(envelope).warnings
    assert harness.keys("res:*") == []


def test_a_direct_fetch_is_checked_like_a_fast_one() -> None:
    renderer = FakeRenderer(
        rendered("", url=DOC, responses=[response(DOC, content_type="application/pdf")])
    )
    harness = Harness(
        None,
        {DOC: pdf(headers={"x-robots-tag": "noai"})},
        pool=FakePool(AssertionError("not converted")),
        renderer=renderer,
        **BROWSER,
    )

    envelope = harness.run(mode="browser")

    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.TDM_RESERVED
    assert data_of(envelope).mode_used == "fast"


def test_a_direct_fetch_is_compared_with_the_draft_and_stored_once() -> None:
    renderer = FakeRenderer(
        rendered("", url=DOC, responses=[response(DOC, content_type="application/pdf")])
    )
    harness = Harness(None, {DOC: pdf()}, pool=FakePool(reply()), renderer=renderer, **BROWSER)
    draft = Harness(None, {DOC: fetched(DOC, "<html><body></body></html>")}, pool=FakePool()).run()
    assert draft.status == "failed"

    envelope = harness.run(mode="auto", draft=draft)

    assert envelope.status == "success", envelope.error
    data = data_of(envelope)
    assert data.document is not None
    assert data.attempts == 3  # the draft's fetch, the render's answer and the direct fetch
    assert len(harness.keys("res:*")) == 1
    fingerprint = data.fingerprint
    assert fingerprint is not None
    assert fingerprint.changed is None  # compared once: the first


def test_app_wires_one_lazy_document_pool() -> None:
    from fastapi.testclient import TestClient

    settings = make_settings(document_concurrency=3, document_memory_limit_mb=1024)
    app = create_public_app(settings, FakeAsyncRedis(), CollectorRegistry())
    pool: DocumentPool = app.state.document_pool

    assert app.state.pipeline._documents.pool is pool
    assert pool._processes == 3
    assert pool._memory_limit_mb == 1024
    assert pool._target == MARKITDOWN_TARGET
    with TestClient(app):
        assert pool.child_pids() == []  # no document yet: no child started
    assert pool._closed  # the lifespan shut it down


def test_app_starts_the_document_pool_before_it_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    events: list[str] = []

    async def start(self: DocumentPool) -> None:
        events.append("start")

    async def shutdown(self: DocumentPool) -> None:
        events.append("shutdown")

    monkeypatch.setattr(DocumentPool, "start", start)
    monkeypatch.setattr(DocumentPool, "shutdown", shutdown)
    app = create_public_app(
        make_settings(), FakeAsyncRedis(), CollectorRegistry(), start_documents=True
    )

    with TestClient(app):
        assert events == ["start"]
    assert events == ["start", "shutdown"]


def test_workers_wire_and_start_their_document_pools(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    async def start(self: DocumentPool) -> None:
        events.append("start")

    async def nothing() -> None:
        pass

    monkeypatch.setattr(DocumentPool, "start", start)
    monkeypatch.setattr(worker, "_load_language_model", nothing)
    settings = make_settings()
    parts = worker.build_worker(settings, FakeAsyncRedis(), CollectorRegistry())
    try:
        runner = parts.worker._runner
        assert isinstance(runner, worker.ExtractRunner)
        assert runner._pipeline._documents.pool is parts.document_pool
        asyncio.run(worker.start_up(parts))
        assert events == ["start"]
    finally:
        parts.pool.shutdown()

    browser = browser_worker.build_browser_worker(settings, FakeAsyncRedis(), CollectorRegistry())
    try:
        runner = browser.worker._runner
        assert isinstance(runner, browser_worker.BrowserRunner)
        assert runner._pipeline._documents.pool is browser.document_pool
    finally:
        browser.conversion_pool.shutdown()
