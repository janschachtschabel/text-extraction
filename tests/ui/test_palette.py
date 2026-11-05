"""The UI's design tokens (package 15): the colours on ``ui.css``'s ``:root`` are the approved
mockup's, every text and boundary pair meets its WCAG 2.x contrast ratio (text 4.5:1, field
boundaries and the focus outline 3:1, ruling P15-R3), and the fonts are system fonts (D6)."""

import re
from pathlib import Path

import pytest

UI_CSS = Path(__file__).parents[2] / "src" / "text_extraction" / "ui" / "static" / "ui.css"

TEXT_PAIRS = [
    ("ink", "page"),
    ("ink", "paper"),
    ("ink", "accent-wash"),
    ("ink", "neutral-wash"),
    ("muted", "page"),
    ("muted", "paper"),
    ("muted", "neutral-wash"),
    ("muted", "accent-wash"),
    ("accent", "page"),
    ("accent", "paper"),
    ("accent", "accent-wash"),
    ("accent-ink", "accent"),
    ("accent-ink", "accent-strong"),
    ("accent-strong", "accent-wash"),
    ("warn", "warn-wash"),
    ("error", "error-wash"),
]
NON_TEXT_PAIRS = [
    ("field-line", "paper"),
    ("field-line", "page"),
    ("focus", "page"),
    ("focus", "paper"),
]

_ROOT = re.compile(r"^:root\s*\{(?P<body>[^}]*)\}", re.MULTILINE)
_COLOUR = re.compile(r"--(?P<name>[a-z][a-z-]*)\s*:\s*(?P<value>#[0-9a-fA-F]{6})\s*;")


def tokens() -> dict[str, str]:
    """The ``--name: #rrggbb`` pairs of the first top-level ``:root`` block of ``ui.css``, the
    values in lower case."""
    found = _ROOT.search(UI_CSS.read_text(encoding="utf-8"))
    assert found is not None, "ui.css has no :root block"
    return {match["name"]: match["value"].lower() for match in _COLOUR.finditer(found["body"])}


def _luminance(colour: str) -> float:
    """The relative luminance of ``#rrggbb`` (WCAG 2.x)."""
    channels = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(fore: str, back: str) -> float:
    """The WCAG 2.x contrast ratio of two ``#rrggbb`` colours."""
    lighter, darker = sorted((_luminance(fore), _luminance(back)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_tokens_are_the_mockups_values() -> None:
    found = tokens()
    assert {k: found[k] for k in ("page", "paper", "line", "ink", "muted")} == {
        "page": "#f3f4f6",
        "paper": "#ffffff",
        "line": "#dde1e6",
        "ink": "#1f2328",
        "muted": "#59636e",
    }
    assert (found["accent"], found["accent-strong"], found["accent-wash"]) == (
        "#1d6f6a",
        "#155a56",
        "#e6f2f0",
    )


def test_field_borders_are_darker_than_the_mockup() -> None:
    assert tokens()["field-line"] == "#808993"  # ruling P15-R3


@pytest.mark.parametrize(("fore", "back"), TEXT_PAIRS)
def test_text_contrast(fore: str, back: str) -> None:
    assert contrast(tokens()[fore], tokens()[back]) >= 4.5


@pytest.mark.parametrize(("fore", "back"), NON_TEXT_PAIRS)
def test_non_text_contrast(fore: str, back: str) -> None:
    assert contrast(tokens()[fore], tokens()[back]) >= 3.0


def test_contrast_formula() -> None:
    assert round(contrast("#000000", "#ffffff"), 2) == 21.0
    assert round(contrast("#59636e", "#f3f4f6"), 2) == 5.56


def test_colours_outside_root_are_tokens() -> None:
    css = UI_CSS.read_text(encoding="utf-8")
    found = _ROOT.search(css)
    assert found is not None
    rest = css[: found.start()] + css[found.end() :]
    assert re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", rest) == []


def test_reading_and_ui_fonts_are_system_fonts() -> None:
    css = UI_CSS.read_text(encoding="utf-8")
    assert '--font-ui: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif' in css
    assert '--font-read: Charter, "Bitstream Charter", "Sitka Text", Cambria, Georgia, serif' in css
    assert "--font-code: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" in css
    assert "@font-face" not in css
    assert "url(" not in css
