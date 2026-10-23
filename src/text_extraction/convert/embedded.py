"""Content a page carries as data: article bodies, KMap lessons and YouTube's video details.

Adapted from Website-Textextraction-Selenium (Apache-2.0), see NOTICE.
"""

import json
import re
from collections.abc import Iterator
from html import escape
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from text_extraction.convert.links import absolute_url, base_url_of
from text_extraction.convert.markup import attr, parse_html

_YOUTUBE_PLAYER = re.compile(r"\bytInitialPlayerResponse\s*=\s*")
_ATTACHMENT_HEADINGS = {"explanation": "Explanations", "idea": "Ideas", "usage": "Applications"}
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
) -> str:
    """The historic payload names its files as inline:<file>; point those at the real URL
    and list whatever the body did not already use."""
    base = base_url_of(soup, url)
    attachments = [a for a in node.get("attachments", []) if isinstance(a, dict)]
    targets: dict[Any, str] = {}
    for attachment in attachments:
        href = attachment.get("href")
        target = absolute_url(base, href) if isinstance(href, str) else None
        if target:
            targets[attachment.get("file")] = target
    used: set[Any] = set()
    for tag in fragment.select("[href], [src]"):
        for name in ("href", "src"):
            original = attr(tag, name)
            if original.startswith("inline:") and original[7:] in targets:
                tag[name] = targets[original[7:]]
                used.add(original[7:])
    sections: dict[str, list[str]] = {}
    for item in attachments:
        label = item.get("name") or item.get("file")
        target = targets.get(item.get("file"))
        if not label or not target or item.get("file") in used:
            continue
        heading = _ATTACHMENT_HEADINGS.get(str(item.get("tag")), "Attachments")
        sections.setdefault(heading, []).append(
            f'<li><a href="{escape(target, quote=True)}">{escape(str(label))}</a></li>'
        )
    return "".join(
        f"<h2>{heading}</h2><ul>{''.join(items)}</ul>" for heading, items in sections.items()
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


def embedded_html(soup: BeautifulSoup, url: str) -> str | None:
    """HTML for the page's real content when it sits in data, else None.

    A YouTube watch page yields its video details, a KMap page its lesson and any other page
    the ``articleBody`` of its JSON-LD - never a generic SEO description as a full document,
    and never when the page shows a substantial article itself."""
    host = (urlsplit(url).hostname or "").lower()
    if host == "youtube.com" or host.endswith(".youtube.com"):
        video = youtube_video(soup)
        if video:
            return video
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
                return prefix + str(fragment)
            # Resolve first: the call rewrites the inline: references inside fragment,
            # and the operands of a return expression are evaluated left to right.
            sections = _attachment_sections(node, fragment, soup, url)
            return prefix + str(fragment) + sections
    return None
