"""The link lists of a listing page.

Trafilatura keeps prose and drops a list whose items are only links, so a hub or index page
loses its topic lists. ``listing_lists`` collects those lists as headed sections, leaving out
the page's chrome, links inside running text and what the extraction already holds.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from html import escape, unescape
from typing import Final, Literal
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

from bs4 import Tag
from bs4.element import NavigableString, PageElement

from text_extraction.convert.links import markdown_link, url_key, urls_in
from text_extraction.convert.markup import (
    BLOCK_TAGS,
    attr,
    is_outside_content,
    prepare_html,
    strip_non_content,
)

# The same type as ``convert.html.OutputFormat``, declared here because that module imports
# this one.
OutputFormat = Literal["markdown", "text", "html"]

# A page needs this many collected links to count as a listing.
LISTING_MIN_LINKS: Final = 5
# An item (the link's closest block) longer than this is running text, not a list entry.
MAX_ITEM_CHARS: Final = 150
# The link text must make up this share of its item's text. A link wrapped in a span counts
# too when all the links of its item make up this share (a tag list of span-wrapped links).
LINK_SHARE_OF_ITEM: Final = 0.8

_HEADINGS: Final = ["h1", "h2", "h3", "h4", "h5", "h6"]
# The blocks a link's item can be. Inline wrappers (span, em, strong, b, i, small) are climbed
# past, so a link wrapped in one inside a paragraph is that paragraph's.
_ITEM_TAGS: Final = ["li", "div", "p", "dd", "dt", "td", "th"]
# A nested list inside an item is items of its own, not the item's text.
_NESTED_LISTS: Final = frozenset({"ul", "ol"})
# Words in an id or class that mark the site's furniture, not the page's content.
_CHROME_WORDS: Final = frozenset(
    {
        *("footer", "breadcrumb", "breadcrumbs", "cookie", "consent", "sidebar", "social"),
        *("share", "skip"),
        *("pagination", "pager", "menu", "feedback"),
        *("nav", "navbar", "navigation", "offcanvas"),
    }
)
_TOKEN_SPLIT = re.compile(r"[\s_-]+")
_LINE_MARKERS = re.compile(r"^[#\-*+\s]+")
# The tags of an HTML result that end a line of its text, and any tag.
_LINE_TAG = re.compile(
    r"<(?:/?(?:" + "|".join(sorted(BLOCK_TAGS)) + r")|br)\b[^>]*>", re.IGNORECASE
)
_ANY_TAG = re.compile(r"<[^>]*>")


@dataclass(frozen=True)
class LinkSection:
    """A list of links under an optional heading. ``links`` are ``(text, absolute_url)``;
    ``level`` (1-6) is ignored when ``heading`` is None."""

    heading: str | None
    level: int
    links: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _Event:
    """A heading (``level`` 1-6, ``url`` None) or a link (``level`` 0) in document order."""

    level: int
    text: str
    url: str | None = None


@dataclass
class _Draft:
    heading: str | None
    level: int
    links: list[tuple[str, str]] = field(default_factory=list)


def _norm(text: str) -> str:
    return " ".join(text.split())


def _known_lines(existing: str, fmt: OutputFormat) -> frozenset[str]:
    """The lines of ``existing`` without heading and list markers, whitespace collapsed; of an
    HTML result the lines of its text, each block a line."""
    if fmt == "html":
        existing = unescape(_ANY_TAG.sub("", _LINE_TAG.sub("\n", existing)))
    return frozenset(
        line for raw in existing.splitlines() if (line := _norm(_LINE_MARKERS.sub("", raw)))
    )


def _own_text(item: Tag) -> tuple[int, int]:
    """The length of the item's text and of the link text in it, both without the nested lists
    of the item."""
    texts: list[str] = []
    links: list[str] = []
    stack: list[tuple[PageElement, bool]] = [(child, False) for child in reversed(item.contents)]
    while stack:
        node, in_link = stack.pop()
        if isinstance(node, Tag):
            if node.name not in _NESTED_LISTS:
                inner = in_link or node.name == "a"
                stack.extend((child, inner) for child in reversed(node.contents))
        elif type(node) is NavigableString:
            texts.append(node)
            if in_link:
                links.append(node)
    return len(_norm(" ".join(texts))), len(_norm(" ".join(links)))


def _page_key(url: str) -> str:
    """``url`` without its fragment and the trailing slash of its path, to tell a link to the
    page itself."""
    parts = urlsplit(urldefrag(url).url)
    return urlunsplit(parts._replace(path=parts.path.rstrip("/")))


class _Page:
    """The prepared page with what its links share, computed once per element: whether an
    element is chrome and how long an item's text is. Without the caches a block of thousands
    of links costs quadratic time."""

    def __init__(self, html: str) -> None:
        self.soup = prepare_html(html)
        strip_non_content(self.soup)
        self.first_h1 = self.soup.find("h1")
        # The wrappers of the page's heading: an id or class word on them names the layout
        # ("has-sidebar"), not furniture.
        self._holds_h1 = (
            {id(node) for node in (self.first_h1, *self.first_h1.parents)}
            if self.first_h1 is not None
            else set()
        )
        self._chrome: dict[int, bool] = {}
        self._item_chars: dict[int, tuple[int, int]] = {}

    def _own_chrome(self, node: Tag) -> bool:
        if is_outside_content(node):
            return True
        if id(node) in self._holds_h1:
            return False
        words = _TOKEN_SPLIT.split(f"{attr(node, 'id')} {attr(node, 'class')}".lower())
        return not _CHROME_WORDS.isdisjoint(words)

    def is_chrome(self, tag: Tag) -> bool:
        """The element or an ancestor is a landmark outside the content or named like
        furniture."""
        path: list[Tag] = []
        result = False
        for node in (tag, *tag.parents):
            if not isinstance(node, Tag) or node.name in {"html", "body", "[document]"}:
                break
            if (cached := self._chrome.get(id(node))) is not None:
                result = cached
                break
            path.append(node)
        for node in reversed(path):
            result = result or self._own_chrome(node)
            self._chrome[id(node)] = result
        return result

    def link_text(self, link: Tag) -> str | None:
        """The link's text when it is a list entry of its own, else None: the link makes up
        its item, or it is wrapped in a span in an item of links only (a tag list)."""
        text = _norm(link.get_text(" "))
        if not text or link.find_parent(_HEADINGS) is not None:
            return None
        item = link.find_parent(_ITEM_TAGS)
        if item is None:
            return None
        if (counted := self._item_chars.get(id(item))) is None:
            counted = _own_text(item)
            self._item_chars[id(item)] = counted
        item_chars, link_chars = counted
        if item_chars <= MAX_ITEM_CHARS and len(text) >= LINK_SHARE_OF_ITEM * item_chars:
            return text
        if link_chars >= LINK_SHARE_OF_ITEM * item_chars and _in_span(link, item):
            return text
        return None


def _in_span(link: Tag, item: Tag) -> bool:
    """Whether a span wraps the link inside its item."""
    for node in link.parents:
        if node is item:
            return False
        if node.name == "span":
            return True
    return False


def _known_urls(existing: str, fmt: OutputFormat) -> frozenset[str]:
    """The URLs ``existing`` holds, whole, as ``url_key`` compares them."""
    return frozenset(url_key(found) for found in urls_in(existing, unescape_entities=fmt == "html"))


def _events(
    html: str, url: str, fmt: OutputFormat, existing: str, known: frozenset[str]
) -> list[_Event]:
    """Headings and links in document order, without a link whose URL is in ``existing`` or
    whose text is a line of it."""
    page_tree = _Page(html)
    known_urls = _known_urls(existing, fmt)
    started = page_tree.first_h1 is None
    page = _page_key(url)
    seen: set[str] = set()
    events: list[_Event] = []
    for element in page_tree.soup.find_all(True):
        if element is page_tree.first_h1:
            started = True
            continue
        if not started:
            continue
        if element.name in _HEADINGS:
            # Of nested headings the outer one carries the text; the inner one repeats it. A
            # heading inside a link (a card) is part of the link's label.
            text = _norm(element.get_text(" "))
            if (
                text
                and element.find_parent([*_HEADINGS, "a"]) is None
                and not page_tree.is_chrome(element)
            ):
                events.append(_Event(int(element.name[1]), text))
        elif element.name == "a" and element.get("href"):
            href = urldefrag(urljoin(url, attr(element, "href"))).url
            if urlsplit(href).scheme not in {"http", "https"} or _page_key(href) == page:
                continue
            if href in seen or url_key(href) in known_urls:
                continue
            label = page_tree.link_text(element)
            if label is not None and label not in known and not page_tree.is_chrome(element):
                seen.add(href)
                events.append(_Event(0, label, href))
    return events


def _has_links(events: Sequence[_Event], start: int) -> bool:
    """Whether a link follows the heading at ``start`` before a heading of its level or higher."""
    level = events[start].level
    for event in events[start + 1 :]:
        if event.url is not None:
            return True
        if event.level <= level:
            return False
    return False


def listing_lists(html: str, url: str, fmt: OutputFormat, *, existing: str) -> str:
    """The standalone-link lists of the page as headed sections in ``fmt``; ``""`` when the page
    has fewer than ``LISTING_MIN_LINKS`` of them. Links whose URL is one of the URLs in
    ``existing`` (in ``fmt``) and texts that are lines of ``existing`` are not repeated."""
    known = _known_lines(existing, fmt)
    events = _events(html, url, fmt, existing, known)
    if sum(1 for event in events if event.url is not None) < LISTING_MIN_LINKS:
        return ""
    drafts: list[_Draft] = []
    for index, event in enumerate(events):
        if event.url is not None:
            if not drafts:
                drafts.append(_Draft(None, 1))
            drafts[-1].links.append((event.text, event.url))
        elif _has_links(events, index):
            drafts.append(_Draft(event.text, event.level))
    sections = [
        LinkSection(draft.heading, draft.level, tuple(draft.links))
        for draft in drafts
        if draft.links or draft.heading
    ]
    return render_link_sections(sections, fmt)


def render_link_sections(
    sections: Sequence[LinkSection], fmt: OutputFormat, *, text_urls: bool = False
) -> str:
    """The sections as Markdown, plain text or HTML lists; ``""`` for no sections. A plain
    text item is its label, or ``label: url`` with ``text_urls``."""
    parts: list[str] = []
    for section in sections:
        if fmt == "html":
            head = (
                f"<h{section.level}>{escape(section.heading, quote=True)}</h{section.level}>"
                if section.heading
                else ""
            )
            items = "".join(
                f'<li><a href="{escape(link, quote=True)}">{escape(text, quote=True)}</a></li>'
                for text, link in section.links
            )
            parts.append(f"{head}<ul>{items}</ul>" if items else head)
            continue
        lines: list[str] = []
        if section.heading:
            mark = "#" * section.level + " " if fmt == "markdown" else ""
            lines += [mark + section.heading] + ([""] if section.links else [])
        if fmt == "markdown":
            lines += [f"- {markdown_link(text, link)}" for text, link in section.links]
        elif text_urls:
            lines += [f"- {text}: {link}" for text, link in section.links]
        else:
            lines += [f"- {text}" for text, _ in section.links]
        parts.append("\n".join(lines))
    return ("" if fmt == "html" else "\n\n").join(parts)
