"""The UI's in-process forward to the REST routes (D5 a, ruling P10-S14): a ``/ui/run/<op>``
route sends its REST request through httpx's ASGI transport to the app itself, with no network,
so every REST check (body limits, validation, auth, roles, rate limit, the 202 hand-over) runs
exactly as for any client."""

from dataclasses import dataclass
from typing import Any, Final, Literal

import httpx
import structlog
from starlette.requests import Request

from text_extraction.tracing import B3_HEADERS, TRACE_HEADER, current_trace_id

# Only these headers of the browser's request reach the REST route: the keys (sent by ui.js as
# headers, never as cookies or fields) and the B3 trace context. ``x-client-trace-id`` is set to
# the outer request's trace id, so both requests log under one id.
PASSED_HEADERS: Final = ("authorization", "x-llm-api-key", "x-asr-api-key", *B3_HEADERS)
BASE_URL: Final = "http://ui.internal"
API_PREFIX: Final = "/v1/"

Method = Literal["GET", "POST", "DELETE"]


@dataclass(frozen=True)
class Forwarded:
    """What the REST route answered: its status, its JSON object (None for any other body),
    the raw bytes and the content type."""

    status: int
    body: dict[str, Any] | None
    content: bytes
    content_type: str


# Percent-encoded dot, slash and backslash: a path that holds one is refused, so no segment can
# turn into a dot segment or a separator after decoding.
_ENCODED_SEPARATORS: Final = ("%2e", "%2f", "%5c")


def checked_path(path: str) -> str:
    """``path`` when it is a plain relative path under ``/v1/``: printable ASCII without a
    backslash, ``?``, ``#``, an empty or dot segment or an encoded dot or separator. Anything
    else (an absolute URL, a scheme or host, ``..``) raises `ValueError`: query parameters go
    through ``params``, and the path never leaves the REST API."""
    if not path.startswith(API_PREFIX) or not all(" " < char < "\x7f" for char in path):
        raise ValueError("forward takes a relative path under /v1/")
    if any(char in path for char in "\\?#") or any(
        encoded in path.lower() for encoded in _ENCODED_SEPARATORS
    ):
        raise ValueError("forward takes no backslash, query, fragment or encoded separator")
    segments = path[1:].split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise ValueError("forward takes no empty or dot segment")
    return path


def _outer_trace_id(request: Request) -> str | None:
    state = request.scope.get("state") or {}
    trace_id = state.get("trace_id")
    return trace_id if isinstance(trace_id, str) else current_trace_id()


def _forwarded_headers(request: Request) -> dict[str, str]:
    headers = {
        name: value for name in PASSED_HEADERS if (value := request.headers.get(name)) is not None
    }
    trace_id = _outer_trace_id(request)
    if trace_id is not None:
        headers[TRACE_HEADER] = trace_id
    # The outer request's Host, which the Host allowlist has passed (M3/S4): the inner request
    # meets the same check under AUTH_MODE=none.
    if (host := request.headers.get("host")) is not None:
        headers["host"] = host
    return headers


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    if not response.headers.get("content-type", "").startswith("application/json"):
        return None
    try:
        parsed = response.json()
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def forward(
    request: Request,
    method: Method,
    path: str,
    *,
    json: Any = None,
    data: Any = None,
    files: Any = None,
    params: Any = None,
) -> Forwarded:
    """Send ``method path`` to the app of ``request`` in-process, with only the headers of
    ``PASSED_HEADERS``, and return its answer. No timeout of its own: the route bounds its work
    (``wait_seconds``, the hand-over to a job). An exception of the app is its 500 answer.
    ``path`` must pass `checked_path` (else `ValueError`, before anything is sent).

    The inner request runs in this task, and its ``TraceMiddleware`` unbinds the principal and
    resets its trace fields when it ends: the outer request's log context is bound again
    afterwards, so its later log lines keep them."""
    path = checked_path(path)
    transport = httpx.ASGITransport(app=request.app, raise_app_exceptions=False)
    outer_context = structlog.contextvars.get_contextvars()
    try:
        # No timeout of the client's own: the route bounds its work.
        async with httpx.AsyncClient(
            transport=transport,
            base_url=BASE_URL,
            timeout=None,  # noqa: S113
        ) as client:
            response = await client.request(
                method,
                path,
                json=json,
                data=data,
                files=files,
                params=params,
                headers=_forwarded_headers(request),
            )
    finally:
        structlog.contextvars.bind_contextvars(**outer_context)
    return Forwarded(
        status=response.status_code,
        body=_json_object(response),
        content=response.content,
        content_type=response.headers.get("content-type", ""),
    )
