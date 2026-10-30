"""Each tool call (rulings P10-S4, P10-S22): the caller, the rate limit, the tool, then one log
line and the metrics. Errors become results and never reach the client as exceptions.

The MCP app runs a call in a task of its own lifespan (stateless HTTP), not in the request's
task, so the request's log context (trace id, B3 fields, principal) is bound again here from the
request's scope state.
"""

import logging
import time
from abc import abstractmethod
from collections.abc import Iterable, Mapping
from typing import Any, ClassVar

import structlog
from fastapi.exceptions import RequestValidationError
from fastmcp import FastMCP
from fastmcp.exceptions import FastMCPError, NotFoundError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool, ToolResult
from mcp_types import CallToolRequestParams
from prometheus_client import CollectorRegistry, Counter, Histogram
from pydantic import BaseModel, PrivateAttr, ValidationError
from starlette.datastructures import State
from starlette.responses import Response

from text_extraction.auth import Principal
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.mcp_server.gate import PRINCIPAL_STATE
from text_extraction.mcp_server.results import envelope_result, error_result
from text_extraction.ratelimit import check_rate
from text_extraction.tracing import request_log_context

_log = structlog.get_logger(__name__)

# The ``tool`` label of a call whose name is no tool of the server: a caller's text is never a
# label.
UNKNOWN_TOOL = "unknown"
_OUTCOMES = ("success", "partial", "failed")
_SECONDS_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 25, 60, 120)


def _internal() -> ServiceError:
    return ServiceError(ErrorCode.INTERNAL_ERROR, "Internal server error.", 500)


def call_principal() -> Principal:
    """The caller of the current tool call, as the gate stored it; a missing one is
    ``internal_error`` (the gate always stores it, so it is our fault)."""
    try:
        request = get_http_request()
    except RuntimeError:
        raise _internal() from None
    principal = (request.scope.get("state") or {}).get(PRINCIPAL_STATE)
    if not isinstance(principal, Principal):
        raise _internal()
    return principal


def call_header(name: str) -> str | None:
    """A header of the HTTP request of the current tool call (``X-LLM-Api-Key``,
    ``X-ASR-Api-Key``), or None."""
    try:
        request = get_http_request()
    except RuntimeError:
        raise _internal() from None
    return request.headers.get(name)


class ToolCallError(FastMCPError):
    """A ``ModelTool`` failed; carries the error to the call guard. FastMCP passes a
    ``FastMCPError`` on unchanged and logs it at its ``log_level`` (debug, without a traceback),
    where it would log anything else with a traceback and mask it."""

    def __init__(self, error: Exception) -> None:
        super().__init__("The tool call failed.", log_level=logging.DEBUG)
        self.error = error


class ModelTool(Tool):
    """A tool whose arguments are a pydantic model (``args_model``). ``call`` runs it with the
    app state, the caller and the validated arguments, and answers an ``Envelope``, a
    ``Response`` whose body is one, or a ``ToolResult``. ``register`` binds the state.

    The tool runs no check of the call guard (rate limit) but every check of its route."""

    args_model: ClassVar[type[BaseModel]]
    _state: State | None = PrivateAttr(default=None)

    def bind(self, state: State) -> None:
        self._state = state

    @abstractmethod
    async def call(self, state: State, principal: Principal, args: BaseModel) -> object:
        """Run the tool."""

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            if self._state is None:
                raise _internal()
            # The settings bound some arguments (``max_chars`` by ``MCP_MAX_CONTENT_CHARS``).
            args = self.args_model.model_validate(
                arguments, context={"settings": self._state.settings}
            )
            return as_result(await self.call(self._state, call_principal(), args))
        except Exception as error:
            raise ToolCallError(error) from error


def as_result(answer: object) -> ToolResult:
    """A tool's answer as a result: a ``ToolResult`` as it is, an envelope or a response through
    ``envelope_result``."""
    if isinstance(answer, ToolResult):
        return answer
    if isinstance(answer, Envelope | Response):
        return envelope_result(answer)
    raise TypeError(f"A tool answered {type(answer).__name__}.")


def register(server: FastMCP[Any], state: State, tool: ModelTool) -> None:
    """Add ``tool`` to ``server``, bound to the public app's ``state``."""
    tool.bind(state)
    server.add_tool(tool)


class ToolMetrics:
    """``text_extraction_mcp_tool_calls_total{tool, outcome}`` and
    ``text_extraction_mcp_tool_seconds{tool}``."""

    def __init__(self, registry: CollectorRegistry) -> None:
        self.calls = Counter(
            "text_extraction_mcp_tool_calls_total",
            "MCP tool calls by tool and outcome (success, partial, failed, job or the error code "
            "of a refusal).",
            ["tool", "outcome"],
            registry=registry,
        )
        self.seconds = Histogram(
            "text_extraction_mcp_tool_seconds",
            "Seconds of one MCP tool call, the rate limit check included.",
            ["tool"],
            buckets=_SECONDS_BUCKETS,
            registry=registry,
        )

    def export(self, tools: Iterable[str]) -> None:
        """Export each tool with ``success`` at 0 from the start."""
        for tool in tools:
            self.calls.labels(tool, "success")
            self.seconds.labels(tool)


def outcome_of(result: ToolResult) -> tuple[str, str | None]:
    """The ``outcome`` label of a result and the id of the job it answered, if any: a job it
    started, read or deleted."""
    envelope: Mapping[str, Any] = result.structured_content or {}
    if result.is_error:
        error = envelope.get("error")
        code = error.get("code") if isinstance(error, Mapping) else None
        return (code if code in set(ErrorCode) else ErrorCode.INTERNAL_ERROR.value), None
    data = envelope.get("data")
    # A job's answer (accepted, its view, its deletion) has its ``id`` and ``kind``.
    job_id = data.get("id") if isinstance(data, Mapping) and "kind" in data else None
    job_id = job_id if isinstance(job_id, str) else None
    if isinstance(data, Mapping) and isinstance(data.get("status_url"), str):
        return "job", job_id
    status = envelope.get("status")
    return (status if status in _OUTCOMES else "success"), job_id


class CallGuard(Middleware):
    """FastMCP middleware around every ``tools/call``: one ``check_rate`` hit (``initialize``,
    ``tools/list`` and notifications are free), the tool, the log line ``mcp_tool_call`` and the
    metrics. Every error becomes an error result (``error_result``)."""

    def __init__(self, state: State, metrics: ToolMetrics) -> None:
        self._state = state
        self._metrics = metrics

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        started = time.perf_counter()
        tool = UNKNOWN_TOOL
        with structlog.contextvars.bound_contextvars(**_log_context()):
            try:
                tool = await _tool_label(context)
                await check_rate(self._state, call_principal())
                result = await call_next(context)
            except Exception as error:
                result = self._failed(tool, error)
            seconds = time.perf_counter() - started
            outcome, job_id = outcome_of(result)
            self._metrics.calls.labels(tool, outcome).inc()
            self._metrics.seconds.labels(tool).observe(seconds)
            _log.info(
                "mcp_tool_call",
                tool=tool,
                outcome=outcome,
                duration_ms=round(seconds * 1000, 3),
                **({"job_id": job_id} if job_id is not None else {}),
            )
        return result

    @staticmethod
    def _failed(tool: str, error: Exception) -> ToolResult:
        if isinstance(error, ToolCallError):
            error = error.error
        if isinstance(error, NotFoundError):
            error = ServiceError(ErrorCode.NOT_FOUND, "Unknown tool.", 404)
        if not isinstance(error, ServiceError | ValidationError | RequestValidationError):
            _log.error("mcp_tool_failed", tool=tool, exc_info=error)
        return error_result(error)


async def _tool_label(context: MiddlewareContext[CallToolRequestParams]) -> str:
    """The ``tool`` label of a call: its name when the server has such a tool, else
    ``UNKNOWN_TOOL``."""
    server = context.fastmcp_context.fastmcp if context.fastmcp_context else None
    known = server is not None and await server.get_tool(context.message.name) is not None
    return context.message.name if known else UNKNOWN_TOOL


def _log_context() -> dict[str, str]:
    """The trace fields and the principal of the current call's request."""
    try:
        request = get_http_request()
    except RuntimeError:
        return {}
    fields = request_log_context(request)
    principal = (request.scope.get("state") or {}).get("principal")
    if isinstance(principal, str):
        fields["principal"] = principal
    return fields
