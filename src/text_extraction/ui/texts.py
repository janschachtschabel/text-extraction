"""The UI's texts of result warnings and API errors (rulings P10-S17, P12-S5). A warning's
one-line text is the catalogue key ``warning.<name>``, for each warning that
``ExtractResult.warnings`` and ``docs/architecture.md`` describe; an unknown warning (a
converter note, a warning newer than the catalogues) is shown by its name. An error's text is
``error.<code>``, for each ``ErrorCode``; the API's own English message is shown below it."""

import re
from typing import Final

from text_extraction.ui.i18n import CATALOGUES, Language, Message

_ACTION_FAILED: Final = re.compile(r"action_(\d+)_failed")
# The text of an answer that names no known error code.
HTTP_ANSWER: Final = "result.http_answer"


def warning_message(name: str) -> Message | None:
    """The text of the warning ``name``; None for a warning the catalogues do not know (it is
    shown by its name)."""
    action = _ACTION_FAILED.fullmatch(name)
    if action is not None:
        return Message("warning.action_index_failed", {"index": int(action[1])})
    key = f"warning.{name}"
    return Message(key) if key in CATALOGUES["en"] else None


def warning_text(name: str, language: Language) -> str | None:
    """The one-line text of the warning ``name`` in ``language``, or None (`warning_message`)."""
    message = warning_message(name)
    return message.text(language) if message is not None else None


def error_text(code: object, http_status: int) -> Message:
    """The text of the error ``code`` (``error.<code>``); for a code the catalogues do not
    know, "The service answered HTTP <http_status>."."""
    key = f"error.{code}"
    if isinstance(code, str) and key in CATALOGUES["en"]:
        return Message(key)
    return Message(HTTP_ANSWER, {"status": http_status})
