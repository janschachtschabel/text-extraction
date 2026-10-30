"""Helpers the UI's views share to show REST answers: the message of an envelope, the text
fields of a form, a raw body, the screenshot an answer references and the fragment of a refusal
of a view that calls the cores."""

import re
from collections.abc import Mapping
from typing import Any, Final

from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from text_extraction.errors import ServiceError
from text_extraction.ui.forms import OPERATIONS
from text_extraction.ui.forward import Forwarded, forward
from text_extraction.ui.results import render_result
from text_extraction.vision.images import data_url, sniff_image

# The key headers a curl line names (with a shell variable, never the value).
KEY_HEADERS: Final = ("authorization", "x-llm-api-key", "x-asr-api-key")
# A body that is not JSON is shown up to this many characters.
RAW_MAX_CHARS: Final = 10_000
# An artifact name never starts with a dot (so never "." or "..").
ARTIFACT_URL: Final = re.compile(
    r"/v1/jobs/([A-Za-z0-9_-]+)/artifacts/([A-Za-z0-9_-][A-Za-z0-9_.-]*)"
)


def envelope_message(body: dict[str, Any] | None, status: int) -> str:
    """The envelope's error message, or "The service answered HTTP <status>."."""
    error = (body or {}).get("error")
    message = error.get("message") if isinstance(error, dict) else None
    return message if isinstance(message, str) else f"The service answered HTTP {status}."


def text_fields(form: FormData) -> dict[str, str]:
    """The text fields of a form (its last value of each name); files are left out."""
    return {name: value for name, value in form.items() if isinstance(value, str)}


def raw_text(answer: Forwarded) -> str:
    return answer.content[: RAW_MAX_CHARS * 4].decode("utf-8", "replace")[:RAW_MAX_CHARS]


def sent_keys(request: Request) -> list[str]:
    """The key headers the browser's request carries (for a curl line)."""
    return [name for name in KEY_HEADERS if request.headers.get(name)]


def artifact_of(envelope: Mapping[str, Any] | None) -> tuple[str, str] | None:
    """The job id and artifact name of the screenshot an answer's ``data.screenshot``
    references, or None."""
    data = (envelope or {}).get("data")
    shot = data.get("screenshot") if isinstance(data, Mapping) else None
    url = shot.get("url") if isinstance(shot, Mapping) else None
    found = ARTIFACT_URL.fullmatch(url) if isinstance(url, str) else None
    return (found[1], found[2]) if found is not None else None


async def read_screenshot(
    request: Request, job_id: str, name: str
) -> tuple[str | None, str | None]:
    """The ``data:`` URI of the PNG artifact ``name`` of job ``job_id``, read from the artifact
    route (a REST call with the browser's key), or the note why it could not be read."""
    path = OPERATIONS["artifact"].path.format(job_id=job_id, name=name)
    try:
        answer = await forward(request, "GET", path)
    except ValueError:  # not a plain path under /v1/
        return None, "The screenshot could not be read: it is no artifact of a job."
    if answer.status != 200:
        message = envelope_message(answer.body, answer.status)
        return None, f"The screenshot could not be read: HTTP {answer.status} {message}"
    if sniff_image(answer.content) != "image/png":
        return None, "The screenshot could not be read: it is not a PNG."
    return data_url(answer.content, "image/png"), None


async def screenshot_of(
    request: Request, envelope: Mapping[str, Any] | None
) -> tuple[str | None, str | None]:
    """The screenshot an answer's ``data.screenshot`` references (`read_screenshot`); (None,
    None) when the answer references none."""
    found = artifact_of(envelope)
    if found is None:
        return None, None
    return await read_screenshot(request, *found)


def refusal(error: ServiceError) -> Response:
    """A refusal of a view that calls the cores (401, 403, 404, 429, 503) as a fragment, HTTP
    200 (so htmx swaps it)."""
    envelope = {
        "status": "failed",
        "error": {"code": str(error.code), "message": error.message, "retryable": error.retryable},
    }
    retry_after = (error.headers or {}).get("Retry-After")
    html = render_result(
        op="view", http_status=error.http_status, envelope=envelope, retry_after=retry_after
    )
    return HTMLResponse(html)
