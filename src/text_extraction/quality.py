"""Quality gate: is the extracted content usable, or is it a wall, an error page or an empty shell?

``assess`` looks at the page and the extracted content and reports facts (``Quality``);
``quality_error`` turns them into the verdict of the result envelope. In ``auto`` mode
``browser_recommended`` tells that a browser could do better (the escalation itself is a later
package).

Word count (``count_words``): whitespace-separated tokens that contain at least one letter or
digit, so Markdown syntax (``#``, ``|``, ``---``, ``-``) is not counted. ``Quality.word_count``
is that count over the extracted content; the pipeline and the live harness rely on it.
"""

import re
from typing import Literal

from pydantic import BaseModel

from text_extraction.convert.markup import attr, parse_html, strip_non_content
from text_extraction.envelope import ErrorCode

# Fewer visible characters than this on a page that needs JavaScript means the page is a shell.
JS_HEAVY_BELOW_CHARS = 500
# Soft-404 phrases count only on pages this short (words, inclusive); a long page that mentions
# "404" or "not found" is an article about it.
SOFT_404_MAX_WORDS = 50
# Login walls and the weaker bot-challenge signals count only below this many words.
THIN_PAGE_BELOW_WORDS = 100

_EXECUTABLE_SCRIPT_TYPES = {
    "",
    "module",
    "text/javascript",
    "application/javascript",
    "text/ecmascript",
    "application/ecmascript",
}
_SPA_ROOTS = ("root", "app", "__next")

# Phrases that only a challenge page says, however long the page is.
_CHALLENGE_PHRASES = (
    "checking your browser",
    "verifying you are human",
    "enable javascript and cookies to continue",
    "making sure you're not a bot",
    "protected by anubis",
    "anubis uses a proof-of-work",
)
# Words that also appear on pages that merely embed a captcha or a protection script, so they
# count only on thin pages.
_WEAK_CHALLENGE_WORDS = re.compile(r"just a moment|captcha|datadome|perimeterx")
# Markup of the challenge itself (Cloudflare's ``__cf_chl_*`` / ``cf-chl-*``, Anubis' script).
_CHALLENGE_MARKUP = re.compile(r"cf[-_]chl|within\.website/x/cmd/anubis")
_SOFT_404_PHRASES = re.compile(
    r"\b404\b|not found|nicht gefunden|existiert nicht|does not exist|no longer available"
    r"|nicht mehr verfügbar"
)


class Quality(BaseModel):
    """Facts about the fetched page and its extracted content."""

    visible_chars: int  # characters of the text without scripts and styles, whitespace collapsed
    word_count: int  # see ``count_words``; over the extracted content
    script_count: int  # executable ``<script>`` elements (JSON data blocks are not counted)
    js_heavy: bool  # little visible text and the page needs JavaScript to show its content
    bot_challenge: bool
    soft_404: bool
    login_wall: bool
    browser_recommended: bool  # js_heavy or bot_challenge


def count_words(text: str) -> int:
    """Whitespace-separated tokens that contain a letter or a digit."""
    return sum(1 for token in text.split() if any(char.isalnum() for char in token))


def _normalise(text: str) -> str:
    """Lower case, typographic apostrophes straight, whitespace collapsed to single spaces."""
    return " ".join(text.casefold().replace("\N{RIGHT SINGLE QUOTATION MARK}", "'").split())


class _Markup:
    """What the quality rules need from the HTML, read in one parse."""

    def __init__(self, html: str) -> None:
        soup = parse_html(html)
        self.raw = html.casefold()
        # Only the head's title: an SVG ``<title>`` in the body is a tooltip, not the page's.
        head_title = soup.head.find("title") if soup.head else None
        self.title = head_title.get_text(" ") if head_title else ""
        self.script_count = sum(
            1
            for script in soup("script")
            if attr(script, "type").strip().lower() in _EXECUTABLE_SCRIPT_TYPES
        )
        self.noscript_hint = any(
            "javascript" in noscript.get_text(" ").casefold() for noscript in soup("noscript")
        )
        strip_non_content(soup)
        if soup.head:
            soup.head.decompose()  # the title is not page text
        self.empty_spa_root = any(
            (root := soup.find(id=name)) is not None and not root.get_text(strip=True)
            for name in _SPA_ROOTS
        )
        self.password_input = any(
            attr(field, "type").strip().lower() == "password" for field in soup("input")
        )
        self.text = " ".join(soup.get_text(" ").split())


def _is_bot_challenge(page_text: str, own_text: str, markup: _Markup | None, words: int) -> bool:
    """``page_text``: title, page and content; ``own_text``: title and content only."""
    if any(phrase in page_text for phrase in _CHALLENGE_PHRASES):
        return True
    if markup is not None and _CHALLENGE_MARKUP.search(markup.raw):
        return True
    return words < THIN_PAGE_BELOW_WORDS and bool(_WEAK_CHALLENGE_WORDS.search(own_text))


def assess(html: str | None, content: str) -> Quality:
    """Judge a page. ``html`` is the fetched markup, ``None`` for plain text; ``content`` is
    what extraction produced. CPU-bound: it parses the markup once.

    The facts do not depend on a length threshold; ``quality_error`` applies it.
    """
    words = count_words(content)
    markup = _Markup(html) if html is not None else None
    visible = markup.text if markup else " ".join(content.split())
    title = markup.title if markup else ""
    # A challenge says so anywhere on the page. The weak signals (a captcha note in a template, a
    # consent list naming a protection vendor) and error pages count in the title and content only.
    page_text = _normalise(f"{title} {markup.text if markup else ''} {content}")
    own_text = _normalise(f"{title} {content}")
    js_heavy = (
        markup is not None
        and len(visible) < JS_HEAVY_BELOW_CHARS
        and (markup.script_count >= 1 or markup.noscript_hint or markup.empty_spa_root)
    )
    bot_challenge = _is_bot_challenge(page_text, own_text, markup, words)
    return Quality(
        visible_chars=len(visible),
        word_count=words,
        script_count=markup.script_count if markup else 0,
        js_heavy=js_heavy,
        bot_challenge=bot_challenge,
        soft_404=words <= SOFT_404_MAX_WORDS and bool(_SOFT_404_PHRASES.search(own_text)),
        login_wall=markup is not None and markup.password_input and words < THIN_PAGE_BELOW_WORDS,
        browser_recommended=js_heavy or bot_challenge,
    )


def quality_error(
    q: Quality, min_words: int
) -> tuple[Literal["failed", "partial"], ErrorCode] | None:
    """The envelope verdict for a quality report, or ``None`` when the content is fine."""
    if q.bot_challenge:
        return "failed", ErrorCode.BOT_PROTECTION
    if q.soft_404:
        return "failed", ErrorCode.LIKELY_ERROR_PAGE
    if q.login_wall:
        return "failed", ErrorCode.LOGIN_REQUIRED
    if q.word_count == 0:
        return "failed", ErrorCode.EMPTY_CONTENT
    if q.word_count < min_words:
        return "partial", ErrorCode.CONTENT_TOO_SHORT
    return None
