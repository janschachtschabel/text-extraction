"""Which converter a document goes to (ruling F11): the one rule, for every caller.

MarkItDown converts PDFs with a text layer, Office files of the OOXML and old Excel formats,
EPUB and CSV, in the pod that holds the bytes. Docling (OCR, layout, tables) converts scans,
broken text layers, images and OpenDocument files, in the Docling worker, and only while that
worker exists: ``DOCLING_ENABLED``, and ``JOBS_ENABLED`` for the queue it is reached through.

The request's ``ocr`` option: ``auto`` sends what needs it to Docling, ``never`` nothing (images
and OpenDocument files are then unsupported), ``always`` also every PDF.

Light: no converter imports, so the parent processes may use it.
"""

from typing import Final

from text_extraction.documents.types import DocumentConversion, DocumentKind, OcrOption
from text_extraction.settings import Settings

MARKITDOWN_KINDS: Final[frozenset[DocumentKind]] = frozenset(
    {"pdf", "docx", "pptx", "xlsx", "xls", "epub", "csv"}
)
DOCLING_KINDS: Final[frozenset[DocumentKind]] = frozenset(
    {"odt", "ods", "odp", "png", "jpeg", "tiff", "webp", "bmp"}
)
OCR_DISABLED_MESSAGE: Final = "OCR is not enabled on this service."


def ocr_available(settings: Settings) -> bool:
    """Whether this service has the Docling worker and the job queue that reaches it."""
    return settings.docling_enabled and settings.jobs_enabled


def wants_ocr(kind: DocumentKind, conversion: DocumentConversion | None, ocr: OcrOption) -> bool:
    """Whether the request wants this document read by Docling, whatever the service has.

    ``conversion`` is MarkItDown's result of a PDF (None when there is none): its probe verdict
    (``needs_ocr``: a scan or a broken text layer, F4) decides under ``auto``."""
    if ocr == "never":
        return False
    if kind in DOCLING_KINDS:
        return True
    if kind != "pdf":
        return False
    return ocr == "always" or (conversion is not None and conversion.needs_ocr)


def needs_ocr(
    kind: DocumentKind, conversion: DocumentConversion | None, ocr: OcrOption, settings: Settings
) -> bool:
    """Whether the document goes to Docling: the request wants it, and the service has it."""
    return ocr_available(settings) and wants_ocr(kind, conversion, ocr)
