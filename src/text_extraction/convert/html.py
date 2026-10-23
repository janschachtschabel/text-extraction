"""HTML and plain text to the requested output format, with a record of the converter used.

``convert_html`` is synchronous and CPU-bound (callers run it in a thread) and keeps no global
state. It parses the page once, edits that tree in place and serialises it once for
Trafilatura; there is no further copy of the document.

Pipeline: equations become ``$$...$$`` source, data-carried content (JSON-LD ``articleBody``,
YouTube details) replaces a page that shows none, scripts and hidden elements go, and every
reference becomes absolute. Trafilatura extracts the main content; when it fails or finds
nothing, the visible text of the page without its navigation takes over. Files that the
content links to and the extractor dropped (a worksheet page is often nothing but a list of
PDF links) follow the text.

Adapted from Website-Textextraction-Selenium (Apache-2.0), see NOTICE.
"""

import re
from copy import copy
from dataclasses import dataclass, field
from html import escape, unescape
from typing import Literal
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup
from trafilatura import extract

from text_extraction.convert.embedded import embedded_html
from text_extraction.convert.links import absolute_url, base_url_of, has_file_extension
from text_extraction.convert.markup import (
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
_URL = re.compile(r"https?://[^\s<>\"'()\[\]]+")
_TRAFILATURA_FORMAT = {"markdown": "markdown", "text": "txt", "html": "html"}


@dataclass(frozen=True)
class Conversion:
    """The converted content, the converter that produced it and what went wrong on the way."""

    content: str
    converter: Literal["trafilatura", "bs4", "plain"]
    title: str | None = None
    warnings: list[str] = field(default_factory=list)


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
    text = unescape(re.sub(r"<[^>]*>|\]\([^)]*\)", "", text))
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


def _document_links(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """Label and URL of each file the page's content links to, in page order: an http(s) link
    whose path ends in one of DOWNLOAD_EXTS.

    Expects the prepared document, whose references are already absolute. A link without text
    is labelled with its aria-label, its title or its file name."""
    found: dict[str, str] = {}
    for anchor in soup.find_all("a", href=True):
        url = attr(anchor, "href").strip()
        parts = urlsplit(url)
        if url in found or parts.scheme not in {"http", "https"}:
            continue
        if not has_file_extension(parts.path):
            continue
        if is_outside_content(anchor) or anchor.find_parent(is_outside_content):
            continue
        label = (
            anchor.get_text(" ", strip=True) or attr(anchor, "aria-label") or attr(anchor, "title")
        )
        found[url] = " ".join(label.split()) or unquote(parts.path.rsplit("/", 1)[-1])
    return [(label, url) for url, label in found.items()]


def _urls_in(text: str, *, unescape_entities: bool) -> set[str]:
    """The URLs the text holds, whole: a link to ``a.pdf.zip`` is no link to ``a.pdf``."""
    if unescape_entities:
        text = unescape(text)
    return {url.rstrip(".,;:") for url in _URL.findall(text)}


def _with_documents(text: str, documents: list[tuple[str, str]], fmt: OutputFormat) -> str:
    # Trafilatura drops a list that holds nothing but links, as navigation. On a worksheet page
    # that list is the material, so the files the content links to and the text lacks follow it.
    present = _urls_in(text, unescape_entities=fmt == "html")
    missing = [(label, url) for label, url in documents if url not in present]
    if not missing:
        return text
    if fmt == "markdown":
        items = []
        for label, url in missing:
            label = label.replace("[", r"\[").replace("]", r"\]")
            target = url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
            items.append(f"- [{label}]({target})")
        return text + "\n\n---\n\n" + "\n".join(items)
    if fmt == "text":
        return text + "\n\n" + "\n".join(f"{label}: {url}" for label, url in missing)
    items = [
        f'<li><a href="{escape(url, quote=True)}">{escape(label)}</a></li>'
        for label, url in missing
    ]
    block = f"<hr/><ul>{''.join(items)}</ul>"
    if "</body>" in text:
        return text.replace("</body>", block + "</body>", 1)
    return text + block


def _make_absolute(soup: BeautifulSoup, url: str) -> None:
    """Every href and src resolved against the page (or its <base>) - or removed, when no URL
    parser accepts it. The link's text stays."""
    base = base_url_of(soup, url)
    for tag in soup.select("[href], [src]"):
        for name in ("href", "src"):
            if tag.get(name):
                target = absolute_url(base, attr(tag, name))
                if target is None:
                    del tag[name]
                else:
                    tag[name] = target


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


def _extract(html: str, url: str, fmt: OutputFormat, *, recall: bool) -> str | None:
    return extract(
        html,
        url=url,
        output_format=_TRAFILATURA_FORMAT[fmt],
        include_links=fmt != "text",
        include_tables=True,
        include_comments=False,
        favor_recall=recall,
    )


def convert_html(html: str, url: str, fmt: OutputFormat) -> Conversion:
    """The page's main content in ``fmt``.

    ``title`` is the page's first visible ``<h1>``, else its ``<title>``. Trafilatura is the
    converter; ``bs4`` means its result was empty or it failed (see ``warnings``) and the
    visible page text was used instead. Whatever the converter, the page heading and the files
    that the content links to are in the output.
    """
    soup = prepare_html(html)
    embedded = embedded_html(soup, url)
    if embedded:
        soup = prepare_html(embedded)
    strip_non_content(soup)
    _make_absolute(soup, url)
    heading = page_heading(soup)
    title = _title(soup, heading)
    if not soup.get_text(" ", strip=True):
        return Conversion("", "bs4", title, ["The page contains no text"])
    documents = _document_links(soup)
    for navigation in soup.select('nav, [role="navigation"]'):
        if not navigation.decomposed:
            navigation.decompose()
    equations = protect_equations(soup)
    markup = fmt == "html"
    warnings: list[str] = []
    # The payload is already the selected content; main-content heuristics tend to discard
    # short lesson fragments, so favour recall for it.
    try:
        text = _extract(str(soup), url, fmt, recall=embedded is not None)
    except Exception as exc:  # whatever the extractor raises, the fallback decides
        warnings.append(f"trafilatura failed ({type(exc).__name__}); used the page text instead")
        text = None
    if text and text.strip():
        text = restore_equations(text.strip(), equations, markup=markup)
        text = _with_documents(_with_heading(text, heading, fmt), documents, fmt)
        if fmt == "markdown":
            text = enhance_table_structure(text)
        return Conversion(text, "trafilatura", title, warnings)
    if not warnings:
        warnings.append("trafilatura found no main content; used the page text instead")
    fallback = _visible_text(soup)
    if not fallback:
        return Conversion("", "bs4", title, [*warnings, "No converter produced text"])
    content = restore_equations(_as_format(fallback, fmt), equations, markup=markup)
    content = _with_documents(_with_heading(content, heading, fmt), documents, fmt)
    return Conversion(content, "bs4", title, warnings)
