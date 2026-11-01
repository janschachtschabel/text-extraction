"""The Host allowlist of the public app under ``AUTH_MODE=none`` (audit finding M3/S4).

Without keys every caller is an admin, so a web page that rebinds its own DNS name to the
service's address could drive the API and the UI from a visitor's browser: the browser sends the
page's host name in ``Host``. The service answers only the hosts of ``ALLOWED_HOSTS``
(``localhost``, ``127.0.0.1``, ``[::1]`` and ``api`` by default; ``*`` switches the check off);
any other ``Host`` is a 400 ``invalid_request`` envelope before the request reaches a route.
Under ``AUTH_MODE=api_key`` the check is not installed: a rebinding page has no key.
"""

from collections.abc import Iterable
from http import HTTPStatus

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from text_extraction.envelope import ErrorCode, ErrorInfo
from text_extraction.errors import failed_response

REFUSED_MESSAGE = "The Host header is not allowed on this deployment (ALLOWED_HOSTS)."


def host_of(header: str) -> str:
    """The host name of a ``Host`` header, lower case, without its port and a trailing dot;
    an IPv6 address keeps its brackets."""
    value = header.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[: end + 1] if end != -1 else value
    name, _, port = value.rpartition(":")
    if name and port.isdigit():
        value = name
    return value.rstrip(".")


class HostGuardMiddleware:
    """Pure ASGI middleware: refuse an HTTP or WebSocket request whose ``Host`` is not one of
    ``allowed`` (as ``host_of`` gives it)."""

    def __init__(self, app: ASGIApp, *, allowed: Iterable[str]) -> None:
        self.app = app
        self._allowed = frozenset(allowed)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        connection = HTTPConnection(scope)
        if host_of(connection.headers.get("host", "")) in self._allowed:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        error = ErrorInfo(code=ErrorCode.INVALID_REQUEST, message=REFUSED_MESSAGE)
        response = failed_response(connection, HTTPStatus.BAD_REQUEST, error)
        await response(scope, receive, send)
