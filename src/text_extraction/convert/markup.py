"""Document preparation before extraction, and Markdown table postprocessing.

``prepare_html`` parses the page once and replaces equations by ``$$...$$`` source, so that no
extractor drops them. ``strip_non_content`` then removes what no reader sees. Both work in
place on one tree; nothing here copies the document.

Adapted from Website-Textextraction-Selenium (Apache-2.0), see NOTICE.
"""

import re
from html import escape

from bs4 import BeautifulSoup, Tag

from text_extraction.convert.mathml import to_latex

HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)

# Marks an element that holds an equation's ``$$source$$``.
_MATH_MARK = "data-extracted-math"
_TOKEN = re.compile(r"EQUATIONTOKEN(\d+)END")
_NON_CONTENT_TAGS = ["script", "style", "noscript", "template"]
_HIDDEN_CLASSES = {"sr-only", "visually-hidden", "visuallyhidden", "screen-reader-text", "hidden"}
# A file linked from the page's navigation, banner, footer or sidebar is the site's, not the page's.
_CHROME_ROLES = {"navigation", "banner", "contentinfo", "complementary"}
_SECTIONING = ["article", "aside", "main", "nav", "section"]
_XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>")
_ROW = re.compile(r"(?<!\\)\|")
_SEPARATOR_CELL = re.compile(r"\s*:?-{3,}:?\s*")
_FENCE = re.compile(r"^(`{3,}|~{3,})")


def attr(tag: Tag, name: str) -> str:
    """An attribute as one string; empty when absent (a multi-valued one is joined by spaces)."""
    value = tag.get(name)
    if value is None:
        return ""
    return value if isinstance(value, str) else " ".join(value)


def has_hidden_class(tag: Tag) -> bool:
    """A screen-reader helper or similar: invisible to sighted readers by class name."""
    return bool(_HIDDEN_CLASSES & set(attr(tag, "class").split()))


def is_hidden(tag: Tag) -> bool:
    """Hidden from every reader: the ``hidden`` attribute, ``aria-hidden`` or an inline style.

    Wikipedia's archive placeholders are of this kind. ``hidden="until-found"`` is findable and
    visible on demand, so it stays."""
    return (
        tag.get("hidden") not in (None, "until-found")
        or attr(tag, "aria-hidden").lower() == "true"
        or bool(HIDDEN_STYLE.search(attr(tag, "style")))
    )


def is_outside_content(tag: Tag) -> bool:
    """Hidden, or a landmark around the page's content, as browsers map them: a header or footer
    is the page's only outside article, aside, main, nav and section, and an aside is a sidebar
    only outside article and section."""
    if is_hidden(tag) or tag.name == "nav" or attr(tag, "role") in _CHROME_ROLES:
        return True
    if tag.name in {"header", "footer"}:
        return tag.find_parent(_SECTIONING) is None
    if tag.name == "aside":
        return tag.find_parent(["article", "section"]) is None
    return False


def _presentation(equation: Tag) -> str:
    """MathML carries the structure of the formula; other markup holds rendered glyphs only."""
    if equation.name == "math":
        return to_latex(equation) or equation.get_text(" ", strip=True)
    return equation.get_text(" ", strip=True)


def _reveal(replacement: Tag) -> None:
    """Unhide the wrappers that exist only to keep this formula from sighted readers.

    Wikipedia ships its MathML in a ``display: none`` span next to a rendered image, and
    extractors drop hidden content - so the substituted LaTeX would never reach the output.
    A wrapper holding anything besides the formula keeps its styling: one more element, even
    a text-free one such as a tracking pixel, ends the walk.
    """
    formula = replacement.string
    for parent in replacement.parents:
        children = [child for child in parent.children if isinstance(child, Tag)]
        if parent.name in {"body", "html", "[document]"} or len(children) != 1:
            return
        if parent.get_text(" ", strip=True) != formula:
            return
        if HIDDEN_STYLE.search(attr(parent, "style")):
            del parent["style"]
        for attribute in ("aria-hidden", "hidden"):
            parent.attrs.pop(attribute, None)


def _equation_source(equation: Tag) -> str:
    annotation = equation.select_one('annotation[encoding="application/x-tex"]')
    return (
        attr(equation, "data-tex")
        or attr(equation, "data-latex")
        or (annotation.get_text() if annotation else _presentation(equation))
    )


def parse_html(html: str) -> BeautifulSoup:
    """Parse with lxml's HTML parser. A leading XML declaration (XHTML pages) is dropped: it
    adds nothing to HTML parsing and makes BeautifulSoup warn that the page is XML."""
    return BeautifulSoup(_XML_DECLARATION.sub("", html), "lxml")


def prepare_html(html: str) -> BeautifulSoup:
    """Parse the page and put every equation's source in the text as ``$$source$$``."""
    soup = parse_html(html)
    for equation in soup.select('script[type^="math/tex"], math, mjx-container'):
        if not equation.parent:
            continue
        source = _equation_source(equation)
        if source:
            replacement = soup.new_tag("span", attrs={_MATH_MARK: "1"})
            replacement.string = "$$" + source + "$$"
            equation.replace_with(replacement)
            _reveal(replacement)
    for tag in soup.select("[data-tex], [data-latex]"):
        source = attr(tag, "data-tex") or attr(tag, "data-latex")
        tag.clear()
        tag.append("$$" + source + "$$")
        tag[_MATH_MARK] = "1"
    return soup


def protect_equations(soup: BeautifulSoup) -> list[str]:
    """Swap each equation for an inert token, in place, and return the equations by token number.

    Trafilatura escapes Markdown characters in prose (``z_0`` becomes ``z\\_0``), which breaks
    LaTeX. ``restore_equations`` puts the untouched source back into the extracted text."""
    equations: list[str] = []
    for tag in soup.select(f"[{_MATH_MARK}]"):
        equations.append(tag.get_text())
        tag.clear()
        tag.append(f"EQUATIONTOKEN{len(equations) - 1}END")
    return equations


def restore_equations(text: str, equations: list[str], *, markup: bool) -> str:
    """Replace the tokens of ``protect_equations``; ``markup`` escapes them for HTML output."""

    def restore(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if index >= len(equations):
            return match.group()
        return escape(equations[index], quote=False) if markup else equations[index]

    return _TOKEN.sub(restore, text)


def strip_non_content(soup: BeautifulSoup) -> None:
    """Remove scripts, styles, templates and everything hidden from readers, in place."""
    for tag in soup(_NON_CONTENT_TAGS):
        tag.decompose()
    for tag in soup.find_all(is_hidden):
        if not tag.decomposed and tag.name not in {"html", "body"}:
            tag.decompose()


def _cells(line: str) -> list[str]:
    return _ROW.split(line)[1:-1]


def _is_row(line: str) -> bool:
    return line.startswith("|") and line.endswith("|")


def enhance_table_structure(text: str) -> str:
    """Add the ``| --- |`` separator row to a table that has none, so Markdown renders it."""
    lines = text.split("\n")
    result: list[str] = []
    fence: str | None = None
    in_table = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if _FENCE.match(stripped):
            marker = stripped[0]
            fence = None if fence == marker else (fence or marker)
        row = not fence and _is_row(stripped)
        next_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
        result.append(line)
        if row and not in_table and _is_row(next_line):
            cells = _cells(stripped)
            next_cells = _cells(next_line)
            separator = all(_SEPARATOR_CELL.fullmatch(cell) for cell in next_cells)
            if len(cells) == len(next_cells) and not separator:
                result.append("|" + "|".join(" --- " for _ in cells) + "|")
        in_table = row
    return "\n".join(result)
