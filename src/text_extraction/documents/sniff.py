"""Decide what a body is, by its bytes, and refuse container bombs before conversion.

Magic bytes win; the declared type decides only where the bytes do not; the name (URL path
or upload name) only breaks the CSV tie. Nothing here decompresses a ZIP entry beyond the few
hundred bytes it needs to name the format, and nothing imports a converter.
"""

import io
import re
import struct
import zipfile
from typing import Final, Literal

from text_extraction.documents.types import DocumentKind

__all__ = ["DocumentTooLarge", "check_container", "media_type", "sniff"]

MAX_ZIP_ENTRIES: Final = 10_000
MAX_RATIO: Final = 100
# Entries below this size are exempt from the ratio rule: a sparse sheet of a few kilobytes
# legitimately compresses by more than 100, and the entry and total limits bound them anyway.
RATIO_MIN_BYTES: Final = 1024 * 1024

_PDF_WINDOW: Final = 1024
_HTML_TYPES: Final = frozenset({"text/html", "application/xhtml+xml"})
_CSV_SAMPLE: Final = 8192
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


class DocumentTooLarge(Exception):  # noqa: N818 - the name is part of the task's interface
    """A container is too big, too packed or too odd to convert safely."""


def media_type(kind: DocumentKind) -> str:
    """The media type a result reports for a document of this kind."""
    return _MEDIA_TYPES[kind]


def sniff(
    body: bytes, declared: str | None, name: str | None
) -> DocumentKind | Literal["html", "text"] | None:
    """The kind of a body, or None when it is not supported.

    A declared HTML or XHTML type is always "html" (the page path). For anything else the
    bytes decide first; then CSV (declared, or a ``.csv`` name, with decodable text); then a
    declared ``text/plain`` is "text".
    """
    media = (declared or "").split(";", 1)[0].strip().lower()
    if media in _HTML_TYPES:
        return "html"
    by_bytes = _sniff_bytes(body)
    if by_bytes is not None:
        return by_bytes
    if (media == "text/csv" or (name or "").lower().endswith(".csv")) and _looks_like_text(body):
        return "csv"
    if media == "text/plain":
        return "text"
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
    except (zipfile.BadZipFile, EOFError, OSError, ValueError, NotImplementedError) as exc:
        raise DocumentTooLarge("The archive is unreadable.") from exc
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


def _sniff_bytes(body: bytes) -> DocumentKind | None:
    if b"%PDF-" in body[:_PDF_WINDOW]:
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
    except (zipfile.BadZipFile, EOFError, OSError, ValueError, NotImplementedError, KeyError):
        return None
    return None


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

    Reads the header, the FAT (from the DIFAT) and the directory sector chain. The chain walk
    stops at the end marker, at a sector seen before, at the end of the body or after
    ``_OLE_MAX_DIRECTORY_SECTORS`` sectors, so a corrupt file cannot make it loop.
    """
    if len(body) < 512:
        return False
    shift = struct.unpack_from("<H", body, 0x1E)[0]
    if shift not in (9, 12):
        return False
    sector_size = 1 << shift
    first_directory, difat_start = (
        struct.unpack_from("<I", body, 0x30)[0],
        struct.unpack_from("<I", body, 0x44)[0],
    )
    fat_ids = list(struct.unpack_from("<109I", body, 0x4C))
    difat_id, visited = difat_start, 0
    while difat_id <= _OLE_MAX_SECTOR_ID and visited < _OLE_MAX_DIFAT_SECTORS:
        offset = (difat_id + 1) * sector_size
        if offset + sector_size > len(body):
            break
        entries = struct.unpack_from(f"<{sector_size // 4}I", body, offset)
        fat_ids.extend(entries[:-1])
        difat_id = entries[-1]
        visited += 1
    fat: list[int] = []
    for fat_id in fat_ids:
        offset = (fat_id + 1) * sector_size
        if fat_id > _OLE_MAX_SECTOR_ID or offset + sector_size > len(body):
            continue
        fat.extend(struct.unpack_from(f"<{sector_size // 4}I", body, offset))
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
        sector_id = fat[sector_id] if sector_id < len(fat) else _OLE_END_OF_CHAIN
    return False
