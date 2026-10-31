"""The UI's warning texts follow the warnings that docs/architecture.md lists."""

import re
from pathlib import Path

from text_extraction.ui.texts import WARNING_TEXTS, warning_text

ARCHITECTURE = Path(__file__).resolve().parents[2] / "docs" / "architecture.md"
# Backticked snake_case names of the warnings block that are request or tool fields, not warnings.
NOT_WARNINGS = {"content_offset", "describe_images", "learn_selectors", "max_chars", "max_pages"}


def documented_warnings() -> set[str]:
    """The backticked snake_case names of the paragraph on ``warnings`` in architecture.md."""
    text = ARCHITECTURE.read_text(encoding="utf-8")
    start = text.index("`warnings` names what did not go")
    end = text.index("A licence in `metadata`", start)
    names = set(re.findall(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`", text[start:end]))
    return names - NOT_WARNINGS


def test_every_documented_warning_has_a_text() -> None:
    assert documented_warnings() == set(WARNING_TEXTS)


def test_mcp_content_truncated_has_a_text() -> None:
    assert "mcp_content_truncated" in documented_warnings()
    text = warning_text("mcp_content_truncated")
    assert text is not None
    assert "content_offset" in text
