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
    ``www.`` dropped from the host. A URL that ``normalize_url`` refuses is its own key."""
    normal = normalize_url(url)
    if normal is None:
        return url
    rest = normal.split("://", 1)[1]
    return rest.removeprefix("www.")


def site_host(url: str) -> str:
    """The URL's host, lower case, without a leading ``www.``; ``""`` when it has none."""
    try:
        host = urlsplit(url.strip()).hostname or ""
    except ValueError:
        return ""
    return host.removeprefix("www.")


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
