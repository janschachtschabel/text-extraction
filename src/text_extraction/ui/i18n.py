"""The UI's languages (rulings P12-S5, P12-S6): the catalogues ``locales/en.json`` and
``de.json`` (flat dotted keys, plain-text values with ``{name}`` placeholders), the lookup, and
how a request chooses its language. English is the source language and the fallback. The API's
own texts never pass through here: they stay English."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

from starlette.requests import Request

from text_extraction.settings import Settings

Language = Literal["en", "de"]
LANGUAGES: Final[tuple[Language, ...]] = ("en", "de")
LANGUAGE_HEADER: Final = "x-ui-language"
LANGUAGE_QUERY: Final = "lang"
# The request's language, as ``request.state.ui_language`` (set by the UI router's dependency).
STATE_KEY: Final = "ui_language"
LOCALES_DIR: Final = Path(__file__).parent / "locales"

_PLACEHOLDER: Final = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def _load(language: Language) -> Mapping[str, str]:
    catalogue = json.loads((LOCALES_DIR / f"{language}.json").read_text(encoding="utf-8"))
    if not isinstance(catalogue, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in catalogue.items()
    ):
        raise ValueError(f"locales/{language}.json must be an object of strings")
    return MappingProxyType(catalogue)


# Loaded once, at import.
CATALOGUES: Final[Mapping[Language, Mapping[str, str]]] = {
    language: _load(language) for language in LANGUAGES
}


def _known(value: str | None) -> Language | None:
    for language in LANGUAGES:
        if value == language:
            return language
    return None


def translate(language: Language, key: str, /, **params: object) -> str:
    """The text of ``key`` in ``language``, else in English, else the key itself, with each
    ``{name}`` placeholder replaced by ``str(params[name])`` (a placeholder without a value stays
    as it is; a value is never read for placeholders again)."""
    text = CATALOGUES[language].get(key)
    if text is None:
        text = CATALOGUES["en"].get(key, key)
    return fill(text, params) if params else text


def fill(text: str, params: Mapping[str, object]) -> str:
    """``text`` with each ``{name}`` placeholder replaced by ``str(params[name])``, in one pass;
    a placeholder without a value stays as it is."""

    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        return str(params[name]) if name in params else match.group(0)

    return _PLACEHOLDER.sub(value, text)


def _quality(parameters: list[str]) -> float | None:
    """The ``q`` of an ``Accept-Language`` entry's parameters (1 without one); None for a value
    that is not a number from 0 to 1."""
    for parameter in parameters:
        name, _, value = parameter.partition("=")
        if name.strip().lower() == "q":
            try:
                quality = float(value.strip())
            except ValueError:
                return None
            return quality if 0 <= quality <= 1 else None
    return 1.0


def negotiate(accept_language: str | None, default: Language) -> Language:
    """The language of ``LANGUAGES`` that ``accept_language`` ranks highest, by ``q`` and then
    by order, matched on the primary subtag (``de-DE`` is ``de``); ``default`` when it names
    none of them (an entry with ``q=0`` or a malformed ``q`` names none, and ``*`` names the
    default)."""
    ranked: list[tuple[float, int, Language]] = []
    for position, entry in enumerate((accept_language or "").split(",")):
        tag, *parameters = entry.split(";")
        quality = _quality(parameters)
        if quality is None or quality <= 0:
            continue
        primary = tag.strip().split("-")[0].lower()
        language = default if primary == "*" else _known(primary)
        if language is not None:
            ranked.append((-quality, position, language))
    return min(ranked)[2] if ranked else default


def page_language(request: Request, settings: Settings) -> Language:
    """The language of ``GET /ui``: the ``lang`` query (``en`` or ``de``), then
    ``Accept-Language``, then ``UI_DEFAULT_LANGUAGE``. Any other ``lang`` is ignored."""
    chosen = _known(request.query_params.get(LANGUAGE_QUERY))
    if chosen is not None:
        return chosen
    return negotiate(request.headers.get("accept-language"), settings.ui_default_language)


def run_language(request: Request, settings: Settings) -> Language:
    """The language of a ``/ui/run/*`` fragment: the ``X-UI-Language`` header (``en`` or
    ``de``, which ``ui.js`` sends from ``<html lang>``), then ``Accept-Language``, then
    ``UI_DEFAULT_LANGUAGE``. Any other header value is ignored."""
    chosen = _known(request.headers.get(LANGUAGE_HEADER))
    if chosen is not None:
        return chosen
    return negotiate(request.headers.get("accept-language"), settings.ui_default_language)


@dataclass(frozen=True)
class Message:
    """A UI text from Python: its catalogue key and placeholder values, rendered in the
    request's language at the template boundary."""

    key: str
    params: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def text(self, language: Language) -> str:
        return translate(language, self.key, **self.params)
