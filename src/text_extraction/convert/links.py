"""URL resolution and the classified links of a page.

Adapted from Website-Textextraction-Selenium (Apache-2.0), see NOTICE.
"""

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from text_extraction.convert.markup import attr, parse_html

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
    ``internal`` when it stays on the page's host (ignoring a leading ``www.``). In-page
    fragments and non-http schemes (``mailto:``, ``javascript:``, ...) are left out.
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
        links.append(Link(url=target, text=text, kind=kind))
    return links
