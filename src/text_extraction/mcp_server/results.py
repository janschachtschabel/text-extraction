"""Tool results (ruling P10-S6): the route's envelope as ``structuredContent``, the same JSON as
the last text content, one-line hints (and a screenshot as an image) before it, and ``isError``
only where REST answers 4xx or 5xx. A target's ``failed`` (REST 200) is a normal result.
``content`` is cut to the caller's share (``slice_content``); no other field is cut."""

import base64
import json
from collections.abc import Mapping, Sequence
from typing import Any

from fastapi.exceptions import RequestValidationError
from fastmcp.tools import ToolResult
from mcp_types import ImageContent, TextContent
from pydantic import ValidationError
from starlette.responses import Response

from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.errors import ServiceError
from text_extraction.tracing import current_trace_id

INTERNAL_MESSAGE = "Internal server error."
INVALID_MESSAGE = "The request is invalid."
# The warning of a result whose ``content`` an MCP answer returns in part (P10-S6).
TRUNCATED_WARNING = "mcp_content_truncated"


def _result(
    payload: dict[str, Any],
    *,
    is_error: bool,
    hints: Sequence[str] = (),
    images: Sequence[ImageContent] = (),
) -> ToolResult:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    content: list[TextContent | ImageContent] = [
        TextContent(type="text", text=hint) for hint in hints
    ]
    content.extend(images)
    content.append(TextContent(type="text", text=text))
    return ToolResult(content=content, structured_content=payload, is_error=is_error)


def _validation_details(error: RequestValidationError | ValidationError) -> list[dict[str, Any]]:
    """``loc`` and ``msg`` of each error, as REST gives them; never the input."""
    items = (
        error.errors()
        if isinstance(error, RequestValidationError)
        else error.errors(include_url=False, include_input=False, include_context=False)
    )
    return [{"loc": list(item["loc"]), "msg": item["msg"]} for item in items]


def error_info(error: Exception) -> ErrorInfo:
    """What a failed call tells the caller: a ``ServiceError``'s code, message and
    ``retryable``; ``invalid_request`` with the details of a validation error; else
    ``internal_error`` with a generic message (the caller logs the traceback)."""
    if isinstance(error, ServiceError):
        return ErrorInfo(code=error.code, message=error.message, retryable=error.retryable)
    if isinstance(error, RequestValidationError | ValidationError):
        return ErrorInfo(
            code=ErrorCode.INVALID_REQUEST,
            message=INVALID_MESSAGE,
            details=_validation_details(error),
        )
    return ErrorInfo(code=ErrorCode.INTERNAL_ERROR, message=INTERNAL_MESSAGE)


def error_result(error: Exception) -> ToolResult:
    """The failed envelope of ``error`` (``error_info``) as an error result."""
    envelope: Envelope[None] = Envelope(
        status="failed", error=error_info(error), trace_id=current_trace_id()
    )
    return _result(envelope.model_dump(mode="json"), is_error=True)


def answer_payload(answer: Envelope[Any] | Response) -> tuple[dict[str, Any], bool]:
    """The envelope of a route core's answer as JSON, and whether it is an error: an envelope
    (200, or 202 with a job), or a response whose body is an envelope (``JSONResponse``), an
    error when its status is 4xx or 5xx."""
    if isinstance(answer, Envelope):
        return answer.model_dump(mode="json"), False
    payload = json.loads(bytes(answer.body))
    if not isinstance(payload, dict):
        raise TypeError("The answer of a route core is not an envelope.")
    return payload, answer.status_code >= 400


def payload_result(
    payload: dict[str, Any],
    *,
    is_error: bool,
    hints: Sequence[str] = (),
    images: Sequence[ImageContent] = (),
) -> ToolResult:
    """The result of an envelope (``payload``): the hints, the images, then the JSON."""
    return _result(payload, is_error=is_error, hints=hints, images=images)


def envelope_result(answer: Envelope[Any] | Response, *, hints: Sequence[str] = ()) -> ToolResult:
    """The result of a route core's answer (``answer_payload``); ``hints`` are one-line texts
    put before the JSON."""
    payload, is_error = answer_payload(answer)
    return _result(payload, is_error=is_error, hints=hints)


def job_hint(payload: Mapping[str, Any], wait_seconds: int) -> str | None:
    """The hint of an answer that is a job (its ``data`` has a ``status_url``), else None."""
    data = payload.get("data")
    if not isinstance(data, Mapping) or not isinstance(data.get("status_url"), str):
        return None
    job_id, kind = data.get("id"), data.get("kind")
    return (
        f"Continues as job {job_id} ({kind}): call get_job with job_id={job_id} and "
        f"wait_seconds up to {wait_seconds}."
    )


def slice_content(envelope: dict[str, Any], *, offset: int, share: int) -> str | None:
    """Cut ``data.content`` of ``envelope`` (in place) to ``share`` characters from ``offset``.
    A content that is not returned whole gets the warning ``mcp_content_truncated``. Returns the
    hint to read on, or None when nothing follows (or there is no content)."""
    data = envelope.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("content"), str):
        return None
    content: str = data["content"]
    total = len(content)
    end = min(offset + share, total)
    if offset == 0 and end == total:
        return None
    data["content"] = content[offset:end]
    warnings = data.get("warnings")
    if isinstance(warnings, list):
        if TRUNCATED_WARNING not in warnings:
            warnings.append(TRUNCATED_WARNING)
    else:
        data["warnings"] = [TRUNCATED_WARNING]
    if end >= total:
        return None
    return f"Cut at {end} of {total} characters; call again with content_offset={end}."


def image_over_limit_hint(size: object, url: str) -> str:
    """The hint of a screenshot of ``size`` bytes that is not attached."""
    return f"The screenshot ({size} bytes) is over MCP_IMAGE_MAX_BYTES; download it from {url}."


def png_content(data: bytes, *, limit: int, url: str) -> ImageContent | str:
    """A screenshot as image content when it is at most ``limit`` bytes
    (``MCP_IMAGE_MAX_BYTES``), else the hint to download it from ``url``."""
    if len(data) > limit:
        return image_over_limit_hint(len(data), url)
    return ImageContent(type="image", data=base64.b64encode(data).decode(), mime_type="image/png")
