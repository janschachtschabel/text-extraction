"""Security of the UI (rulings P10-S12 to P10-S14): the headers of every ``/ui`` answer, the guard
of ``/ui/run/*`` (htmx and the page's own origin), the caller of the reading views and the body
limits of the ``/ui/run`` routes. No ``jinja2`` here."""

from collections.abc import Iterable
from typing import Final
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from text_extraction.auth import Principal, authenticate
from text_extraction.file_api import text_body_limit, upload_body_limit
from text_extraction.jobs_api import batch_body_limit
from text_extraction.ratelimit import check_rate
from text_extraction.settings import Settings

PREFIX: Final = "/ui"
RUN_PREFIX: Final = "/ui/run"
STATIC_PREFIX: Final = "/ui/static"

CONTENT_SECURITY_POLICY: Final = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)
SECURITY_HEADERS: Final[tuple[tuple[str, str], ...]] = (
    ("content-security-policy", CONTENT_SECURITY_POLICY),
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "no-referrer"),
    ("x-frame-options", "DENY"),
    ("cross-origin-opener-policy", "same-origin"),
    ("cross-origin-resource-policy", "same-origin"),
    ("permissions-policy", "camera=(), microphone=(), geolocation=()"),
)
NO_STORE: Final = "no-store"
STATIC_CACHE: Final = "public, max-age=3600"
# Replaced on every /ui answer; the UI never sets a cookie (P10-S13).
_REPLACED: Final = frozenset(
    {name.encode() for name, _ in SECURITY_HEADERS} | {b"cache-control", b"set-cookie"}
)
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
REFUSED_TEXT: Final = "This request must come from the UI page itself."
# Bytes around the batch file of /ui/run/batch-file: the multipart framing and the batch form's
# other fields (format and options).
_BATCH_FILE_ALLOWANCE: Final = 65_536


def is_ui_path(path: str) -> bool:
    return path == PREFIX or path.startswith(f"{PREFIX}/")


def _ui_headers(raw: Iterable[tuple[bytes, bytes]], *, cache: str) -> list[tuple[bytes, bytes]]:
    headers = [(name, value) for name, value in raw if name.lower() not in _REPLACED]
    headers.extend((name.encode(), value.encode()) for name, value in SECURITY_HEADERS)
    headers.append((b"cache-control", cache.encode()))
    return headers


class UiHeadersMiddleware:
    """Pure ASGI middleware: every answer under ``/ui`` gets the CSP and the other security
    headers of P10-S12 and ``Cache-Control: no-store`` (``public, max-age=3600`` for a static
    file that was found); a ``Set-Cookie`` is dropped. A request to ``/ui/run/*`` that is not
    `from_the_page` is answered 403 (plain text) here, before anything reads its body. An
    exception that no handler answered before the response started becomes a plain 500 with
    these headers here (and is raised on, so the app's handler still logs it), since
    Starlette's own 500 is sent outside every middleware."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_ui_path(scope["path"]):
            await self.app(scope, receive, send)
            return
        static = scope["path"].startswith(f"{STATIC_PREFIX}/")
        started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                found = message["status"] in (200, 206, 304)
                cache = STATIC_CACHE if static and found else NO_STORE
                headers = _ui_headers(message.get("headers", []), cache=cache)
                message = {**message, "headers": headers}
            await send(message)

        if is_run_path(scope["path"]) and not from_the_page(Headers(scope=scope)):
            # Before the body limit, the routing and any body parsing: nothing is read.
            refused = PlainTextResponse(REFUSED_TEXT, status_code=403)
            await refused(scope, receive, send_with_headers)
            return
        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            if not started:
                response = PlainTextResponse("Internal server error.", status_code=500)
                await response(scope, receive, send_with_headers)
            raise


class UiRefused(Exception):  # noqa: N818 - a refusal, answered as a plain 403
    """A request to ``/ui/run/*`` that is not an htmx request of the page's own origin."""


async def ui_refused_response(request: Request, error: Exception) -> Response:
    """The plain-text 403 of `UiRefused` (registered on the app while the UI is on)."""
    return PlainTextResponse(REFUSED_TEXT, status_code=403)


def _host_and_port(netloc: str, scheme: str | None) -> tuple[str, int | None] | None:
    """``netloc``'s lower-case host and its port, None for the scheme's default port (or, for
    a ``Host`` header, which has no scheme, for 80 and 443: a proxy may end TLS). None when it is
    not a plain host and port."""
    try:
        parts = urlsplit(f"//{netloc}")
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname or parts.path or parts.query or parts.fragment or parts.username:
        return None
    defaults = {_DEFAULT_PORTS[scheme]} if scheme is not None else set(_DEFAULT_PORTS.values())
    return parts.hostname, None if port in defaults else port


def same_origin(origin: str, host: str | None) -> bool:
    """Whether the ``Origin`` header ``origin`` names the host and port of the ``Host`` header
    ``host`` (a default port counts as none; the scheme is not compared, as TLS may end at a
    proxy)."""
    if host is None:
        return False
    parts = urlsplit(origin)
    if parts.scheme not in _DEFAULT_PORTS or parts.path or parts.query or parts.fragment:
        return False
    theirs = _host_and_port(parts.netloc, parts.scheme)
    ours = _host_and_port(host, None)
    return theirs is not None and theirs == ours


def from_the_page(headers: Headers) -> bool:
    """Whether a request to ``/ui/run/*`` is an htmx request of the page's own origin (P10-S13):
    ``HX-Request: true``, and no ``Origin`` or one that names the ``Host`` header's host and port.
    A cross-site page cannot send ``HX-Request``: the header needs a CORS preflight, and the
    service answers none."""
    if headers.get("hx-request") != "true":
        return False
    origin = headers.get("origin")
    return origin is None or same_origin(origin, headers.get("host"))


def is_run_path(path: str) -> bool:
    return path == RUN_PREFIX or path.startswith(f"{RUN_PREFIX}/")


async def require_htmx(request: Request) -> None:
    """Dependency of every ``/ui/run/*`` route: refuse (403, plain text) a request that is not
    `from_the_page`. `UiHeadersMiddleware` refuses such a request already, before any body is
    read or a route runs (FastAPI parses ``Form`` and ``File`` parameters before dependencies);
    this repeats the check for a route reached some other way."""
    if not from_the_page(request.headers):
        raise UiRefused


async def ui_principal(request: Request) -> Principal:
    """The caller of a UI view that reads through the route cores (P10-S14): the key is checked
    as REST checks it (401 `ServiceError`), and the view counts one rate-limit hit (429 / 503),
    however many reads it combines. The view checks the role a core needs."""
    principal = await authenticate(request)
    await check_rate(request.app.state, principal)
    return principal


def form_part_limit(settings: Settings, path: str) -> int:
    """The largest field of a ``/ui/run`` route's form: the route's whole body limit (Starlette's
    default of 1 MiB would refuse a text or URL list the REST route takes)."""
    return run_body_limits(settings).get(path, settings.request_max_body_bytes)


def run_body_limits(settings: Settings) -> dict[str, int]:
    """The body limits of the ``/ui/run`` routes that take more than ``REQUEST_MAX_BODY_BYTES``
    (P10-S14): a file as the upload route takes it, a text to chunk as the chunk route, a batch
    file, and the URLs of as many batches as a caller may have open."""
    return {
        f"{RUN_PREFIX}/file": upload_body_limit(settings),
        f"{RUN_PREFIX}/chunk": text_body_limit(settings),
        f"{RUN_PREFIX}/batch-file": settings.ui_batch_file_max_bytes + _BATCH_FILE_ALLOWANCE,
        f"{RUN_PREFIX}/batch-urls": settings.batch_max_open_per_client * batch_body_limit(settings),
    }
