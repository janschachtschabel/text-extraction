"""Conversion of fetched content to text, Markdown or HTML, and the links of a page."""

from text_extraction.convert.decode import decode_html
from text_extraction.convert.html import Conversion, OutputFormat, convert_html, convert_plain
from text_extraction.convert.links import Link, page_links

__all__ = [
    "Conversion",
    "Link",
    "OutputFormat",
    "convert_html",
    "convert_plain",
    "decode_html",
    "page_links",
]
