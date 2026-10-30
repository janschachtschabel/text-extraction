"""The UI's in-process forward to the REST routes (D5 a, ruling P10-S14): a ``/ui/run/<op>``
route sends its REST request through httpx's ASGI transport to the app itself, with no network,
so every REST check (body limits, validation, auth, roles, rate limit, the 202 hand-over) runs
exactly as for any client."""

from dataclasses import dataclass
from typing import Any, Final, Literal

import httpx
from starlette.requests import Request

from text_extraction.tracing import B3_HEADERS, TRACE_HEADER, current_trace_id

# Only these headers of the browser's request reach the REST route: the keys (sent by ui.js as
# headers, never as cookies or fields) and the B3 trace context. ``x-client-trace-id`` is set to
# the outer request's trace id, so both requests log under one id.
PASSED_HEADERS: Final = ("authorization", "x-llm-api-key", "x-asr-api-key", *B3_HEADERS)
BASE_URL: Final = "http://ui.internal"

Method = Literal["GET", "POST", "DELETE"]


@dataclass(frozen=True)
class Forwarded:
    """What the REST route answered: its status, its JSON object (None for any other body),
    the raw bytes and the content type."""

    status: int
    body: dict[str, Any] | None
    content: bytes
    content_type: str


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
    (``wait_seconds``, the hand-over to a job). An exception of the app is its 500 answer."""
    transport = httpx.ASGITransport(app=request.app, raise_app_exceptions=False)
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
    return Forwarded(
        status=response.status_code,
        body=_json_object(response),
        content=response.content,
        content_type=response.headers.get("content-type", ""),
    )
