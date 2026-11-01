"""Service errors and the FastAPI handlers that turn every failure into an envelope."""

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any, Final

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import HTTPConnection

from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.tracing import current_trace_id, request_log_context

_log = structlog.get_logger(__name__)

_GENERIC_INTERNAL_MESSAGE = "Internal server error."


class ServiceError(Exception):
    """An error the service reports on purpose, with the HTTP status to answer with."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        http_status: int,
        retryable: bool = False,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retryable = retryable
        self.headers = headers


class BodyTooLarge(StarletteHTTPException):
    """The request body is over the limit of its path (413, ``invalid_request``). Raised while the
    body is read (``body_limit.py``); an HTTPException, so FastAPI passes it on unchanged."""

    def __init__(self, limit: int) -> None:
        super().__init__(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            f"The request body is too large: at most {limit} bytes are accepted.",
        )


def _trace_id(request: HTTPConnection) -> str | None:
    """Trace id of the request.

    The request comes first: the catch-all handler runs outside `TraceMiddleware`, where the
    contextvar is already reset. The contextvar covers code that has no request at hand.
    """
    return request_log_context(request).get("trace_id") or current_trace_id()


def failed_response(
    request: HTTPConnection,
    http_status: int,
    error: ErrorInfo,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """The failed envelope of ``error`` with the request's trace id, as a JSON response."""
    envelope: Envelope[None] = Envelope(status="failed", error=error, trace_id=_trace_id(request))
    return JSONResponse(
        status_code=http_status,
        content=envelope.model_dump(mode="json"),
        headers=dict(headers) if headers else None,
    )


# The Retry-After of a retryable 503 without a wait of its own (C12): an unavailable backend
# usually comes back within seconds, and a client that honours the header does not hammer it.
UNAVAILABLE_RETRY_AFTER_SECONDS: Final = 5


async def _handle_service_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ServiceError)  # noqa: S101 - narrows the type for mypy
    error = ErrorInfo(code=exc.code, message=exc.message, retryable=exc.retryable)
    headers = dict(exc.headers or {})
    waits = any(name.lower() == "retry-after" for name in headers)
    if exc.http_status == HTTPStatus.SERVICE_UNAVAILABLE and exc.retryable and not waits:
        headers["Retry-After"] = str(UNAVAILABLE_RETRY_AFTER_SECONDS)
    return failed_response(request, exc.http_status, error, headers)


def _not_json(request: Request, exc: RequestValidationError) -> bool:
    """Whether the body as a whole failed because it was sent as something else than JSON: a
    JSON route then answers 415, not a 422 that says the body is no object."""
    if not any(tuple(item["loc"]) == ("body",) for item in exc.errors()):
        return False
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if not media_type:
        return int(request.headers.get("content-length", "0") or 0) > 0
    return media_type != "application/json" and not media_type.endswith("+json")


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101 - narrows the type for mypy
    if _not_json(request, exc):
        error = ErrorInfo(
            code=ErrorCode.INVALID_REQUEST,
            message="The body must be JSON (Content-Type: application/json).",
        )
        return failed_response(request, HTTPStatus.UNSUPPORTED_MEDIA_TYPE, error)
    details: list[dict[str, Any]] = [
        {"loc": list(item["loc"]), "msg": item["msg"]} for item in exc.errors()
    ]
    error = ErrorInfo(
        code=ErrorCode.INVALID_REQUEST,
        message="The request is invalid.",
        details=details,
    )
    return failed_response(request, HTTPStatus.UNPROCESSABLE_ENTITY, error)


def body_too_large_response(request: HTTPConnection, error: BodyTooLarge) -> JSONResponse:
    """413 ``invalid_request``; the connection is closed, since the rest of the body is unread."""
    info = ErrorInfo(code=ErrorCode.INVALID_REQUEST, message=error.detail)
    return failed_response(request, error.status_code, info, {"connection": "close"})


async def _handle_body_too_large(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, BodyTooLarge)  # noqa: S101 - narrows the type for mypy
    return body_too_large_response(request, exc)


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101 - narrows the type for mypy
    code = ErrorCode.NOT_FOUND if exc.status_code == HTTPStatus.NOT_FOUND else None
    if code is None:
        code = ErrorCode.INTERNAL_ERROR if exc.status_code >= 500 else ErrorCode.INVALID_REQUEST
    try:
        message = HTTPStatus(exc.status_code).phrase
    except ValueError:
        message = "Request failed."
    error = ErrorInfo(code=code, message=message)
    return failed_response(request, exc.status_code, error, exc.headers)


async def _handle_unhandled(request: Request, exc: Exception) -> JSONResponse:
    # The contextvars are already reset here, so bind the request's trace fields again.
    with structlog.contextvars.bound_contextvars(**request_log_context(request)):
        _log.error("unhandled_exception", exc_info=exc)
    error = ErrorInfo(code=ErrorCode.INTERNAL_ERROR, message=_GENERIC_INTERNAL_MESSAGE)
    return failed_response(request, HTTPStatus.INTERNAL_SERVER_ERROR, error)


def install_error_handlers(app: FastAPI) -> None:
    """Make every error answer an `Envelope(status="failed")`."""
    app.add_exception_handler(ServiceError, _handle_service_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(BodyTooLarge, _handle_body_too_large)
    app.add_exception_handler(Exception, _handle_unhandled)
