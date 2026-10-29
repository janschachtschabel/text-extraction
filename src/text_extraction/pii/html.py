"""HTML text for PII handling: the text nodes of a fragment as one string, and a way back.

The analyser reads a string, but a name may stand across tags (``<b>Anna</b> Schmidt``). ``parse``
collects what a reader sees as text, the text nodes and the ``alt`` and ``title`` attribute
values, into one string and remembers which node each stretch of it came from; ``render`` writes
replacements for spans of that string back into the nodes. Markup stays where it is, and lxml
escapes what is written. The ``href`` of a ``mailto:`` or ``tel:`` link is one more node (its
whole value, as N5 treats the URL of such a link), because the address is personal data; other
hrefs are never touched.

What the analyser does not read is dropped on request (``render(..., drop_unread=True)``, every
mode that replaces; R8 and the final review's M4), because nothing unprocessed may leave:

- comments and processing instructions (a leading XML declaration too);
- ``script`` and ``style`` elements (their tails are text of the parent and stay);
- every attribute but ``alt`` and ``title`` (analysed), the ``href`` of a contact link (analysed)
  and the structural ones of ``KEPT_ATTRIBUTES``: URLs (``href``, ``src``, ``srcset``,
  ``poster``, ``cite``; N5 keeps URLs), ``class``, ``id``, language and direction, table and list
  structure, sizes, ``datetime``, and the presentation attributes of MathML. So ``aria-*``,
  ``data-*``, ``value``, ``placeholder``, ``srcdoc``, ``content``, ``name``, ``label``, ``style``,
  ``on*`` handlers and every attribute not listed (those of inline SVG, too) go.

A source that lxml cannot parse but that holds text raises ``HtmlParseFailed``: the caller
withholds it (``pii_failed``) instead of answering empty content as a success.

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
# The attributes a replacing mode keeps besides the analysed ones: structure, never text a reader
# sees (see the module docstring). Names as lxml's HTML parser gives them, in lower case.
KEPT_ATTRIBUTES = frozenset(
    {
        # URLs (N5 keeps URLs; a contact link's href is analysed instead)
        "href", "src", "srcset", "sizes", "poster", "cite",
        # identity, language, direction, role
        "class", "id", "lang", "xml:lang", "dir", "role", "hidden", "xmlns",
        # links and media
        "rel", "target", "type", "width", "height", "align", "valign",
        # tables and lists
        "colspan", "rowspan", "span", "scope", "headers", "start", "reversed",
        # time
        "datetime",
        # MathML presentation (convert/mathml.py writes these)
        "display", "displaystyle", "mathvariant", "mathsize", "scriptlevel", "open", "close",
        "separators", "stretchy", "fence", "separator", "form", "accent", "accentunder",
        "columnalign", "rowalign", "linethickness", "lspace", "rspace", "encoding",
    }
)  # fmt: skip
_CONTACT_SCHEMES = ("mailto:", "tel:")
# Characters XML (and so lxml) does not accept in text.
_XML_INVALID = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
# lxml refuses a str that declares an encoding: the declaration goes before parsing.
_XML_DECLARATION = re.compile(r"\A(\ufeff?\s*)<\?xml\b[^>]*\?>", re.IGNORECASE)
_HTML_TAG = re.compile(r"<html[\s>/]", re.IGNORECASE)
_COMMENT_START = re.compile(r"<!--|<\?")
_BODY_TAG = re.compile(r"<body[\s>/]", re.IGNORECASE)
# What is left of a source once comments, processing instructions and tags are taken out: text.
_NOT_TEXT_MARKUP = re.compile(r"<!--.*?(?:-->|\Z)|<\?.*?(?:>|\Z)|<[^>]*>?", re.DOTALL)


class HtmlParseFailed(Exception):  # noqa: N818 - a failed parse, named as such
    """lxml could not parse a source that holds text. The message never quotes the source."""


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
        has_unread: bool = False,
    ) -> None:
        self._source = source
        self._has_unread = has_unread
        self._root = root
        self._nodes = nodes
        self._starts = [node.start for node in nodes]
        self.text = "".join(_gaps_and_nodes(nodes))

    @property
    def has_unread(self) -> bool:
        """Whether the HTML holds a comment or processing instruction."""
        return self._has_unread

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
        cleaned = _XML_INVALID.sub("", html)
        declared = _XML_DECLARATION.match(cleaned)
        if declared is not None:
            cleaned = declared.group(1) + cleaned[declared.end() :]
        try:
            root = lxml.html.document_fromstring(cleaned, parser=parser)
        except (ValueError, lxml.etree.ParserError) as error:
            if _NOT_TEXT_MARKUP.sub("", cleaned).strip():
                raise HtmlParseFailed(type(error).__name__) from None
            # No element to parse and no text: maybe only a comment (it has nothing left once
            # dropped).
            return cls(html, None, [], has_unread=bool(_COMMENT_START.search(html)))
        nodes, has_unread = _collect(root)
        return cls(html, root, nodes, has_unread or declared is not None)

    def render(self, replacements: Sequence[tuple[Span, str]], *, drop_unread: bool = False) -> str:
        """The HTML with each replacement written over its span.

        The spans refer to ``text``, are sorted by start and do not overlap (``spans.resolve``).
        A replacement goes into the node where its span starts; the rest of the span is deleted
        from the nodes that follow. A span that starts in a separator begins at the next node.
        Without replacements the original string is returned as it was. With ``drop_unread``
        what the analyser does not read is left out of the result (the module docstring), also
        when there is nothing to replace (the string is then unchanged only if it holds none of
        it; a source of nothing but comments becomes an empty string).
        """
        if drop_unread and self._has_unread and self._root is None:
            return ""
        if self._root is None or not (replacements or (drop_unread and self._has_unread)):
            return self._source
        if drop_unread and self._has_unread:
            # Dropping edits the tree, so it works on a tree of its own: ``render`` stays
            # repeatable.
            return HtmlText.parse(self._source)._render(replacements, drop_unread=True)
        return self._render(replacements, drop_unread=False)

    def _render(self, replacements: Sequence[tuple[Span, str]], *, drop_unread: bool) -> str:
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
        if drop_unread:  # a comment's or a script's tail is text of its parent: it stays
            lxml.etree.strip_elements(
                root,
                lxml.etree.Comment,
                lxml.etree.ProcessingInstruction,
                *_NOT_TEXT,
                with_tail=False,
            )
            for element in root.iter():
                for name in [name for name in element.attrib if _dropped_attribute(element, name)]:
                    del element.attrib[name]
        return self._serialise(root, drop_unread=drop_unread)

    def _serialise(self, root: lxml.etree._Element, *, drop_unread: bool) -> str:
        if _HTML_TAG.search(self._source):
            tree = root.getroottree()
            if drop_unread:  # not the tree: comments beside the root element would stay
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


def _dropped_attribute(element: lxml.etree._Element, name: str) -> bool:
    """Whether a replacing mode drops the attribute: it is neither analysed nor structural."""
    lowered = name.lower()
    return not (
        lowered in KEPT_ATTRIBUTES
        or lowered in _TEXT_ATTRIBUTES
        or (lowered == "href" and _is_contact_href(element.get(name) or ""))
    )


def _collect(root: lxml.etree._Element) -> tuple[list[_Node], bool]:
    """The nodes in document order, with the offsets they get in the joined string, and whether
    the tree holds what a replacing mode drops (a comment or processing instruction, a script or
    style element, an attribute that is neither analysed nor kept)."""
    nodes: list[_Node] = []
    has_unread = root.getprevious() is not None or root.getnext() is not None
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
            has_unread = True
            add(element, "tail", "", element.tail or "")
            continue
        block = _is_block(element)
        if event == "start":
            boundary = boundary or block
            if str(element.tag).lower() in _NOT_TEXT or any(
                _dropped_attribute(element, name) for name in element.attrib
            ):
                has_unread = True
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
    return nodes, has_unread


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
