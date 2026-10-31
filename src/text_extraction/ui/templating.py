"""The UI's Jinja2 templates, with autoescape on for every template whatever its name
(ruling P10-S12), and the language context of every template (ruling P12-S5): ``t``, ``show``,
``lang`` and ``languages``. ``status_text`` and ``state_text`` name the texts of a result's
status and a job's state."""

from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup, escape
from starlette.requests import Request

from text_extraction.envelope import ErrorCode
from text_extraction.ui.i18n import (
    LANGUAGES,
    PART_SEPARATOR,
    STATE_KEY,
    Code,
    Language,
    Message,
    fill,
    help_url,
    translate,
)
from text_extraction.ui.texts import state_text, status_text

TEMPLATES_DIR: Final = Path(__file__).parent / "templates"


def code(value: object) -> Markup:
    """``value`` as ``<code>`` markup (escaped), for a placeholder of ``t``: catalogue values
    hold no markup, so the template wraps it around a placeholder's output."""
    return Markup("<code>{}</code>").format(value)


def _markup(value: object, t: Callable[..., Markup]) -> Markup:
    """``value`` as markup: a `Message` rendered by ``t``, a `Code` as `code`, a list or tuple
    its parts so rendered and joined by ", ", anything else escaped."""
    if isinstance(value, Message):
        return t(value)
    if isinstance(value, Code):
        return code(value)
    if isinstance(value, list | tuple):
        separator = Markup(PART_SEPARATOR)  # noqa: S704 - a constant, no markup
        return separator.join(_markup(part, t) for part in value)
    return escape(value)


def text_function(language: Language) -> Callable[..., Markup]:
    """The templates' ``t(key, **params)``: the catalogue text of ``key`` in ``language``,
    escaped, with each placeholder filled with its escaped value (markup such as `code` stays
    markup). ``t(message)`` renders a `Message` from Python the same way. A value that is a
    `Message` is rendered by ``t`` too, a `Code` as `code`, and a list or tuple is its parts,
    each rendered so, joined by ", "."""

    def t(key: str | Message, /, **params: object) -> Markup:
        if isinstance(key, Message):
            return t(key.key, **{**key.params, **params})
        text = str(escape(translate(language, key)))
        escaped = {name: _markup(value, t) for name, value in params.items()}
        return Markup(fill(text, escaped))  # noqa: S704 - escaped text, escaped values

    return t


def language_context(language: Language) -> dict[str, Any]:
    """What every template gets for ``language``: ``t``, ``show`` (a value as ``t`` renders a
    placeholder's value: a `Message`, a `Code`, a list of them or a plain value), ``lang`` and
    ``languages``."""
    t = text_function(language)

    def show(value: object) -> Markup:
        return _markup(value, t)

    return {"t": t, "show": show, "lang": language, "languages": LANGUAGES}


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
templates.env.globals["status_text"] = status_text
templates.env.globals["state_text"] = state_text
templates.env.globals["help_url"] = help_url
# The API's error codes, whose texts the page holds for ui.js (``base.html``).
templates.env.globals["error_codes"] = tuple(code.value for code in ErrorCode)
