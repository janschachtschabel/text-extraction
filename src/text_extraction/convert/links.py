"""URL resolution and the classified links of a page.

Adapted from Website-Textextraction-Selenium (Apache-2.0), see NOTICE.
"""

import re
from dataclasses import dataclass
from html import unescape
from typing import Annotated, Literal
from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup
from pydantic import Field

from text_extraction.convert.markup import attr, parse_html
from text_extraction.page_types import TolerantLinkCategory, link_category

# The description of a link's ``category`` in the OpenAPI spec (``links`` and ``site.links``).
LINK_CATEGORY_DESCRIPTION = (
    "What the link leads to, by rules: `download` (a file); `social` (a social network or "
    "video platform); for a link on the page's own site the type its URL says (`home`, "
    "`listing`, `imprint`, `privacy`, `legal`, `contact`, `about`, `auth`, `search`), else "
    '`nav` (a navigation helper such as "Back to top") or `content`. Any other external link '
    "is `content`. Null in results stored before link categories."
)

DOWNLOAD_EXTS = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".ppt",
        ".pptx",
        ".xls",
        ".xlsx",
        ".zip",
        ".rar",
        ".7z",
        ".tar",
        ".gz",
        ".csv",
        ".txt",
        ".rtf",
        ".odt",
        ".ods",
        ".odp",
        ".mp3",
        ".mp4",
        ".avi",
        ".mov",
        ".mkv",
        ".wav",
        ".epub",
        ".mobi",
    }
)

FILE_SUFFIXES = tuple(sorted(DOWNLOAD_EXTS))


@dataclass(frozen=True)
class Link:
    url: str
    text: str
    kind: Literal["internal", "external", "file"]
    # None only in results cached before link categories.
    category: Annotated[
        TolerantLinkCategory | None, Field(description=LINK_CATEGORY_DESCRIPTION)
    ] = None


_URL_CHAR = r"[^\s<>\"'()\[\]]"
# A bare URL, with balanced parentheses inside it (``wiki/Licht_(Physik)``) but not the one that
# closes a Markdown link or a remark around it. The two alternatives start with different
# characters, so the match stays linear.
_URL = re.compile(rf"https?://(?:{_URL_CHAR}|\({_URL_CHAR}*\))+")
# A Markdown target in angle brackets, which may hold any parentheses and spaces.
_ANGLE_TARGET = re.compile(r"\]\(<(https?://[^<>\n]+)>")
# An HTML href value, read before the entities are unescaped.
_HREF = re.compile(r"""\bhref\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)


def urls_in(text: str, *, unescape_entities: bool) -> set[str]:
    """The URLs the text holds, whole: a link to ``a.pdf.zip`` is no link to ``a.pdf``.
    ``unescape_entities`` reads HTML, whose ``&`` in a URL is ``&amp;``, and takes the http(s)
    ``href`` values whole too."""
    found = set(_ANGLE_TARGET.findall(text))
    if unescape_entities:
        for double, single in _HREF.findall(text):
            value = unescape(double or single).strip()
            if value.lower().startswith(("http://", "https://")):
                found.add(value)
        text = unescape(text)
    return found | {url.rstrip(".,;:") for url in _URL.findall(text)}


def url_key(url: str) -> str:
    """``url`` without its fragment and with ``%20``, ``%28`` and ``%29`` read as a space and
    parentheses, so that the forms a URL takes in Markdown and in HTML compare equal."""
    return urldefrag(url).url.replace("%20", " ").replace("%28", "(").replace("%29", ")")


def markdown_target(url: str) -> str:
    """``url`` with its space and parentheses percent-encoded, so that it does not end a
    Markdown link early."""
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def markdown_link(text: str, url: str) -> str:
    """``[text](url)`` with the brackets of the text escaped and the URL as
    ``markdown_target`` writes it."""
    label = text.replace("[", r"\[").replace("]", r"\]")
    return f"[{label}]({markdown_target(url)})"


def absolute_url(base: str, href: str) -> str | None:
    """href resolved against base; None when no URL parser accepts it - a template placeholder
    such as http://[URL] - so that one such link does not cost a page its conversion."""
    try:
        return urljoin(base, href)
    except ValueError:
        return None


def base_url_of(soup: BeautifulSoup, url: str) -> str:
    """The URL that relative references resolve against: ``<base href>`` when it is usable."""
    base_tag = soup.find("base", href=True)
    if base_tag:
        return absolute_url(url, attr(base_tag, "href").strip()) or url
    return url


def has_file_extension(path: str) -> bool:
    return path.lower().endswith(FILE_SUFFIXES)


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").removeprefix("www.")
    except ValueError:
        return ""


def _label(anchor_text: str, aria_label: str, title: str) -> str:
    """The visible text; an icon-only link falls back to its aria-label or title."""
    return " ".join((anchor_text or aria_label or title).split())


def page_links(html: str, base_url: str) -> list[Link]:
    """The page's http(s) links in page order, once per absolute URL (first occurrence wins).

    A link to a path ending in one of ``DOWNLOAD_EXTS`` is a ``file``; any other link is
    ``internal`` when it stays on the page's host (ignoring a leading ``www.``). The
    fragment is dropped (``page#a`` and ``page`` are one link); links that only point into the
    page and non-http schemes (``mailto:``, ``javascript:``, ...) are left out. Each link has its
    ``page_types.link_category``.
    """
    soup = parse_html(html)
    base = base_url_of(soup, base_url)
    page_host = _host(base_url)
    seen: set[str] = set()
    links: list[Link] = []
    for anchor in soup.find_all("a", href=True):
        href = attr(anchor, "href").strip()
        if not href or href.startswith("#"):
            continue
        target = absolute_url(base, href)
        if target is not None:
            target = urldefrag(target).url  # page#a and page are one link
        if target is None or target in seen:
            continue
        try:
            parts = urlsplit(target)
        except ValueError:
            continue
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            continue
        seen.add(target)
        text = _label(
            anchor.get_text(" ", strip=True), attr(anchor, "aria-label"), attr(anchor, "title")
        )
        if has_file_extension(parts.path):
            kind: Literal["internal", "external", "file"] = "file"
        elif _host(target) == page_host:
            kind = "internal"
        else:
            kind = "external"
        links.append(
            Link(url=target, text=text, kind=kind, category=link_category(target, text, kind))
        )
    return links
