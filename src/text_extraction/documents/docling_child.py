"""The ``DocumentPool`` target of the Docling worker's children: ``DocumentPool(target=
"text_extraction.documents.docling_child")``.

The child imports this module (and with it Docling, torch and the models' code) once, after its
memory limit (``DOCLING_MEMORY_LIMIT_MB``) is set; the parent never imports it. Only this target
imports Docling, so the MarkItDown children (``documents/child.py``) and every parent process
stay without it. ``FUNCTIONS`` is the allowlist of what a parent may call by name.

Before anything else is imported, logging is silenced (``child_logging``): what Docling,
Tesseract's wrapper or our own ``meta.py`` log or warn about may quote the document.
"""

from collections.abc import Callable
from typing import Any

from text_extraction.documents.child_logging import silence_logging

silence_logging()

from text_extraction.documents import docling_convert  # noqa: E402 - after the silencing

FUNCTIONS: dict[str, Callable[[Any], Any]] = {
    "docling": docling_convert.convert,
}
