"""Small DOM helpers shared by the converter modules."""

import re

from bs4 import Tag

HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)


def attr(tag: Tag, name: str) -> str:
    """An attribute as one string; empty when absent (a multi-valued one is joined by spaces)."""
    value = tag.get(name)
    if value is None:
        return ""
    return value if isinstance(value, str) else " ".join(value)
