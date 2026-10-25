"""Types shared by the document code, the request models and the result models.

Kept free of imports from the rest of the package, so that the models can use them.
"""

from dataclasses import dataclass
from typing import Any, Literal

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


_REPLY_FIELDS = frozenset(
    {"content", "title", "warnings", "pages", "pages_converted", "needs_ocr", "metadata"}
)


def _is_count(value: object) -> bool:
    return value is None or (type(value) is int and value >= 0)


@dataclass(frozen=True, slots=True)
class DocumentConversion:
    """What a ``DocumentPool`` child's ``"markitdown"`` function converted, on the parent side.

    The child answers with a plain dict (the pool unpickles builtins only); ``from_reply``
    checks its shape before anything trusts it."""

    content: str  # in the requested format; "" for a document without text
    title: str | None
    warnings: list[str]  # e.g. "pages_truncated"
    pages: int | None  # in the document; None for kinds without pages
    pages_converted: int | None  # fewer than ``pages`` when the page limit cut it
    needs_ocr: bool  # a PDF without a usable text layer (scan or broken text, ruling F4)
    metadata: dict[str, Any] | None  # ``PageMetadata`` as JSON, when the call asked for it

    @classmethod
    def from_reply(cls, reply: object) -> "DocumentConversion":
        """The conversion from the child's reply; ``ValueError`` when it is not one."""
        if not isinstance(reply, dict) or set(reply) != _REPLY_FIELDS:
            raise ValueError("malformed conversion reply")
        content, title, warnings = reply["content"], reply["title"], reply["warnings"]
        metadata = reply["metadata"]
        if (
            type(content) is not str
            or not (title is None or type(title) is str)
            or type(warnings) is not list
            or not all(type(warning) is str for warning in warnings)
            or not _is_count(reply["pages"])
            or not _is_count(reply["pages_converted"])
            or type(reply["needs_ocr"]) is not bool
            or not (metadata is None or type(metadata) is dict)
        ):
            raise ValueError("malformed conversion reply")
        return cls(
            content=content,
            title=title,
            warnings=warnings,
            pages=reply["pages"],
            pages_converted=reply["pages_converted"],
            needs_ocr=reply["needs_ocr"],
            metadata=metadata,
        )
