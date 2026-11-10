"""The text of an SVG document (package 22): parsed, never rendered.

``svg_text`` reads the title and each ``<text>``, ``<flowRoot>`` and ``<desc>`` element as one
paragraph, in document order, the lines of a ``<tspan>`` chain joined with a space. The XML goes
through ``defusedxml``: an entity declaration (an internal one too, so a billion-laughs file) and
an external reference are refused, and nothing is fetched or expanded. The caller has already
held the body to ``DOCUMENT_MAX_BYTES``. Light: no converter imports.
"""

from typing import Final
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as parse_xml

__all__ = ["SvgUnreadable", "svg_text"]

_TEXT_TAGS: Final = frozenset({"text", "flowRoot", "desc"})


class SvgUnreadable(Exception):  # noqa: N818 - named like DocumentUnreadable
    """The body is not an SVG document this service reads: broken XML, an entity declaration, an
    external reference or a tree too deep to walk."""


def svg_text(data: bytes, max_chars: int) -> tuple[str | None, str]:
    """The title (the root's own ``<title>``) and the text of an SVG: one paragraph for each
    ``<text>``, ``<flowRoot>`` and ``<desc>``, in document order, at most ``max_chars``
    characters. An SVG without text gives "". ``SvgUnreadable`` for anything the parser
    refuses, including a nesting too deep for it."""
    try:
        root = parse_xml(data, forbid_dtd=False, forbid_entities=True, forbid_external=True)
        return _title(root), _paragraphs(root, max_chars)
    except (DefusedXmlException, ParseError, RecursionError, ValueError, UnicodeError) as exc:
        raise SvgUnreadable("The SVG could not be read.") from exc


def _name(element: Element) -> str:
    tag = element.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _line(pieces: list[str]) -> str:
    return " ".join(" ".join(pieces).split())


def _title(root: Element) -> str | None:
    for child in root:
        if _name(child) == "title":
            return _line(list(child.itertext())) or None
    return None


def _paragraphs(root: Element, max_chars: int) -> str:
    """Walk the tree with a stack, not recursion: a nesting of any depth costs memory only."""
    paragraphs: list[str] = []
    length = 0
    stack = [root]
    while stack and length <= max_chars:
        element = stack.pop()
        if _name(element) in _TEXT_TAGS:
            paragraph = _line(list(element.itertext()))
            if paragraph:
                paragraphs.append(paragraph)
                length += len(paragraph) + 2
            continue  # its children are its text
        stack.extend(reversed(element))
    return "\n\n".join(paragraphs)[:max_chars].rstrip()
