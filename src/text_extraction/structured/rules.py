"""Selector rules run on a page (rulings S3, R4, R19): the code that a selector child runs.

``run_rules`` takes the page's HTML, its final URL and the rules as builtins (``RuleRequest``)
and returns what each rule found, as builtins. It is what ``selector_child`` offers a parent,
and nothing else runs a caller's, a learned or a proposed selector: soupsieve and lxml have no
budget of their own (a CSS selector of a few descendant combinators backtracks for hours on a
deeply nested page), so the parent runs them in a child process it kills at a deadline
(``selector_pool``). This module imports only the standard library, BeautifulSoup (with
soupsieve), lxml and structlog, so that a child starts quickly and holds no settings or keys.

* CSS runs on BeautifulSoup (parsed with lxml, on the first CSS rule); XPath on lxml's tree,
  parsed as ``metadata`` parses a page (``parse_page``). An XPath that fails at evaluation (an
  unknown function, variable or prefix) leaves its field out of the answer, as a rule that
  raised does.
* A value is cut to ``MAX_VALUE_CHARS`` characters; a rule takes at most ``MAX_FIELD_VALUES``
  values and ``MAX_FIELD_CHARS`` characters in all, then it stops and is marked ``truncated``.
  Without ``all`` a rule stops at its first value.
* ``href`` and ``src`` values are made absolute against the page's base URL (``<base href>``,
  else the final URL).

Nothing here logs a selector, a value or the page.
"""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, NotRequired, TypedDict
from urllib.parse import urljoin

import structlog
from bs4 import BeautifulSoup, Tag
from lxml import etree
from lxml.html import HtmlElement, HTMLParser, fromstring

_log = structlog.get_logger(__name__)

MAX_VALUE_CHARS = 10_000  # one value
MAX_FIELD_VALUES = 1_000  # values of one field from a rule
MAX_FIELD_CHARS = 100_000  # characters of one field from a rule, all values together
_URL_ATTRIBUTES = frozenset({"href", "src"})


class RuleRequest(TypedDict):
    """One rule as a child gets it: exactly one of ``css`` and ``xpath``."""

    field: str
    css: NotRequired[str | None]
    xpath: NotRequired[str | None]
    attr: NotRequired[str | None]
    all: bool  # every match; else the first value only


# What a rule found: its values (str, or the bool and numbers of an XPath) and whether they were
# cut to the limits above.
type Found = tuple[list[Any], bool]


def parse_page(html: str) -> HtmlElement:
    """The page as an lxml tree; an empty document when it cannot be parsed. Bytes, because
    lxml rejects a str that carries an XML encoding declaration. ``huge_tree`` lifts libxml2's
    limit of 256 nested elements, which would cut a deeply nested page off, footer and all."""
    try:
        return fromstring(html.encode(), parser=HTMLParser(encoding="utf-8", huge_tree=True))
    except Exception as error:  # an empty document, or lxml giving up
        _log.debug("metadata_parse_failed", error=type(error).__name__)
        return fromstring(b"<html></html>", parser=HTMLParser(encoding="utf-8"))


def page_base(tree: HtmlElement, url: str) -> str:
    """The URL that relative references resolve against: ``<base href>`` when it is usable."""
    for tag in tree.iter("base"):
        href = (tag.get("href") or "").strip()
        if href:
            return _absolute(url, href) or url
    return url


def _absolute(base: str, href: str) -> str | None:
    # As ``convert.links.absolute_url``, which a child does not import (it loads Trafilatura).
    try:
        return urljoin(base, href)
    except ValueError:
        return None


def _squash(text: str) -> str:
    return " ".join(text.split())


def bounded(pieces: Iterable[str]) -> str:
    """The pieces of an element's text joined and whitespace collapsed, read only as far as
    ``MAX_VALUE_CHARS`` needs."""
    parts: list[str] = []
    size = 0
    for piece in pieces:
        squashed = _squash(piece)
        if not squashed:
            continue
        parts.append(squashed)
        size += len(squashed) + 1
        if size > MAX_VALUE_CHARS:
            break
    return " ".join(parts)[:MAX_VALUE_CHARS]


class _Found:
    """The values of one rule, within ``MAX_FIELD_VALUES`` and ``MAX_FIELD_CHARS``."""

    def __init__(self, take_all: bool) -> None:
        self.take_all = take_all
        self.values: list[Any] = []
        self.chars = 0
        self.truncated = False

    def add(self, value: Any) -> bool:
        """Keep ``value`` (blank text is skipped); False once no more are wanted."""
        if isinstance(value, str):
            value = str(value[:MAX_VALUE_CHARS])  # a plain str, also of lxml's str subclass
            if not value.strip():
                return True
        if len(self.values) >= MAX_FIELD_VALUES:
            self.truncated = True
            return False
        if isinstance(value, str):
            room = MAX_FIELD_CHARS - self.chars
            if len(value) > room:
                self.truncated = True
                if value[:room].strip():
                    self.values.append(value[:room])
                return False
            self.chars += len(value)
        self.values.append(value)
        return self.take_all


class _Page:
    """The page for selector rules: lxml's tree for XPath, BeautifulSoup for CSS, parsed on
    the first CSS rule."""

    def __init__(self, html: str, url: str) -> None:
        self._html = html
        self._tree = parse_page(html)
        self._base = page_base(self._tree, url)
        self._soup: BeautifulSoup | None = None

    def found(self, rule: RuleRequest) -> Found:
        found = _Found(rule["all"])
        css = rule.get("css")
        if css is not None:
            self._css(css, rule.get("attr"), found)
        else:
            self._xpath(rule.get("xpath") or "", rule.get("attr"), found)
        return found.values, found.truncated

    def _url(self, attr: str | None, value: str) -> str:
        if attr is not None and attr.lower() in _URL_ATTRIBUTES:
            return _absolute(self._base, value.strip()) or value
        return value

    def _css(self, css: str, attr: str | None, found: _Found) -> None:
        if self._soup is None:
            self._soup = BeautifulSoup(self._html, "lxml")
        for tag in self._soup.css.iselect(css, limit=MAX_FIELD_VALUES + 1):
            value = _tag_value(tag, attr)
            if value is not None and not found.add(self._url(attr, value)):
                break

    def _xpath(self, xpath: str, attr: str | None, found: _Found) -> None:
        result = self._tree.getroottree().xpath(xpath)
        matches: Iterable[Any] = result if isinstance(result, list) else [result]
        for match in matches:
            value = self._match_value(match, attr)
            if value is not None and not found.add(value):
                break

    def _match_value(self, match: Any, attr: str | None) -> Any | None:
        if isinstance(match, etree._Element):
            if not isinstance(match.tag, str):  # a comment or a processing instruction
                return match.text
            if attr is not None:
                value = match.get(attr)
                return None if value is None else self._url(attr, value)
            return bounded(match.itertext())
        if isinstance(match, str):
            # An attribute's value is made absolute like a CSS rule's; other strings (text,
            # ``string(...)``) are taken as they are.
            attrname = getattr(match, "attrname", None)
            return self._url(attrname, str(match)) if isinstance(attrname, str) else str(match)
        if isinstance(match, bool | float | int):
            return match
        return None


def _tag_value(tag: Tag, attr: str | None) -> str | None:
    if attr is None:
        return bounded(tag.strings)
    value = tag.get(attr)
    if isinstance(value, list):  # `class` and the other multi-valued attributes
        return " ".join(value)
    return None if value is None else str(value)


def run_rules(html: str, url: str, rules: Sequence[RuleRequest]) -> dict[str, Found]:
    """What each rule finds on the page ``html`` (final URL ``url``), by field; a rule that
    raised (an XPath that fails at evaluation) is left out. Never raises for a rule."""
    page = _Page(html, url)
    answers: dict[str, Found] = {}
    for rule in rules:
        try:
            answers[rule["field"]] = page.found(rule)
        except Exception as error:  # an XPath that fails at evaluation (M2.5), or worse
            _log.debug("structured_rule_failed", error_type=type(error).__name__)
    return answers


def run_rules_payload(payload: Mapping[str, Any]) -> dict[str, Found]:
    """``run_rules`` of a child's payload: ``{"html", "url", "rules"}``."""
    return run_rules(payload["html"], payload["url"], payload["rules"])
