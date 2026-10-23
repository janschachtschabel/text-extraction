"""Service errors and the FastAPI handlers that turn every failure into an envelope."""

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo

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


def _current_trace_id() -> str | None:
    """Trace id of the current request. Task 3 replaces this with `current_trace_id()`."""
    return None


def _failed(
    http_status: int,
    error: ErrorInfo,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    envelope: Envelope[None] = Envelope(status="failed", error=error, trace_id=_current_trace_id())
    return JSONResponse(
        status_code=http_status,
        content=envelope.model_dump(mode="json"),
        headers=dict(headers) if headers else None,
    )


async def _handle_service_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ServiceError)  # noqa: S101 - narrows the type for mypy
    error = ErrorInfo(code=exc.code, message=exc.message, retryable=exc.retryable)
    return _failed(exc.http_status, error, exc.headers)


async def _handle_validation_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101 - narrows the type for mypy
    details: list[dict[str, Any]] = [
        {"loc": list(item["loc"]), "msg": item["msg"]} for item in exc.errors()
    ]
    error = ErrorInfo(
        code=ErrorCode.INVALID_REQUEST,
        message="The request is invalid.",
        details=details,
    )
    return _failed(HTTPStatus.UNPROCESSABLE_ENTITY, error)


async def _handle_http_exception(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101 - narrows the type for mypy
    code = ErrorCode.NOT_FOUND if exc.status_code == HTTPStatus.NOT_FOUND else None
    if code is None:
        code = ErrorCode.INTERNAL_ERROR if exc.status_code >= 500 else ErrorCode.INVALID_REQUEST
    try:
        message = HTTPStatus(exc.status_code).phrase
    except ValueError:
        message = "Request failed."
    error = ErrorInfo(code=code, message=message)
    return _failed(exc.status_code, error, exc.headers)


async def _handle_unhandled(_request: Request, exc: Exception) -> JSONResponse:
    _log.error("unhandled_exception", exc_info=exc)
    error = ErrorInfo(code=ErrorCode.INTERNAL_ERROR, message=_GENERIC_INTERNAL_MESSAGE)
    return _failed(HTTPStatus.INTERNAL_SERVER_ERROR, error)


def install_error_handlers(app: FastAPI) -> None:
    """Make every error answer an `Envelope(status="failed")`."""
    app.add_exception_handler(ServiceError, _handle_service_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unhandled)
