"""Content a page carries as data: article bodies, KMap lessons and YouTube's video details.

Adapted from Website-Textextraction-Selenium (Apache-2.0), see NOTICE.
"""

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from html import escape
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from text_extraction.convert.links import absolute_url, base_url_of, url_key
from text_extraction.convert.listings import LinkSection
from text_extraction.convert.markup import attr, parse_html

_YOUTUBE_PLAYER = re.compile(r"\bytInitialPlayerResponse\s*=\s*")
_ATTACHMENT_HEADINGS = {
    "explanation": {"en": "Explanations", "de": "Erklärungen"},
    "idea": {"en": "Ideas", "de": "Ideen"},
    "usage": {"en": "Applications", "de": "Anwendungen"},
    "attachments": {"en": "Attachments", "de": "Anhänge"},
}
# A page whose visible article is at least this long needs no help from its JSON-LD body.
_VISIBLE_ARTICLE_CHARS = 200


def _nodes(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            yield from _nodes(item)
    elif isinstance(value, dict):
        yield value
        for key in ("@graph", "mainEntity", "article", "item"):
            yield from _nodes(value.get(key))


def _attachment_sections(
    node: dict[str, Any], fragment: BeautifulSoup, soup: BeautifulSoup, url: str
) -> tuple[LinkSection, ...]:
    """The historic payload names its files as inline:<file>; point those at the real URL
    and list whatever the body did not already use or link to, each under its own target.
    Only http(s) targets count."""
    base = base_url_of(soup, url)
    attachments = [a for a in node.get("attachments", []) if isinstance(a, dict)]
    entries: list[tuple[dict[str, Any], str]] = []
    files: dict[str, str] = {}
    for attachment in attachments:
        href = attachment.get("href")
        target = absolute_url(base, href.strip()) if isinstance(href, str) else None
        if not target or urlsplit(target).scheme.lower() not in {"http", "https"}:
            continue
        entries.append((attachment, target))
        file = attachment.get("file")
        if isinstance(file, str) and file:
            files[file] = target
    used: set[str] = set()
    for tag in fragment.select("[href], [src]"):
        for name in ("href", "src"):
            original = attr(tag, name)
            if original.startswith("inline:") and original[7:] in files:
                tag[name] = files[original[7:]]
                used.add(original[7:])
    linked = {
        url_key(target)
        for tag in fragment.select("a[href]")
        if (target := absolute_url(base, attr(tag, "href").strip()))
    }
    language = (
        "de" if str(soup.html.get("lang", "") if soup.html else "").lower()[:2] == "de" else "en"
    )
    grouped: dict[str, list[tuple[str, str]]] = {}
    for item, target in entries:
        label = item.get("name") or item.get("file")
        if not label or item.get("file") in used or url_key(target) in linked:
            continue
        key = str(item.get("tag"))
        key = key if key in _ATTACHMENT_HEADINGS else "attachments"
        grouped.setdefault(key, []).append((str(label), target))
    return tuple(
        LinkSection(_ATTACHMENT_HEADINGS[key][language], 2, tuple(links))
        for key, links in grouped.items()
    )


def youtube_video(soup: BeautifulSoup) -> str | None:
    """A YouTube watch page's title, channel and description, from the player data it embeds.

    Plain HTTP gets only the page's footer as text, and a fresh browser in the EU meets a consent
    page, but the watch page carries the video's text in ytInitialPlayerResponse.
    """
    for script in soup.find_all("script", src=False):
        text = script.string or ""
        match = _YOUTUBE_PLAYER.search(text)
        if not match:
            continue
        try:
            player, _ = json.JSONDecoder().raw_decode(text, match.end())
        except ValueError:
            return None
        details = player.get("videoDetails") if isinstance(player, dict) else None
        title = details.get("title") if isinstance(details, dict) else None
        if not isinstance(details, dict) or not isinstance(title, str) or not title.strip():
            return None
        parts = [f"<h1>{escape(title)}</h1>"]
        author = details.get("author")
        if isinstance(author, str) and author.strip():
            parts.append(f"<p>{escape(author)}</p>")
        description = details.get("shortDescription")
        if isinstance(description, str):
            parts += [f"<p>{escape(line)}</p>" for line in description.splitlines() if line.strip()]
        return "".join(parts)
    return None


def _json_payload(script: Tag) -> tuple[Any, bool] | None:
    """The parsed payload of a JSON script and whether it is the historic KMap one."""
    raw = script.get_text().strip()
    historic = script.get("id") == "embedded-topic"
    if historic:
        raw = raw[raw.find("{") : raw.rfind("}") + 1]
    try:
        return json.loads(raw), historic
    except (ValueError, TypeError):
        return None  # Scripts need not contain a supported JSON payload.


@dataclass(frozen=True)
class Embedded:
    """The page's real content: ``html`` for the converter, and the attachment link lists
    that Trafilatura would drop from it."""

    html: str
    sections: tuple[LinkSection, ...] = ()


def embedded_content(soup: BeautifulSoup, url: str) -> Embedded | None:
    """The page's real content when it sits in data, else None.

    A YouTube watch page yields its video details, a KMap page its lesson and any other page
    the ``articleBody`` of its JSON-LD - never a generic SEO description as a full document,
    and never when the page shows a substantial article itself."""
    host = (urlsplit(url).hostname or "").lower()
    if host == "youtube.com" or host.endswith(".youtube.com"):
        video = youtube_video(soup)
        if video:
            return Embedded(video)
    scripts = soup.select(
        'script[type="json"], script[type="application/json"], script[type="application/ld+json"]'
    )
    for script in scripts:
        payload = _json_payload(script)
        if payload is None:
            continue
        value, historic = payload
        for node in _nodes(value):
            body = node.get("articleBody") or (node.get("description") if historic else None)
            if not isinstance(body, str) or not body.strip():
                continue
            fragment = parse_html(body)
            title = (
                node.get("title") or node.get("headline") or node.get("name") or node.get("topic")
            )
            prefix = f"<h1>{escape(title)}</h1>" if isinstance(title, str) else ""
            if not historic:
                visible = soup.select_one('article, main, [role="main"]')
                if visible and len(visible.get_text(" ", strip=True)) >= _VISIBLE_ARTICLE_CHARS:
                    return None
                return Embedded(prefix + str(fragment))
            # Resolve first: the call rewrites the inline: references inside fragment,
            # and the operands of a return expression are evaluated left to right.
            sections = _attachment_sections(node, fragment, soup, url)
            return Embedded(prefix + str(fragment), sections)
    return None
