"""Files a page offers as its material: links in its content and Drupal file entities.

A thin page (a worksheet overview, a lesson plan) often carries its material as a download.
``link_attachments`` lists the linked files of the content; ``attachment_candidates`` adds the
``file--file`` entities that a Drupal front end embeds in script data (Next.js flight chunks or
a JSON script), which no link shows. Only candidates are collected here; fetching is the
caller's business.
"""

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

from text_extraction.convert.dom import attr
from text_extraction.convert.links import absolute_url, base_url_of, has_file_extension
from text_extraction.convert.markup import is_outside_content

MAX_ATTACHMENT_CANDIDATES = 20
_MAX_SCRIPT_CHARS = 2_000_000  # a larger script is skipped unread
_MAX_ENTITY_MARKERS = 100  # "file--file" occurrences examined per script
_MAX_OPENERS = 8  # enclosing "{" tried per marker
_FILE_ENTITY = '"type":"file--file"'
_FLIGHT_CHUNK = re.compile(r'__next_f\.push\(\[\s*\d+\s*,\s*("(?:[^"\\]|\\.)*")\s*\]\)')


@dataclass(frozen=True)
class Attachment:
    """A file a page offers, as far as the page tells."""

    url: str
    label: str
    mime: str | None = None
    size: int | None = None


def link_attachments(soup: BeautifulSoup, base: str | None = None) -> list[tuple[str, str]]:
    """Label and URL of each file the page's content links to, in page order: an http(s) link
    whose path ends in one of DOWNLOAD_EXTS.

    Without ``base`` the document is expected to be prepared, its references already absolute;
    with it, each href is resolved against it. A link without text is labelled with its
    aria-label, its title or its file name."""
    found: dict[str, str] = {}
    for anchor in soup.find_all("a", href=True):
        url = attr(anchor, "href").strip()
        if base is not None:
            url = absolute_url(base, url) or ""
        parts = urlsplit(url)
        if url in found or parts.scheme not in {"http", "https"}:
            continue
        if not has_file_extension(parts.path):
            continue
        if is_outside_content(anchor) or anchor.find_parent(is_outside_content):
            continue
        label = (
            anchor.get_text(" ", strip=True) or attr(anchor, "aria-label") or attr(anchor, "title")
        )
        found[url] = " ".join(label.split()) or unquote(parts.path.rsplit("/", 1)[-1])
    return [(label, url) for url, label in found.items()]


def _safe_url(base: str, href: object) -> str | None:
    if not isinstance(href, str) or not href.strip():
        return None
    url = absolute_url(base, href.strip())
    if not url:
        return None
    try:
        parts = urlsplit(url)
        has_user_info = parts.username is not None or parts.password is not None
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname or has_user_info:
        return None
    return url


def _script_texts(text: str) -> list[str]:
    """The script itself and the strings of its Next.js flight chunks, decoded."""
    texts = [text]
    for match in _FLIGHT_CHUNK.finditer(text):
        try:
            decoded = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(decoded, str):
            texts.append(decoded)
    return texts


def _entity_at(text: str, marker: int) -> dict[str, Any] | None:
    """The object that holds the marker, parsed from the nearest enclosing "{" that works."""
    decoder = json.JSONDecoder()
    end = marker
    for _ in range(_MAX_OPENERS):
        start = text.rfind("{", 0, end)
        if start < 0:
            return None
        end = start
        try:
            obj, stop = decoder.raw_decode(text, start)
        except ValueError:
            continue
        if isinstance(obj, dict) and stop > marker and obj.get("type") == "file--file":
            return obj
    return None


def _entity_attachment(obj: dict[str, Any], base: str) -> Attachment | None:
    fields = obj.get("attributes")
    source: dict[str, Any] = fields if isinstance(fields, dict) else obj
    name, mime, uri = source.get("filename"), source.get("filemime"), source.get("uri")
    if not isinstance(name, str) or not name.strip() or not isinstance(uri, dict):
        return None
    if not isinstance(mime, str) or mime.lower().startswith("image/"):
        return None
    url = _safe_url(base, uri.get("url"))
    if url is None:
        return None
    size = source.get("filesize")
    if isinstance(size, str) and size.isdigit():
        size = int(size)
    return Attachment(
        url,
        name.strip(),
        mime,
        size if isinstance(size, int) and not isinstance(size, bool) else None,
    )


def _file_entities(soup: BeautifulSoup, base: str) -> list[Attachment]:
    found: list[Attachment] = []
    for script in soup.find_all("script"):
        raw = script.string if script.string is not None else script.get_text()
        if not raw or len(raw) > _MAX_SCRIPT_CHARS or "file--file" not in raw:
            continue
        markers = 0
        for text in _script_texts(raw):
            pos = text.find(_FILE_ENTITY)
            while pos >= 0 and markers < _MAX_ENTITY_MARKERS:
                markers += 1
                obj = _entity_at(text, pos)
                if obj is not None and (item := _entity_attachment(obj, base)):
                    found.append(item)
                pos = text.find(_FILE_ENTITY, pos + len(_FILE_ENTITY))
    return found


def attachment_candidates(html: str, url: str) -> list[Attachment]:
    """Files the page offers: document links of its content first, in page order, then Drupal
    file entities from script data; deduplicated by URL, http(s) only, at most 20."""
    soup = BeautifulSoup(html, "lxml")
    base = base_url_of(soup, url)
    candidates: dict[str, Attachment] = {}
    for label, link in link_attachments(soup, base):
        safe = _safe_url(base, link)
        if safe:
            candidates.setdefault(safe, Attachment(safe, label))
    for item in _file_entities(soup, base):
        candidates.setdefault(item.url, item)
    return list(candidates.values())[:MAX_ATTACHMENT_CANDIDATES]
