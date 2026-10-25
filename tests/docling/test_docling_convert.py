"""Real Docling conversions in a Docling child (``documents/docling_convert.py``): Tesseract and
the models on ``DOCLING_MODELS_PATH``, filled by ``text-extraction-docling-worker
--download-models``. Marker ``docling``: ``uv sync --extra docling``, then ``uv run pytest -m
docling`` (CI job ``documents``). Without Docling installed they are skipped; with Docling but
without the models they fail, as the worker's start would.
"""

import asyncio
import importlib.util
import os
import re
from collections.abc import Coroutine, Iterator
from pathlib import Path
from typing import Any

import pytest

from text_extraction.docling_worker import create_docling_pool, docling_payload
from text_extraction.documents.pool import DocumentPool
from text_extraction.documents.sniff import media_type
from text_extraction.documents.types import DocumentConversion, DocumentKind
from text_extraction.pipeline_documents import DocumentInput, DocumentInputMeta
from text_extraction.settings import Settings

pytestmark = pytest.mark.docling

CORPUS = Path(__file__).parents[1] / "documents" / "corpus"
_WORD = re.compile(r"\w+")


class Child:
    """One Docling child for the module, on an event loop of its own."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.loop = asyncio.new_event_loop()
        self.pool: DocumentPool = create_docling_pool(settings)
        self.run_on_loop(self.pool.start())

    def run_on_loop[T](self, work: Coroutine[Any, Any, T]) -> T:
        return self.loop.run_until_complete(work)

    def convert(
        self, name: str, kind: DocumentKind, fmt: str = "markdown", **payload: Any
    ) -> DocumentConversion:
        meta = DocumentInputMeta(
            kind=kind, content_type=media_type(kind), raw_sha256="0" * 64, needs_ocr=True
        )
        values = docling_payload(
            DocumentInput((CORPUS / name).read_bytes(), meta), fmt, self.settings
        )
        values.update(payload)

        async def call() -> Any:
            deadline = asyncio.get_running_loop().time() + self.settings.docling_timeout_seconds
            return await self.pool.run("docling", values, deadline)

        return DocumentConversion.from_reply(self.run_on_loop(call()))

    def close(self) -> None:
        self.run_on_loop(self.pool.shutdown())
        self.loop.close()


@pytest.fixture(scope="module")
def child() -> Iterator[Child]:
    if importlib.util.find_spec("docling") is None:
        pytest.skip("Docling is not installed (uv sync --extra docling)")
    settings = Settings(
        auth_mode="none",
        docling_concurrency=1,
        docling_models_path=Path(os.environ.get("DOCLING_MODELS_PATH", "/models")),
    )
    instance = Child(settings)
    try:
        yield instance
    finally:
        instance.close()


def words(text: str) -> list[str]:
    return [word.casefold() for word in _WORD.findall(text)]


def recall(expected: str, content: str) -> float:
    wanted = words(expected)
    found = set(words(content))
    return sum(1 for word in wanted if word in found) / len(wanted)


def test_scan_pdf_german_ocr(child: Child) -> None:
    conversion = child.convert("scan.pdf", "pdf")

    expected = (CORPUS / "scan.expected.txt").read_text(encoding="utf-8")
    assert recall(expected, conversion.content) >= 0.9
    for word in ("Blätter", "läuft", "Größere", "Stärke", "Übung", "grünen"):
        assert word in conversion.content
    assert (conversion.pages, conversion.pages_converted) == (2, 2)
    assert conversion.warnings == []


@pytest.mark.parametrize(("name", "kind"), [("page.png", "png"), ("photo.jpeg", "jpeg")])
def test_png_and_jpeg_ocr(child: Child, name: str, kind: DocumentKind) -> None:
    conversion = child.convert(name, kind, "text")

    assert "Photosynthese" in conversion.content or "Chlorophyll" in conversion.content
    assert recall("Pflanzen Lichtenergie Blätter Sonnenlicht", conversion.content) >= 0.25
    assert conversion.pages is None


def test_odt_converts(child: Child) -> None:
    conversion = child.convert("text.odt", "odt", metadata=True)

    assert "Die Photosynthese" in conversion.content
    assert "Größere Pflanzen speichern" in conversion.content
    assert conversion.title == "Die Photosynthese"
    assert conversion.metadata is not None
    assert conversion.metadata["language"] == "de"


def test_text_pdf_always_mode_keeps_table(child: Child) -> None:
    """``ocr: always`` sends a PDF with a text layer to Docling for its layout and tables."""
    conversion = child.convert("text.pdf", "pdf", full_page_ocr=False)

    header = re.compile(r"^\|\s*Stoff\s*\|\s*Menge\s*\|\s*Aufgabe\s*\|", re.MULTILINE)
    assert header.search(conversion.content), conversion.content


def test_page_cap_warning(child: Child) -> None:
    conversion = child.convert("scan.pdf", "pdf", max_pages=1)

    assert (conversion.pages, conversion.pages_converted) == (2, 1)
    assert conversion.warnings == ["pages_truncated"]
    assert "Photosynthese" in conversion.content
    assert "Chlorophyll" not in conversion.content  # on page 2


def test_a_partial_conversion_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """Docling stopped at its own ``document_timeout`` (or skipped a page): what it read is
    answered with the warning ``pages_partial``. In this process, with Docling's converter
    replaced: the status is what is under test, not the models."""
    docling_convert = pytest.importorskip("text_extraction.documents.docling_convert")
    from docling.datamodel.base_models import ConversionStatus

    class Document:
        def export_to_markdown(self, image_placeholder: str) -> str:
            return "# Die Photosynthese\n\nErste Seite."

        def export_to_text(self) -> str:
            return "Die Photosynthese\n\nErste Seite."

    class Result:
        status = ConversionStatus.PARTIAL_SUCCESS
        document = Document()

    class Converter:
        def convert(self, *_args: Any, **_kwargs: Any) -> Result:
            return Result()

    monkeypatch.setattr(docling_convert, "_converter", lambda **_options: Converter())
    meta = DocumentInputMeta(kind="odt", content_type=media_type("odt"), raw_sha256="0" * 64)
    payload = docling_payload(
        DocumentInput((CORPUS / "text.odt").read_bytes(), meta),
        "markdown",
        Settings(auth_mode="none"),
    )

    reply = docling_convert.convert(payload)

    assert reply["warnings"] == ["pages_partial"]
    assert "Erste Seite." in reply["content"]
