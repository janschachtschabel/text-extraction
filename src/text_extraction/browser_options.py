"""Browser options of a request (page actions, cookies, extra headers) and the artifact
reference of a result.

These are the parts of ``ExtractRequest`` and ``ScreenshotRequest`` that only the browser can
use. They may carry secrets (a session cookie, a token header), so the models keep them out of
``repr``. ``browser_models`` re-exports everything here; this module exists because
``extract_models`` needs these types and ``browser_models`` needs ``extract_models``.
"""

import ipaddress
import re
from datetime import datetime
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_ACTIONS = 20
MAX_COOKIES = 50
MAX_HEADERS = 20
MAX_HEADER_VALUE_LENGTH = 4096

Selector = Annotated[str, Field(min_length=1, max_length=500)]


class _Action(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class ClickAction(_Action):
    """Click the first element that matches ``selector``."""

    type: Literal["click"] = "click"
    selector: Selector


class ScrollAction(_Action):
    """Scroll down one viewport, ``times`` times."""

    type: Literal["scroll"] = "scroll"
    times: int = Field(default=1, ge=1, le=50)
    pause_ms: int = Field(default=500, ge=0, le=5000)


class WaitForAction(_Action):
    """Wait for an element to show up (``selector``) or for a fixed time (``ms``)."""

    type: Literal["wait_for"] = "wait_for"
    selector: Selector | None = None
    ms: int | None = Field(default=None, ge=0, le=10_000)

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.selector is None) == (self.ms is None):
            raise ValueError("wait_for needs exactly one of selector and ms")
        return self


class TypeAction(_Action):
    """Type ``text`` into the element that matches ``selector``, replacing its value."""

    type: Literal["type"] = "type"
    selector: Selector
    text: str = Field(max_length=1000, repr=False)


class PressAction(_Action):
    """Press a key (for example ``Enter``), on ``selector`` or on the page."""

    type: Literal["press"] = "press"
    key: str = Field(min_length=1, max_length=32)
    selector: Selector | None = None


class SelectAction(_Action):
    """Choose the option ``value`` of the ``<select>`` that matches ``selector``."""

    type: Literal["select"] = "select"
    selector: Selector
    value: str = Field(max_length=500, repr=False)


class ExpandAllAction(_Action):
    """Open collapsed sections: ``<details>`` and elements with ``aria-expanded="false"``."""

    type: Literal["expand_all"] = "expand_all"


class InfiniteScrollAction(_Action):
    """Scroll to the bottom until the page stops growing, at most ``max_scrolls`` times."""

    type: Literal["infinite_scroll"] = "infinite_scroll"
    max_scrolls: int = Field(default=10, ge=1, le=50)
    pause_ms: int = Field(default=1000, ge=0, le=5000)


class AcceptCookiesAction(_Action):
    """Accept the page's cookie banner."""

    type: Literal["accept_cookies"] = "accept_cookies"


class EvaluateAction(_Action):
    """Run JavaScript in the page. Admins only (the API enforces it)."""

    type: Literal["evaluate"] = "evaluate"
    script: str = Field(min_length=1, max_length=10_000, repr=False)


Action = Annotated[
    ClickAction
    | ScrollAction
    | WaitForAction
    | TypeAction
    | PressAction
    | SelectAction
    | ExpandAllAction
    | InfiniteScrollAction
    | AcceptCookiesAction
    | EvaluateAction,
    Field(discriminator="type"),
]


_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
# RFC 6265 cookie-value: cookie-octets (printable ASCII but space, DQUOTE, comma, semicolon and
# backslash), optionally in one pair of double quotes. Chromium refuses anything else.
_COOKIE_OCTETS = r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]*"
_COOKIE_VALUE = re.compile(f'{_COOKIE_OCTETS}|"{_COOKIE_OCTETS}"')
_COOKIE_NAME_MESSAGE = "a cookie name must be a token (letters, digits and !#$%&'*+-.^_`|~)"
_COOKIE_VALUE_MESSAGE = (
    "a cookie value may hold printable ASCII except space, double quote, comma, semicolon and "
    "backslash, optionally in double quotes (RFC 6265)"
)


class BrowserCookie(BaseModel, extra="forbid", hide_input_in_errors=True):
    """A cookie to set before the page loads. ``domain`` defaults to the request URL's host.
    ``name`` is a token and ``value`` follows RFC 6265, as Chromium requires; an error message
    never shows the value."""

    name: str = Field(min_length=1, max_length=256)
    value: str = Field(max_length=4096, repr=False)
    domain: str | None = Field(default=None, min_length=1, max_length=253)
    path: str = Field(default="/", min_length=1, max_length=1024)
    secure: bool = False
    http_only: bool = False

    @field_validator("name")
    @classmethod
    def _token_name(cls, name: str) -> str:
        if not _TOKEN.fullmatch(name):
            raise ValueError(_COOKIE_NAME_MESSAGE)
        return name

    @field_validator("value")
    @classmethod
    def _cookie_octets(cls, value: str) -> str:
        if not _COOKIE_VALUE.fullmatch(value):
            raise ValueError(_COOKIE_VALUE_MESSAGE)
        return value


_PRINTABLE_ASCII = re.compile(r"[\x20-\x7e]*")
# Set by the browser itself or by the connection; a caller must not override them.
_REFUSED_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "transfer-encoding",
        "te",
        "upgrade",
        "cookie",
        "accept-encoding",
        "user-agent",
    }
)


def validate_headers(headers: dict[str, str]) -> dict[str, str]:
    """Check the extra request headers and return them with lower-case names."""
    if len(headers) > MAX_HEADERS:
        raise ValueError(f"at most {MAX_HEADERS} headers are allowed")
    checked: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if not _TOKEN.fullmatch(name):
            raise ValueError("a header name must be a token (letters, digits and !#$%&'*+-.^_`|~)")
        if lowered in _REFUSED_HEADERS or lowered.startswith("proxy-"):
            raise ValueError(f"the header {lowered} is not allowed")
        if lowered in checked:
            raise ValueError(f"the header {lowered} is given twice")
        if len(value) > MAX_HEADER_VALUE_LENGTH:
            raise ValueError(f"a header value is at most {MAX_HEADER_VALUE_LENGTH} characters")
        if not _PRINTABLE_ASCII.fullmatch(value):
            raise ValueError("a header value must be printable ASCII")
        checked[lowered] = value
    return checked


Headers = Annotated[dict[str, str], AfterValidator(validate_headers)]


def check_cookie_domains(url: str, cookies: list[BrowserCookie]) -> None:
    """A cookie may only name the URL's host or a parent domain of it, never a bare public
    suffix (one label such as ``org``), so a request cannot plant cookies for other sites."""
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        host = ""
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    for cookie in cookies:
        if cookie.domain is None:
            continue
        domain = cookie.domain.lower().strip(".")
        parent = "." in domain and host.endswith(f".{domain}") and not is_ip
        if not host or not (domain == host or parent):
            raise ValueError("a cookie domain must be the URL's host or a parent domain of it")


# The limits that ``ExtractRequest`` and ``ScreenshotRequest`` share; each model adds its default,
# ``repr=False`` and its own description.
ActionList = Annotated[list[Action], Field(max_length=MAX_ACTIONS)]
CookieList = Annotated[list[BrowserCookie], Field(max_length=MAX_COOKIES)]
WaitSeconds = Annotated[float | None, Field(ge=0, le=60)]

ACTIONS_DESCRIPTION = (
    "Page actions to run in the browser after the page has loaded, in order, at most 20. The "
    "first action that fails stops the rest and adds the warning `action_<index>_failed`, the "
    "index counting from 0; the page is then read as it is. A click that leads to a page that "
    "fails to load (or that the compliance checks refuse) fails the whole render with that "
    "page's error. A click that starts a download discards the page: the render ends without "
    "content. `evaluate` runs JavaScript and is for admins only (403 `forbidden` for other "
    "keys). Action text, values and scripts are never logged or shown in a job."
)
COOKIES_DESCRIPTION = (
    "Cookies to set before the page loads, at most 50. A `domain` must be the URL's host or a "
    "parent domain of it. `name` is a token and `value` follows RFC 6265 (printable ASCII "
    "except space, double quote, comma, semicolon and backslash, optionally in double quotes); "
    "anything else answers 422. Never logged or shown in a job."
)
HEADERS_DESCRIPTION = (
    "Extra request headers, at most 20 (for example `Authorization` for a partner's logged-in "
    "pages). They are sent only to the requested URL's host and port, never to other sites, "
    "also not across a redirect. Never logged or shown in a job."
)


class BrowserSecrets(BaseModel, hide_input_in_errors=True):
    """The parts of a request that may carry secrets (session cookies, token headers); they stay
    out of ``repr``."""

    actions: list[Action] = Field(default=[], max_length=MAX_ACTIONS, repr=False)
    cookies: list[BrowserCookie] = Field(default=[], max_length=MAX_COOKIES, repr=False)
    headers: Headers = Field(default={}, repr=False)

    def empty(self) -> bool:
        return not (self.actions or self.cookies or self.headers)


class ArtifactRef(BaseModel):
    """A file the service keeps for a while (a screenshot) and where to fetch it."""

    name: str
    url: str
    content_type: str
    bytes: int
    width: int
    height: int
    expires_at: datetime
