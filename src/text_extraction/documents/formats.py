"""The output formats of a converted document (ruling F6), from the Markdown a converter gives.

- ``markdown``: the Markdown with its whitespace normalised (line ends, form feeds, trailing
  blanks, runs of blank lines, control characters).
- ``html``: that Markdown rendered by markdown-it-py (CommonMark plus tables). Raw HTML in the
  Markdown is escaped, never passed through: a document that quotes ``<script>`` shows it as
  text. markdown-it's link check drops ``javascript:``, ``vbscript:`` and ``data:`` links
  (except images in a few raster types).
- ``text``: the text of that HTML with a newline after each block, a tab between table cells.

Light enough for the parent process as well: it imports no converter library.
"""

import re
from functools import cache

from markdown_it import MarkdownIt
from markdown_it.token import Token

from text_extraction.convert import OutputFormat

# C0 controls other than tab and line feed; vertical tab and form feed (a page break in
# pdfminer's output) become line breaks before this applies.
_CONTROLS = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")
_TRAILING_BLANKS = re.compile(r"[ \t]+$", re.MULTILINE)
_BLANK_RUNS = re.compile(r"\n{3,}")
_HEADING = re.compile(r"^#{1,2} +(.+?) *#* *$", re.MULTILINE)


@cache
def _parser() -> MarkdownIt:
    return MarkdownIt("commonmark", {"html": False}).enable("table")


def from_markdown(markdown: str, fmt: OutputFormat) -> str:
    """The document in the requested format; an empty string when it holds no text."""
    normalised = _normalise(markdown)
    if not normalised:
        return ""
    if fmt == "markdown":
        return normalised
    if fmt == "html":
        return f"<html><body>{_parser().render(normalised)}</body></html>"
    return _text(_parser().parse(normalised))


def first_heading(markdown: str) -> str | None:
    """The text of the first heading of level 1 or 2, a title for a document without one."""
    match = _HEADING.search(markdown)
    return match.group(1) if match else None


def _normalise(markdown: str) -> str:
    text = markdown.removeprefix("\ufeff").replace("\r\n", "\n")
    text = text.translate({ord("\r"): "\n", ord("\v"): "\n", ord("\f"): "\n\n"})
    text = _CONTROLS.sub("", text)
    text = _TRAILING_BLANKS.sub("", text)
    return _BLANK_RUNS.sub("\n\n", text).strip("\n")


def _text(tokens: list[Token]) -> str:
    parts: list[str] = []
    for token in tokens:
        match token.type:
            case "inline":
                parts.append(_inline_text(token.children or []))
            case "fence" | "code_block":
                parts.append(token.content)
            case "th_close" | "td_close":
                parts.append("\t")
            case "tr_close":
                if parts and parts[-1] == "\t":
                    parts.pop()
                parts.append("\n")
            case _ if token.nesting == -1:
                parts.append("\n")  # the end of a block: paragraph, heading, list item, ...
    lines = (line.rstrip() for line in "".join(parts).splitlines())
    return "\n".join(line for line in lines if line)


def _inline_text(children: list[Token]) -> str:
    out: list[str] = []
    for child in children:
        match child.type:
            case "text" | "code_inline":
                out.append(child.content)
            case "softbreak" | "hardbreak":
                out.append("\n")
            case "image":  # its alternative text
                out.append(_inline_text(child.children or []))
    return "".join(out)
