"""Service errors and the FastAPI handlers that turn every failure into an envelope."""

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

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


def _failed(
    request: HTTPConnection,
    http_status: int,
    error: ErrorInfo,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    envelope: Envelope[None] = Envelope(status="failed", error=error, trace_id=_trace_id(request))
    return JSONResponse(
        status_code=http_status,
        content=envelope.model_dump(mode="json"),
        headers=dict(headers) if headers else None,
    )


async def _handle_service_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ServiceError)  # noqa: S101 - narrows the type for mypy
    error = ErrorInfo(code=exc.code, message=exc.message, retryable=exc.retryable)
    return _failed(request, exc.http_status, error, exc.headers)


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101 - narrows the type for mypy
    details: list[dict[str, Any]] = [
        {"loc": list(item["loc"]), "msg": item["msg"]} for item in exc.errors()
    ]
    error = ErrorInfo(
        code=ErrorCode.INVALID_REQUEST,
        message="The request is invalid.",
        details=details,
    )
    return _failed(request, HTTPStatus.UNPROCESSABLE_ENTITY, error)


def body_too_large_response(request: HTTPConnection, error: BodyTooLarge) -> JSONResponse:
    """413 ``invalid_request``; the connection is closed, since the rest of the body is unread."""
    info = ErrorInfo(code=ErrorCode.INVALID_REQUEST, message=error.detail)
    return _failed(request, error.status_code, info, {"connection": "close"})


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
    return _failed(request, exc.status_code, error, exc.headers)


async def _handle_unhandled(request: Request, exc: Exception) -> JSONResponse:
    # The contextvars are already reset here, so bind the request's trace fields again.
    with structlog.contextvars.bound_contextvars(**request_log_context(request)):
        _log.error("unhandled_exception", exc_info=exc)
    error = ErrorInfo(code=ErrorCode.INTERNAL_ERROR, message=_GENERIC_INTERNAL_MESSAGE)
    return _failed(request, HTTPStatus.INTERNAL_SERVER_ERROR, error)


def install_error_handlers(app: FastAPI) -> None:
    """Make every error answer an `Envelope(status="failed")`."""
    app.add_exception_handler(ServiceError, _handle_service_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(BodyTooLarge, _handle_body_too_large)
    app.add_exception_handler(Exception, _handle_unhandled)
