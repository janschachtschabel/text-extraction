"""HTML text for PII handling: the text nodes of a fragment as one string, and a way back.

The analyser reads a string, but a name may stand across tags (``<b>Anna</b> Schmidt``). ``parse``
collects what a reader sees as text, the text nodes and the ``alt`` and ``title`` attribute
values, into one string and remembers which node each stretch of it came from; ``render`` writes
replacements for spans of that string back into the nodes. Markup and every other attribute stay
where they are, and lxml escapes what is written. The ``href`` of a ``mailto:`` or ``tel:`` link
is one more node (its whole value, as N5 treats the URL of such a link), because the address is
personal data; other hrefs are never touched. Script and style bodies are not text, and comments
and processing instructions are dropped on request (``render(..., drop_comments=True)``): a
reader never sees them, but they may hold personal data.

Pure and free of Presidio and spaCy.
"""

import re
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import lxml.etree
import lxml.html

from text_extraction.pii.types import Span

# Elements that start a new block of text for a reader: the text of two of them is not one word
# even when no whitespace stands between it (``<p>Anna</p><p>Schmidt</p>``).
_BLOCKS = frozenset(
    {
        "address", "article", "aside", "blockquote", "br", "dd", "details", "div", "dl", "dt",
        "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
        "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "summary", "table",
        "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
    }
)  # fmt: skip
_NOT_TEXT = frozenset({"script", "style"})
_TEXT_ATTRIBUTES = ("alt", "title")
_CONTACT_SCHEMES = ("mailto:", "tel:")
# Characters XML (and so lxml) does not accept in text.
_XML_INVALID = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_HTML_TAG = re.compile(r"<html[\s>/]", re.IGNORECASE)
_COMMENT_START = re.compile(r"<!--|<\?")
_BODY_TAG = re.compile(r"<body[\s>/]", re.IGNORECASE)


@dataclass(slots=True)
class _Node:
    """One stretch of the string: ``start`` and ``end`` are offsets into ``HtmlText.text``."""

    start: int
    end: int
    element: lxml.etree._Element
    where: Literal["text", "tail", "attribute"]
    attribute: str  # the attribute's name when ``where`` is ``attribute``
    original: str


class HtmlText:
    """The text of an HTML fragment (or page) as one string, with a map back to its nodes."""

    def __init__(
        self,
        source: str,
        root: lxml.etree._Element | None,
        nodes: list[_Node],
        has_comments: bool = False,
    ) -> None:
        self._source = source
        self._has_comments = has_comments
        self._root = root
        self._nodes = nodes
        self._starts = [node.start for node in nodes]
        self.text = "".join(_gaps_and_nodes(nodes))

    @property
    def has_comments(self) -> bool:
        """Whether the HTML holds a comment or processing instruction."""
        return self._has_comments

    @classmethod
    def parse(cls, html: str) -> "HtmlText":
        """Collect the text of ``html`` in document order. Between two nodes that are not
        adjacent inline text (a block boundary, or an attribute value) the string has one space,
        unless whitespace stands there already."""
        parser = lxml.html.HTMLParser(
            collect_ids=False,
            default_doctype=False,
            encoding="utf-8",
            remove_comments=False,
            remove_pis=False,  # the HTML parser reads them as comments: seen, so droppable
            huge_tree=True,
        )
        try:
            root = lxml.html.document_fromstring(_XML_INVALID.sub("", html), parser=parser)
        except (ValueError, lxml.etree.ParserError):
            # No element to parse: no text, but maybe only a comment (it has nothing left once
            # dropped).
            return cls(html, None, [], has_comments=bool(_COMMENT_START.search(html)))
        nodes, has_comments = _collect(root)
        return cls(html, root, nodes, has_comments)

    def render(
        self, replacements: Sequence[tuple[Span, str]], *, drop_comments: bool = False
    ) -> str:
        """The HTML with each replacement written over its span.

        The spans refer to ``text``, are sorted by start and do not overlap (``spans.resolve``).
        A replacement goes into the node where its span starts; the rest of the span is deleted
        from the nodes that follow. A span that starts in a separator begins at the next node.
        Without replacements the original string is returned as it was. With ``drop_comments``
        every comment and processing instruction is left out of the result, also when there is
        nothing to replace (the string is then unchanged only if it holds none; a source of
        nothing but comments becomes an empty string).
        """
        if drop_comments and self._has_comments and self._root is None:
            return ""
        if self._root is None or not (replacements or (drop_comments and self._has_comments)):
            return self._source
        if drop_comments and self._has_comments:
            # Dropping edits the tree, so it works on a tree of its own: ``render`` stays
            # repeatable.
            return HtmlText.parse(self._source)._render(replacements, drop_comments=True)
        return self._render(replacements, drop_comments=False)

    def _render(self, replacements: Sequence[tuple[Span, str]], *, drop_comments: bool) -> str:
        root = self._root
        if root is None:
            return self._source
        edits: dict[int, list[tuple[int, int, str]]] = {}
        for span, replacement in replacements:
            index = bisect_right(self._starts, span.start) - 1
            if index < 0 or span.start >= self._nodes[index].end:
                index += 1  # in a separator, or before the first node
            for position in range(index, len(self._nodes)):
                node = self._nodes[position]
                if node.start >= span.end:
                    break
                edits.setdefault(position, []).append(
                    (
                        max(span.start, node.start) - node.start,
                        min(span.end, node.end) - node.start,
                        replacement if position == index else "",
                    )
                )
        for position, node in enumerate(self._nodes):
            _write(node, _edited(node.original, edits.get(position, [])))
        if drop_comments:  # a comment's tail is text of its parent: it stays
            lxml.etree.strip_elements(
                root, lxml.etree.Comment, lxml.etree.ProcessingInstruction, with_tail=False
            )
        return self._serialise(root, drop_comments=drop_comments)

    def _serialise(self, root: lxml.etree._Element, *, drop_comments: bool) -> str:
        if _HTML_TAG.search(self._source):
            tree = root.getroottree()
            if drop_comments:  # not the tree: comments beside the root element would stay
                doctype = tree.docinfo.doctype
                page = str(lxml.html.tostring(root, encoding="unicode"))
                return f"{doctype}\n{page}" if doctype else page
            return str(lxml.html.tostring(tree, encoding="unicode"))
        body = root.find("body")
        if body is None:  # a page without a body (only a head): as it is
            return str(lxml.html.tostring(root, encoding="unicode"))
        if _BODY_TAG.search(self._source):
            return str(lxml.html.tostring(body, encoding="unicode", with_tail=False))
        # A fragment: what the parser put into the ``<body>`` it added.
        parts = [_escaped(body.text or "")]
        parts.extend(str(lxml.html.tostring(child, encoding="unicode")) for child in body)
        return "".join(parts)


def _escaped(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _is_block(element: lxml.etree._Element) -> bool:
    return isinstance(element.tag, str) and element.tag.lower() in _BLOCKS


def _is_contact_href(value: str) -> bool:
    return value.lstrip().lower().startswith(_CONTACT_SCHEMES)


def _collect(root: lxml.etree._Element) -> tuple[list[_Node], bool]:
    """The nodes in document order, with the offsets they get in the joined string, and whether
    the tree holds a comment or processing instruction."""
    nodes: list[_Node] = []
    has_comments = root.getprevious() is not None or root.getnext() is not None
    length = 0
    last = ""  # the last character so far
    boundary = False  # a block boundary or an attribute lies between the last node and the next

    def add(
        element: lxml.etree._Element,
        where: Literal["text", "tail", "attribute"],
        name: str,
        text: str,
    ) -> None:
        nonlocal length, last, boundary
        if not text:
            return
        if boundary and last and not last.isspace() and not text[0].isspace():
            length += 1
        nodes.append(_Node(length, length + len(text), element, where, name, text))
        length += len(text)
        last = text[-1]
        boundary = where == "attribute"

    for event, element in lxml.etree.iterwalk(root, events=("start", "end", "comment", "pi")):
        if event in ("comment", "pi"):  # the text after it is the parent's
            has_comments = True
            add(element, "tail", "", element.tail or "")
            continue
        block = _is_block(element)
        if event == "start":
            boundary = boundary or block
            for name in _TEXT_ATTRIBUTES:
                value = element.get(name)
                if value:
                    boundary = True
                    add(element, "attribute", name, value)
            href = element.get("href")
            if href and _is_contact_href(href):
                boundary = True
                add(element, "attribute", "href", href)
            if str(element.tag).lower() not in _NOT_TEXT:
                add(element, "text", "", element.text or "")
        else:
            boundary = boundary or block
            if element is not root:
                add(element, "tail", "", element.tail or "")
    return nodes, has_comments


def _gaps_and_nodes(nodes: list[_Node]) -> list[str]:
    """The pieces of the string: each node's text, and one space where the offsets leave a gap."""
    pieces: list[str] = []
    position = 0
    for node in nodes:
        if node.start > position:
            pieces.append(" ")
        pieces.append(node.original)
        position = node.end
    return pieces


def _edited(original: str, edits: list[tuple[int, int, str]]) -> str:
    """``original`` with each ``(start, end, insert)`` written over its range, left to right."""
    if not edits:
        return original
    parts: list[str] = []
    position = 0
    for start, end, insert in sorted(edits, key=lambda edit: edit[0]):
        parts.append(original[position : max(start, position)])
        parts.append(insert)
        position = max(position, end)
    parts.append(original[position:])
    return "".join(parts)


def _write(node: _Node, value: str) -> None:
    match node.where:
        case "text":
            node.element.text = value
        case "tail":
            node.element.tail = value
        case "attribute":
            node.element.set(node.attribute, value)
