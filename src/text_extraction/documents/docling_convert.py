"""Docling conversion of scans, images and OpenDocument files, in a ``DocumentPool`` child of the
Docling worker (rulings F3, F6, F14).

Child side only: importing this module imports Docling, and with it torch and the layout and
table models' code, which the parent never does (see ``documents/docling_child.py``). One call
converts one document:

1. An OpenDocument file passes ``check_container`` (F5): a bomb is ``DocumentTooLarge``. An
   image with a frame over ``MAX_IMAGE_PIXELS`` (in the first ``max_pages`` frames) is
   ``DocumentTooLarge`` before it is decoded; one Pillow cannot read is ``DocumentUnreadable``
   (``documents/images.check_image``).
2. A PDF is probed with pdfium, as for MarkItDown (F4): a password is ``PasswordProtected``, a
   PDF without pages is empty content without a conversion, pages after ``max_pages`` are cut
   (``pages_truncated``). Other paged kinds (a multi-page TIFF) convert their first
   ``max_pages`` pages.
3. Docling converts it: PDFs and images through its PDF pipeline, with Tesseract's command line
   for OCR (``languages``, the data in ``<models_path>/tessdata``), full-page OCR when the probe
   said "scan" (``full_page_ocr``; always for an image), the layout model and TableFormer in
   fast mode from ``<models_path>/docling`` (``artifacts_path``), on the CPU with ``threads``
   threads, and ``timeout`` as Docling's ``document_timeout`` (0.9 of DOCLING_TIMEOUT_SECONDS,
   so it stops between pages before the parent kills the child; what it read then is a
   ``PARTIAL_SUCCESS``, answered with the warning ``pages_partial``, as is a page Docling could
   not read); OpenDocument files through its simple pipeline (no OCR, no models). Nothing is
   fetched: the parent starts the child with ``HF_HUB_OFFLINE``. Models that are not on the
   volume are ``ModelsMissing``; any other failure of Docling is ``DocumentUnreadable``, with a
   message of our own (Docling's may quote the document).
4. The output (F6): ``markdown`` is Docling's ``export_to_markdown`` (whitespace normalised as
   for MarkItDown), ``text`` its ``export_to_text``. ``html`` renders that Markdown with
   markdown-it-py (``formats.from_markdown``), as for MarkItDown, instead of Docling's
   ``export_to_html``: that page carries a stylesheet whose words the quality gate would count
   (and ``escalation.better`` compare), and our renderer escapes raw HTML of a hostile document.

One converter is built per set of options and kept for the child's life, so the models load
once (the start-up's self-check loads them for images); the pool replaces the child after 50
conversions or a failure.

Docling's API as used here, checked against ``docling-slim==2.137.0`` (``docling-core`` 2.x):
``docling.document_converter.DocumentConverter(allowed_formats, format_options)``,
``PdfFormatOption``, ``ImageFormatOption``, ``OdtFormatOption``, ``OdsFormatOption``,
``OdpFormatOption``; ``DocumentConverter.convert(DocumentStream, raises_on_error, page_range)``
returning a ``ConversionResult`` (``status``: ``ConversionStatus``, ``document``:
``DoclingDocument``); ``docling.datamodel.pipeline_options.PdfPipelineOptions`` (``do_ocr``,
``ocr_options``, ``do_table_structure``, ``table_structure_options``, ``artifacts_path``,
``accelerator_options``, ``document_timeout``), ``TesseractCliOcrOptions(lang, path, mode)``,
``OcrMode.FULL_PAGE``, ``TableStructureOptions(mode=TableFormerMode.FAST)``;
``docling.datamodel.accelerator_options.AcceleratorOptions(num_threads, device=
AcceleratorDevice.CPU)``. A missing model raises ``FileNotFoundError`` when the pipeline is
built. Docling's own downloader (``docling.utils.model_downloader.download_models``, Hugging
Face snapshots) is not used: ``documents/models.toml`` pins each file with its SHA-256.

The reply is a dict of builtins (the pool unpickles nothing else); the parent reads it with
``DocumentConversion.from_reply``.
"""

import io
from functools import cache
from pathlib import Path
from typing import Any, Final

from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
from docling.datamodel.base_models import ConversionStatus, InputFormat
from docling.datamodel.pipeline_options import (
    OcrMode,
    PdfPipelineOptions,
    TableFormerMode,
    TableStructureOptions,
    TesseractCliOcrOptions,
)
from docling.document_converter import (
    DocumentConverter,
    FormatOption,
    ImageFormatOption,
    OdpFormatOption,
    OdsFormatOption,
    OdtFormatOption,
    PdfFormatOption,
)
from docling_core.types.io import DocumentStream

from text_extraction.convert import OutputFormat
from text_extraction.documents.formats import first_heading, from_markdown
from text_extraction.documents.images import check_image
from text_extraction.documents.meta import document_metadata
from text_extraction.documents.pdf import probe_pdf, truncate_pdf
from text_extraction.documents.sniff import DocumentUnreadable, check_container
from text_extraction.documents.types import DocumentKind

# The file name extension Docling recognises each kind by.
_EXTENSIONS: Final[dict[str, str]] = {
    "pdf": ".pdf",
    "png": ".png",
    "jpeg": ".jpg",
    "tiff": ".tiff",
    "webp": ".webp",
    "bmp": ".bmp",
    "odt": ".odt",
    "ods": ".ods",
    "odp": ".odp",
}
_IMAGE_KINDS: Final = frozenset({"png", "jpeg", "tiff", "webp", "bmp"})
_OUTPUTS: Final = frozenset({"markdown", "text", "html"})
_OK: Final = frozenset({ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS})
# Docling stopped early (its ``document_timeout``) or skipped pages it could not read.
PAGES_PARTIAL_WARNING: Final = "pages_partial"


class ModelsMissing(Exception):  # noqa: N818 - a state of the volume, named as such
    """A model Docling needs is not under ``<models_path>/docling``."""


def convert(payload: dict[str, Any]) -> dict[str, Any]:
    """The ``"docling"`` function of the child. ``payload``: ``body`` (bytes), ``kind``,
    ``format``, ``full_page_ocr`` (the probe said "scan"), ``languages`` (Tesseract codes),
    ``threads``, ``max_pages``, ``models_path``, ``timeout`` (seconds), ``metadata`` (bool: put
    the document's metadata in the reply), ``base`` (its URL, None for an upload),
    ``content_language`` (the header) and ``max_uncompressed``."""
    body: bytes = payload["body"]
    kind: DocumentKind = payload["kind"]
    fmt: OutputFormat = payload["format"]
    if kind not in _EXTENSIONS:
        raise ValueError(f"no Docling format for kind {kind!r}")
    if fmt not in _OUTPUTS:
        raise ValueError(f"unknown output format {fmt!r}")
    max_pages: int = payload["max_pages"]
    check_container(body, kind, payload["max_uncompressed"])
    warnings: list[str] = []
    pages: int | None = None
    pages_converted: int | None = None
    source = body
    if kind == "pdf":
        pages = pages_converted = probe_pdf(body).pages
        if pages > max_pages:
            source = truncate_pdf(body, max_pages)
            pages_converted = max_pages
            warnings.append("pages_truncated")
    elif kind in _IMAGE_KINDS:
        check_image(body, max_frames=max_pages)
    if pages == 0:
        markdown, text = "", ""  # nothing to convert (as for MarkItDown: empty content)
    else:
        markdown, text, partial = _docling(source, kind, payload)
        if partial:
            warnings.append(PAGES_PARTIAL_WARNING)
    normalised = from_markdown(markdown, "markdown")
    content = {
        "markdown": normalised,
        "text": from_markdown(text, "markdown") if normalised else "",
        "html": from_markdown(normalised, "html"),
    }[fmt]
    metadata = document_metadata(
        body, kind, payload["base"], payload["content_language"], normalised
    )
    return {
        "content": content,
        "title": metadata.title or first_heading(normalised),
        "warnings": warnings,
        "pages": pages,
        "pages_converted": pages_converted,
        "needs_ocr": False,
        "metadata": metadata.model_dump(mode="json") if payload["metadata"] else None,
    }


def _docling(body: bytes, kind: DocumentKind, payload: dict[str, Any]) -> tuple[str, str, bool]:
    """Docling's Markdown and text of the document, and whether it read only a part of it."""
    extension = _EXTENSIONS[kind]
    converter = _converter(
        full_page=bool(payload["full_page_ocr"]) or kind in _IMAGE_KINDS,
        languages=tuple(payload["languages"]),
        threads=int(payload["threads"]),
        models_path=str(payload["models_path"]),
        timeout=float(payload["timeout"]),
    )
    stream = DocumentStream(name=f"document{extension}", stream=io.BytesIO(body))
    try:
        result = converter.convert(
            stream, raises_on_error=False, page_range=(1, int(payload["max_pages"]))
        )
    except MemoryError:
        raise
    except FileNotFoundError as exc:  # a model is not on the volume (raised by the pipeline)
        raise ModelsMissing("A Docling model is missing on the models volume.") from exc
    except Exception as exc:  # a parser boundary on hostile input
        raise DocumentUnreadable(f"The {kind} document could not be converted.") from exc
    if result.status not in _OK:
        raise DocumentUnreadable(f"The {kind} document could not be converted.")
    document = result.document
    markdown: str = document.export_to_markdown(image_placeholder="")
    text: str = document.export_to_text()
    return markdown, text, result.status == ConversionStatus.PARTIAL_SUCCESS


@cache
def _converter(
    *, full_page: bool, languages: tuple[str, ...], threads: int, models_path: str, timeout: float
) -> DocumentConverter:
    """The converter for one set of options; built once, so its models load once."""
    models = Path(models_path)
    pdf_options = PdfPipelineOptions(
        artifacts_path=models / "docling",
        do_ocr=True,
        ocr_options=TesseractCliOcrOptions(
            lang=list(languages),
            path=str(models / "tessdata"),
            mode=OcrMode.FULL_PAGE if full_page else OcrMode.DEFAULT,
        ),
        do_table_structure=True,
        table_structure_options=TableStructureOptions(mode=TableFormerMode.FAST),
        accelerator_options=AcceleratorOptions(num_threads=threads, device=AcceleratorDevice.CPU),
        document_timeout=timeout,
    )
    format_options: dict[InputFormat, FormatOption] = {
        InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options),
        InputFormat.IMAGE: ImageFormatOption(pipeline_options=pdf_options),
        InputFormat.ODT: OdtFormatOption(),
        InputFormat.ODS: OdsFormatOption(),
        InputFormat.ODP: OdpFormatOption(),
    }
    return DocumentConverter(allowed_formats=list(format_options), format_options=format_options)
