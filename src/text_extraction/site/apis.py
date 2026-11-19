"""The feeds and CMS APIs a start page names (package 26). Pure: no fetch, no Valkey.

``detect_site_apis`` reads the page's head ``<link>`` elements and its ``Link`` response header
for the WordPress REST API root (``rel="https://api.w.org/"``), RSS and Atom feeds
(``rel="alternate"`` with a feed type), and the markers of a Drupal site. Only addresses on the
page's own site host count.
"""

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from text_extraction.convert.links import absolute_url, base_url_of
from text_extraction.convert.markup import attr
from text_extraction.site.types import SiteApis
from text_extraction.site.urls import normalize_url, site_host

__all__ = ["MAX_FEEDS", "detect_site_apis"]

MAX_FEEDS = 3
_WORDPRESS_REL = "https://api.w.org/"
_FEED_TYPES = frozenset({"application/rss+xml", "application/atom+xml"})
_LINK_HEADER_ITEM = re.compile(r"<([^>]*)>([^,<]*)")
_REL_PARAM = re.compile(r'rel\s*=\s*(?:"([^"]*)"|([^\s;,]+))', re.IGNORECASE)
_DRUPAL_GENERATOR = re.compile(r"^\s*drupal\b", re.IGNORECASE)


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
    marked = (
        soup.find("script", string=re.compile("drupalSettings")) is not None
        or soup.select_one("[data-drupal-selector]") is not None
        or any(
            _DRUPAL_GENERATOR.match(attr(tag, "content"))
            for tag in soup.find_all("meta", attrs={"name": re.compile("^generator$", re.I)})
            if isinstance(tag, Tag)
        )
    )
    if not marked:
        return None
    parts = urlsplit(base_url)
    origin = normalize_url(f"{parts.scheme}://{parts.netloc}/")
    return None if origin is None or site_host(origin) != host else origin.rstrip("/") + "/jsonapi"
