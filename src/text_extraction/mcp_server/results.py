"""Tool results (ruling P10-S6): the route's envelope as ``structuredContent``, the same JSON as
the last text content, one-line hints before it, and ``isError`` only where REST answers 4xx or
5xx. A target's ``failed`` (REST 200) is a normal result."""

import json
from collections.abc import Sequence
from typing import Any

from fastapi.exceptions import RequestValidationError
from fastmcp.tools import ToolResult
from mcp_types import TextContent
from pydantic import ValidationError
from starlette.responses import Response

from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.errors import ServiceError
from text_extraction.tracing import current_trace_id

INTERNAL_MESSAGE = "Internal server error."
INVALID_MESSAGE = "The request is invalid."


def _result(payload: dict[str, Any], *, is_error: bool, hints: Sequence[str] = ()) -> ToolResult:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    content = [TextContent(type="text", text=hint) for hint in hints]
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


def envelope_result(answer: Envelope[Any] | Response, *, hints: Sequence[str] = ()) -> ToolResult:
    """The result of a route core's answer: an envelope (200, or 202 with a job), or a response
    whose body is an envelope (``JSONResponse``); a response with status 4xx or 5xx is an error
    result. ``hints`` are one-line texts put before the JSON."""
    if isinstance(answer, Envelope):
        return _result(answer.model_dump(mode="json"), is_error=False, hints=hints)
    payload = json.loads(bytes(answer.body))
    if not isinstance(payload, dict):
        raise TypeError("The answer of a route core is not an envelope.")
    return _result(payload, is_error=answer.status_code >= 400, hints=hints)
