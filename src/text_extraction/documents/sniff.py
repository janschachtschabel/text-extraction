"""Decide what a body is, by its bytes, and refuse container bombs before conversion.

Magic bytes win; the declared type decides only where the bytes do not; the name (URL path
or upload name) only breaks the CSV tie, and for an upload without a real declared type it
names a text body's type (``named_text_type``, issue #17). Nothing here decompresses a ZIP
entry beyond the few hundred bytes it needs to name the format, and nothing imports a converter.
"""

import io
import re
import struct
import zipfile
from typing import Final, Literal

from text_extraction.documents.types import DocumentKind

__all__ = [
    "TEXT_TYPES",
    "DocumentTooLarge",
    "check_container",
    "media_type",
    "named_text_type",
    "not_text",
    "sniff",
]

MAX_ZIP_ENTRIES: Final = 10_000
MAX_RATIO: Final = 100
# Entries below this size are exempt from the ratio rule: a sparse sheet of a few kilobytes
# legitimately compresses by more than 100, and the entry and total limits bound them anyway.
RATIO_MIN_BYTES: Final = 1024 * 1024

_PDF_WINDOW: Final = 1024
_HTML_TYPES: Final = frozenset({"text/html", "application/xhtml+xml"})
_HTML_START: Final = re.compile(rb"<(?:!doctype\s+html|html)(?:[\s>/]|$)", re.IGNORECASE)
_HTML_PREFIX_LIMIT: Final = 1024  # whitespace before the tag is skipped up to this
_TEXT_PREFIX_LIMIT: Final = 1024  # whitespace before a text/plain body's signature, likewise
# Plain text and Markdown (issue #15): read as text, the Markdown by the pipeline as Markdown.
TEXT_TYPES: Final = frozenset({"text/plain", "text/markdown", "text/x-markdown"})
_CSV_SAMPLE: Final = 8192
# Upload names that make a text body of no real declared type a text type (issue #17).
_NAMED_TEXT_TYPES: Final = (
    (".md", "text/markdown"),
    (".markdown", "text/markdown"),
    (".txt", "text/plain"),
)
_UNTYPED: Final = frozenset({"", "application/octet-stream"})
_CONTENT_TYPES_LIMIT: Final = 256 * 1024
_MIMETYPE_LIMIT: Final = 128

_MEDIA_TYPES: Final[dict[DocumentKind, str]] = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "epub": "application/epub+zip",
    "csv": "text/csv",
    "odt": "application/vnd.oasis.opendocument.text",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "odp": "application/vnd.oasis.opendocument.presentation",
    "png": "image/png",
    "jpeg": "image/jpeg",
    "tiff": "image/tiff",
    "webp": "image/webp",
    "bmp": "image/bmp",
}
# Declared types under which a body that starts like an HTML page is that page: the document
# types, an unknown type and none. Other types (``text/plain``, ``application/json``, ...) keep
# their path.
_HTML_SNIFFED_TYPES: Final = frozenset({*_MEDIA_TYPES.values(), "", "application/octet-stream"})
_ZIP_KINDS: Final[frozenset[DocumentKind]] = frozenset(
    {"docx", "pptx", "xlsx", "epub", "odt", "ods", "odp"}
)
_MIMETYPE_KINDS: Final[dict[bytes, DocumentKind]] = {
    b"application/epub+zip": "epub",
    b"application/vnd.oasis.opendocument.text": "odt",
    b"application/vnd.oasis.opendocument.spreadsheet": "ods",
    b"application/vnd.oasis.opendocument.presentation": "odp",
}
_OOXML_PARTS: Final[tuple[tuple[DocumentKind, re.Pattern[bytes]], ...]] = (
    ("docx", re.compile(rb"""PartName=["']/word/document\.xml["']""")),
    ("pptx", re.compile(rb"""PartName=["']/ppt/presentation\.xml["']""")),
    ("xlsx", re.compile(rb"""PartName=["']/xl/workbook\.xml["']""")),
)

# OLE2 (compound file) limits: a directory entry is 128 bytes, and nothing here walks more
# than a bounded number of sectors, whatever a corrupt chain says.
_OLE_SIGNATURE: Final = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_OLE_MAX_DIRECTORY_SECTORS: Final = 256
_OLE_MAX_DIFAT_SECTORS: Final = 1024
_OLE_END_OF_CHAIN: Final = 0xFFFFFFFE
_OLE_MAX_SECTOR_ID: Final = 0xFFFFFFF9
_OLE_WORKBOOK_STREAMS: Final = frozenset({"Workbook", "Book"})
_BMP_DIB_SIZES: Final = frozenset({12, 40, 52, 56, 64, 108, 124})
# A NUL byte in this many first bytes makes a body declared as HTML or text binary (B1).
_BINARY_SAMPLE: Final = 1024
# UTF-16 text without a byte order mark: at least this share of one byte lane is NUL.
_UTF16_NUL_SHARE: Final = 0.8
_LABELS: Final[dict[DocumentKind, str]] = {
    "pdf": "a PDF document",
    "docx": "a DOCX document",
    "pptx": "a PPTX document",
    "xlsx": "an XLSX document",
    "xls": "an XLS document",
    "epub": "an EPUB document",
    "csv": "a CSV file",
    "odt": "an ODT document",
    "ods": "an ODS document",
    "odp": "an ODP document",
    "png": "a PNG image",
    "jpeg": "a JPEG image",
    "tiff": "a TIFF image",
    "webp": "a WebP image",
    "bmp": "a BMP image",
}


class DocumentTooLarge(Exception):  # noqa: N818 - the name is part of the task's interface
    """A container is too big, too packed or too odd to convert safely."""


class DocumentUnreadable(Exception):  # noqa: N818 - named like DocumentTooLarge
    """A container cannot be read at all (corrupt or truncated archive)."""


def media_type(kind: DocumentKind) -> str:
    """The media type a result reports for a document of this kind."""
    return _MEDIA_TYPES[kind]


def sniff(
    body: bytes, declared: str | None, name: str | None
) -> DocumentKind | Literal["html", "text"] | None:
    """The kind of a body, or None when it is not supported.

    A declared HTML or XHTML type is "html" (the page path) unless its bytes are not text
    (``not_text``: a document's or image's signature at the start, or binary data): then None
    (B1), so that the body is refused instead of read as mojibake. For anything else the
    bytes decide first; then an HTML page declared as a document type, as
    ``application/octet-stream`` or not at all ("html": an upload named report.pdf that is
    HTML); then CSV (declared, or a ``.csv`` name, with decodable text); then a declared
    ``text/plain``, ``text/markdown`` or ``text/x-markdown`` is "text". Such a body is a
    document only when its signature stands at the start (after a byte order mark and
    whitespace), so prose that quotes ``%PDF-`` stays text. Any parser error means None.
    """
    media = (declared or "").split(";", 1)[0].strip().lower()
    if media in _HTML_TYPES:
        return "html" if not_text(body) is None else None
    try:
        by_bytes = _sniff_bytes(body, at_start=media in TEXT_TYPES)
    except Exception:  # a parser boundary on hostile input: no kind is always safe
        by_bytes = None
    if by_bytes is not None:
        return by_bytes
    if media in _HTML_SNIFFED_TYPES and _starts_like_html(body):
        return "html"
    if (media == "text/csv" or (name or "").lower().endswith(".csv")) and _looks_like_text(body):
        return "csv"
    if media in TEXT_TYPES:
        return "text"
    return None


def not_text(body: bytes) -> str | None:
    """What a body declared as HTML is when it is no text at all: "a PDF document",
    "a PNG image" and so on by a signature at its start (after a byte order mark and
    whitespace), "binary data" for a NUL byte in its first KiB (UTF-16 text excepted), else
    None. A signature later in the body is quoted text, not the body's kind."""
    try:
        kind = _sniff_bytes(body, at_start=True)
    except Exception:  # a parser boundary on hostile input: the NUL test still applies
        kind = None
    if kind is not None:
        return _LABELS[kind]
    return "binary data" if _binary(body) else None


def _binary(body: bytes) -> bool:
    """A NUL byte in the first KiB, unless the bytes are UTF-16: a UTF-16 or UTF-32 byte order
    mark, or (without one) NULs in one byte lane only, filling most of it, as ASCII-range
    text in UTF-16 has."""
    sample = body[:_BINARY_SAMPLE]
    if b"\x00" not in sample:
        return False
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False
    even, odd = sample[0::2], sample[1::2]
    for lane, other in ((even, odd), (odd, even)):
        if b"\x00" not in other and lane.count(0) >= _UTF16_NUL_SHARE * len(lane):
            return False
    return True


def named_text_type(body: bytes, declared: str | None, name: str | None) -> str | None:
    """The text type an upload is read as by its name, or None (issue #17).

    A part named ``*.md`` or ``*.markdown`` (any case), declared ``application/octet-stream``
    or not at all, whose bytes look like text, is ``text/markdown``: ``curl -F file=@notes.md``
    and many browsers send such a file without its type. One named ``*.txt`` is
    ``text/plain`` by the same rule. A declared real type is never overridden. The caller then
    sniffs the part as that type (review I1): a document's signature counts at the start only,
    and a body that starts like an HTML page is text, as for a part that declares the type.
    """
    media = (declared or "").split(";", 1)[0].strip().lower()
    if media not in _UNTYPED:
        return None
    lowered = (name or "").lower()
    for suffix, text_type in _NAMED_TEXT_TYPES:
        if lowered.endswith(suffix):
            return text_type if _looks_like_text(body) else None
    return None


def check_container(body: bytes, kind: DocumentKind, max_uncompressed: int) -> None:
    """Refuse a ZIP-based document that is a bomb, from its central directory alone.

    Limits: at most ``MAX_ZIP_ENTRIES`` entries, at most ``max_uncompressed`` bytes in total,
    and no entry over ``RATIO_MIN_BYTES`` whose size is over ``MAX_RATIO`` times its
    compressed size. Other kinds pass. Nothing is decompressed.
    """
    if kind not in _ZIP_KINDS:
        return
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            infos = archive.infolist()
    except Exception as exc:  # a parser boundary on hostile input: fail closed
        raise DocumentUnreadable("The archive is unreadable.") from exc
    if len(infos) > MAX_ZIP_ENTRIES:
        raise DocumentTooLarge(f"The archive has more than {MAX_ZIP_ENTRIES} entries.")
    total = 0
    for info in infos:
        total += info.file_size
        if total > max_uncompressed:
            raise DocumentTooLarge(f"The archive is over {max_uncompressed} bytes uncompressed.")
        if info.file_size > RATIO_MIN_BYTES and info.file_size > MAX_RATIO * max(
            info.compress_size, 1
        ):
            raise DocumentTooLarge(f"An archive entry is compressed by a ratio over {MAX_RATIO}.")


def _sniff_bytes(body: bytes, *, at_start: bool) -> DocumentKind | None:
    if at_start:
        # Skip a byte order mark and whitespace (in the first KiB only: a body of megabytes of
        # spaces is no document); the signature must follow at once.
        start = 3 if body.startswith(b"\xef\xbb\xbf") else 0
        window = body[start : start + _TEXT_PREFIX_LIMIT]
        skipped = start + len(window) - len(window.lstrip(b" \t\r\n\f\v"))
        if skipped:
            body = body[skipped:]
        if body.startswith(b"%PDF-"):
            return "pdf"
    elif b"%PDF-" in body[:_PDF_WINDOW]:
        return "pdf"
    head = body[:32]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if _is_bmp(head):
        return "bmp"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return _sniff_zip(body)
    if head.startswith(_OLE_SIGNATURE):
        return "xls" if _ole_has_workbook(body) else None
    return None


def _is_bmp(head: bytes) -> bool:
    # "BM" alone starts plain text too, so the reserved field and the DIB header size must fit.
    if len(head) < 18 or not head.startswith(b"BM"):
        return False
    reserved = struct.unpack_from("<I", head, 6)[0]
    dib_size = struct.unpack_from("<I", head, 14)[0]
    return reserved == 0 and dib_size in _BMP_DIB_SIZES


def _sniff_zip(body: bytes) -> DocumentKind | None:
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            infos = archive.infolist()
            if not infos:
                return None
            if infos[0].filename == "mimetype":
                with archive.open(infos[0]) as stream:
                    mimetype = stream.read(_MIMETYPE_LIMIT).strip()
                return _MIMETYPE_KINDS.get(mimetype)
            if any(info.filename == "[Content_Types].xml" for info in infos):
                with archive.open("[Content_Types].xml") as stream:
                    types = stream.read(_CONTENT_TYPES_LIMIT)
                for kind, pattern in _OOXML_PARTS:
                    if pattern.search(types):
                        return kind
    except Exception:  # bad, encrypted or corrupt entries (zlib.error, RuntimeError, ...): None
        return None
    return None


def _starts_like_html(body: bytes) -> bool:
    """Whether the body starts with ``<!doctype html`` or ``<html`` (any case), after a byte
    order mark and whitespace."""
    head = body[:_HTML_PREFIX_LIMIT]
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    return _HTML_START.match(head.lstrip(b" \t\r\n\f\v")) is not None


def _looks_like_text(body: bytes) -> bool:
    sample = body[:_CSV_SAMPLE]
    if sample.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
        return True  # a byte order mark: UTF-8 or UTF-16 (which has NUL bytes)
    if b"\0" in sample:
        return False
    controls = sum(1 for byte in sample if byte < 0x20 and byte not in b"\t\n\r\f")
    return controls * 100 <= len(sample)


def _ole_has_workbook(body: bytes) -> bool:
    """Whether an OLE2 file has a ``Workbook`` or ``Book`` stream in its directory.

    Reads the header, the DIFAT and the directory sector chain, and looks up one FAT entry at
    a time. Every loop is bounded by the body size, a seen-set or a sector cap, so a corrupt
    file cannot make it loop or allocate more than a list of the body's own sector count.
    """
    if len(body) < 512:
        return False
    shift = struct.unpack_from("<H", body, 0x1E)[0]
    if shift not in (9, 12):
        return False
    sector_size = 1 << shift
    sector_count = len(body) // sector_size
    first_directory = struct.unpack_from("<I", body, 0x30)[0]
    fat_ids = list(struct.unpack_from("<109I", body, 0x4C))
    difat_id, seen_difat = struct.unpack_from("<I", body, 0x44)[0], set[int]()
    while (
        difat_id <= _OLE_MAX_SECTOR_ID
        and difat_id not in seen_difat
        and len(seen_difat) < min(_OLE_MAX_DIFAT_SECTORS, sector_count)
        and len(fat_ids) < sector_count
    ):
        seen_difat.add(difat_id)
        offset = (difat_id + 1) * sector_size
        if offset + sector_size > len(body):
            break
        entries = struct.unpack_from(f"<{sector_size // 4}I", body, offset)
        fat_ids.extend(entries[:-1])
        difat_id = entries[-1]
    del fat_ids[sector_count:]
    per_fat_sector = sector_size // 4

    def next_sector(sector_id: int) -> int:
        index = sector_id // per_fat_sector
        if index >= len(fat_ids) or fat_ids[index] > _OLE_MAX_SECTOR_ID:
            return _OLE_END_OF_CHAIN
        offset = (fat_ids[index] + 1) * sector_size + sector_id % per_fat_sector * 4
        if offset + 4 > len(body):
            return _OLE_END_OF_CHAIN
        return int(struct.unpack_from("<I", body, offset)[0])

    sector_id, seen = first_directory, set[int]()
    while (
        sector_id <= _OLE_MAX_SECTOR_ID
        and sector_id not in seen
        and len(seen) < _OLE_MAX_DIRECTORY_SECTORS
    ):
        seen.add(sector_id)
        offset = (sector_id + 1) * sector_size
        if offset + sector_size > len(body):
            return False
        for entry in range(offset, offset + sector_size, 128):
            name_length, entry_type = struct.unpack_from("<HB", body, entry + 0x40)
            if entry_type != 2 or not 2 <= name_length <= 64:
                continue
            name = body[entry : entry + name_length - 2].decode("utf-16-le", "replace")
            if name in _OLE_WORKBOOK_STREAMS:
                return True
        sector_id = next_sector(sector_id)
    return False
