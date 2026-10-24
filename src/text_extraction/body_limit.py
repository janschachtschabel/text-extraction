"""Inbound body limit of the public app: a body over the limit of its path is refused with 413
before it is read whole, parsed or authenticated (FastAPI reads and parses the body before it
runs any dependency, authentication included).

- A ``Content-Length`` over the limit is answered at once; the body is not read.
- Otherwise the bytes of the ``http.request`` messages are counted as the app reads them. The
  read that goes over the limit raises ``BodyTooLarge``, which the app's error handler turns into
  the 413 envelope; should it reach this middleware unhandled, the middleware answers it, as long
  as no response has started.

The 413 envelope has ``invalid_request``, the request's trace id and ``Connection: close``.
"""

from collections.abc import Mapping

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from text_extraction.errors import BodyTooLarge, body_too_large_response


def _declared_length(scope: Scope) -> int | None:
    """The largest ``Content-Length`` of the request, or None without a valid one."""
    lengths = [
        int(value)
        for name, value in scope.get("headers", [])
        if name == b"content-length" and value.isdigit()
    ]
    return max(lengths) if lengths else None


class BodyLimitMiddleware:
    """Pure ASGI middleware: ``path_limits`` maps exact paths to their limit in bytes; every
    other path gets ``default_limit``."""

    def __init__(
        self, app: ASGIApp, *, default_limit: int, path_limits: Mapping[str, int] | None = None
    ) -> None:
        self.app = app
        self._default_limit = default_limit
        self._path_limits = dict(path_limits or {})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._path_limits.get(scope["path"], self._default_limit)
        declared = _declared_length(scope)
        if declared is not None and declared > limit:
            await self._refuse(scope, receive, send, BodyTooLarge(limit))
            return

        received = 0
        started = False

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLarge(limit)
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except BodyTooLarge as error:
            if started:
                raise
            await self._refuse(scope, receive, send, error)

    @staticmethod
    async def _refuse(scope: Scope, receive: Receive, send: Send, error: BodyTooLarge) -> None:
        response = body_too_large_response(HTTPConnection(scope), error)
        await response(scope, receive, send)
