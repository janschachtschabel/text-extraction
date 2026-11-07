"""The UI's texts of result warnings, API errors, result statuses and job states (rulings
P10-S17, P12-S5, R-P12-4). A warning's
one-line text is the catalogue key ``warning.<name>``, for each warning that
``ExtractResult.warnings`` and ``docs/architecture.md`` describe; an unknown warning (a
converter note, a warning newer than the catalogues) is shown by its name. An error's text is
``error.<code>``, for each ``ErrorCode``; the API's own English message is shown below it. One
message has a text of its own: transcription switched off (``result.transcription_disabled``),
which the API answers as ``unsupported_content_type`` for an audio or video upload and as
``invalid_request`` for ``transcribe: true``; "this kind of content cannot be converted" would
send the caller looking for a wrong format."""

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from text_extraction.pipeline_media import TRANSCRIPTION_DISABLED_MESSAGE
from text_extraction.ui.i18n import CATALOGUES, Language, Message

_ACTION_FAILED: Final = re.compile(r"action_(\d+)_failed")
# The text of an answer that names no known error code.
HTTP_ANSWER: Final = "result.http_answer"
# The text of the API's answer while the service does not transcribe.
TRANSCRIPTION_DISABLED: Final = "result.transcription_disabled"


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


def error_text(code: object, http_status: int, message: object = None) -> Message:
    """The text of the error ``code`` (``error.<code>``); for a code the catalogues do not
    know, "The service answered HTTP <http_status>.". The API's English ``message`` picks
    the text of switched-off transcription (`TRANSCRIPTION_DISABLED`): it is the whole message
    of an upload or ``transcribe: true``, the end of a fetched media file's."""
    if isinstance(message, str) and message.endswith(TRANSCRIPTION_DISABLED_MESSAGE):
        return Message(TRANSCRIPTION_DISABLED)
    key = f"error.{code}"
    if isinstance(code, str) and key in CATALOGUES["en"]:
        return Message(key)
    return Message(HTTP_ANSWER, {"status": http_status})


# A result's ``status`` and a job's (or a waiting item's) state, as badges show them.
STATUS_TEXTS: Final[Mapping[str, Message]] = MappingProxyType(
    {
        "success": Message("status.success"),
        "partial": Message("status.partial"),
        "failed": Message("status.failed"),
    }
)
STATE_TEXTS: Final[Mapping[str, Message]] = MappingProxyType(
    {
        "queued": Message("job_state.queued"),
        "running": Message("job_state.running"),
        "completed": Message("job_state.completed"),
    }
)


def status_text(value: object) -> Message | None:
    """The text of a result's status; None for another value (shown as code)."""
    return STATUS_TEXTS.get(value) if isinstance(value, str) else None


def state_text(value: object) -> Message | None:
    """The text of a job's state; None for another value (shown as code)."""
    return STATE_TEXTS.get(value) if isinstance(value, str) else None
