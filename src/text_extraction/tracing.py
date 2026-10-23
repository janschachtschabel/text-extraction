"""Request trace context: read trace headers, bind them to every log line, log one access line."""

import re
import time
import uuid
from collections.abc import MutableMapping
from typing import Any

import structlog
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

TRACE_HEADER = "x-client-trace-id"
B3_HEADERS = ("b3", "x-b3-traceid", "x-b3-spanid", "x-b3-parentspanid", "x-b3-sampled")

_VALID_VALUE = re.compile(r"[A-Za-z0-9._:-]{1,128}")

_access_log = structlog.get_logger("text_extraction.access")


def _log_field(header: str) -> str:
    return header.replace("-", "_")


def _valid(value: str | None) -> str | None:
    return value if value is not None and _VALID_VALUE.fullmatch(value) else None


def _first_header_values(scope: Scope) -> dict[str, str]:
    """First value of each header, by lowercase name (ASGI header names are lowercase)."""
    values: dict[str, str] = {}
    for raw_name, raw_value in scope["headers"]:
        values.setdefault(raw_name.decode("latin-1"), raw_value.decode("latin-1"))
    return values


def current_trace_id() -> str | None:
    """Trace id bound to the current context, or None outside a traced request."""
    value = structlog.contextvars.get_contextvars().get("trace_id")
    return value if isinstance(value, str) else None


def request_log_context(connection: HTTPConnection) -> dict[str, str]:
    """Trace fields of a request, for code that runs outside `TraceMiddleware`'s context.

    Starlette runs the catch-all error handler in `ServerErrorMiddleware`, outside every
    user middleware, so the contextvars are already reset there. The middleware therefore
    also stores the values in the ASGI scope state, which the handlers can read.
    """
    state = connection.scope.get("state") or {}
    trace_id = state.get("trace_id")
    if not isinstance(trace_id, str):
        return {}
    fields: dict[str, str] = state.get("trace_fields", {})
    return {"trace_id": trace_id, **fields}


class TraceMiddleware:
    """Pure ASGI middleware: trace ids from request headers, structlog binding, access log.

    The trace headers are read and logged but never echoed back in the response.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = _first_header_values(scope)
        trace_id = _valid(headers.get(TRACE_HEADER)) or uuid.uuid4().hex
        b3_fields = {
            _log_field(name): value
            for name in B3_HEADERS
            if (value := _valid(headers.get(name))) is not None
        }

        state: MutableMapping[str, Any] = scope.setdefault("state", {})
        state["trace_id"] = trace_id
        state["trace_fields"] = b3_fields

        tokens = structlog.contextvars.bind_contextvars(trace_id=trace_id, **b3_fields)
        status = 500  # what the client gets if the app raises before it starts a response
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            principal = state.get("principal")
            _access_log.info(
                "request",
                method=scope["method"],
                path=scope["path"],
                status=status,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                trace_id=trace_id,
                **b3_fields,
                **({"principal": principal} if isinstance(principal, str) else {}),
            )
            structlog.contextvars.reset_contextvars(**tokens)
            structlog.contextvars.unbind_contextvars("principal")  # bound by `authenticate`
