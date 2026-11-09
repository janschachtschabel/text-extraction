"""Safe rendering of a result's Markdown for the UI (ruling P10-S15): markdown-it-py with raw
HTML off, links only for http(s) (with ``rel="noopener noreferrer"``), and images rendered as
their alt text, so a page's content can neither run a script nor load anything.

Equations, which the extraction writes as ``$$LaTeX$$``, become MathML on the server (the UI's
CSP runs no inline script): latex2mathml builds the element tree, which keeps only MathML
elements and no link, source or handler attribute, and is serialised here so that every text
is escaped. LaTeX it cannot convert is shown as its escaped source in ``<code>``."""

import html
from collections.abc import Sequence
from typing import Any, Final
from xml.etree.ElementTree import Element, tostring

from latex2mathml.converter import convert_to_element
from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from markdown_it.rules_core import StateCore
from markdown_it.rules_inline import StateInline
from markdown_it.token import Token
from markdown_it.utils import EnvType, OptionsDict
from markupsafe import Markup, escape

LINK_REL: Final = "noopener noreferrer"
_SCHEMES: Final = ("http://", "https://")
_MATH_MARK: Final = "$$"
# The MathML elements a converted equation may hold (MathML Core and menclose); any other
# element makes the equation fall back to its source.
_MATH_TAGS: Final = frozenset(
    {
        "math", "menclose", "merror", "mfrac", "mi", "mmultiscripts", "mn", "mo", "mover",
        "mpadded", "mphantom", "mprescripts", "mroot", "mrow", "ms", "mspace", "msqrt", "mstyle",
        "msub", "msubsup", "msup", "mtable", "mtd", "mtext", "mtr", "munder", "munderover",
        "none", "semantics",
    }
)  # fmt: skip


def is_http(url: object) -> bool:
    """Whether ``url`` is an http(s) URL, the only kind the UI makes clickable (Markdown links
    and the result's links alike)."""
    return isinstance(url, str) and url.strip().lower().startswith(_SCHEMES)


def _link_open(
    self: RendererHTML, tokens: Sequence[Token], idx: int, options: OptionsDict, env: EnvType
) -> str:
    tokens[idx].attrSet("rel", LINK_REL)
    return self.renderToken(tokens, idx, options, env)


def _image(
    self: RendererHTML, tokens: Sequence[Token], idx: int, options: OptionsDict, env: EnvType
) -> str:
    alt = self.renderInlineAsText(tokens[idx].children or [], options, env)
    return str(escape(alt))


def _math_inline(state: StateInline, silent: bool) -> bool:
    """``$$source$$`` (non-empty, no ``$$`` inside) as one ``math`` token whose source is taken
    raw: it runs before ``escape``, so neither backslashes nor ``*`` or ``_`` are Markdown."""
    start = state.pos
    if not state.src.startswith(_MATH_MARK, start):
        return False
    end = state.src.find(_MATH_MARK, start + len(_MATH_MARK))
    source = state.src[start + len(_MATH_MARK) : end]
    if end < 0 or not source.strip():
        return False
    if not silent:
        token = state.push("math", "math", 0)
        token.content = source
        token.markup = _MATH_MARK
    state.pos = end + len(_MATH_MARK)
    return True


def _math_display(state: StateCore) -> None:
    """Mark an equation that is its paragraph's only content as a block."""
    for previous, token in zip(state.tokens, state.tokens[1:], strict=False):
        children = token.children or []
        if (
            previous.type == "paragraph_open"
            and token.type == "inline"
            and len(children) == 1
            and children[0].type == "math"
        ):
            children[0].meta["display"] = "block"


def _local_name(name: str) -> str:
    return name.rpartition("}")[2].rpartition(":")[2].lower()


def _clean(element: Element) -> None:
    """Keep only MathML elements, drop link, source and handler attributes, and turn the
    character references latex2mathml writes into the characters (serialising escapes them)."""
    for node in element.iter():
        if node.tag not in _MATH_TAGS:
            raise ValueError(f"not a MathML element: {node.tag}")
        for name in list(node.attrib):
            local = _local_name(name)
            if local in {"href", "src"} or local.startswith("on"):
                del node.attrib[name]
        node.text = html.unescape(node.text) if node.text else node.text
        node.tail = html.unescape(node.tail) if node.tail else node.tail


def _mathml(source: str, display: str) -> str:
    element = convert_to_element(source, display=display)
    _clean(element)
    return tostring(element, encoding="unicode", short_empty_elements=False)


def _math(
    self: RendererHTML, tokens: Sequence[Token], idx: int, options: OptionsDict, env: EnvType
) -> str:
    source = tokens[idx].content
    try:
        return _mathml(source, str(tokens[idx].meta.get("display", "inline")))
    except Exception:  # any failure of the converter shows the source
        return f"<code>{escape(source)}</code>"


def _parser() -> MarkdownIt:
    parser = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    parser.validateLink = is_http  # type: ignore[method-assign]
    parser.inline.ruler.before("escape", "math", _math_inline)
    parser.core.ruler.after("inline", "math_display", _math_display)
    renderer: Any = parser.renderer
    renderer.rules["link_open"] = _link_open.__get__(renderer)
    renderer.rules["image"] = _image.__get__(renderer)
    renderer.rules["math"] = _math.__get__(renderer)
    return parser


_PARSER: Final = _parser()


def render_markdown(text: str) -> Markup:
    """``text`` as HTML: raw HTML is shown as text, a link that is not http(s) stays its source
    text, an image becomes its alt text, ``$$LaTeX$$`` becomes MathML and ``~~text~~`` ``<s>``.
    Every text is escaped by markdown-it or, in an equation, by the MathML serialisation."""
    return Markup(_PARSER.render(text))  # noqa: S704 - markdown-it escapes, html is off
