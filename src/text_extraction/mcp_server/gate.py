"""The gate in front of the MCP app (ruling P10-S3): origin, key, and the whole body read first.

Pure ASGI. For every HTTP request to ``/mcp``, in order:

1. An ``Origin`` header whose value is not in ``MCP_ALLOWED_ORIGINS`` (exact match on scheme,
   host and port, in any case, a default port dropped: ``normalize_origin``) gets a 403 envelope
   ``forbidden``. A client without a browser sends no ``Origin``.
2. ``principal_for`` checks the key; a failure is the REST 401 envelope with
   ``WWW-Authenticate: Bearer``.
3. The principal goes into the scope state (``mcp_principal``, and its name as ``principal`` for
   the access line), and its name is bound to the log context.

The gate then reads the body (bounded by ``BodyLimitMiddleware``, which raises ``BodyTooLarge``
here, outside the MCP app, so a body over the limit is the REST 413 envelope however it arrives)
and replays it to the MCP app. The refusals are raised as ``ServiceError``; the public app's
handlers turn them into envelopes with the request's trace id.
"""

from collections.abc import MutableMapping
from typing import Any

import structlog
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from text_extraction.auth import principal_for
from text_extraction.envelope import ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.settings import Settings, normalize_origin

PRINCIPAL_STATE = "mcp_principal"


def _forbidden_origin() -> ServiceError:
    return ServiceError(ErrorCode.FORBIDDEN, "This origin may not call the MCP endpoint.", 403)


class McpGate:
    """Checks a request to ``/mcp`` before the MCP app (``app``) sees it."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self._settings = settings
        self._origins = frozenset(settings.mcp_allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if any(
            normalize_origin(origin) not in self._origins for origin in headers.getlist("origin")
        ):
            raise _forbidden_origin()
        principal = principal_for(self._settings, headers.get("authorization"))
        state: MutableMapping[str, Any] = scope.setdefault("state", {})
        state[PRINCIPAL_STATE] = principal
        state["principal"] = principal.name  # the access line (`TraceMiddleware`)
        structlog.contextvars.bind_contextvars(principal=principal.name)

        replay = await _read_body(receive)
        # A copy of the scope: the MCP app is a Starlette app of its own and writes its keys
        # (``app``, its exception handlers) into the scope. The state is the same dict.
        await self.app(dict(scope), replay, send)


async def _read_body(receive: Receive) -> Receive:
    """Read the request body whole and return a ``receive`` that hands it over in one message,
    then goes on with ``receive`` (a disconnect)."""
    chunks: list[bytes] = []
    last: Message | None = None
    while True:
        message = await receive()
        if message["type"] != "http.request":
            last = message  # the client is gone; the MCP app learns it after the body
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    pending: list[Message] = [
        {"type": "http.request", "body": b"".join(chunks), "more_body": False}
    ]
    if last is not None:
        pending.append(last)

    async def replay() -> Message:
        if pending:
            return pending.pop(0)
        return await receive()

    return replay
