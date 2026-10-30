"""URLs, seen keys and the crawl's scope (ruling P9-S4). Pure: no fetch, no Valkey.

A crawl keeps one item per page. ``normalize_url`` is the form an item's URL is stored in;
``seen_key`` is what makes two URLs the same page (http and https, with and without ``www.``);
``SiteScope`` and ``in_scope`` say which links the crawl follows (D6 a: the start page's host, a
leading ``www.`` ignored, and with ``scope: path`` only paths under the start URL's directory).
"""

from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from text_extraction.netguard import MAX_URL_LENGTH, InvalidUrlError, validate_url
from text_extraction.site.types import SiteScope

__all__ = ["in_scope", "normalize_url", "scope_for", "seen_key", "site_host"]

_DEFAULT_PORTS = {"http": 80, "https": 443}
# DNS limits (RFC 1035); a Unicode host over them cannot have an ASCII form within them.
_MAX_HOST_CHARS = 253
_MAX_LABEL_CHARS = 63


def normalize_url(url: str) -> str | None:
    """The URL in the form a crawl stores it, or None when it is no absolute http(s) URL.

    Scheme and host lower case, the default port dropped, the fragment dropped and an empty
    path ``/``; the query and the percent-encoding stay as given. None when the URL (or its
    normal form) is longer than ``MAX_URL_LENGTH``, cannot be parsed, has user information or a
    port outside 1 to 65535.
    """
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = parts.hostname  # lower case, IPv6 brackets removed
    if scheme not in _DEFAULT_PORTS or not host or "@" in parts.netloc:
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"
    normal = urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))
    if not parts.query and "?" in url.partition("#")[0]:
        normal += "?"  # an empty query is kept as given (urlunsplit drops it)
    try:
        return validate_url(normal)  # the same rules as a fetch (length, port range)
    except InvalidUrlError:
        return None


def seen_key(url: str) -> str:
    """What makes two URLs one page: the normal form without its scheme and with a leading
    ``www.`` dropped from the host (its IDN and trailing-dot spellings folded as by
    ``site_host``). A URL that ``normalize_url`` refuses is its own key."""
    normal = normalize_url(url)
    if normal is None:
        return url
    parts = urlsplit(normal)
    host = parts.hostname or ""
    netloc = parts.netloc
    if ":" not in host:  # an IPv6 address has no IDN or trailing-dot form
        port = netloc[len(host) :]  # "" or ":<port>"
        netloc = _ascii_host(host) + port
    rest = netloc + normal.split("://", 1)[1][len(parts.netloc) :]
    return rest.removeprefix("www.")


def _ascii_host(host: str) -> str:
    """One spelling per host: a trailing dot dropped and an IDN in its ASCII (punycode) form,
    so ``Bücher.example.`` and ``xn--bcher-kva.example`` are one host. A host the ``idna``
    codec cannot encode (a label over 63 characters, a host over 253, an empty label) stays as
    given. The lengths are checked first: the codec punycodes a whole label before it checks
    its length, so a page could otherwise spend milliseconds per link on hosts that have no
    ASCII form."""
    host = host.rstrip(".")
    if host.isascii():
        return host.lower()
    if len(host) > _MAX_HOST_CHARS or any(
        len(label) > _MAX_LABEL_CHARS for label in host.split(".")
    ):
        return host
    try:
        return _idna(host)
    except UnicodeError:
        return host


def _idna(host: str) -> str:
    return host.encode("idna").decode("ascii").lower()


def site_host(url: str) -> str:
    """The URL's host, lower case, without a trailing dot and a leading ``www.``, an IDN in its
    ASCII form; ``""`` when it has none."""
    try:
        host = urlsplit(url.strip()).hostname or ""
    except ValueError:
        return ""
    if ":" in host:  # IPv6
        return host
    return _ascii_host(host).removeprefix("www.")


def _path(url: str) -> str:
    try:
        return urlsplit(url.strip()).path or "/"
    except ValueError:
        return "/"


def scope_for(start_url: str, final_url: str | None, mode: Literal["host", "path"]) -> SiteScope:
    """The scope the start page sets, from its ``final_url`` (after redirects), else its URL.
    With ``mode == "path"`` the prefix is the start path up to and including its last ``/``."""
    base = final_url or start_url
    prefix = None
    if mode == "path":
        path = _path(base)
        prefix = path[: path.rfind("/") + 1] or "/"
    return SiteScope(host=site_host(base), path_prefix=prefix)


def in_scope(url: str, scope: SiteScope) -> bool:
    """The URL is on the scope's host (a leading ``www.`` ignored) and, with a prefix, under
    it."""
    if site_host(url) != scope.host:
        return False
    return scope.path_prefix is None or _path(url).startswith(scope.path_prefix)
