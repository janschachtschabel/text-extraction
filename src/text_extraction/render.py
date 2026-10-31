"""What the pipeline needs from a browser, and what a render gives back.

The pipeline (``pipeline.py``) renders a page through a ``Renderer``; the browser worker gives it
one (``browser.py``, ``browser_unblock.py``). This module holds only the protocols and the data
they exchange, so nothing that imports it pulls in Playwright or Patchright.

A render runs in its host's outbound-gate turn, which the pipeline takes before the first
navigation and gives back when ``Renderer.render`` returns. Every main-frame navigation after the
first goes through the ``NavigationCheck`` before the browser may send it (docs/architecture.md,
"Browser worker", B2).
"""

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from text_extraction.browser_options import Action, ArtifactRef

# Called with the URL of a main-frame navigation after the first, before it is sent. Raises
# ``FetchError`` (a block, an invalid or private URL) or a gate refusal (``OutboundBusy``,
# ``CircuitOpen``); the renderer aborts the navigation and lets the error end the render.
NavigationCheck = Callable[[str], Awaitable[None]]

# A render that contacted one of these hosts and produced a thin page shows a bot challenge
# (B12): exact names, and hCaptcha with its subdomains. reCAPTCHA is left out: it sits on too
# many ordinary contact forms.
CHALLENGE_HOSTS = frozenset({"challenges.cloudflare.com", "geo.captcha-delivery.com"})
CHALLENGE_DOMAINS = frozenset({"hcaptcha.com"})

_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class CookieData:
    """A browser cookie. ``expires`` is Unix time, None for a session cookie. ``domain`` is
    as Chromium reports it: with a leading dot for a domain cookie (sent to the subdomains
    too), without for a host-only one; ``add_cookies`` reads it the same way."""

    name: str
    value: str = field(repr=False)
    domain: str
    path: str = "/"
    secure: bool = False
    http_only: bool = False
    expires: float | None = None


@dataclass(frozen=True)
class MainResponse:
    """One main-frame document answer.

    ``redirects`` are the URLs whose HTTP redirects led to ``url``, oldest first (the first is
    the URL the navigation asked for); empty when ``url`` answered directly. ``headers`` have
    lower-case names; ``content_type`` is the media type only, lower case."""

    url: str
    status: int
    headers: Mapping[str, str] = field(repr=False)
    content_type: str | None
    charset: str | None = None
    redirects: tuple[str, ...] = ()


@dataclass(frozen=True)
class RenderOptions:
    """How to render one page.

    ``cookies`` are set before the first navigation; their domains are resolved. ``headers``
    are added only to requests whose ``site`` equals ``header_host`` (B2 step 5), and never
    follow a redirect elsewhere: such a request is fetched through the page's proxy context
    without following redirects, a redirect of the main document becomes a navigation that is
    checked like any other (with the headers again only on that site), and any other redirect
    is followed by the browser without them. Resource types in ``blocked_resources`` are not
    loaded (B3). ``transcript_panel`` runs the steps that read YouTube's new transcript view
    (``browser_panel.py``) before the page is read."""

    actions: tuple[Action, ...] = field(default=(), repr=False)
    cookies: tuple[CookieData, ...] = field(default=(), repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    header_host: str = ""
    screenshot: bool = False
    full_page: bool = True
    allow_evaluate: bool = False
    unblock: bool = False
    blocked_resources: frozenset[str] = frozenset()
    transcript_panel: bool = False


@dataclass(frozen=True)
class Rendered:
    """What a render produced.

    ``responses`` holds every main-frame document answer, in order; the last one is the
    document ``html`` was read from. ``hosts`` are the hosts the page contacted (through the
    egress proxy). ``cookies`` are the target host's and its parent domain's, for the
    clearance reuse of unblock mode (B14). ``download_url`` is the URL of a download the main
    frame started (by a navigation, also a clicked link): the render then has no document; the
    pipeline may fetch the file directly (F18). ``transcript_panel`` is the raw body of the
    page's ``get_panel`` answer when the render was asked for it and it could be read (else the
    warning ``transcript_panel_unavailable``)."""

    html: str = field(repr=False)
    final_url: str
    responses: tuple[MainResponse, ...]
    screenshot: bytes | None = field(default=None, repr=False)
    screenshot_size: tuple[int, int] | None = None  # width, height in pixels
    warnings: tuple[str, ...] = ()
    hosts: frozenset[str] = frozenset()
    cookies: tuple[CookieData, ...] = field(default=(), repr=False)
    download_url: str | None = None
    transcript_panel: bytes | None = field(default=None, repr=False)


class Renderer(Protocol):
    """Renders a page in a browser."""

    @property
    def user_agent(self) -> str:
        """The User-Agent the browser sends."""
        ...

    async def render(
        self, url: str, check: NavigationCheck, options: RenderOptions, *, deadline: float
    ) -> Rendered:
        """Load ``url`` and return the rendered page. ``deadline`` is ``loop.time()`` based:
        the render returns (or raises ``FetchError`` with ``timeout``) before it. A failure of
        the target or the browser is a ``FetchError``; an error of ``check`` propagates.

        When a main-frame answer was seen before the failure, the ``FetchError`` carries its
        status as ``upstream_status``: the pipeline reports it to the outbound gate, so a 429
        or 503 still pauses the host. A ``timeout`` or ``connection_error`` counts towards the
        host's circuit breaker."""
        ...


class ArtifactUnavailable(Exception):  # noqa: N818 - the sink's refusal, not a bug
    """The sink cannot store a file now (Valkey's memory is high, or it is down)."""


class ArtifactSink(Protocol):
    """Stores the files of a job's item (the screenshot) for a while."""

    async def put_screenshot(self, png: bytes, width: int, height: int) -> ArtifactRef:
        """Store ``png`` and return where it can be fetched; raises ``ArtifactUnavailable``."""
        ...


class ClearanceStore(Protocol):
    """Cookies of a cleared bot challenge per host, reused by unblock renders only (B14)."""

    async def get(self, host: str) -> list[CookieData]: ...

    async def put(self, host: str, cookies: list[CookieData]) -> None: ...


def site(url: str) -> str:
    """``host:port`` of ``url``, lower case, with the scheme's default port filled in: the scope
    of the caller's headers (a second port on the same host is another site). Empty when the
    URL has no host or a bad port."""
    try:
        parts = urlsplit(url)
        port = parts.port or _DEFAULT_PORTS.get(parts.scheme.lower())
    except ValueError:
        return ""
    host = (parts.hostname or "").rstrip(".")
    if not host or port is None:
        return ""
    return f"{host}:{port}"


def contacted_challenge_host(hosts: Iterable[str]) -> bool:
    """Whether any of ``hosts`` is a challenge host (B12)."""
    for host in hosts:
        name = host.lower().rstrip(".")
        if name in CHALLENGE_HOSTS:
            return True
        if any(name == domain or name.endswith(f".{domain}") for domain in CHALLENGE_DOMAINS):
            return True
    return False
