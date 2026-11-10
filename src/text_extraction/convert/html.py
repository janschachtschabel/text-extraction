"""HTML and plain text to the requested output format, with a record of the converter used.

``convert_html`` is synchronous and CPU-bound (callers run it in a thread) and keeps no global
state. It parses the page once, edits that tree in place and serialises it once, which lxml
parses for Trafilatura (with ``huge_tree``, so deep nesting is kept); there is no further copy.

Pipeline: equations become ``$$...$$`` source, data-carried content (JSON-LD ``articleBody``,
YouTube details) replaces a page that shows none, scripts, hidden elements and the PHP error
output a site printed into its page go, and every reference becomes absolute. A block that
starts with an inline run gets that run wrapped in a <p>, which Trafilatura would otherwise
split into one paragraph per element. Trafilatura extracts the main content; when it fails or
finds nothing, the visible text of the page without its navigation takes over. Files that the
content links to and the extractor dropped (a worksheet page is often nothing but a list of
PDF links) follow the text.

Adapted from Website-Textextraction-Selenium (Apache-2.0), see NOTICE.
"""

import re
from copy import copy
from dataclasses import dataclass, field
from html import escape, unescape
from typing import Literal
from urllib.parse import urlsplit

import lxml.etree
import lxml.html
from bs4 import BeautifulSoup
from bs4.element import NavigableString, PageElement, Tag
from trafilatura import extract

from text_extraction.convert.attachments import link_attachments
from text_extraction.convert.embedded import Embedded, embedded_content
from text_extraction.convert.links import (
    absolute_url,
    base_url_of,
    markdown_link,
    urls_in,
)
from text_extraction.convert.listings import render_link_sections
from text_extraction.convert.markup import (
    BLOCK_TAGS,
    append_to_body,
    attr,
    enhance_table_structure,
    has_hidden_class,
    is_hidden,
    is_outside_content,
    prepare_html,
    protect_equations,
    restore_equations,
    strip_non_content,
)
from text_extraction.convert.server_errors import (
    SERVER_ERRORS_REMOVED_WARNING,
    ServerError,
    strip_server_errors,
)

OutputFormat = Literal["markdown", "text", "html"]

_MAX_HEADING_CHARS = 200
_BLOCKS = [
    "address",
    "article",
    "aside",
    "blockquote",
    "dd",
    "details",
    "div",
    "dl",
    "dt",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tr",
    "ul",
]
_CELLS = ["td", "th"]
_TRAFILATURA_FORMAT = {"markdown": "markdown", "text": "txt", "html": "html"}
# Markup and Markdown link targets, matched in linear time: neither part can run past the start
# of the next candidate, so text full of "<" or "](" cannot make the match quadratic.
_MARKUP_OR_LINK_TARGET = re.compile(r"<[^<>]*>|\]\([^()\[\]]*\)")
_LINK_SCHEMES = frozenset({"http", "https"})
# Characters XML (and so lxml) does not accept in text; Trafilatura removes them too.
_XML_INVALID = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


@dataclass(frozen=True)
class Conversion:
    """The converted content, the converter that produced it and what went wrong on the way."""

    content: str
    converter: Literal["trafilatura", "bs4", "plain", "markdown"]
    title: str | None = None
    warnings: list[str] = field(default_factory=list)
    # PHP error messages the site printed into the page, removed from ``content``.
    server_errors: list[ServerError] = field(default_factory=list)


def convert_plain(text: str) -> Conversion:
    """Text that needs no conversion: line endings are normalised, the ends trimmed."""
    content = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return Conversion(content=content, converter="plain")


def page_heading(soup: BeautifulSoup) -> str:
    """Text of the first visible <h1>; empty for screen-reader helpers and overlong headings
    (an unclosed <h1> swallows the rest of the page)."""
    for h1 in soup.find_all("h1"):
        if is_hidden(h1) or has_hidden_class(h1):
            continue
        heading = copy(h1)
        for br in heading.find_all("br"):
            br.replace_with(" ")
        text = " ".join(heading.get_text().split())  # inline markup (H<sub>2</sub>O) adds no spaces
        return text if len(text) <= _MAX_HEADING_CHARS else ""
    return ""


def _title(soup: BeautifulSoup, heading: str) -> str | None:
    if heading:
        return heading
    title = soup.title.get_text() if soup.title else ""
    return " ".join(title.split()) or None


def _compact(text: str) -> str:
    # Letters and digits only, so Markdown emphasis or link targets do not hide a kept heading.
    text = unescape(_MARKUP_OR_LINK_TARGET.sub("", text))
    return re.sub(r"[\W_]+", "", text).casefold()


def _with_heading(text: str, heading: str, fmt: OutputFormat) -> str:
    # Trafilatura keeps the page heading only when it sits inside the detected main content.
    if not heading or _compact(heading) in _compact(text):
        return text
    if fmt == "markdown":
        return text if text.lstrip().startswith("# ") else f"# {heading}\n\n{text}"
    if fmt == "text":
        return f"{heading}\n{text}"
    heading_html = f"<h1>{escape(heading)}</h1>"
    return re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + heading_html, text, count=1)


def _with_documents(text: str, documents: list[tuple[str, str]], fmt: OutputFormat) -> str:
    # Trafilatura drops a list that holds nothing but links, as navigation. On a worksheet page
    # that list is the material, so the files the content links to and the text lacks follow it.
    present = urls_in(text, unescape_entities=fmt == "html")
    missing = [(label, url) for label, url in documents if url not in present]
    if not missing:
        return text
    if fmt == "markdown":
        items = [f"- {markdown_link(label, url)}" for label, url in missing]
        return text + "\n\n---\n\n" + "\n".join(items)
    if fmt == "text":
        return text + "\n\n" + "\n".join(f"{label}: {url}" for label, url in missing)
    items = [
        f'<li><a href="{escape(url, quote=True)}">{escape(label)}</a></li>'
        for label, url in missing
    ]
    return append_to_body(text, f"<hr/><ul>{''.join(items)}</ul>")


def _make_absolute(soup: BeautifulSoup, url: str) -> None:
    """Every href and src resolved against the page (or its <base>) - or removed, when no URL
    parser accepts it or it is not an http(s) URL (``javascript:``, ``data:``, ``mailto:``, ...):
    the output carries web links only. The link's text stays. Without a page URL (an upload)
    a reference that stays relative is kept as it is."""
    base = base_url_of(soup, url)
    for tag in soup.select("[href], [src]"):
        for name in ("href", "src"):
            if tag.get(name):
                target = absolute_url(base, attr(tag, name).strip())
                scheme = _scheme(target) if target is not None else None  # None: unparsable
                if target is not None and (scheme in _LINK_SCHEMES or (scheme == "" and not url)):
                    tag[name] = target
                else:
                    del tag[name]


def _scheme(url: str) -> str | None:
    try:
        return urlsplit(url).scheme.lower()
    except ValueError:
        return None


def _visible_text(soup: BeautifulSoup) -> str:
    """The page's text without its navigation, one line per block, in place (destructive)."""
    if soup.head:
        soup.head.decompose()  # the title is not page text
    for tag in soup.find_all(is_outside_content):
        if not tag.decomposed:
            tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(_BLOCKS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    for tag in soup.find_all(_CELLS):
        tag.insert_after(" ")
    lines = (" ".join(line.split()) for line in soup.get_text("").splitlines())
    return "\n".join(line for line in lines if line)


def _as_format(text: str, fmt: OutputFormat) -> str:
    """Fallback text in the requested format: Markdown and text need no markup, HTML gets <p>."""
    if fmt != "html":
        return text
    paragraphs = "".join(f"<p>{escape(line)}</p>" for line in text.splitlines())
    return f"<html><body>{paragraphs}</body></html>"


# Deepest table nesting handed to Trafilatura: its table handling is quadratic in the nesting
# (2000 nested tables, 30 KB, took about 35 s of CPU). Real pages nest a handful of levels.
MAX_TABLE_NESTING = 16


def flatten_deep_tables(soup: BeautifulSoup) -> None:
    """Rename every ``<table>`` nested more than ``MAX_TABLE_NESTING`` deep to ``<div>``: its
    text stays, its table structure goes. One pass in document order; each table looks up
    only its nearest table ancestor."""
    depth: dict[int, int] = {}
    deep = []
    for table in soup.find_all("table"):
        parent = table.find_parent("table")
        level = 1 if parent is None else depth[id(parent)] + 1
        depth[id(table)] = level
        if level > MAX_TABLE_NESTING:
            deep.append(table)
    for table in deep:
        table.name = "div"


# Block containers whose leading inline run Trafilatura splits: when the first thing in one is
# an inline element or a <br>, every inline element becomes a paragraph of its own and the
# space after it is lost (``<div><b>W</b>: msg in <b>/x.php</b></div>`` gives ``**W**: msg
# in`` and ``**/x.php**``, Trafilatura 2.3.1). The same run inside a <p> comes out right.
# Lists and quotes are not split and stay as they are: a <p> in an <li> can tip a short page
# into Trafilatura's justext rescue, which drops every link and emphasis on the page.
_RUN_CONTAINERS = frozenset(
    {
        *("article", "aside", "div", "figcaption", "figure", "footer", "form", "header"),
        *("main", "section"),
    }
)
# Kept as they are, with everything inside: paragraphs, table cells (Markdown tables must not
# change), preformatted text, headings and code.
_KEEP_STRUCTURE = frozenset(
    {"caption", "code", "h1", "h2", "h3", "h4", "h5", "h6", "p", "pre", "td", "textarea", "th"}
)


def wrap_inline_runs(soup: BeautifulSoup) -> None:
    """Wrap each run of inline children of a block container in a ``<p>``, in place, when the
    container starts with an inline element or a ``<br>`` (see ``_RUN_CONTAINERS``).

    A run is a maximal sequence of text, inline elements and ``<br>``; whitespace and ``<br>``
    at its edges are dropped. Only a run with text of the container's own is wrapped
    (``<b>Fach:</b> Mathematik``): adjacent inline elements alone (``<a>…</a><span>…</span>``
    in a teaser, date and subject spans on a card) stay as they are, as Trafilatura reads them
    apart or drops them. An element that holds a block
    (``<a><div>``) is no inline element here. A container that starts with text is left alone,
    so ordinary prose keeps its paragraphs. One pass over the tree, without recursion."""
    pending: list[Tag] = [soup]
    while pending:
        tag = pending.pop()
        if tag.name in _RUN_CONTAINERS and _starts_inline(tag):
            _wrap_runs(soup, tag)
        pending.extend(
            child
            for child in tag.children
            if isinstance(child, Tag) and child.name not in _KEEP_STRUCTURE
        )


def _flows_inline(node: PageElement) -> bool:
    if isinstance(node, Tag):
        return node.name not in BLOCK_TAGS and node.find(_is_block) is None
    return True  # text, comments


def _is_block(tag: Tag) -> bool:
    return tag.name in BLOCK_TAGS


def _starts_inline(container: Tag) -> bool:
    for child in container.children:
        if isinstance(child, Tag):
            return _flows_inline(child)
        if type(child) is NavigableString and child.strip():
            return False  # leading text: the container is prose
    return False


def _wrap_runs(soup: BeautifulSoup, container: Tag) -> None:
    """Wrap the container's runs, the last first: bs4 finds a node's position in its parent by
    a linear search, so the nodes are taken out from the end with their position given, which
    keeps a container with many children linear."""
    children = list(container.children)
    runs: list[tuple[int, int]] = []
    begin = 0
    for index, child in enumerate(children):
        if not _flows_inline(child):
            runs.append((begin, index))
            begin = index + 1
    runs.append((begin, len(children)))
    for begin, stop in reversed(runs):
        _wrap(soup, container, children, begin, stop)


def _wrap(
    soup: BeautifulSoup, container: Tag, children: list[PageElement], begin: int, stop: int
) -> None:
    if not any(_is_text(children[index]) for index in range(begin, stop)):
        return  # no text of the container's own: teaser links, card meta, key-value spans
    content = [index for index in range(begin, stop) if _has_content(children[index])]
    # Whitespace and <br> before the first and after the last content go: a paragraph's edges
    # need no line break, and a run of nothing else between blocks is dropped.
    moved: list[PageElement] = []
    for index in range(stop - 1, begin - 1, -1):
        node = children[index].extract(_self_index=index)
        if content and content[0] <= index <= content[-1]:
            moved.append(node)
    if not moved:
        return
    paragraph = soup.new_tag("p")
    for node in reversed(moved):
        paragraph.append(node)
    container.insert(begin, paragraph)


def _has_content(node: PageElement) -> bool:
    if isinstance(node, Tag):
        return node.name != "br"
    return _is_text(node)


def _is_text(node: PageElement) -> bool:
    return type(node) is NavigableString and bool(node.strip())


def _tree(html: str) -> lxml.html.HtmlElement | str:
    """The page parsed for Trafilatura, with ``huge_tree``: Trafilatura's own parse stops at
    libxml2's depth limit of 255 elements and silently drops everything nested deeper. The
    options are otherwise Trafilatura's own. Should lxml refuse the text, Trafilatura gets the
    string and parses it itself."""
    parser = lxml.html.HTMLParser(
        collect_ids=False,
        default_doctype=False,
        encoding="utf-8",
        remove_comments=True,
        remove_pis=True,
        huge_tree=True,
    )
    try:
        return lxml.html.fromstring(_XML_INVALID.sub("", html), parser=parser)
    except (ValueError, lxml.etree.ParserError):
        return html


def _extract(html: str, url: str, fmt: OutputFormat, *, recall: bool) -> str | None:
    return extract(
        _tree(html),
        url=url,
        output_format=_TRAFILATURA_FORMAT[fmt],
        include_links=fmt != "text",
        include_tables=True,
        include_comments=False,
        favor_recall=recall,
    )


def _with_attachments(text: str, embedded: Embedded | None, fmt: OutputFormat) -> str:
    """``text`` followed by the link lists of the page's embedded attachments; in plain text
    each item with its URL."""
    rendered = render_link_sections(embedded.sections, fmt, text_urls=True) if embedded else ""
    if not rendered:
        return text
    return append_to_body(text, rendered) if fmt == "html" else f"{text}\n\n{rendered}"


def convert_html(html: str, url: str, fmt: OutputFormat) -> Conversion:
    """The page's main content in ``fmt``.

    ``title`` is the page's first visible ``<h1>``, else its ``<title>``. Trafilatura is the
    converter; ``bs4`` means its result was empty or it failed (see ``warnings``) and the
    visible page text was used instead. Whatever the converter, the page heading and the files
    that the content links to are in the output.
    """
    soup = prepare_html(html)
    embedded = embedded_content(soup, url)
    if embedded:
        soup = prepare_html(embedded.html)
    strip_non_content(soup)
    server_errors = strip_server_errors(soup)
    notes = [SERVER_ERRORS_REMOVED_WARNING] if server_errors else []
    _make_absolute(soup, url)
    heading = page_heading(soup)
    title = _title(soup, heading)
    if not soup.get_text(" ", strip=True):
        return Conversion("", "bs4", title, [*notes, "The page contains no text"], server_errors)
    documents = link_attachments(soup)
    for navigation in soup.select('nav, [role="navigation"]'):
        if not navigation.decomposed:
            navigation.decompose()
    flatten_deep_tables(soup)
    wrap_inline_runs(soup)
    equations = protect_equations(soup)
    markup = fmt == "html"
    warnings = list(notes)
    failed = False
    # The payload is already the selected content; main-content heuristics tend to discard
    # short lesson fragments, so favour recall for it.
    try:
        text = _extract(str(soup), url, fmt, recall=embedded is not None)
    except Exception as exc:  # whatever the extractor raises, the fallback decides
        warnings.append(f"trafilatura failed ({type(exc).__name__}); used the page text instead")
        failed = True
        text = None
    if text and text.strip():
        text = restore_equations(text.strip(), equations, markup=markup)
        text = _with_attachments(_with_heading(text, heading, fmt), embedded, fmt)
        text = _with_documents(text, documents, fmt)
        if fmt == "markdown":
            text = enhance_table_structure(text)
        return Conversion(text, "trafilatura", title, warnings, server_errors)
    if not failed:
        warnings.append("trafilatura found no main content; used the page text instead")
    fallback = _visible_text(soup)
    if not fallback:
        return Conversion(
            "", "bs4", title, [*warnings, "No converter produced text"], server_errors
        )
    content = restore_equations(_as_format(fallback, fmt), equations, markup=markup)
    content = _with_attachments(_with_heading(content, heading, fmt), embedded, fmt)
    content = _with_documents(content, documents, fmt)
    return Conversion(content, "bs4", title, warnings, server_errors)
