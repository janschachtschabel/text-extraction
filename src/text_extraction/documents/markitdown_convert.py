"""MarkItDown conversion of PDF, Office, EPUB and CSV documents, in a ``DocumentPool`` child.

Child side only: importing this module imports MarkItDown, pdfminer, pdfplumber, pandas and
magika, which the parent never does (see ``documents/child.py``). One call converts one
document:

1. A ZIP-based kind passes ``check_container`` (ruling F5): a bomb is ``DocumentTooLarge``, a
   broken archive ``DocumentUnreadable``.
2. A PDF is probed with pdfium (ruling F4): one that needs a password is ``PasswordProtected``,
   one pdfium cannot open ``PdfUnreadable``. A PDF without pages is empty content, with no
   conversion. Pages after ``max_pages`` are cut before conversion (``pages_truncated``).
3. MarkItDown's converter class for the kind converts it, called directly: no plugin discovery,
   no magika guess, no LLM, nothing fetched. Whatever the converter raises (other than
   ``MemoryError``, which the pool reports as out of memory) is ``DocumentUnreadable``, with a
   message of our own: a converter's message may quote the document.
4. A PDF ``needs_ocr`` when the probe calls it a scan or the text is broken (``(cid:N)``).

The reply is a dict of builtins (the pool unpickles nothing else); the parent reads it with
``DocumentConversion.from_reply``.
"""

import io
import re
from typing import Any

from markitdown import DocumentConverter, StreamInfo
from markitdown.converters import (
    CsvConverter,
    DocxConverter,
    EpubConverter,
    PdfConverter,
    PptxConverter,
    XlsConverter,
    XlsxConverter,
)

from text_extraction.convert import OutputFormat
from text_extraction.convert.decode import decode_html
from text_extraction.documents.formats import from_markdown
from text_extraction.documents.meta import document_metadata
from text_extraction.documents.pdf import broken_text, probe_pdf, scan_verdict, truncate_pdf
from text_extraction.documents.sniff import DocumentUnreadable, check_container, media_type
from text_extraction.documents.types import DocumentKind

_CONVERTERS: dict[str, tuple[type[DocumentConverter], str]] = {
    "pdf": (PdfConverter, ".pdf"),
    "docx": (DocxConverter, ".docx"),
    "pptx": (PptxConverter, ".pptx"),
    "xlsx": (XlsxConverter, ".xlsx"),
    "xls": (XlsConverter, ".xls"),
    "epub": (EpubConverter, ".epub"),
    "csv": (CsvConverter, ".csv"),
}
_FORMATS = frozenset({"markdown", "text", "html"})
# MarkItDown's slide markers in a presentation: an HTML comment, which the HTML output would
# show as escaped text. The slide titles stay as headings.
_SLIDE_MARKER = re.compile(r"^<!-- Slide number: \d+ -->\n?", re.MULTILINE)
_HEADING = re.compile(r"^#{1,2} +(.+?) *#* *$", re.MULTILINE)


def convert(payload: dict[str, Any]) -> dict[str, Any]:
    """The ``"markitdown"`` function of the child. ``payload``: ``body`` (bytes), ``kind``,
    ``format``, ``metadata`` (bool: put the document's metadata in the reply), ``base`` (its
    URL, None for an upload), ``content_language`` (the header), ``max_pages``,
    ``max_uncompressed``."""
    body: bytes = payload["body"]
    kind: DocumentKind = payload["kind"]
    fmt: OutputFormat = payload["format"]
    if kind not in _CONVERTERS:
        raise ValueError(f"no MarkItDown converter for kind {kind!r}")
    if fmt not in _FORMATS:
        raise ValueError(f"unknown output format {fmt!r}")
    check_container(body, kind, payload["max_uncompressed"])
    warnings: list[str] = []
    pages: int | None = None
    pages_converted: int | None = None
    needs_ocr = False
    source = body
    if kind == "pdf":
        probe = probe_pdf(body)
        pages = pages_converted = probe.pages
        needs_ocr = scan_verdict(probe)
        max_pages: int = payload["max_pages"]
        if probe.pages > max_pages:
            source = truncate_pdf(body, max_pages)
            pages_converted = max_pages
            warnings.append("pages_truncated")
    if kind == "pdf" and pages == 0:
        markdown, converter_title = "", None  # nothing to convert (ruling: empty content)
    else:
        markdown, converter_title = _markitdown(source, kind)
    if kind == "pdf" and not needs_ocr:
        needs_ocr = broken_text(markdown)
    normalised = from_markdown(markdown, "markdown")
    metadata = document_metadata(
        body, kind, payload["base"], payload["content_language"], normalised
    )
    return {
        "content": normalised if fmt == "markdown" else from_markdown(normalised, fmt),
        "title": metadata.title or converter_title or _first_heading(normalised),
        "warnings": warnings,
        "pages": pages,
        "pages_converted": pages_converted,
        "needs_ocr": needs_ocr,
        "metadata": metadata.model_dump(mode="json") if payload["metadata"] else None,
    }


def _markitdown(body: bytes, kind: DocumentKind) -> tuple[str, str | None]:
    """MarkItDown's Markdown and title for the document."""
    converter_class, extension = _CONVERTERS[kind]
    charset = None
    if kind == "csv":
        # Our own decoding (the one of fetched pages): MarkItDown's charset guess reads short
        # Latin-1 text with umlauts as some other code page.
        body = decode_html(body, None).encode("utf-8")
        charset = "utf-8"
    info = StreamInfo(mimetype=media_type(kind), extension=extension, charset=charset)
    try:
        result = converter_class().convert(io.BytesIO(body), info)
    except MemoryError:
        raise
    except Exception as exc:  # a parser boundary on hostile input
        raise DocumentUnreadable(f"The {kind} document could not be converted.") from exc
    markdown = result.markdown or ""
    if kind == "pptx":
        markdown = _SLIDE_MARKER.sub("", markdown)
    title = result.title.strip() if isinstance(result.title, str) else None
    return markdown, title or None


def _first_heading(markdown: str) -> str | None:
    match = _HEADING.search(markdown)
    return match.group(1) if match else None
