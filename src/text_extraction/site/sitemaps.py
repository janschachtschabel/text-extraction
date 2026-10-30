"""Sitemap parsing within bounds (rulings P9-S8, P9-S9). Pure: no fetch, no Valkey; the
runner calls it on the worker's conversion pool.

A sitemap is untrusted input, so every step is bounded. ``inflate_bounded`` inflates a gzip body
(found by its magic bytes, whatever its name or type) in steps of at most 64 KiB and stops at
``max_bytes``, so a gzip bomb costs no more than the cap. ``parse_sitemap`` streams the XML
through ``defusedxml`` (no DTD, no entities, nothing external) and drops every element once it
is read, so the tree never holds more than the open path. ``pack_parts`` splits a large file's
entries into parts that each fit an item's result.

Formats: ``urlset`` (``url/loc``, ``lastmod``, ``changefreq``, ``priority``), ``index``
(``sitemapindex``: ``sitemap/loc``, ``lastmod``), ``rss`` (``channel/item/link``, ``pubDate``),
``atom`` (``feed/entry/link[@href]`` with ``rel`` ``alternate`` or none, ``updated``) and
``text`` (one URL per line). Namespaces and prefixes are ignored; elements match by local name,
and a file that uses a prefix without declaring it (``image:`` with no ``xmlns:image``) is read
again with its tag prefixes dropped. Nesting deeper than ``MAX_XML_DEPTH``, an encoding Python
cannot read incrementally (``shift_jis``, ``big5``, an unknown name) and a UTF-16 file are
``invalid``.
Not read: the ``image:``, ``video:``, ``news:`` and ``xhtml:link`` extensions.
"""

import io
import re
import time
import zlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pyexpat import errors as expat_errors
from typing import Literal
from urllib.parse import urljoin
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException, DTDForbidden
from defusedxml.ElementTree import iterparse

from text_extraction.site.types import SitemapChild, SitemapEntry
from text_extraction.site.urls import normalize_url

__all__ = [
    "MAX_INDEX_CHILDREN",
    "MAX_XML_DEPTH",
    "ParsedFormat",
    "ParsedSitemap",
    "inflate_bounded",
    "pack_parts",
    "parse_sitemap",
]

ParsedFormat = Literal["urlset", "index", "text", "rss", "atom"]
# The protocol's limit of sitemaps in one index; more are dropped and the parse is truncated.
MAX_INDEX_CHILDREN = 50_000

_GZIP_MAGIC = b"\x1f\x8b"
_INFLATE_STEP = 1 << 16
_UTF8_BOM = b"\xef\xbb\xbf"
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
_LEADING_BLANKS = re.compile(rb"[ \t\r\n]*")
# The deepest layout read is rss/channel/item/<field>; deeper nesting is refused, since every
# open element stays in memory until it closes.
MAX_XML_DEPTH = 32
# How many parser events (or text lines) pass between two looks at the clock of a deadline.
_CLOCK_EVERY = 1024
# A tag's namespace prefix, for the retry of a file that uses one without declaring it.
_PREFIXED_TAG = re.compile(rb"(</?)[A-Za-z_][\w.\-]*:")
_UNBOUND_PREFIX = expat_errors.codes[expat_errors.XML_ERROR_UNBOUND_PREFIX]


class _UnboundPrefixError(Exception):
    """The file uses a namespace prefix it does not declare (``image:`` without ``xmlns``)."""


class _DeadlineError(Exception):
    """The parse ran into its deadline: what was read by then is the answer."""


class _CapReachedError(Exception):
    """``stop_at_cap``: an entry over ``max_entries`` was read, so the parse ends there."""


@dataclass(frozen=True)
class _Layout:
    """Where a format keeps its records: the root's local name, the record's local name and
    its depth below the root, and which child elements are read into which field."""

    format: ParsedFormat
    record: str
    depth: int
    fields: dict[str, str]


_LAYOUTS = {
    "urlset": _Layout(
        "urlset",
        "url",
        1,
        {"loc": "loc", "lastmod": "lastmod", "changefreq": "changefreq", "priority": "priority"},
    ),
    "sitemapindex": _Layout("index", "sitemap", 1, {"loc": "loc", "lastmod": "lastmod"}),
    "rss": _Layout("rss", "item", 2, {"link": "loc", "pubDate": "lastmod"}),
    "feed": _Layout("atom", "entry", 1, {"updated": "lastmod"}),  # loc: <link href>, below
}


@dataclass
class ParsedSitemap:
    """What one sitemap file held.

    ``format`` is None when the file is no sitemap (``error: "unsupported"``) or broke before its
    root was read. ``entries`` holds at most ``max_entries`` entries, ``entry_count`` counts every
    entry with a usable ``loc`` in the whole file. ``children`` (an index's sitemaps) say
    ``crawl: listed`` until the runner decides. ``truncated``: entries or children were left out
    (over the caps, or the file broke after some were read). ``error``: ``"unsupported"`` (another
    root, such as an HTML page) or ``"invalid"`` (no XML, a DTD or entity, broken before any
    entry). ``timed_out``: the parse stopped at its deadline (``truncated`` too); ``entry_count``
    then counts what was read by then.
    """

    format: ParsedFormat | None
    entries: list[SitemapEntry] = field(default_factory=list)
    children: list[SitemapChild] = field(default_factory=list)
    entry_count: int = 0
    truncated: bool = False
    error: Literal["invalid", "unsupported"] | None = None
    timed_out: bool = False


def inflate_bounded(body: bytes, max_bytes: int) -> tuple[bytes, bool]:
    """The body inflated when it starts with the gzip magic bytes, else unchanged; and whether
    the result was cut: at ``max_bytes``, or because the stream broke or ended early (what was
    inflated by then is kept). Concatenated gzip members are read in turn; anything else after
    the last member is ignored.

    The input goes in in slices of 64 KiB and each step yields at most 64 KiB, so no step copies
    more than a slice (``unconsumed_tail`` is a copy of what is left of the input it was given).
    """
    if not body.startswith(_GZIP_MAGIC):
        return body, False
    view = memoryview(body)
    chunks: list[bytes] = []  # joined once: a growing bytearray over-allocates
    size = 0
    pos = 0  # the input before it has been handed to the inflater
    try:
        while view[pos : pos + len(_GZIP_MAGIC)] == _GZIP_MAGIC:
            inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
            pending: bytes | memoryview = b""
            while not inflater.eof:
                if not pending and pos < len(view):
                    pending = view[pos : pos + _INFLATE_STEP]
                    pos += len(pending)
                room = max_bytes - size
                # At the cap, one more byte tells whether the stream had more.
                chunk = inflater.decompress(pending, max(1, min(room, _INFLATE_STEP)))
                pending = inflater.unconsumed_tail
                if chunk and room <= 0:
                    return b"".join(chunks), True
                chunks.append(chunk)
                size += len(chunk)
                if not chunk and not pending and pos >= len(view) and not inflater.eof:
                    return b"".join(chunks), True  # the input ended inside the stream
            pos -= len(inflater.unused_data)  # the next member starts in the last slice
    except zlib.error:
        return b"".join(chunks), True
    return b"".join(chunks), False


def _local(tag: object) -> str:
    """An element's name without its namespace (``{ns}url`` and ``sm:url`` read ``url``)."""
    if not isinstance(tag, str):
        return ""
    return tag.rpartition("}")[2].rpartition(":")[2]


def _resolve(loc: str | None, base_url: str) -> str | None:
    if not loc:
        return None
    loc = loc.strip()
    if not loc:
        return None
    try:
        absolute = urljoin(base_url, loc)
    except ValueError:
        return None
    return normalize_url(absolute)


class _Collector:
    """Counts and keeps the records of one file within the caps."""

    def __init__(
        self, max_entries: int, deadline: float | None = None, *, stop_at_cap: bool = False
    ) -> None:
        self.max_entries = max_entries
        self.parsed = ParsedSitemap(format=None)
        self._deadline = deadline
        self._stop_at_cap = stop_at_cap
        self._ticks = 0

    def tick(self) -> None:
        """Count one parser event; every ``_CLOCK_EVERY`` events, raise ``_DeadlineError``
        once the deadline (``time.monotonic``) has passed."""
        if self._deadline is None:
            return
        self._ticks += 1
        if self._ticks % _CLOCK_EVERY == 0 and time.monotonic() >= self._deadline:
            raise _DeadlineError

    @property
    def found(self) -> bool:
        return self.parsed.entry_count > 0 or bool(self.parsed.children)

    def entry(self, loc: str, values: dict[str, str]) -> None:
        parsed = self.parsed
        parsed.entry_count += 1
        if len(parsed.entries) >= self.max_entries:
            parsed.truncated = True
            if self._stop_at_cap:
                raise _CapReachedError
            return
        parsed.entries.append(SitemapEntry.model_validate({**values, "loc": loc}))

    def child(self, url: str, values: dict[str, str]) -> None:
        parsed = self.parsed
        if len(parsed.children) >= MAX_INDEX_CHILDREN:
            parsed.truncated = True
            return
        parsed.children.append(
            SitemapChild.model_validate(
                {"url": url, "lastmod": values.get("lastmod"), "crawl": "listed"}
            )
        )


def _text_lines(body: bytes, start: int) -> Iterator[str]:
    for line in body[start:].decode("utf-8", errors="replace").splitlines():
        if "\ufffd" not in line:  # not UTF-8: no URL
            yield line


def _parse_text(body: bytes, start: int, collector: _Collector) -> ParsedSitemap:
    collector.parsed.format = "text"
    try:
        for line in _text_lines(body, start):
            collector.tick()
            url = normalize_url(line)
            if url is not None:
                collector.entry(url, {})
    except _DeadlineError:
        collector.parsed.truncated = collector.parsed.timed_out = True
    except _CapReachedError:
        pass  # ``truncated`` is set
    return collector.parsed


def _read_field(layout: _Layout, element: Element, name: str, record: dict[str, str]) -> None:
    if layout.format == "atom" and name == "link":
        rel = (element.get("rel") or "alternate").strip().lower()
        href = element.get("href")
        if rel == "alternate" and href is not None:
            record.setdefault("loc", href)
        return
    key = layout.fields.get(name)
    text = (element.text or "").strip()
    if key is not None and text:
        record.setdefault(key, text)  # the first one that says something wins


def _parse_xml(
    body: bytes, start: int, base_url: str, collector: _Collector, *, retry_prefixes: bool
) -> ParsedSitemap:
    """Raises ``_UnboundPrefixError`` (when ``retry_prefixes``) for the caller to parse again
    without prefixes; every other failure is in the answer."""
    parsed = collector.parsed
    source = io.BytesIO(body)  # shares the bytes, no copy
    source.seek(start)
    try:
        layout: _Layout | None = None
        stack: list[Element] = []
        record: dict[str, str] | None = None
        root_closed = False
        try:
            for event, element in iterparse(
                source,
                events=("start", "end"),
                forbid_dtd=True,
                forbid_entities=True,
                forbid_external=True,
            ):
                collector.tick()
                name = _local(element.tag)
                if event == "start":
                    if layout is None:
                        layout = _LAYOUTS.get(name)
                        if layout is None:
                            return ParsedSitemap(format=None, error="unsupported")
                        parsed.format = layout.format
                    if len(stack) == layout.depth and name == layout.record:
                        record = {}
                    stack.append(element)
                    if len(stack) > MAX_XML_DEPTH:
                        raise ParseError("nested too deep")
                    continue
                stack.pop()
                if layout is not None and record is not None:
                    if len(stack) == layout.depth + 1:
                        _read_field(layout, element, name, record)
                    elif len(stack) == layout.depth and name == layout.record:
                        loc = _resolve(record.pop("loc", None), base_url)
                        if loc is not None:
                            if layout.format == "index":
                                collector.child(loc, record)
                            else:
                                collector.entry(loc, record)
                        record = None
                if stack:
                    stack[-1].remove(element)  # keep only the open path in memory
                else:
                    root_closed = True
        except _DeadlineError:
            parsed.truncated = parsed.timed_out = True
        except _CapReachedError:
            pass  # ``truncated`` is set
        except DTDForbidden as exc:
            if (exc.name or "").lower() == "html":
                return ParsedSitemap(format=None, error="unsupported")  # an HTML page
            return ParsedSitemap(format=None, error="invalid")
        except ParseError as exc:
            if retry_prefixes and getattr(exc, "code", None) == _UNBOUND_PREFIX:
                raise _UnboundPrefixError from None
            if root_closed:
                return parsed  # junk after the root: every record was read
            if not collector.found:
                return ParsedSitemap(format=parsed.format, error="invalid")
            parsed.truncated = True
        except (DefusedXmlException, LookupError, ValueError):
            # LookupError, ValueError: an unknown or a multi-byte encoding declared
            if not collector.found:
                return ParsedSitemap(format=parsed.format, error="invalid")
            parsed.truncated = True
        return parsed
    finally:
        # The parser's objects form a cycle that holds the stream; closing it drops the buffer
        # (the retry's prefix-stripped copy) now, not at the next cyclic collection.
        source.close()


def parse_sitemap(
    body: bytes,
    base_url: str,
    *,
    max_entries: int,
    deadline: float | None = None,
    stop_at_cap: bool = False,
) -> ParsedSitemap:
    """Read a sitemap file (already inflated): its format, at most ``max_entries`` entries (or an
    index's children), and how many entries it held. Each ``loc`` is resolved against
    ``base_url`` and normalized; one that is no http(s) URL is dropped and not counted. A body
    whose first byte after a BOM and blanks is not ``<`` is a text sitemap.

    ``deadline`` (``time.monotonic``) bounds the parse's time, which grows with the file (about
    0.35 s per MB of an ordinary sitemap, up to 2.7 s per MB of a tag-dense one): at it the
    parse stops with what it has read (``timed_out``). A thread cannot be stopped from outside,
    so the runner gives the deadline here, where the work is.

    ``stop_at_cap`` ends the parse at the first entry over ``max_entries`` (``truncated``;
    ``entry_count`` counts what was read by then): for a crawl whose entry budget is used up,
    which would only count the rest. An index is read whole: its children are no entries."""
    if body.startswith(_UTF16_BOMS):
        return ParsedSitemap(format=None, error="invalid")  # not read: no empty text sitemap
    start = len(_UTF8_BOM) if body.startswith(_UTF8_BOM) else 0
    match = _LEADING_BLANKS.match(body, start)
    start = match.end() if match else start
    if not body.startswith(b"<", start):
        return _parse_text(body, start, _Collector(max_entries, deadline, stop_at_cap=stop_at_cap))
    try:
        return _parse_xml(
            body,
            start,
            base_url,
            _Collector(max_entries, deadline, stop_at_cap=stop_at_cap),
            retry_prefixes=True,
        )
    except _UnboundPrefixError:
        pass
    # Elements match by local name anyway, so a prefix nobody declared is dropped and the file
    # read once more from the start, with a fresh count and the same defusedxml rules. The copy
    # lives only for the retry: it is bound to no local here, since the first pass's error stays
    # in the pull parser's queue (a reference cycle) and keeps this frame until a collection.
    return _parse_xml(
        _PREFIXED_TAG.sub(rb"\1", memoryview(body)[start:]),
        0,
        base_url,
        _Collector(max_entries, deadline, stop_at_cap=stop_at_cap),
        retry_prefixes=False,
    )


def pack_parts(entries: Sequence[SitemapEntry], max_bytes: int) -> list[list[SitemapEntry]]:
    """The entries in order, split greedily so that each part's JSON array takes at most
    ``max_bytes`` (UTF-8). An entry larger than that alone is a part of its own. No entries
    are one empty part, so a sitemap always has part 1."""
    parts: list[list[SitemapEntry]] = [[]]
    size = 2  # the brackets
    for entry in entries:
        length = len(entry.model_dump_json().encode())
        if parts[-1] and size + 1 + length > max_bytes:
            parts.append([])
            size = 2
        size += length + (1 if parts[-1] else 0)  # a comma before all but the first
        parts[-1].append(entry)
    return parts
