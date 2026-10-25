"""Types shared by the document code, the request models and the result models.

Kept free of imports from the rest of the package, so that the models can use them.
"""

from typing import Literal

from pydantic import BaseModel

DocumentKind = Literal[
    "pdf",
    "docx",
    "pptx",
    "xlsx",
    "xls",
    "epub",
    "csv",
    "odt",
    "ods",
    "odp",
    "png",
    "jpeg",
    "tiff",
    "webp",
    "bmp",
]

# The `ocr` option of a request: `auto` reads scans, broken text layers and images with OCR,
# `never` does not, `always` also reads every PDF with OCR.
OcrOption = Literal["auto", "never", "always"]


class DocumentInfo(BaseModel):
    """What a result says about the document it was extracted from."""

    kind: DocumentKind
    converter: Literal["markitdown", "docling"]
    pages: int | None  # in the document; None for kinds without pages
    pages_converted: int | None  # fewer than ``pages`` when the page limit cut it
    ocr: bool  # the text comes from OCR
    file_name: str | None = None  # uploads only
