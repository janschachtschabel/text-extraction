"""What a document says about itself: title, authors, dates, keywords, licence, language.

Read from the PDF information dictionary and XMP packet, from ``docProps/core.xml`` of Office
files, from ``meta.xml`` of OpenDocument files and from the package document of an EPUB. Every
source is untrusted: the XML goes through ``defusedxml`` (no DTD, no entities, nothing external
is ever fetched), every ZIP entry is read up to ``_MAX_ENTRY_BYTES`` only, and a source that
cannot be read is left out and costs nothing else. Nothing from the document is logged.

Licences are ranked by ``text_extraction.licenses`` like those of a web page. Runs in a
``DocumentPool`` child next to the converter, so it imports neither MarkItDown, pdfminer nor
pandas.
"""

import io
import re
import zipfile
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from itertools import islice
from typing import Any
from xml.etree.ElementTree import Element

import pypdfium2 as pdfium
import structlog
from defusedxml.ElementTree import fromstring as parse_xml

from text_extraction.documents.types import DocumentKind
from text_extraction.licenses import LicenseCollector
from text_extraction.metadata import MAX_FIELD_LENGTH, MAX_KEYWORDS, PageMetadata, detect_language

_log = structlog.get_logger(__name__)

_MAX_ENTRY_BYTES = 1 << 20  # read from one ZIP entry
_MAX_XMP_BYTES = 1 << 20  # one XMP packet
_MAX_XMP_PACKETS = 16
_MAX_ELEMENTS = 20_000  # looked at in one XML document
_MAX_VALUES = 50  # of one property, and names in the result

_RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
_COLLECTIONS = frozenset({_RDF + "Alt", _RDF + "Seq", _RDF + "Bag"})

_DC = "{http://purl.org/dc/elements/1.1/}"
_DCTERMS = "{http://purl.org/dc/terms/}"
_XMP = "{http://ns.adobe.com/xap/1.0/}"
_XMP_RIGHTS = "{http://ns.adobe.com/xap/1.0/rights/}"
_PDF_NS = "{http://ns.adobe.com/pdf/1.3/}"
_CC = "{http://creativecommons.org/ns#}"
_CORE = "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"
_ODF_META = "{urn:oasis:names:tc:opendocument:xmlns:meta:1.0}"
_OPF = "{http://www.idpf.org/2007/opf}"

_Fields = dict[str, Any]  # keyword arguments of PageMetadata, without licences and language

_PDF_DATE = re.compile(
    r"D:(\d{4})(\d\d)?(\d\d)?(\d\d)?(\d\d)?(\d\d)?(?:(Z)|([+-])(\d\d)(?:'?(\d\d)'?)?)?",
    re.IGNORECASE,
)
_SPLIT_KEYWORDS = re.compile(r"[,;]")


# --- helpers -----------------------------------------------------------------------------------


def _squash(value: str) -> str:
    return " ".join(value.split())[:MAX_FIELD_LENGTH]


def _first(values: Iterable[str]) -> str | None:
    for value in values:
        text = _squash(value)
        if text:
            return text
    return None


def _distinct(values: Iterable[str], limit: int) -> list[str]:
    found: dict[str, None] = {}
    for value in values:
        text = _squash(value)
        if text:
            found.setdefault(text)
            if len(found) >= limit:
                break
    return list(found)


def _names(values: Iterable[str]) -> list[str]:
    """People: one entry per value, and per ``;`` inside a value (``Author: A; B``)."""
    return _distinct((part for value in values for part in value.split(";")), _MAX_VALUES)


def _keywords(values: Iterable[str]) -> list[str]:
    return _distinct(
        (part for value in values for part in _SPLIT_KEYWORDS.split(value)), MAX_KEYWORDS
    )


def _guard(read: Callable[..., _Fields], kind: str, *args: Any) -> _Fields:
    """What ``read`` finds; nothing when it fails. Only the exception's type is logged."""
    try:
        return read(*args)
    except Exception as error:  # a broken or hostile source costs that source only
        _log.debug("document_metadata_failed", kind=kind, error=type(error).__name__)
        return {}


def _merge(*sources: _Fields) -> _Fields:
    """The first non-empty value of each field, the sources in order of trust."""
    merged: _Fields = {}
    for source in sources:
        for key, value in source.items():
            if value and not merged.get(key):
                merged[key] = value
    return merged


# --- XML ---------------------------------------------------------------------------------------


def _parse(data: bytes) -> Element:
    # A DTD is refused outright: no document metadata part carries one.
    root: Element = parse_xml(data, forbid_dtd=True)
    return root


def _values(element: Element) -> list[str]:
    """The values of one property: its text, its ``rdf:resource``, and the items of an
    ``rdf:Alt``/``Seq``/``Bag`` inside it (``x-default`` first)."""
    found: list[str] = []
    resource = element.get(_RDF + "resource")
    if resource:
        found.append(resource)
    if element.text and element.text.strip():
        found.append(element.text)
    for child in element:
        if child.tag in _COLLECTIONS:
            items = [item for item in child if item.tag == _RDF + "li"]
            items.sort(key=lambda item: item.get(_XML_LANG) != "x-default")
            for item in items[:_MAX_VALUES]:
                found.extend(_values(item))
    return found[:_MAX_VALUES]


def _properties(root: Element, wanted: frozenset[str]) -> dict[str, list[str]]:
    """Values by property name (``{namespace}local``) of the wanted properties, as elements
    and as attributes (the short form of XMP: ``<rdf:Description dc:format="...">``)."""
    found: dict[str, list[str]] = {}
    for element in islice(root.iter(), _MAX_ELEMENTS):
        for name, value in element.attrib.items():
            if name in wanted and value.strip():
                found.setdefault(name, []).append(value)
        if element.tag in wanted:
            found.setdefault(element.tag, []).extend(_values(element))
    return found


def _get(properties: dict[str, list[str]], *names: str) -> list[str]:
    return [value for name in names for value in properties.get(name, [])]


def _read_entry(archive: zipfile.ZipFile, name: str) -> bytes:
    with archive.open(name) as entry:
        return entry.read(_MAX_ENTRY_BYTES)


# --- PDF ---------------------------------------------------------------------------------------

_XMP_PROPERTIES = frozenset(
    {
        _DC + "title",
        _DC + "description",
        _DC + "creator",
        _DC + "subject",
        _DC + "language",
        _DC + "rights",
        _DC + "date",
        _PDF_NS + "Keywords",
        _CC + "license",
        _XMP_RIGHTS + "WebStatement",
        _XMP_RIGHTS + "UsageTerms",
        _XMP + "CreateDate",
        _XMP + "ModifyDate",
    }
)


_XMP_START, _XMP_END = b"<x:xmpmeta", b"</x:xmpmeta>"
_ROOT_REFERENCE = re.compile(rb"/Root\s+(\d+)\s+(\d+)\s+R")
_METADATA_REFERENCE = re.compile(rb"/Metadata\s+(\d+)\s+(\d+)\s+R")
_METADATA_TYPE = re.compile(rb"/Type\s*/Metadata\b")
# What may stand between ``stream`` and the packet: white space and the ``<?xpacket?>`` header.
_PACKET_PREAMBLE = re.compile(rb"\s*(?:<\?xpacket[^>]{0,200}\?>\s*)?")
_OBJECT_DICTIONARY_BYTES = 1 << 16  # looked at after ``N G obj``
_TYPE_WINDOW_BYTES = 256  # before the ``stream`` keyword of a packet found by search
# A hostile body is full of look-alikes: the searches below stay bounded or linear.
_MAX_ROOTS = 3  # ``/Root`` references tried, newest first
# PDF white space and the delimiters that may end the token before ``N G obj``; after ``obj``
# any white space or delimiter ends the keyword, a regular character would continue it.
_PDF_WHITESPACE = b"\x00\t\n\x0c\r "
_BEFORE_OBJECT = frozenset(_PDF_WHITESPACE + b">])")
_AFTER_OBJECT = frozenset(_PDF_WHITESPACE + b"()<>[]{}/%")


def _last_definition(body: bytes, number: bytes, generation: bytes) -> int | None:
    """The offset just after the last ``N G obj`` in the file (the newest definition), found
    by searching backwards for the usual spelling with single spaces.

    A hit counts only after white space, a delimiter or at the start of the file, and only
    when white space, a delimiter or the end of the file follows ``obj``: ``11 0 obj`` and
    ``1 0 objx`` are look-alikes and skipped. Each search goes on from the previous hit, so the
    searches cover disjoint stretches and the walk reads the body once, however many
    look-alikes it holds. None when there is none."""
    needle = b"%b %b obj" % (number, generation)
    end = len(body)
    while (found := body.rfind(needle, 0, end)) >= 0:
        after = found + len(needle)
        if (found == 0 or body[found - 1] in _BEFORE_OBJECT) and (
            after == len(body) or body[after] in _AFTER_OBJECT
        ):
            return after
        end = found  # the needle cannot overlap itself: it starts with a digit, ends with j
    return None


def _packet_at(body: bytes, start: int) -> bytes | None:
    end = body.find(_XMP_END, start, start + _MAX_XMP_BYTES)
    return body[start : end + len(_XMP_END)] if end >= 0 else None


def _root_references(body: bytes) -> Iterator[tuple[bytes, bytes]]:
    """``(number, generation)`` of the trailer's ``/Root``, newest first."""
    end = len(body)
    for _ in range(_MAX_ROOTS):
        found = body.rfind(b"/Root", 0, end)
        if found < 0:
            return
        match = _ROOT_REFERENCE.match(body, found)
        if match:
            yield match[1], match[2]
        end = found


def _catalog_packets(body: bytes) -> Iterator[bytes]:
    """The packet in the stream that the catalog's ``/Metadata N G R`` points to. Finds none
    when the catalog sits in an object stream or the file spells ``N G obj`` unusually."""
    definitions: dict[tuple[bytes, bytes], int | None] = {}  # each object is looked up once

    def definition(number: bytes, generation: bytes) -> int | None:
        if (number, generation) not in definitions:
            definitions[number, generation] = _last_definition(body, number, generation)
        return definitions[number, generation]

    for root in _root_references(body):
        root_start = definition(*root)
        if root_start is None:
            continue
        reference = _METADATA_REFERENCE.search(
            body, root_start, root_start + _OBJECT_DICTIONARY_BYTES
        )
        start = definition(reference[1], reference[2]) if reference else None
        if start is None:
            continue
        window = body[start : start + _MAX_XMP_BYTES + _OBJECT_DICTIONARY_BYTES]
        stream = window.find(b"stream")
        packet_start = window.find(_XMP_START, stream)
        if stream < 0 or packet_start < 0 or not _METADATA_TYPE.search(window, 0, stream):
            continue
        if _PACKET_PREAMBLE.fullmatch(window, stream + 6, packet_start):
            packet = _packet_at(window, packet_start)
            if packet:
                yield packet


def _typed_packets(body: bytes) -> Iterator[bytes]:
    """Packets whose stream says ``/Type /Metadata`` and starts with the packet, newest first:
    the fallback where the catalog cannot be read. A packet inside another kind of stream (the
    XMP of an embedded JPEG) is not the document's."""
    end = len(body)
    for _ in range(_MAX_XMP_PACKETS):
        start = body.rfind(_XMP_START, 0, end)
        if start < 0:
            return
        end = start
        keyword = body.rfind(b"stream", max(0, start - 256), start)
        if keyword < 0 or not _PACKET_PREAMBLE.fullmatch(body, keyword + 6, start):
            continue
        if _METADATA_TYPE.search(body, max(0, keyword - _TYPE_WINDOW_BYTES), keyword):
            packet = _packet_at(body, start)
            if packet:
                yield packet


def _xmp_packets(body: bytes) -> Iterator[bytes]:
    """The document's XMP packets, most trusted first.

    pypdfium2 has no call for the ``/Metadata`` stream, and XMP is stored uncompressed so that
    tools without a PDF parser can find it: a bounded byte search does the same. Only the
    stream the catalog names counts, or, without a readable catalog, a stream typed
    ``/Type /Metadata``; the XMP of an embedded image (raw JPEG) is not the document's.
    Packets inside compressed streams are not seen."""
    yield from _catalog_packets(body)
    yield from _typed_packets(body)


def _pdf_xmp(body: bytes, licenses: LicenseCollector) -> _Fields:
    props = None
    for packet in _xmp_packets(body):
        try:
            props = _properties(_parse(packet), _XMP_PROPERTIES)
        except Exception as error:  # a damaged packet: the next candidate may be sound
            _log.debug("document_metadata_failed", kind="pdf-xmp", error=type(error).__name__)
            continue
        break
    if props is None:
        return {}
    for value in _get(props, _CC + "license"):
        licenses.add(value, "xmp", "high")
    for value in _get(props, _XMP_RIGHTS + "WebStatement", _XMP_RIGHTS + "UsageTerms"):
        licenses.add(value, "xmp", "medium")
    for value in _get(props, _DC + "rights"):
        licenses.add(value, "xmp", "medium")
    return {
        "title": _first(_get(props, _DC + "title")),
        "description": _first(_get(props, _DC + "description")),
        "authors": _names(_get(props, _DC + "creator")),
        "published": _first(_get(props, _XMP + "CreateDate", _DC + "date")),
        "modified": _first(_get(props, _XMP + "ModifyDate")),
        "keywords": _keywords(_get(props, _DC + "subject", _PDF_NS + "Keywords")),
        "language": _first(_get(props, _DC + "language")),
    }


def _pdf_date(value: str) -> str | None:
    """``2024-03-15T09:30:00+01:00`` from ``D:20240315093000+01'00'``; None for other text."""
    match = _PDF_DATE.match(value.strip())
    if not match:
        return None
    year, month, day, hour, minute, second, zulu, sign, offset_h, offset_m = match.groups()
    text = f"{year}-{month or '01'}-{day or '01'}"
    if hour:
        text += f"T{hour}:{minute or '00'}:{second or '00'}"
        if zulu:
            text += "Z"
        elif sign:
            text += f"{sign}{offset_h}:{offset_m or '00'}"
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return None
    return text


def _pdf_info(body: bytes) -> _Fields:
    pdf = pdfium.PdfDocument(body)
    try:
        info = pdf.get_metadata_dict(skip_empty=True)
    finally:
        pdf.close()
    return {
        "title": _first([info.get("Title", "")]),
        "description": _first([info.get("Subject", "")]),
        "authors": _names([info.get("Author", "")]),
        "published": _pdf_date(info.get("CreationDate", "")),
        "modified": _pdf_date(info.get("ModDate", "")),
        "keywords": _keywords([info.get("Keywords", "")]),
    }


def _pdf(body: bytes, licenses: LicenseCollector) -> _Fields:
    return _merge(_guard(_pdf_xmp, "pdf-xmp", body, licenses), _guard(_pdf_info, "pdf-info", body))


# --- Office, OpenDocument, EPUB ----------------------------------------------------------------

_CORE_PROPERTIES = frozenset(
    {
        _DC + "title",
        _DC + "description",
        _DC + "creator",
        _DC + "language",
        _CORE + "keywords",
        _DCTERMS + "created",
        _DCTERMS + "modified",
    }
)


def _core_properties(body: bytes) -> _Fields:
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        root = _parse(_read_entry(archive, "docProps/core.xml"))
    props = _properties(root, _CORE_PROPERTIES)
    return {
        "title": _first(_get(props, _DC + "title")),
        "description": _first(_get(props, _DC + "description")),
        "authors": _names(_get(props, _DC + "creator")),
        "published": _first(_get(props, _DCTERMS + "created")),
        "modified": _first(_get(props, _DCTERMS + "modified")),
        "keywords": _keywords(_get(props, _CORE + "keywords")),
        "language": _first(_get(props, _DC + "language")),
    }


_ODF_PROPERTIES = frozenset(
    {
        _DC + "title",
        _DC + "description",
        _DC + "creator",
        _DC + "date",
        _DC + "language",
        _ODF_META + "initial-creator",
        _ODF_META + "creation-date",
        _ODF_META + "keyword",
    }
)


def _odf_meta(body: bytes) -> _Fields:
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        root = _parse(_read_entry(archive, "meta.xml"))
    props = _properties(root, _ODF_PROPERTIES)
    return {
        "title": _first(_get(props, _DC + "title")),
        "description": _first(_get(props, _DC + "description")),
        # dc:creator is whoever saved last; the author is meta:initial-creator.
        "authors": _names(_get(props, _ODF_META + "initial-creator", _DC + "creator")),
        "published": _first(_get(props, _ODF_META + "creation-date")),
        "modified": _first(_get(props, _DC + "date")),
        "keywords": _keywords(_get(props, _ODF_META + "keyword")),
        "language": _first(_get(props, _DC + "language")),
    }


_OPF_PROPERTIES = frozenset(
    {
        _DC + "title",
        _DC + "creator",
        _DC + "description",
        _DC + "language",
        _DC + "date",
        _DC + "subject",
        _DC + "publisher",
        _DC + "rights",
    }
)


def _opf_path(container: Element) -> str:
    for element in islice(container.iter(), _MAX_ELEMENTS):
        if element.tag.rpartition("}")[2] == "rootfile" and element.get("full-path"):
            return str(element.get("full-path"))
    raise ValueError("no rootfile")


def _epub(body: bytes, licenses: LicenseCollector) -> _Fields:
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        container = _parse(_read_entry(archive, "META-INF/container.xml"))
        package = _parse(_read_entry(archive, _opf_path(container)))
    props = _properties(package, _OPF_PROPERTIES)
    modified = _first(
        meta.text or ""
        for meta in islice(package.iter(_OPF + "meta"), _MAX_ELEMENTS)
        if meta.get("property") == "dcterms:modified"
    )
    for value in _get(props, _DC + "rights"):
        licenses.add(value, "meta", "medium")
    return {
        "title": _first(_get(props, _DC + "title")),
        "description": _first(_get(props, _DC + "description")),
        "authors": _names(_get(props, _DC + "creator")),
        "published": _first(_get(props, _DC + "date")),
        "modified": modified,
        "keywords": _keywords(_get(props, _DC + "subject")),
        "site_name": _first(_get(props, _DC + "publisher")),
        "language": _first(_get(props, _DC + "language")),
    }


# --- entry point -------------------------------------------------------------------------------


def document_metadata(
    body: bytes,
    kind: DocumentKind,
    base: str | None,
    content_language: str | None,
    text: str,
) -> PageMetadata:
    """The metadata a document declares. ``base`` is the document's URL (None for an upload);
    ``content_language`` the ``Content-Language`` header; ``text`` the converted text, for the
    language guess when neither the document nor the header names one.

    Never raises on a bad file: a source that cannot be read is left out. Kinds without
    metadata of their own (spreadsheets of the old format, CSV, images) give the language only."""
    licenses = LicenseCollector(base or "")
    fields: _Fields
    match kind:
        case "pdf":
            fields = _pdf(body, licenses)
        case "docx" | "pptx" | "xlsx":
            fields = _guard(_core_properties, kind, body)
        case "odt" | "ods" | "odp":
            fields = _guard(_odf_meta, kind, body)
        case "epub":
            fields = _guard(_epub, kind, body, licenses)
        case _:
            fields = {}
    language = detect_language(fields.pop("language", None), content_language, text)
    found = licenses.result()
    return PageMetadata(
        **{key: value for key, value in fields.items() if value},
        language=language,
        license=found[0] if found else None,
        licenses=found,
    )
