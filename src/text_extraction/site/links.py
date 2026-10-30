"""The typed links of a page (ruling P9-S5). Pure: no fetch, no Valkey.

``site_links`` reads one page's ``<a href>``, ``<area href>``, ``<link href>`` (``canonical``,
``alternate``, ``prev``, ``next``) and ``<iframe src>`` in document order, with the rules of
``convert.links.page_links``: http(s) only, the fragment dropped, an in-page ``#`` link skipped,
the first occurrence per URL and element kind kept, ``kind`` by file extension and then host.
Each link says where on the page it is (``region``, from the nearest ancestor that tells), its
``rel`` tokens and ``hreflang``; the page says whether it asked robots not to follow its links.
Every link's ``crawl`` is ``listed`` and its ``target`` None: the crawl's follow rules set them.

A page is untrusted input, so the answer is bounded: at most ``max_links`` links, a text of at
most 200 characters (read no further than that), at most 8 ``rel`` tokens, and a URL longer than
``MAX_URL_LENGTH`` is dropped.
"""

import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from text_extraction.compliance import robots_metas_say, x_robots_tag_says
from text_extraction.convert.links import absolute_url, base_url_of, has_file_extension
from text_extraction.convert.markup import attr, parse_html
from text_extraction.site.types import (
    LINK_TEXT_MAX_CHARS,
    LinkKind,
    LinkRegion,
    LinkSource,
    SiteLink,
)
from text_extraction.site.urls import normalize_url, site_host

__all__ = ["MAX_REL_TOKENS", "PageLinks", "site_links"]

MAX_REL_TOKENS = 8
# A ``rel`` token or an ``hreflang`` longer than this is no keyword or language tag: dropped.
_TOKEN_MAX_CHARS = 64
# The attribute that holds each element's URL.
_URL_ATTRIBUTES: dict[str, str] = {"a": "href", "area": "href", "link": "href", "iframe": "src"}
# A ``<link>`` is read only for these; ``alternate stylesheet`` is a stylesheet, not a page.
_LINK_RELS = frozenset({"canonical", "alternate", "prev", "next"})
_PAGINATION_RELS = frozenset({"next", "prev"})
_NOFOLLOW = frozenset({"nofollow", "none"})
_ARTICLE_OR_MAIN = frozenset({"article", "main"})
_WORD = re.compile(r"\S+")

# Per element: the region it is in, and whether an ancestor is an <article> or a <main>.
type _Context = tuple[LinkRegion | None, bool]


@dataclass(frozen=True)
class PageLinks:
    """A page's typed links: at most ``max_links`` (``truncated`` when it had more), and
    whether the page said ``nofollow`` (meta robots or ``X-Robots-Tag``)."""

    links: list[SiteLink]
    nofollow: bool
    truncated: bool


def site_links(
    html: str, base_url: str, headers: Mapping[str, str], *, max_links: int
) -> PageLinks:
    """The typed links of the page at ``base_url``; relative URLs resolve against its
    ``<base href>`` when it has one. ``headers`` are the page's response headers."""
    soup = parse_html(html)
    base = base_url_of(soup, base_url)
    page_host = site_host(base_url)
    contexts: dict[int, _Context] = {}
    seen: set[tuple[str, LinkSource]] = set()
    links: list[SiteLink] = []
    truncated = False
    for tag in _link_elements(soup):
        source = cast(LinkSource, tag.name)  # one of _URL_ATTRIBUTES
        href = attr(tag, _URL_ATTRIBUTES[source]).strip()
        if not href or href.startswith("#"):
            continue
        tokens = _rel_tokens(tag)
        if source == "link" and (not _LINK_RELS.intersection(tokens) or "stylesheet" in tokens):
            continue
        rel = _bounded_rel(tokens)
        url = _absolute(base, href)
        if url is None or (url, source) in seen:
            continue
        if len(links) == max_links:
            truncated = True
            break
        seen.add((url, source))
        links.append(
            SiteLink(
                url=url,
                text=_text(tag, source),
                kind=_kind(url, page_host),
                source=source,
                rel=rel,
                hreflang=_hreflang(tag),
                region=_link_region(tag, rel, contexts),
                crawl="listed",
            )
        )
    return PageLinks(links=links, nofollow=_nofollow(soup, headers), truncated=truncated)


def _link_elements(soup: BeautifulSoup) -> Iterator[Tag]:
    """The elements that may hold a link, in document order, read lazily (the caller stops at
    the cap)."""
    for node in soup.descendants:
        if isinstance(node, Tag) and node.name in _URL_ATTRIBUTES:
            yield node


def _absolute(base: str, href: str) -> str | None:
    """The link's normal URL (fragment dropped), or None when it is no usable http(s) URL."""
    target = absolute_url(base, href)
    return None if target is None else normalize_url(target)


def _kind(url: str, page_host: str) -> LinkKind:
    if has_file_extension(urlsplit(url).path):
        return "file"
    return "internal" if site_host(url) == page_host else "external"


def _rel_tokens(tag: Tag) -> list[str]:
    """All of ``rel`` in lower case (the ``<link>`` filter reads every token)."""
    return attr(tag, "rel").lower().split()


def _bounded_rel(tokens: list[str]) -> list[str]:
    """Each token once, in order, tokens over 64 characters dropped; at most
    ``MAX_REL_TOKENS``."""
    kept: list[str] = []
    for token in tokens:
        if len(token) <= _TOKEN_MAX_CHARS and token not in kept:
            kept.append(token)
            if len(kept) == MAX_REL_TOKENS:
                break
    return kept


def _hreflang(tag: Tag) -> str | None:
    value = attr(tag, "hreflang").strip()
    return value if 0 < len(value) <= _TOKEN_MAX_CHARS else None


def _collapsed(strings: Iterable[str]) -> str:
    """The strings' words joined by one space, cut at ``LINK_TEXT_MAX_CHARS``; reads no further
    than the cut."""
    words: list[str] = []
    size = -1  # no space before the first word
    for string in strings:
        for match in _WORD.finditer(string):
            words.append(match.group())
            size += 1 + len(words[-1])
            if size >= LINK_TEXT_MAX_CHARS:
                return " ".join(words)[:LINK_TEXT_MAX_CHARS].rstrip()
    return " ".join(words)


def _text(tag: Tag, source: LinkSource) -> str:
    """The visible text; an element without one (an icon link, an image map area, an iframe)
    falls back to its ``alt``, ``aria-label`` or ``title``."""
    fallbacks = {
        "a": ("aria-label", "title"),
        "area": ("alt", "aria-label", "title"),
        "iframe": ("title", "aria-label"),
        "link": ("title",),
    }[source]
    if source == "a":
        text = _collapsed(tag.strings)
        if text:
            return text
    for name in fallbacks:
        text = _collapsed((attr(tag, name),))
        if text:
            return text
    return ""


def _own_region(tag: Tag, in_article: bool, rel: Iterable[str] = ()) -> LinkRegion | None:
    """The region ``tag`` itself marks, by the rules in order; ``in_article``: an ancestor is
    an ``<article>`` or a ``<main>`` (a header or footer there is the article's own)."""
    marks = f"{attr(tag, 'class')} {attr(tag, 'id')}".lower()
    if (
        "breadcrumb" in marks
        or "breadcrumb" in attr(tag, "aria-label").lower()
        or any(kind.endswith("BreadcrumbList") for kind in attr(tag, "itemtype").split())
    ):
        return "breadcrumb"
    if _PAGINATION_RELS.intersection(rel) or "pagination" in marks or "pager" in marks:
        return "pagination"
    roles = attr(tag, "role").lower().split()
    name = tag.name
    if name == "nav" or "navigation" in roles:
        return "nav"
    if (name == "header" and not in_article) or "banner" in roles:
        return "header"
    if (name == "footer" and not in_article) or "contentinfo" in roles:
        return "footer"
    if name == "aside" or "complementary" in roles:
        return "aside"
    if name in _ARTICLE_OR_MAIN or "main" in roles:
        return "main"
    return None


def _context(tag: Tag | None, contexts: dict[int, _Context]) -> _Context:
    """The region ``tag`` is in (itself or its nearest ancestor that marks one) and whether it
    is inside an ``<article>`` or ``<main>``. Each element is judged once per page
    (``contexts``, by ``id``), without recursion, so deep nesting costs one walk."""
    chain: list[Tag] = []
    node = tag
    while node is not None and id(node) not in contexts:
        chain.append(node)
        node = node.parent
    context: _Context = (None, False) if node is None else contexts[id(node)]
    for element in reversed(chain):
        region, inside = context
        context = (
            _own_region(element, inside) or region,
            inside or element.name in _ARTICLE_OR_MAIN,
        )
        contexts[id(element)] = context
    return context


def _link_region(tag: Tag, rel: list[str], contexts: dict[int, _Context]) -> LinkRegion | None:
    """The link's own marks first (its ``rel`` ``next``/``prev`` is pagination), then the
    nearest ancestor's."""
    region, inside = _context(tag.parent, contexts)
    return _own_region(tag, inside, rel) or region


def _nofollow(soup: BeautifulSoup, headers: Mapping[str, str]) -> bool:
    """``nofollow`` or ``none`` in a ``<meta name="robots">`` or ``<meta name="text-extraction">``
    of the head (as compliance reads metas: one in the body may be a visitor's content) or in an
    ``X-Robots-Tag`` for everyone or this service."""
    for name, value in headers.items():
        if name.lower() == "x-robots-tag" and x_robots_tag_says(value, _NOFOLLOW):
            return True
    if soup.head is None:
        return False
    metas = (
        (attr(meta, "name").strip().lower(), attr(meta, "content")) for meta in soup.head("meta")
    )
    return robots_metas_say(metas, _NOFOLLOW)
