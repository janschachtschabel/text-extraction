"""The templates hold no literal text (ruling P12-S5): every word a user reads comes from the
catalogues through ``t``, or from Python as a ``Message``. Every key the templates and the UI's
Python name exists, every key of the catalogues is used, and the page renders in both
languages."""

import html
import re
from collections.abc import Iterator
from pathlib import Path

import lxml.html
import pytest

from tests.ui.conftest import HX, run, running
from text_extraction.ui.i18n import CATALOGUES, LANGUAGE_HEADER, LANGUAGES, Language, translate
from text_extraction.ui.routes import TABS
from text_extraction.ui.templating import templates

UI_DIR = Path(__file__).parents[2] / "src" / "text_extraction" / "ui"
TEMPLATES = sorted((UI_DIR / "templates").rglob("*.html"))
PYTHON = sorted(UI_DIR.glob("*.py"))

# Names and abbreviations that read the same in every language.
ALLOWED_LITERALS = {
    "curl",
    "JSON",
    "URL",
    "URLs",
    "HTTP",
    "API",
    "MCP",
    "PNG",
    "CSV",
    "LLM",
    "ASR",
    "https",
}
# Attributes whose values a user reads (or hears).
TEXT_ATTRIBUTES = ("placeholder", "title", "aria-label", "alt", "hx-confirm")
# Keys named by a computed string (for example ``"warning." ~ name`` or from Python data).
DYNAMIC_PREFIXES = ("warning.", "error.", "timeline.")

_ATTRIBUTE = re.compile(
    r"(?<![\w-])(" + "|".join(TEXT_ATTRIBUTES) + r""")\s*=\s*(?:"([^"]*)"|'([^']*)')"""
)
_TAG = re.compile(r"<[^>]*>")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_WORD = re.compile(r"[^\W\d_]+")
# A whole key, not a prefix joined with a value (``t("switch." ~ option)``).
_T_CALL = re.compile(r"""\bt\(\s*(["'])([^"']+)\1\s*[,)]""")
_MESSAGE_CALL = re.compile(r"""\bMessage\(\s*(["'])([^"']+)\1\s*[,)]""")
_QUOTED = re.compile(r"""(["'])([a-z0-9_-]+(?:\.[a-z0-9_-]+)+)\1""")
# A key prefix joined with a value in a template: ``t("switch." ~ option)``.
_JOINED_PREFIX = re.compile(r"""(["'])([a-z0-9_.-]+\.)\1\s*~""")


def _data(source: str) -> str:
    """The template's text outside every Jinja block, expression and comment."""
    return "".join(value for _, kind, value in templates.env.lex(source) if kind == "data")


def _literal_words(source: str) -> Iterator[str]:
    markup = _HTML_COMMENT.sub("", _data(source))
    for match in _ATTRIBUTE.finditer(markup):
        yield from _WORD.findall(html.unescape(match.group(2) or match.group(3) or ""))
    yield from _WORD.findall(html.unescape(_TAG.sub(" ", markup)))


def _sources(paths: list[Path]) -> dict[str, str]:
    return {str(path.relative_to(UI_DIR)): path.read_text(encoding="utf-8") for path in paths}


def test_the_literal_check_sees_text_and_attributes() -> None:
    source = (
        '<p class="x">Hello {{ name }} <code>JSON</code></p>{# a note #}'
        '<input placeholder="Type here" value="kept"{% if x %} checked{% endif %}>'
        '<form hx-confirm="Sure?"></form><img alt="{{ t(\'k.alt\') }}">'
    )

    assert sorted(_literal_words(source)) == ["Hello", "JSON", "Sure", "Type", "here"]


def test_templates_have_no_literal_text() -> None:
    found = {
        name: words
        for name, source in _sources(TEMPLATES).items()
        if (words := sorted(set(_literal_words(source)) - ALLOWED_LITERALS))
    }

    assert found == {}


def _keys_named(pattern: re.Pattern[str], paths: list[Path]) -> set[str]:
    return {
        match.group(2) for source in _sources(paths).values() for match in pattern.finditer(source)
    }


def test_every_key_used_exists() -> None:
    named = (
        _keys_named(_T_CALL, TEMPLATES)
        | _keys_named(_MESSAGE_CALL, PYTHON)
        | {tab.label_key for tab in TABS}
    )

    joined = _keys_named(_JOINED_PREFIX, TEMPLATES)

    assert named
    assert joined
    for language in LANGUAGES:
        keys = set(CATALOGUES[language])
        assert sorted(named - keys) == [], language
        empty = [prefix for prefix in joined if not any(key.startswith(prefix) for key in keys)]
        assert empty == [], language


def test_no_unused_keys() -> None:
    sources = _sources(TEMPLATES + PYTHON)
    quoted = {match.group(2) for text in sources.values() for match in _QUOTED.finditer(text)}
    joined = {
        match.group(2)
        for name, text in sources.items()
        if name.endswith(".html")
        for match in _JOINED_PREFIX.finditer(text)
    }
    prefixes = DYNAMIC_PREFIXES + tuple(joined)

    unused = [key for key in CATALOGUES["en"] if key not in quoted and not key.startswith(prefixes)]

    assert unused == []


def _texts(page: str, xpath: str) -> list[str]:
    root = lxml.html.document_fromstring(page)
    return [" ".join(element.text_content().split()) for element in root.xpath(xpath)]


@pytest.mark.parametrize(
    ("language", "heading", "tab"), [("de", "Anmelden", "Extrahieren"), ("en", "Log in", "Extract")]
)
def test_page_renders_in_both_languages(language: Language, heading: str, tab: str) -> None:
    with running() as client:
        login = client.get("/ui", params={"lang": language}).text
    with running(auth_mode="none") as client:
        app = client.get("/ui", params={"lang": language}).text

    assert _texts(login, "//*[@id='login-title']") == [heading]
    assert _texts(app, "//*[@role='tab']")[0] == tab
    assert _texts(app, "//*[@role='tab']") == [translate(language, tab.label_key) for tab in TABS]


@pytest.mark.parametrize("language", LANGUAGES)
def test_python_messages_render_in_the_request_language(language: Language) -> None:
    with running(auth_mode="none") as client:
        page = client.get("/ui", params={"lang": language}).text
    with running() as client:
        refused = client.post(
            "/ui/run/login",
            headers={**HX, "Authorization": "Bearer wrong-key", LANGUAGE_HEADER: language},
        ).text
        no_file = run(client, "file", headers={LANGUAGE_HEADER: language}, format="markdown").text

    assert _texts(page, "//*[@class='banner']") == [translate(language, "shell.banner")]
    assert _texts(refused, "//*[@class='error']") == [translate(language, "login.not_valid")]
    assert _texts(no_file, "//*[@class='error']/p") == [translate(language, "form.no_file")]
