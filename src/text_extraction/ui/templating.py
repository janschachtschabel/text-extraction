"""The UI's Jinja2 templates, with autoescape on for every template whatever its name
(ruling P10-S12), and the language context of every template (ruling P12-S5): ``t``, ``lang``
and ``languages``."""

from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup, escape
from starlette.requests import Request

from text_extraction.ui.i18n import (
    LANGUAGES,
    PART_SEPARATOR,
    STATE_KEY,
    Language,
    Message,
    fill,
    translate,
)

TEMPLATES_DIR: Final = Path(__file__).parent / "templates"


def code(value: object) -> Markup:
    """``value`` as ``<code>`` markup (escaped), for a placeholder of ``t``: catalogue values
    hold no markup, so the template wraps it around a placeholder's output."""
    return Markup("<code>{}</code>").format(value)


def text_function(language: Language) -> Callable[..., Markup]:
    """The templates' ``t(key, **params)``: the catalogue text of ``key`` in ``language``,
    escaped, with each placeholder filled with its escaped value (markup such as `code` stays
    markup). ``t(message)`` renders a `Message` from Python the same way. A value that is a
    `Message` is rendered by ``t`` too, and a list or tuple is its parts, each rendered so,
    joined by ", "."""

    def value_of(value: object) -> Markup:
        if isinstance(value, Message):
            return t(value)
        if isinstance(value, list | tuple):
            separator = Markup(PART_SEPARATOR)  # noqa: S704 - a constant, no markup
            return separator.join(value_of(part) for part in value)
        return escape(value)

    def t(key: str | Message, /, **params: object) -> Markup:
        if isinstance(key, Message):
            return t(key.key, **{**key.params, **params})
        text = str(escape(translate(language, key)))
        escaped = {name: value_of(value) for name, value in params.items()}
        return Markup(fill(text, escaped))  # noqa: S704 - escaped text, escaped values

    return t


def language_context(language: Language) -> dict[str, Any]:
    """What every template gets for ``language``: ``t``, ``lang`` and ``languages``."""
    return {"t": text_function(language), "lang": language, "languages": LANGUAGES}


def request_language(request: Request) -> Language:
    """The language of a UI request (set by the UI router), else ``UI_DEFAULT_LANGUAGE``."""
    language: Language = getattr(request.state, STATE_KEY, None) or (
        request.app.state.settings.ui_default_language
    )
    return language


def _request_language(request: Request) -> dict[str, Any]:
    return language_context(request_language(request))


templates: Final = Jinja2Templates(
    env=Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True),
    context_processors=[_request_language],
)
templates.env.globals["code"] = code
