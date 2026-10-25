"""The ``DocumentPool`` target of MarkItDown children: ``DocumentPool(target=
"text_extraction.documents.child")``.

The child imports this module (and with it MarkItDown, pdfminer, pdfplumber, pandas and magika)
once, after its memory limit is set; the parent never imports it. ``FUNCTIONS`` is the
allowlist of what a parent may call by name. Docling has a target of its own, so that its
models and torch stay out of these children.
"""

from collections.abc import Callable
from typing import Any

from text_extraction.documents import markitdown_convert

FUNCTIONS: dict[str, Callable[[Any], Any]] = {
    "markitdown": markitdown_convert.convert,
}
