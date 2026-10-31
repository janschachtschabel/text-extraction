"""The UI's catalogues and lookups (rulings P12-S5, P12-S6): ``locales/en.json`` and
``de.json`` hold the same keys and placeholders and plain text only; ``translate`` falls back to
English, then to the key; ``negotiate`` reads ``Accept-Language``."""

import json
import re
from pathlib import Path

import pytest

from text_extraction.ui.i18n import (
    CATALOGUES,
    LANGUAGES,
    Language,
    Message,
    negotiate,
    translate,
)

LOCALES = Path(__file__).parents[2] / "src" / "text_extraction" / "ui" / "locales"
_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def _placeholders(text: str) -> set[str]:
    return set(_PLACEHOLDER.findall(text))


def test_languages_and_files() -> None:
    assert LANGUAGES == ("en", "de")
    for language in LANGUAGES:
        on_disk = json.loads((LOCALES / f"{language}.json").read_text(encoding="utf-8"))
        assert dict(CATALOGUES[language]) == on_disk
        assert all(isinstance(value, str) and value for value in on_disk.values())


def test_catalogues_have_the_same_keys() -> None:
    assert set(CATALOGUES["en"]) == set(CATALOGUES["de"])
    assert CATALOGUES["en"]


def test_placeholders_match() -> None:
    problems = [
        key
        for key, english in CATALOGUES["en"].items()
        if _placeholders(english) != _placeholders(CATALOGUES["de"][key])
    ]
    assert problems == []


def test_values_are_plain_text() -> None:
    for language in LANGUAGES:
        catalogue = CATALOGUES[language]
        marked = [key for key, value in catalogue.items() if "<" in value or ">" in value]
        assert marked == [], language


def test_keys_are_flat_and_dotted() -> None:
    for key in CATALOGUES["en"]:
        assert re.fullmatch(r"[a-z0-9_-]+(\.[a-z0-9_-]+)+", key), key


def test_translate_falls_back_to_english(monkeypatch: pytest.MonkeyPatch) -> None:
    english = dict(CATALOGUES["en"])
    german = dict(CATALOGUES["de"])
    english["test.only_english"] = "Only {thing} in English"
    monkeypatch.setitem(CATALOGUES, "en", english)
    monkeypatch.setitem(CATALOGUES, "de", german)

    assert translate("de", "test.only_english", thing="this") == "Only this in English"
    assert translate("de", "test.no_such_key") == "test.no_such_key"


def test_translate_fills_placeholders() -> None:
    assert translate("en", "status.request_failed", status=413, reason="") == "Request failed: 413 "
    assert translate("de", "status.selected", count=3) == "3 ausgewählt"
    # A placeholder without a value stays as it is; braces in a value are not read again.
    assert translate("en", "status.selected") == "{count} selected"
    assert translate("en", "status.selected", count="{count}") == "{count} selected"


def test_message_renders_in_each_language() -> None:
    message = Message("status.selected", {"count": 2})

    assert message.text("en") == "2 selected"
    assert message.text("de") == "2 ausgewählt"
    assert Message("login.title").params == {}
    assert Message("login.title").text("de") == "Anmelden"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("de-DE,de;q=0.9,en;q=0.8", "de"),
        ("en-US", "en"),
        ("fr, de;q=0.5", "de"),
        ("fr", "default"),
        ("", "default"),
        (None, "default"),
        ("de;q=0", "default"),
        ("*;q=abc", "default"),
        ("en;q=0.4, DE;q=0.7", "de"),
        ("de;q=abc, en;q=0.1", "en"),
        ("*", "default"),
    ],
)
@pytest.mark.parametrize("default", LANGUAGES)
def test_negotiate(header: str | None, expected: str, default: Language) -> None:
    assert negotiate(header, default) == (default if expected == "default" else expected)
