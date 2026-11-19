"""The feeds and CMS APIs a start page names (package 26). Pure: no fetch, no Valkey.

``detect_site_apis`` reads the page's head ``<link>`` elements and its ``Link`` response header
for the WordPress REST API root (``rel="https://api.w.org/"``), RSS and Atom feeds
(``rel="alternate"`` with a feed type), and the markers of a Drupal site. Only addresses on the
page's own site host count.
"""

import html
import json
import re
from collections.abc import Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit

from bs4 import BeautifulSoup, Tag

from text_extraction.convert.links import absolute_url, base_url_of
from text_extraction.convert.markup import attr
from text_extraction.page_types import PageType, _fold, post_type_hint
from text_extraction.site.sitemaps import ParsedSitemap
from text_extraction.site.types import ApiFormat, SiteApis, SitemapChild, SitemapEntry
from text_extraction.site.urls import normalize_url, site_host

__all__ = [
    "API_MAX_BYTES",
    "DRUPAL_PAGE_LIMIT",
    "EXCLUDED_WP_TYPES",
    "MAX_API_TYPES",
    "MAX_FEEDS",
    "WP_PER_PAGE",
    "api_type_hint",
    "detect_site_apis",
    "parse_drupal_index",
    "parse_drupal_nodes",
    "parse_wordpress_posts",
    "parse_wordpress_types",
    "path_matches",
    "wordpress_list_url",
    "wordpress_types_url",
]

# The most bytes of one API answer that are read: it is parsed whole (JSON has no partial
# read), so a larger answer fails ``too_large`` before it is parsed.
API_MAX_BYTES = 4 * 1024 * 1024
MAX_FEEDS = 3
WP_PER_PAGE = 100
WP_NAMESPACE = "wp/v2"
DRUPAL_PAGE_LIMIT = 50
MAX_API_TYPES = 50
EXCLUDED_WP_TYPES = frozenset(
    {
        "attachment",
        "nav_menu_item",
        "wp_block",
        "wp_template",
        "wp_template_part",
        "wp_navigation",
        "wp_global_styles",
        "wp_font_family",
        "wp_font_face",
    }
)
_TAG = re.compile(r"<[^>]*>")
_MIN_PREFIX_MATCH = 5
_WORDPRESS_REL = "https://api.w.org/"
_REST_ROUTE = "rest_route"
# A REST namespace (``wp/v2``, ``lms/v1``): path segments, none of them ``.`` or ``..``.
_NAMESPACE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)*")
_FEED_TYPES = frozenset({"application/rss+xml", "application/atom+xml"})
_LINK_HEADER_ITEM = re.compile(r"<([^>]*)>([^,<]*)")
_REL_PARAM = re.compile(r'rel\s*=\s*(?:"([^"]*)"|([^\s;,]+))', re.IGNORECASE)
_DRUPAL_GENERATOR = re.compile(r"^\s*drupal\b(?:\s+(?P<major>\d{1,4})\b)?", re.IGNORECASE)
# The first Drupal version with JSON:API (a module from 8, in core from 8.7).
_FIRST_JSONAPI_DRUPAL = 8


def detect_site_apis(soup: BeautifulSoup, base_url: str, headers: Mapping[str, str]) -> SiteApis:
    """The feeds and APIs of the site that the page at ``base_url`` names."""
    base = base_url_of(soup, base_url)
    host = site_host(base_url)
    wordpress: str | None = None
    feeds: list[str] = []
    for tag in soup.find_all("link"):
        if not isinstance(tag, Tag):
            continue
        rel = attr(tag, "rel").lower().split()
        href = attr(tag, "href").strip()
        if not href:
            continue
        if _WORDPRESS_REL in attr(tag, "rel").split():
            wordpress = wordpress or _same_host(base, href, host)
        elif (
            "alternate" in rel
            and "stylesheet" not in rel
            and attr(tag, "type").strip().lower() in _FEED_TYPES
        ):
            url = _same_host(base, href, host)
            if url is not None and url not in feeds and len(feeds) < MAX_FEEDS:
                feeds.append(url)
    if wordpress is None:
        wordpress = _wordpress_from_header(headers.get("link", ""), base, host)
    if wordpress is not None and not wordpress.endswith("/"):
        wordpress += "/"
    return SiteApis(feeds=feeds, wordpress=wordpress, drupal=_drupal(soup, base_url, host))


def _same_host(base: str, href: str, host: str) -> str | None:
    target = absolute_url(base, href)
    url = None if target is None else normalize_url(target)
    return url if url is not None and site_host(url) == host else None


def _wordpress_from_header(value: str, base: str, host: str) -> str | None:
    for target, params in _LINK_HEADER_ITEM.findall(value):
        match = _REL_PARAM.search(params)
        if match and _WORDPRESS_REL in (match.group(1) or match.group(2)).split():
            url = _same_host(base, target.strip(), host)
            if url is not None:
                return url
    return None


def _drupal(soup: BeautifulSoup, base_url: str, host: str) -> str | None:
    generators = [
        match
        for tag in soup.find_all("meta", attrs={"name": re.compile("^generator$", re.I)})
        if isinstance(tag, Tag) and (match := _DRUPAL_GENERATOR.match(attr(tag, "content")))
    ]
    if any(
        match["major"] is not None and int(match["major"]) < _FIRST_JSONAPI_DRUPAL
        for match in generators
    ):
        return None  # Drupal 1-7 has no JSON:API
    marked = (
        bool(generators)
        or soup.find("script", string=re.compile("drupalSettings")) is not None
        or soup.select_one("[data-drupal-selector]") is not None
    )
    if not marked:
        return None
    parts = urlsplit(base_url)
    origin = normalize_url(f"{parts.scheme}://{parts.netloc}/")
    return None if origin is None or site_host(origin) != host else origin.rstrip("/") + "/jsonapi"


def _invalid(fmt: ApiFormat) -> ParsedSitemap:
    return ParsedSitemap(format=fmt, error="invalid")


def _load(body: bytes) -> object | None:
    try:
        loaded: object = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return loaded


def _wordpress_url(api_root: str, route: str, params: Sequence[tuple[str, str]] = ()) -> str:
    """The address of ``route`` (``wp/v2/posts``) under a REST root, with ``params`` as their own
    query parameters. A root of plain permalinks (``https://ex.org/?rest_route=/``) carries the
    route in ``rest_route``; any other root carries it in the path (``.../wp-json/``)."""
    parts = urlsplit(api_root)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if any(key == _REST_ROUTE for key, _ in query):
        query = [(key, f"/{route}" if key == _REST_ROUTE else value) for key, value in query]
        return parts._replace(query=urlencode([*query, *params], safe="/,")).geturl()
    path = parts.path if parts.path.endswith("/") else f"{parts.path}/"
    return parts._replace(path=path + route, query=urlencode([*query, *params], safe="/,")).geturl()


def wordpress_types_url(api_root: str) -> str:
    """The REST address of the site's post types (``wp/v2/types``)."""
    return _wordpress_url(api_root, "wp/v2/types")


def wordpress_list_url(
    api_root: str, rest_base: str, page: int = 1, *, namespace: str = WP_NAMESPACE
) -> str:
    """The REST address of one page of a post type's list (``page`` is left out for the first)."""
    params = [("per_page", str(WP_PER_PAGE)), ("_fields", "link,title,modified_gmt")]
    if page != 1:
        params.append(("page", str(page)))
    return _wordpress_url(api_root, f"{namespace.strip('/')}/{rest_base}", params)


def path_matches(name: str, path_prefix: str) -> bool:
    """Whether a post type name fits a path prefix: a segment equals the name, or one starts with
    the other and the shorter has at least 5 characters (``veranstaltungen``/``veranstaltung``)."""
    folded = _fold(name)
    for segment in (_fold(part) for part in path_prefix.split("/")):
        if not segment:
            continue
        if segment == folded:
            return True
        short, long = sorted((segment, folded), key=len)
        if len(short) >= _MIN_PREFIX_MATCH and long.startswith(short):
            return True
    return False


def parse_wordpress_types(body: bytes, api_root: str, *, path_prefix: str | None) -> ParsedSitemap:
    """The post types of ``wp/v2/types`` as children, each the first page of its list (under the
    type's ``rest_namespace``, ``wp/v2`` when it names none)."""
    data = _load(body)
    if not isinstance(data, dict):
        return _invalid("wordpress_types")
    parsed = ParsedSitemap(format="wordpress_types")
    for value in data.values():
        if not isinstance(value, dict):
            continue
        rest_base, slug = value.get("rest_base"), value.get("slug")
        if not isinstance(rest_base, str) or not rest_base:
            continue
        slug = slug if isinstance(slug, str) else ""
        if slug in EXCLUDED_WP_TYPES:
            continue
        if path_prefix is not None and not path_matches(slug or rest_base, path_prefix):
            continue
        if len(parsed.children) >= MAX_API_TYPES:
            parsed.truncated = True
            break
        namespace = value.get("rest_namespace")
        if not isinstance(namespace, str) or not _NAMESPACE.fullmatch(namespace):
            namespace = WP_NAMESPACE  # older WordPress names none: its core types' namespace
        url = wordpress_list_url(api_root, rest_base, namespace=namespace)
        child = SitemapChild(url=url, crawl="listed")
        parsed.children.append(child)
    return parsed


def _title(value: object) -> str | None:
    if not isinstance(value, dict) or not isinstance(rendered := value.get("rendered"), str):
        return None
    return html.unescape(_TAG.sub("", rendered))


def _wordpress_page(url: str) -> int:
    try:
        for key, value in parse_qsl(urlsplit(url).query):
            if key == "page" and value.isdigit():
                return max(int(value), 1)
    except ValueError:
        pass
    return 1


def _next_wordpress_url(url: str, page: int) -> str | None:
    try:
        parts = urlsplit(url)
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "page"]
    except ValueError:
        return None
    query.append(("page", str(page + 1)))
    return parts._replace(query=urlencode(query, safe="/,")).geturl()


def parse_wordpress_posts(
    body: bytes, url: str, *, site_host: str, total_pages: int | None, max_entries: int
) -> ParsedSitemap:
    """One page of a post type's list as entries; the next page is the one child."""
    data = _load(body)
    if not isinstance(data, list):
        return _invalid("wordpress_posts")
    parsed = ParsedSitemap(format="wordpress_posts")
    for item in data:
        if not isinstance(item, dict) or not isinstance(link := item.get("link"), str):
            continue
        loc = normalize_url(link)
        if loc is None or _site(loc) != site_host:
            continue
        parsed.entry_count += 1
        if len(parsed.entries) >= max_entries:
            parsed.truncated = True
            continue
        modified = item.get("modified_gmt")
        parsed.entries.append(
            SitemapEntry(
                loc=loc,
                title=_title(item.get("title")),
                lastmod=modified if isinstance(modified, str) else None,
            )
        )
    page = _wordpress_page(url)
    more = page < total_pages if total_pages is not None else len(data) >= WP_PER_PAGE
    if more and (next_url := _next_wordpress_url(url, page)) is not None:
        parsed.children.append(SitemapChild(url=next_url, crawl="listed"))
    return parsed


def _site(url: str) -> str:
    return site_host(url)


def parse_drupal_index(body: bytes, origin: str, *, path_prefix: str | None) -> ParsedSitemap:
    """The node types of the JSON:API index as children, each the first page of its list."""
    data = _load(body)
    links = data.get("links") if isinstance(data, dict) else None
    if not isinstance(links, dict):
        return _invalid("drupal_index")
    host = site_host(origin)
    parsed = ParsedSitemap(format="drupal_index")
    for key, value in links.items():
        if not key.startswith("node--") or not isinstance(value, dict):
            continue
        href = value.get("href")
        if not isinstance(href, str) or (target := normalize_url(href)) is None:
            continue
        if site_host(target) != host:
            continue
        if path_prefix is not None and not path_matches(key.removeprefix("node--"), path_prefix):
            continue
        if len(parsed.children) >= MAX_API_TYPES:
            parsed.truncated = True
            break
        query = urlencode(
            {
                "page[limit]": DRUPAL_PAGE_LIMIT,
                f"fields[{key}]": "title,path,changed,drupal_internal__nid",
            }
        )
        parts = urlsplit(target)
        query = f"{parts.query}&{query}" if parts.query else query
        parsed.children.append(
            SitemapChild(url=parts._replace(query=query).geturl(), crawl="listed")
        )
    return parsed


def parse_drupal_nodes(
    body: bytes, origin: str, *, site_host: str, max_entries: int
) -> ParsedSitemap:
    """One page of a node type's JSON:API list as entries; ``links.next`` is the one child."""
    data = _load(body)
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(data, dict) or not isinstance(rows, list):
        return _invalid("drupal_nodes")
    base = origin.rstrip("/")
    parsed = ParsedSitemap(format="drupal_nodes")
    for row in rows:
        attributes = row.get("attributes") if isinstance(row, dict) else None
        if not isinstance(attributes, dict):
            continue
        path = attributes.get("path")
        alias = path.get("alias") if isinstance(path, dict) else None
        nid = attributes.get("drupal_internal__nid")
        if isinstance(alias, str) and alias.startswith("/"):
            raw = base + alias
        elif (isinstance(nid, int) and not isinstance(nid, bool)) or (
            isinstance(nid, str) and nid.isdigit()
        ):
            raw = f"{base}/node/{nid}"
        else:
            continue
        loc = normalize_url(raw)
        if loc is None or _site(loc) != site_host:
            continue
        parsed.entry_count += 1
        if len(parsed.entries) >= max_entries:
            parsed.truncated = True
            continue
        title, changed = attributes.get("title"), attributes.get("changed")
        parsed.entries.append(
            SitemapEntry(
                loc=loc,
                title=title if isinstance(title, str) else None,
                lastmod=changed if isinstance(changed, str) else None,
            )
        )
    links = data.get("links")
    nxt = links.get("next") if isinstance(links, dict) else None
    href = nxt.get("href") if isinstance(nxt, dict) else None
    target = normalize_url(href) if isinstance(href, str) else None
    if target is not None and _site(target) == site_host:
        parsed.children.append(SitemapChild(url=target, crawl="listed"))
    return parsed


_DRUPAL_TYPE = re.compile(r"node--(?P<name>[^/?#&=]+)|/node/(?P<seg>[^/?#]+)")


def _wordpress_route(url: str) -> str:
    """The REST route of a list address (its ``rest_route``, else its path): the post type's
    ``rest_base`` is its last segment."""
    try:
        parts = urlsplit(url)
        for key, value in parse_qsl(parts.query):
            if key == _REST_ROUTE:
                return value
    except ValueError:
        return ""
    return parts.path


def api_type_hint(api: ApiFormat, url: str) -> PageType | None:
    """The page type a list's post type says about its items, else None."""
    if api == "wordpress_posts":
        base = _wordpress_route(url).rstrip("/").rsplit("/", 1)[-1]
        return post_type_hint(base) if base else None
    if api == "drupal_nodes":
        match = _DRUPAL_TYPE.search(url)
        name = (match["name"] or match["seg"]) if match else None
        return post_type_hint(name) if name else None
    return None
