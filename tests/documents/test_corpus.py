"""The document corpus (``tests/documents/corpus/``, built by ``make_corpus.py``) through the
real pipeline: each file is served at a URL of its own by a fake fetcher, sniffed, converted in a
real ``DocumentPool`` child and judged, as ``POST /v1/extract`` would (package 5, done-when).

``corpus/expected.json`` says per file what the answer holds: the served type, ``kind``,
``converter``, the phrases the content must (``phrases``) and must not (``absent``) contain, the
pages, the warnings and the licence, or the ``error`` code of a file that must fail. The
MarkItDown kinds run in the default suite. The kinds only Docling converts (scans, images,
OpenDocument) run under the marker ``docling`` with a real Docling child: ``uv sync --extra
docling``, the models on ``DOCLING_MODELS_PATH``, then ``uv run pytest -m docling``.
"""

import asyncio
import hashlib
import importlib.util
import json
import os
import re
from collections.abc import Coroutine, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis
from prometheus_client import CollectorRegistry

from tests.fakes import FakeFetcher
from text_extraction.auth import Principal
from text_extraction.docling_worker import DoclingConverter, create_docling_pool
from text_extraction.documents.pool import DocumentPool
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractRequest, ExtractResult
from text_extraction.fetch import Fetcher, FetchResult
from text_extraction.pipeline import Pipeline, create_pipeline
from text_extraction.pipeline_documents import MARKITDOWN_TARGET, OcrNeeded
from text_extraction.settings import Settings

CORPUS = Path(__file__).parent / "corpus"
EXPECTED: dict[str, dict[str, Any]] = json.loads(
    (CORPUS / "expected.json").read_text(encoding="utf-8")
)
USER = Principal(name="alice", role="user", rate_limit_per_minute=None)
# Files of the corpus that are not documents to convert.
_NOT_DOCUMENTS = frozenset({"expected.json", "scan.expected.txt"})
_WORD = re.compile(r"\w+")


def _names(*, converter: str | None = None, failing: bool = False) -> list[str]:
    return [
        name
        for name, spec in EXPECTED.items()
        if ("error" in spec) == failing and (converter is None or spec["converter"] == converter)
    ]


def url_of(name: str) -> str:
    return f"https://example.org/files/{name}"


def served(name: str) -> FetchResult:
    """The file as a server would send it: its media type and no charset."""
    body = (CORPUS / name).read_bytes()
    url = url_of(name)
    return FetchResult(
        url=url,
        final_url=url,
        status=200,
        headers={},
        content=body,
        content_type=EXPECTED[name]["content_type"],
        charset=None,
        elapsed_ms=3,
        redirects=[],
        not_modified=False,
        raw_sha256=hashlib.sha256(body).hexdigest(),
    )


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "auth_mode": "none",
        "outbound_min_delay_ms": 0,
        "quality_min_words": 0,
    }
    values.update(overrides)
    return Settings(**values)


class Loop:
    """An event loop for the module, with one started pool of MarkItDown children; the Docling
    children start with the first ``docling`` test."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.pool = DocumentPool(1, 2048, target=MARKITDOWN_TARGET)
        self.docling: DocumentPool | None = None
        self.run(self.pool.start())

    def run[T](self, work: Coroutine[Any, Any, T]) -> T:
        return self.loop.run_until_complete(work)

    def close(self) -> None:
        for pool in (self.pool, self.docling):
            if pool is not None:
                self.run(pool.shutdown())
        self.loop.close()


@pytest.fixture(scope="module")
def loop() -> Iterator[Loop]:
    instance = Loop()
    try:
        yield instance
    finally:
        instance.close()


def pipeline_for(loop: Loop, settings: Settings, registry: CollectorRegistry) -> Pipeline:
    fetcher = FakeFetcher({url_of(name): served(name) for name in EXPECTED})
    return create_pipeline(
        settings, FakeAsyncRedis(), cast(Fetcher, fetcher), registry, document_pool=loop.pool
    )


def extract(loop: Loop, name: str, **request: Any) -> Envelope[ExtractResult]:
    pipeline = pipeline_for(loop, make_settings(), CollectorRegistry())
    return loop.run(pipeline.run(ExtractRequest(url=url_of(name), **request), USER))


def check(envelope: Envelope[ExtractResult], name: str) -> None:
    """The answer of ``name`` holds what ``expected.json`` says of it."""
    spec = EXPECTED[name]
    assert envelope.status == "success", (name, envelope.error)
    data = envelope.data
    assert data is not None
    assert data.mode_used == "fast"
    assert data.content_type == spec["content_type"]
    document = data.document
    assert document is not None
    assert document.kind == spec["kind"]
    assert document.converter == spec["converter"]
    assert document.ocr is spec["ocr"]
    if "pages" in spec:
        assert (document.pages, document.pages_converted) == (
            spec["pages"],
            spec["pages_converted"],
        )
    assert data.warnings == spec.get("warnings", [])
    content = data.content or ""
    for phrase in spec["phrases"]:
        assert phrase in content, (name, phrase)
    for phrase in spec.get("absent", []):
        assert phrase not in content, (name, phrase)
    if "license" in spec:
        assert data.metadata is not None
        assert data.metadata.license is not None
        assert data.metadata.license.id == spec["license"]["id"]
        assert data.metadata.license.source == spec["license"]["source"]


def test_expected_covers_the_corpus() -> None:
    documents = {path.name for path in CORPUS.iterdir()} - _NOT_DOCUMENTS
    assert documents == set(EXPECTED)


@pytest.mark.parametrize("name", _names(converter="markitdown"))
def test_corpus_converts(loop: Loop, name: str) -> None:
    check(extract(loop, name), name)


@pytest.mark.parametrize("name", _names(failing=True))
def test_corpus_refuses(loop: Loop, name: str) -> None:
    """A container bomb is ``too_large``, a password-protected PDF ``conversion_failed`` and a PDF
    without pages ``empty_content``: none of them retryable."""
    envelope = extract(loop, name)

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == EXPECTED[name]["error"]
    assert envelope.error.retryable is False


# --- OCR and OpenDocument: a real Docling child ------------------------------------------------


def words(text: str) -> list[str]:
    return [word.casefold() for word in _WORD.findall(text)]


def recall(expected: str, content: str) -> float:
    wanted = words(expected)
    found = set(words(content))
    return sum(1 for word in wanted if word in found) / len(wanted)


@pytest.fixture(scope="module")
def docling(loop: Loop) -> Settings:
    """The settings of a service with OCR; the module's Docling children started."""
    if importlib.util.find_spec("docling") is None:
        pytest.skip("Docling is not installed (uv sync --extra docling)")
    settings = make_settings(
        docling_enabled=True,
        docling_concurrency=1,
        docling_models_path=Path(os.environ.get("DOCLING_MODELS_PATH", "/models")),
    )
    if loop.docling is None:
        loop.docling = create_docling_pool(settings)
        loop.run(loop.docling.start())
    return settings


def extract_with_ocr(loop: Loop, settings: Settings, name: str) -> Envelope[ExtractResult]:
    """``name`` through the pipeline, handed to the Docling conversion as the Docling worker
    converts a queued document (``Pipeline.convert_queued_document``)."""
    assert loop.docling is not None
    registry = CollectorRegistry()
    pipeline = pipeline_for(loop, settings, registry)
    converter = DoclingConverter(loop.docling, settings, registry)
    req = ExtractRequest(url=url_of(name))
    handed: list[OcrNeeded] = []

    async def handoff(
        needed: OcrNeeded, _fallback: Envelope[ExtractResult]
    ) -> Envelope[ExtractResult]:
        handed.append(needed)
        return await pipeline.convert_queued_document(
            req, USER, needed.input, needed.draft, converter
        )

    envelope = loop.run(pipeline.run(req, USER, ocr_handoff=handoff))
    assert len(handed) == 1, "the document was not handed to OCR"
    return envelope


@pytest.mark.docling
@pytest.mark.parametrize("name", _names(converter="docling"))
def test_corpus_converts_with_docling(loop: Loop, docling: Settings, name: str) -> None:
    check(extract_with_ocr(loop, docling, name), name)


@pytest.mark.docling
def test_scan_yields_text(loop: Loop, docling: Settings) -> None:
    envelope = extract_with_ocr(loop, docling, "scan.pdf")

    assert envelope.data is not None
    spec = EXPECTED["scan.pdf"]["recall"]
    expected = (CORPUS / spec["of"]).read_text(encoding="utf-8")
    assert recall(expected, envelope.data.content or "") >= spec["min"]
