import contextlib
import io
import random
import struct
import time
import zipfile

import pytest

from text_extraction.documents.sniff import (
    DocumentTooLarge,
    DocumentUnreadable,
    check_container,
    media_type,
    sniff,
)
from text_extraction.documents.types import DocumentKind

PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n"
MAX_UNCOMPRESSED = 200_000_000
ZIP_CONTAINER_KINDS: tuple[DocumentKind, ...] = ("docx", "xlsx", "epub", "odt")
_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def make_zip(entries: list[tuple[str, bytes]], *, stored: bool = False) -> bytes:
    buffer = io.BytesIO()
    method = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(buffer, "w", method) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


def content_types(part: str) -> bytes:
    return (
        b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
        b'content-types"><Default Extension="xml" ContentType="application/xml"/>'
        b'<Override PartName="/' + part.encode() + b'" ContentType="x"/></Types>'
    )


def ooxml(part: str) -> bytes:
    return make_zip([("[Content_Types].xml", content_types(part)), (part, b"<x/>")])


def mimetype_zip(mimetype: str) -> bytes:
    # The mimetype entry comes first and is stored, as the EPUB and ODF specifications say.
    return make_zip([("mimetype", mimetype.encode()), ("content.xml", b"<x/>")], stored=True)


def ole(stream_names: list[str], *, sector_size: int = 512, chain_loop: bool = False) -> bytes:
    """A minimal OLE2 file: sector 0 is the FAT, sector 1 on hold the directory."""
    shift = sector_size.bit_length() - 1
    entries = [("Root Entry", 5)] + [(name, 2) for name in stream_names]
    per_sector = sector_size // 128
    dir_sectors = max(1, -(-len(entries) // per_sector))
    header = bytearray(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(16))
    header += struct.pack("<HHHHH", 0x3E, 3 if sector_size == 512 else 4, 0xFFFE, shift, 6)
    header += bytes(6)
    header += struct.pack("<IIIIIIIII", 0, 1, 1, 0, 4096, 0xFFFFFFFE, 0, 0xFFFFFFFE, 0)
    header += struct.pack("<I", 0) + struct.pack("<I", 0xFFFFFFFF) * 108
    header = header.ljust(sector_size, b"\0")
    fat = [0xFFFFFFFD]  # sector 0 is the FAT itself
    for index in range(dir_sectors):
        last = index == dir_sectors - 1
        fat.append((1 if chain_loop else 0xFFFFFFFE) if last else index + 2)
    fat += [0xFFFFFFFF] * (sector_size // 4 - len(fat))
    fat_sector = struct.pack(f"<{len(fat)}I", *fat)
    directory = b""
    for name, entry_type in entries:
        raw = name.encode("utf-16-le") + b"\0\0"
        directory += raw.ljust(64, b"\0") + struct.pack("<HBB", len(raw), entry_type, 1)
        directory += bytes(128 - 68)
    directory = directory.ljust(dir_sectors * sector_size, b"\0")
    return bytes(header) + fat_sector + directory


@pytest.mark.parametrize("declared", ["text/plain", "application/octet-stream", None])
def test_pdf_signature_wins_over_declared_type(declared: str | None) -> None:
    assert sniff(PDF, declared, None) == "pdf"


def test_pdf_signature_within_first_kilobyte() -> None:
    assert sniff(b"\0" * 1000 + b"%PDF-1.4", None, None) == "pdf"
    assert sniff(b"\0" * 1100 + b"%PDF-1.4", None, None) is None


def test_html_declared_stays_html() -> None:
    assert sniff(b"<html></html>", "text/html", None) == "html"
    # The package 2 path is unchanged: a declared HTML page is HTML even with a PDF body.
    assert sniff(PDF, "text/html", None) == "html"
    assert sniff(PDF, "application/xhtml+xml", "x.pdf") == "html"


HTML_BODIES = [
    b"<!DOCTYPE html><html><body><p>Hi</p></body></html>",
    b"<!doctype HTML>\n<html lang='de'>",
    b"\xef\xbb\xbf \r\n\t<HTML>",
    b"<html>",
]


@pytest.mark.parametrize("body", HTML_BODIES)
@pytest.mark.parametrize(
    "declared",
    [
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/csv; charset=utf-8",
        "image/png",
        "application/octet-stream",
        None,
    ],
)
def test_html_bytes_under_a_document_type_are_html(body: bytes, declared: str | None) -> None:
    """Detection follows the bytes: an upload named report.pdf that is HTML is HTML."""
    assert sniff(body, declared, "report.pdf") == "html"
    assert sniff(body, declared, "table.csv") == "html"


@pytest.mark.parametrize("body", HTML_BODIES)
def test_html_bytes_keep_the_path_of_other_declared_types(body: bytes) -> None:
    assert sniff(body, "text/plain", None) == "text"  # package 2: plain text stays text
    assert sniff(body, "text/html", None) == "html"
    assert sniff(body, "application/json", None) is None
    assert sniff(body, "image/gif", None) is None


def test_html_bytes_need_the_tag_at_the_start() -> None:
    assert sniff(b"Notes: <html> is a tag", None, None) is None
    assert sniff(b"<htmlish>", None, None) is None
    assert sniff(b"<head><title>x</title></head>", "application/pdf", None) is None
    # Document magic wins: a PDF signature in the first kilobyte is a PDF.
    assert sniff(b"<html>" + PDF, None, None) == "pdf"


def test_declared_type_with_parameters_and_case() -> None:
    assert sniff(PDF, "Text/HTML; charset=UTF-8", None) == "html"
    assert sniff(PDF, "TEXT/PLAIN; charset=utf-8", None) == "pdf"
    assert sniff(b"a,b\n", "text/plain; charset=utf-8", None) == "text"
    assert sniff(b"a,b\n", "Text/CSV; charset=utf-8", None) == "csv"


@pytest.mark.parametrize(
    ("part", "kind"),
    [
        ("word/document.xml", "docx"),
        ("ppt/presentation.xml", "pptx"),
        ("xl/workbook.xml", "xlsx"),
    ],
)
def test_ooxml_main_part(part: str, kind: DocumentKind) -> None:
    body = ooxml(part)
    assert sniff(body, "application/octet-stream", None) == kind
    assert sniff(body, "application/zip", "file.zip") == kind
    assert sniff(body, None, None) == kind


def test_ooxml_without_a_main_part_is_unsupported() -> None:
    assert sniff(ooxml("xl/workbook.bin"), None, None) is None
    assert sniff(ooxml("word/glossary/document.xml"), None, None) is None


@pytest.mark.parametrize(
    ("mimetype", "kind"),
    [
        ("application/epub+zip", "epub"),
        ("application/vnd.oasis.opendocument.text", "odt"),
        ("application/vnd.oasis.opendocument.spreadsheet", "ods"),
        ("application/vnd.oasis.opendocument.presentation", "odp"),
    ],
)
def test_epub_and_odf_mimetype_entry(mimetype: str, kind: DocumentKind) -> None:
    assert sniff(mimetype_zip(mimetype), None, None) == kind


def test_other_mimetype_entries_are_unsupported() -> None:
    assert sniff(mimetype_zip("application/vnd.oasis.opendocument.graphics"), None, None) is None
    assert sniff(mimetype_zip("application/x-something"), None, None) is None


def test_mimetype_entry_must_be_first() -> None:
    body = make_zip([("content.xml", b"<x/>"), ("mimetype", b"application/epub+zip")])
    assert sniff(body, None, None) is None


def test_xls_ole_workbook() -> None:
    assert sniff(ole(["Workbook"]), "application/vnd.ms-excel", "a.xls") == "xls"
    assert sniff(ole(["Book"]), None, None) == "xls"
    assert sniff(ole(["SummaryInformation", "Workbook"], sector_size=4096), None, None) == "xls"


def test_xls_workbook_in_a_later_directory_sector() -> None:
    names = [f"Stream{index}" for index in range(10)] + ["Workbook"]
    assert sniff(ole(names), None, None) == "xls"


def test_doc_ole_is_unsupported() -> None:
    assert sniff(ole(["WordDocument", "1Table"]), "application/msword", "a.doc") is None
    assert sniff(ole(["PowerPoint Document"]), None, None) is None


def test_corrupt_ole_is_unsupported() -> None:
    body = ole(["Workbook"])
    assert sniff(body[:100], None, None) is None  # truncated header
    assert sniff(body[:512], None, None) is None  # no directory
    assert sniff(body[:8] + b"\xff" * 600, None, None) is None


def test_ole_directory_chain_loop_terminates() -> None:
    # The directory chain points back at itself; without a cap this would never end.
    assert sniff(ole(["Stream"], chain_loop=True), None, None) is None


def test_ole_stream_must_be_a_stream() -> None:
    body = bytearray(ole(["Workbook"]))
    body[512 + 512 + 128 + 66] = 1  # entry 1 becomes a storage
    assert sniff(bytes(body), None, None) is None


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        (b"\x89PNG\r\n\x1a\n" + b"\0" * 20, "png"),
        (b"\xff\xd8\xff\xe0" + b"\0" * 20, "jpeg"),
        (b"II*\x00" + b"\0" * 20, "tiff"),
        (b"MM\x00*" + b"\0" * 20, "tiff"),
        (b"RIFF\x10\0\0\0WEBPVP8 " + b"\0" * 8, "webp"),
        (b"BM" + struct.pack("<IIII", 70, 0, 54, 40) + b"\0" * 20, "bmp"),
    ],
)
def test_images(body: bytes, kind: DocumentKind) -> None:
    assert sniff(body, "application/octet-stream", None) == kind
    assert sniff(body, None, None) == kind


def test_text_starting_with_bm_is_not_a_bitmap() -> None:
    assert sniff(b"BMW builds cars, and more text follows.", "text/plain", None) == "text"
    assert sniff(b"BMW builds cars, and more text follows.", None, None) is None


def test_gif_and_other_riff_are_unsupported() -> None:
    assert sniff(b"GIF89a" + b"\0" * 20, "image/gif", None) is None
    assert sniff(b"GIF87a" + b"\0" * 20, None, None) is None
    assert sniff(b"RIFF\x10\0\0\0WAVEfmt " + b"\0" * 8, None, None) is None


def test_csv_by_type_or_extension() -> None:
    body = b"a,b\n1,2\n"
    assert sniff(body, "text/csv", None) == "csv"
    assert sniff(body, "text/plain", "data.csv") == "csv"
    assert sniff(body, "text/plain", "DATA.CSV") == "csv"
    assert sniff(body, None, "data.csv") == "csv"
    assert sniff(body, "application/octet-stream", "data.csv") == "csv"
    assert sniff(body, "text/plain", None) == "text"
    assert sniff(body, "text/plain", "notes.txt") == "text"


def test_csv_must_be_decodable_text() -> None:
    latin1 = "name;city\nMüller;Köln\n".encode("latin-1")
    assert sniff(latin1, "text/csv", None) == "csv"
    assert sniff("a,b\r\n€,2\r\n".encode("utf-16"), "text/csv", None) == "csv"
    assert sniff(b"a,b\n" + b"\0\x01\x02\0" * 100, "text/csv", None) is None
    assert sniff(b"\x00\x01\x02\x03" * 100, "text/plain", "data.csv") == "text"


def test_empty_and_unknown_bodies() -> None:
    assert sniff(b"", None, None) is None
    assert sniff(b"", "text/plain", None) == "text"
    assert sniff(b"hello", "application/json", None) is None
    assert sniff(b"hello", None, None) is None
    assert sniff(b"hello", "application/octet-stream", "notes.txt") is None


def test_plain_zip_is_unsupported() -> None:
    body = make_zip([("a.txt", b"hello"), ("b/c.txt", b"world")])
    assert sniff(body, "application/zip", "archive.zip") is None
    assert sniff(body, "text/plain", None) == "text"  # the declared plain-text path stays


def test_corrupt_zip_is_unsupported() -> None:
    body = ooxml("word/document.xml")
    assert sniff(body[: len(body) // 2], None, None) is None
    assert sniff(b"PK\x03\x04" + b"\0" * 40, None, None) is None


def test_media_types() -> None:
    assert media_type("pdf") == "application/pdf"
    assert media_type("docx").endswith("wordprocessingml.document")
    assert media_type("pptx").endswith("presentationml.presentation")
    assert media_type("xlsx").endswith("spreadsheetml.sheet")
    assert media_type("xls") == "application/vnd.ms-excel"
    assert media_type("epub") == "application/epub+zip"
    assert media_type("csv") == "text/csv"
    assert media_type("odt") == "application/vnd.oasis.opendocument.text"
    assert media_type("ods") == "application/vnd.oasis.opendocument.spreadsheet"
    assert media_type("odp") == "application/vnd.oasis.opendocument.presentation"
    assert media_type("png") == "image/png"
    assert media_type("jpeg") == "image/jpeg"
    assert media_type("tiff") == "image/tiff"
    assert media_type("webp") == "image/webp"
    assert media_type("bmp") == "image/bmp"


def test_check_container_accepts_a_normal_document() -> None:
    check_container(ooxml("word/document.xml"), "docx", MAX_UNCOMPRESSED)
    check_container(mimetype_zip("application/epub+zip"), "epub", MAX_UNCOMPRESSED)


def test_check_container_ignores_kinds_that_are_not_zip() -> None:
    check_container(PDF, "pdf", 10)
    check_container(b"not a zip at all", "csv", 10)
    check_container(ole(["Workbook"]), "xls", 10)


def test_zip_bomb_refused() -> None:
    # One entry of 300 MB of zeros: about 300 KB deflated, a ratio far over 100.
    body = make_zip(
        [
            ("[Content_Types].xml", content_types("word/document.xml")),
            ("word/document.xml", bytes(300 * 1024 * 1024)),
        ]
    )
    assert len(body) < 1_000_000
    with pytest.raises(DocumentTooLarge):
        check_container(body, "docx", 10**12)  # refused by the ratio alone
    with pytest.raises(DocumentTooLarge):
        check_container(body, "docx", MAX_UNCOMPRESSED)  # and by the total


def test_high_ratio_refused_below_the_total_limit() -> None:
    body = make_zip([("word/document.xml", bytes(5 * 1024 * 1024))])
    with pytest.raises(DocumentTooLarge, match="ratio"):
        check_container(body, "docx", MAX_UNCOMPRESSED)


def test_small_entries_are_exempt_from_the_ratio() -> None:
    # A sparse sheet of a few kilobytes legitimately compresses by more than 100.
    body = make_zip([("xl/worksheets/sheet1.xml", bytes(64 * 1024))])
    check_container(body, "xlsx", MAX_UNCOMPRESSED)


def test_total_uncompressed_limit() -> None:
    entries = [(f"part{index}.xml", f"{index}".encode() * 400) for index in range(10)]
    body = make_zip(entries)
    check_container(body, "docx", 4000 * 3)
    with pytest.raises(DocumentTooLarge, match="uncompressed"):
        check_container(body, "docx", 100)


def test_too_many_entries_refused() -> None:
    body = make_zip([(f"e{index}", b"") for index in range(10_001)])
    with pytest.raises(DocumentTooLarge, match="entries"):
        check_container(body, "xlsx", MAX_UNCOMPRESSED)
    ok = make_zip([(f"e{index}", b"") for index in range(10_000)])
    check_container(ok, "xlsx", MAX_UNCOMPRESSED)


def test_check_container_never_decompresses(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an entry was opened")

    body = ooxml("word/document.xml")
    monkeypatch.setattr(zipfile.ZipFile, "open", refuse)
    monkeypatch.setattr(zipfile.ZipFile, "read", refuse)
    check_container(body, "docx", MAX_UNCOMPRESSED)


def test_check_container_on_a_corrupt_zip_is_refused() -> None:
    # Called only for sniffed kinds, so a corrupt ZIP here means the body changed or lies:
    # refuse it rather than let a converter walk it.
    with pytest.raises(DocumentUnreadable, match="unreadable"):
        check_container(b"PK\x03\x04" + b"\0" * 40, "docx", MAX_UNCOMPRESSED)
    assert not issubclass(DocumentUnreadable, DocumentTooLarge)


def encrypt_entry(body: bytes, index: int) -> bytes:
    """Set the "encrypted" flag of the index-th entry, in its local and central header."""
    data = bytearray(body)
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        position = -1
        for _ in range(index + 1):
            position = data.index(signature, position + 1)
        data[position + flag_offset] |= 1
    return bytes(data)


def test_encrypted_first_entry_is_unsupported() -> None:
    assert sniff(encrypt_entry(mimetype_zip("application/epub+zip"), 0), None, None) is None
    assert sniff(encrypt_entry(ooxml("word/document.xml"), 0), None, None) is None


def test_corrupt_deflate_stream_is_unsupported() -> None:
    body = bytearray(ooxml("word/document.xml"))
    name_length, extra_length = struct.unpack_from("<HH", body, 26)
    start = 30 + name_length + extra_length
    body[start : start + 8] = b"\xff" * 8  # a deflate block of the invalid type 3
    assert sniff(bytes(body), None, None) is None


def test_self_referencing_difat_is_bounded() -> None:
    # 12 KB, 4096-byte sectors: the DIFAT sector points at itself and lists sector 0 everywhere.
    header = bytearray(_OLE_SIGNATURE + bytes(16))
    header += struct.pack("<HHHHH", 0x3E, 4, 0xFFFE, 12, 6) + bytes(6)
    header += struct.pack("<IIIIIIIII", 0, 109, 0, 0, 4096, 0xFFFFFFFE, 0, 0, 1)
    header += struct.pack("<109I", *([0] * 109))
    sector = struct.pack("<1024I", *([0] * 1023), 0)
    body = bytes(header.ljust(4096, b"\0")) + sector + sector
    started = time.monotonic()
    assert sniff(body, None, None) is None
    assert time.monotonic() - started < 1


def test_text_plain_signature_counts_only_at_the_start() -> None:
    late = b"Some prose that quotes the header %PDF-1.7 in a sentence."
    assert sniff(late, "text/plain", None) == "text"
    assert sniff(late, "text/plain; charset=utf-8", "x.txt") == "text"
    assert sniff(b"\xef\xbb\xbf \r\n\t" + PDF, "text/plain", None) == "pdf"
    assert sniff(b"\n\n" + ooxml("xl/workbook.xml"), "text/plain", None) == "xlsx"
    assert sniff(b"intro\n" + PDF, "text/plain", None) == "text"


@pytest.mark.parametrize("declared", [None, "application/octet-stream", "application/pdf"])
def test_other_types_keep_the_pdf_window(declared: str | None) -> None:
    assert sniff(b"junk\n" + PDF, declared, None) == "pdf"


def test_mutation_fuzz_fails_closed() -> None:
    seeds = [
        ooxml("word/document.xml"),
        mimetype_zip("application/vnd.oasis.opendocument.text"),
        make_zip([("mimetype", b"application/epub+zip"), ("a.xml", b"<x/>" * 50)]),
        ole(["SummaryInformation", "Workbook"]),
        ole(["Workbook"], sector_size=4096),
    ]
    rng = random.Random(20261024)  # noqa: S311 - a deterministic fuzz seed
    kinds: set[object] = set()
    for _ in range(2000):
        data = bytearray(rng.choice(seeds))
        for _ in range(rng.randint(1, 6)):
            data[rng.randrange(len(data))] = rng.randrange(256)
        if rng.random() < 0.2:
            del data[rng.randrange(len(data)) :]
        kind = sniff(
            bytes(data), rng.choice([None, "application/octet-stream", "text/plain"]), None
        )
        kinds.add(kind)
        for container in ZIP_CONTAINER_KINDS:
            with contextlib.suppress(DocumentTooLarge, DocumentUnreadable):
                check_container(bytes(data), container, MAX_UNCOMPRESSED)
    assert {"docx", "odt", "epub", "xls"} <= kinds  # the mutations leave some files intact


def test_text_plain_skips_at_most_a_kilobyte_of_whitespace() -> None:
    """The signature of a ``text/plain`` body counts after a byte order mark and whitespace, but
    only in its first KiB: a body of megabytes of spaces is not scanned byte by byte."""
    assert sniff(b"\xef\xbb\xbf" + b" \r\n" * 300 + PDF, "text/plain", None) == "pdf"
    assert sniff(b" " * 1025 + PDF, "text/plain", None) == "text"
    assert sniff(b" " * 20_000_000, "text/plain", None) == "text"
