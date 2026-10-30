"""Safe rendering of a result's Markdown for the UI (ruling P10-S15): markdown-it-py with raw
HTML off, links only for http(s) (with ``rel="noopener noreferrer"``), and images rendered as
their alt text, so a page's content can neither run a script nor load anything."""

from collections.abc import Sequence
from typing import Any, Final

from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from markdown_it.token import Token
from markdown_it.utils import EnvType, OptionsDict
from markupsafe import Markup, escape

LINK_REL: Final = "noopener noreferrer"
_SCHEMES: Final = ("http://", "https://")


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


def _parser() -> MarkdownIt:
    parser = MarkdownIt("commonmark", {"html": False})
    parser.validateLink = is_http  # type: ignore[method-assign]
    renderer: Any = parser.renderer
    renderer.rules["link_open"] = _link_open.__get__(renderer)
    renderer.rules["image"] = _image.__get__(renderer)
    return parser


_PARSER: Final = _parser()


def render_markdown(text: str) -> Markup:
    """``text`` as HTML: raw HTML is shown as text, a link that is not http(s) stays its source
    text, and an image becomes its alt text. Every text is escaped by markdown-it."""
    return Markup(_PARSER.render(text))  # noqa: S704 - markdown-it escapes, html is off
