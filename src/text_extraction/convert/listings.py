"""The link lists of a listing page.

Trafilatura keeps prose and drops a list whose items are only links, so a hub or index page
loses its topic lists. ``listing_lists`` collects those lists as headed sections, leaving out
the page's chrome, links inside running text and what the extraction already holds.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from html import escape
from typing import Final, Literal
from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import Tag

from text_extraction.convert.markup import attr, is_outside_content, prepare_html, strip_non_content

# The same type as ``convert.html.OutputFormat``, declared here because that module imports
# this one.
OutputFormat = Literal["markdown", "text", "html"]

# A page needs this many collected links to count as a listing.
LISTING_MIN_LINKS: Final = 5
# An item (the link's closest block) longer than this is running text, not a list entry.
MAX_ITEM_CHARS: Final = 150
# The link text must make up this share of its item's text.
LINK_SHARE_OF_ITEM: Final = 0.8

_HEADINGS: Final = ["h1", "h2", "h3", "h4", "h5", "h6"]
_ITEM_TAGS: Final = ["li", "div", "p", "dd", "dt", "td", "th", "span"]
# Words in an id or class that mark the site's furniture, not the page's content.
_CHROME_WORDS: Final = frozenset(
    {
        *("footer", "breadcrumb", "cookie", "consent", "sidebar", "social", "share", "skip"),
        *("pagination", "pager", "menu", "feedback"),
    }
)
_TOKEN_SPLIT = re.compile(r"[\s_-]+")
_LINE_MARKERS = re.compile(r"^[#\-*+\s]+")


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


def _known_lines(existing: str) -> frozenset[str]:
    """The lines of ``existing`` without heading and list markers, whitespace collapsed."""
    return frozenset(
        line for raw in existing.splitlines() if (line := _norm(_LINE_MARKERS.sub("", raw)))
    )


def _is_chrome(tag: Tag) -> bool:
    """The element or an ancestor is a landmark outside the content or named like furniture."""
    for node in (tag, *tag.parents):
        if not isinstance(node, Tag) or node.name in {"html", "body", "[document]"}:
            break
        if is_outside_content(node):
            return True
        words = _TOKEN_SPLIT.split(f"{attr(node, 'id')} {attr(node, 'class')}".lower())
        if _CHROME_WORDS.intersection(words):
            return True
    return False


def _link_text(link: Tag) -> str | None:
    """The link's text when it is a list entry of its own, else None."""
    text = _norm(link.get_text(" "))
    if not text or link.find_parent(_HEADINGS) is not None:
        return None
    item = link.find_parent(_ITEM_TAGS)
    if item is None:
        return None
    item_text = _norm(item.get_text(" "))
    if len(item_text) > MAX_ITEM_CHARS or len(text) < LINK_SHARE_OF_ITEM * len(item_text):
        return None
    return text


def _events(html: str, url: str, existing: str, known: frozenset[str]) -> list[_Event]:
    """Headings and links in document order, without a link whose URL is in ``existing`` or
    whose text is in ``known``."""
    soup = prepare_html(html)
    strip_non_content(soup)
    first_h1 = soup.find("h1")
    started = first_h1 is None
    page = urldefrag(url).url
    seen: set[str] = set()
    events: list[_Event] = []
    for element in soup.find_all(True):
        if element is first_h1:
            started = True
            continue
        if not started:
            continue
        if element.name in _HEADINGS:
            # Of nested headings the outer one carries the text; the inner one repeats it.
            text = _norm(element.get_text(" "))
            if text and element.find_parent(_HEADINGS) is None and not _is_chrome(element):
                events.append(_Event(int(element.name[1]), text))
        elif element.name == "a" and element.get("href"):
            href = urldefrag(urljoin(url, attr(element, "href"))).url
            if urlsplit(href).scheme not in {"http", "https"} or href == page:
                continue
            if href in seen or href in existing:
                continue
            label = _link_text(element)
            if label is not None and label not in known and not _is_chrome(element):
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
    has fewer than ``LISTING_MIN_LINKS`` of them. Links whose URL occurs in ``existing`` and
    headings that are heading lines of ``existing`` are not repeated."""
    known = _known_lines(existing)
    events = _events(html, url, existing, known)
    if sum(1 for event in events if event.url is not None) < LISTING_MIN_LINKS:
        return ""
    drafts: list[_Draft] = []
    for index, event in enumerate(events):
        if event.url is not None:
            if not drafts:
                drafts.append(_Draft(None, 1))
            drafts[-1].links.append((event.text, event.url))
        elif event.text in known:
            drafts.append(_Draft(None, 1))
        elif _has_links(events, index):
            drafts.append(_Draft(event.text, event.level))
    sections = [
        LinkSection(draft.heading, draft.level, tuple(draft.links))
        for draft in drafts
        if draft.links or draft.heading
    ]
    return render_link_sections(sections, fmt)


def render_link_sections(sections: Sequence[LinkSection], fmt: OutputFormat) -> str:
    """The sections as Markdown, plain text (no URLs) or HTML lists; ``""`` for no sections."""
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
            lines += [f"- [{text}]({link})" for text, link in section.links]
        else:
            lines += [f"- {text}" for text, _ in section.links]
        parts.append("\n".join(lines))
    return ("" if fmt == "html" else "\n\n").join(parts)
