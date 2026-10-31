"""The UI's warning, error and timeline texts (rulings P10-S17, P12-S5): every warning that
docs/architecture.md lists and every ``ErrorCode`` has a text in both catalogues; an API error is
shown as its translated ``error.<code>`` text with the API's English message below it."""

import json
import re
from pathlib import Path
from typing import Any

import lxml.html
import pytest

from tests.fakes import PAGE
from tests.ui.conftest import run, running
from tests.ui.site_items import result_data
from text_extraction.envelope import ErrorCode, ErrorInfo
from text_extraction.extract_models import ExtractResult
from text_extraction.ui.i18n import CATALOGUES, LANGUAGE_HEADER, LANGUAGES, Language, text_of
from text_extraction.ui.results import render_result
from text_extraction.ui.texts import warning_text
from text_extraction.ui.timeline import timeline

ARCHITECTURE = Path(__file__).resolve().parents[2] / "docs" / "architecture.md"
# Backticked snake_case names of the warnings block that are request or tool fields, not warnings.
NOT_WARNINGS = {"content_offset", "describe_images", "learn_selectors", "max_chars", "max_pages"}
# A warning whose name holds a number, such as `action_<index>_failed`: its key is
# ``warning.action_index_failed``.
_NUMBERED = re.compile(r"`([a-z][a-z0-9_]*)<([a-z]+)>([a-z0-9_]*)`")


def documented_warnings() -> set[str]:
    """The backticked snake_case names of the paragraph on ``warnings`` in architecture.md, a
    numbered one (``action_<index>_failed``) with its placeholder's name in place of the
    number."""
    text = ARCHITECTURE.read_text(encoding="utf-8")
    start = text.index("`warnings` names what did not go")
    end = text.index("A licence in `metadata`", start)
    paragraph = text[start:end]
    names = set(re.findall(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`", paragraph))
    names |= {"".join(match.groups()) for match in _NUMBERED.finditer(paragraph)}
    return names - NOT_WARNINGS


def keys_under(language: Language, prefix: str) -> set[str]:
    return {key.removeprefix(prefix) for key in CATALOGUES[language] if key.startswith(prefix)}


def tree(html: str) -> Any:
    return lxml.html.fragment_fromstring(html, create_parent="div")


def texts(root: Any, xpath: str) -> list[str]:
    return [" ".join(node.text_content().split()) for node in root.xpath(xpath)]


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_documented_warning_has_a_text(language: Language) -> None:
    assert "action_index_failed" in documented_warnings()
    assert keys_under(language, "warning.") == documented_warnings()


def test_mcp_content_truncated_has_a_text() -> None:
    assert "mcp_content_truncated" in documented_warnings()
    for language in LANGUAGES:
        text = warning_text("mcp_content_truncated", language)
        assert text is not None
        assert "content_offset" in text


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_error_code_has_a_text_in_both_languages(language: Language) -> None:
    assert keys_under(language, "error.") == {str(code) for code in ErrorCode}


def test_action_failed_text_in_german() -> None:
    assert warning_text("action_2_failed", "de") == "Browser-Aktion 2 ist fehlgeschlagen."
    assert warning_text("action_2_failed", "en") == "Browser action 2 failed."
    assert (
        warning_text("browser_unavailable", "de") == CATALOGUES["de"]["warning.browser_unavailable"]
    )
    assert warning_text("no_such_warning_x", "de") is None


def test_fragment_shows_translated_error_and_english_detail() -> None:
    with running() as client:
        answer = run(
            client,
            "extract",
            headers={LANGUAGE_HEADER: "de"},
            url=PAGE,
            format="markdown",
            mode="auto",
            wait_seconds="25",
            options='{"mode2": 1}',
        )

    root = tree(answer.text)
    raw = json.loads(root.xpath("//details[@class='raw']/pre")[0].text_content())
    assert texts(root, "//*[@class='http-status']") == ["HTTP 422"]
    assert texts(root, "//*[@class='error-code']") == ["invalid_request"]
    assert texts(root, "//*[@class='error-text']") == [CATALOGUES["de"]["error.invalid_request"]]
    assert texts(root, "//*[@class='api-detail-label']") == ["Details der API (Englisch)"]
    message = root.xpath("//*[@class='error-message']")[0]
    assert (message.text_content(), message.get("lang")) == (raw["error"]["message"], "en")
    details = root.xpath("//ul[@class='details']")[0]
    assert details.get("lang") == "en"
    assert [line.startswith("body.mode2:") for line in texts(details, "./li")] == [True]


def test_timeline_in_german() -> None:
    browser = ExtractResult.model_validate(
        result_data(
            PAGE,
            mode_used="browser",
            unblocked=True,
            upstream_status=200,
            timings={
                "total_ms": 90,
                "compliance_ms": 1,
                "fetch_ms": 5,
                "convert_ms": 4,
                "render_ms": 70,
                "pii_ms": 3,
                "llm_ms": 6,
            },
            pii={"mode": "mask", "language": "en", "entities": {"PERSON": 2, "EMAIL_ADDRESS": 1}},
            document={
                "kind": "pdf",
                "converter": "docling",
                "pages": 9,
                "pages_converted": 4,
                "ocr": False,
            },
        )
    )
    error = ErrorInfo(code=ErrorCode.TIMEOUT, message="Too slow.", retryable=True)

    steps = timeline(None, browser, status="partial", error=error, requested_mode="auto")

    assert [step.label.text("de") for step in steps] == [
        "Regelprüfung",
        "Abruf",
        "Rendern",
        "Umwandlung",
        "Personenbezogene Daten",
        "LLM",
        "Ergebnis",
    ]
    details = [text_of(step.detail, "de") for step in steps]
    assert details[1] == "1 Versuch, HTTP 200, Cache miss"
    assert details[2] == "Fallback nach HTTP, Entsperrmodus"
    assert details[3] == "text/html, docling, 4 von 9 Seiten"
    assert details[4] == "3 Funde (mask)"
    assert details[6] == "partial, timeout, wiederholbar"

    data = browser.model_dump(mode="json")
    data["warnings"] = ["action_2_failed", "no_such_warning_x"]
    envelope = {"status": "partial", "data": data}
    html = render_result(op="extract", http_status=200, envelope=envelope, language="de")
    root = tree(html)
    assert texts(root, "//ol[@class='timeline']/li/span[@class='step']")[0] == "Regelprüfung"
    assert texts(root, "//ul[@class='warnings']/li") == [
        "action_2_failed: Browser-Aktion 2 ist fehlgeschlagen.",
        "no_such_warning_x",
    ]
