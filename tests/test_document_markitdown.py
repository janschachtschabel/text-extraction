"""MarkItDown conversion in ``DocumentPool`` children (``text_extraction.documents.child``).

The conversions run through a real pool: one child for the whole module, as starting one
imports MarkItDown, pandas and pdfminer (a few seconds).
"""

import asyncio
import io
import re
import subprocess
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from reportlab.pdfgen import canvas

from text_extraction.convert import OutputFormat
from text_extraction.documents.formats import from_markdown
from text_extraction.documents.pool import ConversionError, ConversionTimeout, DocumentPool
from text_extraction.documents.types import DocumentConversion, DocumentKind

CORPUS = Path(__file__).parent / "documents" / "corpus"
TARGET = "text_extraction.documents.child"
MEMORY_LIMIT_MB = 2048  # DOCUMENT_MEMORY_LIMIT_MB's default


def corpus(name: str) -> bytes:
    return (CORPUS / name).read_bytes()


class Converter:
    """A started pool on an event loop of its own, for the module's tests."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.pool = DocumentPool(1, MEMORY_LIMIT_MB, target=TARGET)
        self.loop.run_until_complete(self.pool.start())

    def reply(self, payload: dict[str, Any], timeout: float = 60.0) -> Any:
        async def call() -> Any:
            deadline = asyncio.get_running_loop().time() + timeout
            return await self.pool.run("markitdown", payload, deadline)

        return self.loop.run_until_complete(call())

    def convert(
        self,
        body: bytes,
        kind: DocumentKind,
        *,
        fmt: OutputFormat = "markdown",
        metadata: bool = False,
        max_pages: int = 100,
        max_uncompressed: int = 200_000_000,
        charset: str | None = None,
        timeout: float = 60.0,
    ) -> DocumentConversion:
        payload = {
            "body": body,
            "kind": kind,
            "format": fmt,
            "metadata": metadata,
            "base": "https://example.org/files/doc",
            "content_language": None,
            "max_pages": max_pages,
            "max_uncompressed": max_uncompressed,
            "charset": charset,
        }
        return DocumentConversion.from_reply(self.reply(payload, timeout))

    def close(self) -> None:
        self.loop.run_until_complete(self.pool.shutdown())
        self.loop.close()


@pytest.fixture(scope="module")
def converter() -> Iterator[Converter]:
    instance = Converter()
    try:
        yield instance
    finally:
        instance.close()


def table_row(*cells: str) -> re.Pattern[str]:
    """A Markdown table row with these cells, whatever the padding."""
    inner = r"\s*\|\s*".join(re.escape(cell) for cell in cells)
    return re.compile(rf"^\|\s*{inner}\s*\|$", re.MULTILINE)


def numbered_pdf(pages: int) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, invariant=1)
    for number in range(1, pages + 1):
        pdf.drawString(72, 770, f"Seite {number}: " + "Pflanzen brauchen Licht und Wasser. " * 3)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


# --- conversions per kind ----------------------------------------------------------------------


def test_text_pdf_to_markdown(converter: Converter) -> None:
    result = converter.convert(corpus("text.pdf"), "pdf")
    assert "Photosynthese" in result.content
    assert table_row("Wasser", "6 Teile", "Rohstoff").search(result.content)
    assert result.title == "Photosynthese im Überblick"  # from the information dictionary
    assert (result.pages, result.pages_converted) == (3, 3)
    assert result.needs_ocr is False
    assert result.warnings == []
    assert result.metadata is None  # not asked for
    assert "\f" not in result.content


def test_docx_headings_and_table(converter: Converter) -> None:
    result = converter.convert(corpus("lesson.docx"), "docx")
    assert re.search(r"^# Photosynthese$", result.content, re.MULTILINE)
    assert re.search(r"^## Wichtige Stoffe$", result.content, re.MULTILINE)
    assert table_row("Kohlenstoffdioxid", "6 Teile", "Rohstoff").search(result.content)
    assert result.title == "Photosynthese"
    assert (result.pages, result.pages_converted, result.needs_ocr) == (None, None, False)


def test_pptx_slides_and_notes(converter: Converter) -> None:
    result = converter.convert(corpus("slides.pptx"), "pptx")
    assert re.search(r"^# Die Photosynthese$", result.content, re.MULTILINE)
    assert re.search(r"^# Wichtige Stoffe$", result.content, re.MULTILINE)
    assert "Chlorophyll an der Tafel erklären" in result.content  # speaker notes
    assert "<!--" not in result.content  # MarkItDown's slide markers are no content
    assert result.pages is None


def test_xlsx_sheets_as_tables(converter: Converter) -> None:
    result = converter.convert(corpus("sheets.xlsx"), "xlsx")
    assert re.search(r"^## Noten$", result.content, re.MULTILINE)
    assert re.search(r"^## Termine$", result.content, re.MULTILINE)
    assert table_row("Jörg", "2").search(result.content)
    assert table_row("15.03.2024", "Photosynthese").search(result.content)


def test_xls(converter: Converter) -> None:
    result = converter.convert(corpus("sheets.xls"), "xls")
    assert re.search(r"^## Noten$", result.content, re.MULTILINE)
    assert table_row("Anna", "1").search(result.content)
    assert table_row("15.03.2024", "Photosynthese").search(result.content)


def test_epub_chapters(converter: Converter) -> None:
    result = converter.convert(corpus("book.epub"), "epub")
    first = result.content.index("# Die Photosynthese")
    second = result.content.index("# Wichtige Stoffe")
    assert first < second  # in spine order
    assert "Stärke in Knollen" in result.content
    assert result.title == "Photosynthese für Einsteiger"


def test_csv_latin1(converter: Converter) -> None:
    result = converter.convert(corpus("latin1.csv"), "csv")
    assert table_row("Name", "Größe", "Gewicht").search(result.content)
    assert table_row("Jörg", "1.80", "80").search(result.content)


def test_csv_uses_the_charset_of_the_header(converter: Converter) -> None:
    body = "Имя,Размер\nЕль,30\n".encode("koi8-r")  # noqa: RUF001 - short: a guess misreads it

    guessed = converter.convert(body, "csv")
    declared = converter.convert(body, "csv", charset="koi8-r")

    assert "Размер" not in guessed.content
    assert "Размер" in declared.content


def test_payload_without_a_charset_still_converts(converter: Converter) -> None:
    payload = {
        "body": corpus("latin1.csv"),
        "kind": "csv",
        "format": "markdown",
        "metadata": False,
        "base": None,
        "content_language": None,
        "max_pages": 100,
        "max_uncompressed": 200_000_000,
    }
    assert "Größe" in DocumentConversion.from_reply(converter.reply(payload)).content


def test_child_logging_and_warnings_stay_quiet() -> None:
    """What converters log or warn (pdfminer, openpyxl) may quote the document: in a child it
    reaches neither stderr (the pod's log stream) nor stdout."""
    code = (
        "import logging, warnings, structlog\n"
        "import text_extraction.documents.child\n"
        "logging.getLogger('pdfminer.pdfpage').warning('leak %s', 'secret')\n"
        "logging.getLogger().error('leak')\n"
        "warnings.warn('leak secret')\n"
        "structlog.get_logger('x').error('leak', text='secret')\n"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stderr == ""
    assert out.stdout == ""


# --- PDFs: text layer, page limit, odd files ---------------------------------------------------


def test_scan_pdf_reports_needs_ocr(converter: Converter) -> None:
    result = converter.convert(corpus("scan.pdf"), "pdf")
    assert result.needs_ocr is True
    assert result.content == ""  # the text layer is empty; the form feeds are gone
    assert (result.pages, result.pages_converted) == (2, 2)


def test_broken_text_layer_reports_needs_ocr(converter: Converter) -> None:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, invariant=1)
    pdf.drawString(72, 770, "(cid:12)(cid:7) (cid:9)(cid:3) Pflanzen " * 4)
    pdf.drawString(72, 750, "Ein Satz mit einigen ganz normalen Wörtern, die lesbar sind.")
    pdf.showPage()
    pdf.save()
    result = converter.convert(buffer.getvalue(), "pdf")
    assert result.needs_ocr is True  # the probe saw characters; the text is still garbage


def test_pages_truncated_warning(converter: Converter) -> None:
    result = converter.convert(numbered_pdf(12), "pdf", max_pages=5)
    assert result.warnings == ["pages_truncated"]
    assert (result.pages, result.pages_converted) == (12, 5)
    assert "Seite 5:" in result.content
    assert "Seite 6:" not in result.content


def test_zero_page_pdf_is_empty_without_conversion(converter: Converter) -> None:
    result = converter.convert(corpus("empty.pdf"), "pdf", max_pages=5)
    assert (result.content, result.pages, result.pages_converted) == ("", 0, 0)
    assert (result.needs_ocr, result.warnings) == (False, [])


def test_password_protected_pdf(converter: Converter) -> None:
    with pytest.raises(ConversionError) as caught:
        converter.convert(corpus("encrypted.pdf"), "pdf")
    assert caught.value.type_name == "PasswordProtected"


def test_unreadable_pdf(converter: Converter) -> None:
    with pytest.raises(ConversionError) as caught:
        converter.convert(b"%PDF-1.7\n" + b"\x00garbage" * 100, "pdf")
    assert caught.value.type_name == "PdfUnreadable"


def test_corrupt_office_file_is_unreadable(converter: Converter) -> None:
    body = corpus("lesson.docx")
    with pytest.raises(ConversionError) as caught:
        converter.convert(body[: len(body) // 2], "docx")  # no central directory any more
    assert caught.value.type_name == "DocumentUnreadable"


def test_converter_failure_is_unreadable_and_quotes_nothing(converter: Converter) -> None:
    # A valid archive that is no word document: the converter itself fails.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("geheim.txt", "Vertraulicher Inhalt des Dokuments")
    with pytest.raises(ConversionError) as caught:
        converter.convert(buffer.getvalue(), "docx")
    assert caught.value.type_name == "DocumentUnreadable"
    assert "Vertraulich" not in caught.value.message
    assert "geheim" not in caught.value.message


def test_container_bomb_is_too_large(converter: Converter) -> None:
    with pytest.raises(ConversionError) as caught:
        converter.convert(corpus("lesson.docx"), "docx", max_uncompressed=10_000)
    assert caught.value.type_name == "DocumentTooLarge"


def test_kind_without_markitdown_converter_is_refused(converter: Converter) -> None:
    with pytest.raises(ConversionError) as caught:
        converter.convert(corpus("scan.pdf"), "png")
    assert caught.value.type_name == "ValueError"


# --- metadata ----------------------------------------------------------------------------------


def test_metadata_on_request(converter: Converter) -> None:
    result = converter.convert(corpus("xmp-cc-by-sa.pdf"), "pdf", metadata=True)
    assert result.metadata is not None
    assert result.metadata["title"] == "Lizenzierte Handreichung"
    assert result.metadata["license"]["id"] == "CC-BY-SA-4.0"
    assert result.title == "Lizenzierte Handreichung"


def test_metadata_of_a_zero_page_pdf(converter: Converter) -> None:
    result = converter.convert(corpus("empty.pdf"), "pdf", metadata=True)
    assert result.metadata is not None
    assert result.metadata["license"] is None


# --- formats -----------------------------------------------------------------------------------


def test_formats(converter: Converter) -> None:
    html = converter.convert(corpus("lesson.docx"), "docx", fmt="html").content
    assert "<h1>Photosynthese</h1>" in html
    assert "<table>" in html
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    text = converter.convert(corpus("lesson.docx"), "docx", fmt="text").content
    assert "#" not in text
    assert "|" not in text
    assert "Photosynthese\n" in text
    assert "Wasser\t6 Teile\tRohstoff\n" in text
    assert "<script>alert(1)</script>" in text  # text is text: nothing to escape

    markdown = converter.convert(corpus("lesson.docx"), "docx", fmt="markdown").content
    assert "# Photosynthese" in markdown


def test_from_markdown_normalises_whitespace() -> None:
    markdown = "\ufeff# Titel  \r\n\r\n\r\n\r\nErster Absatz.\t\n\f\nZweiter Absatz.\n\n\n"
    assert from_markdown(markdown, "markdown") == "# Titel\n\nErster Absatz.\n\nZweiter Absatz."


def test_from_markdown_html_escapes_raw_html() -> None:
    markdown = (
        "# Titel\n\n<script>alert(1)</script>\n\n<div onclick='x()'>Text</div>\n\n"
        "Ein <b>fetter</b> Satz. [Link](javascript:alert(1)) und [gut](https://example.org/).\n"
    )
    html = from_markdown(markdown, "html")
    assert "<h1>Titel</h1>" in html
    assert "<script" not in html
    assert "<div" not in html
    assert "<b>" not in html
    assert "&lt;script&gt;" in html
    assert 'href="javascript' not in html
    assert '<a href="https://example.org/">gut</a>' in html


def test_from_markdown_html_renders_tables() -> None:
    markdown = "| A | B |\n| --- | --- |\n| 1 | 2 |\n"
    html = from_markdown(markdown, "html")
    assert "<table>" in html
    assert "<td>1</td>" in html


def test_from_markdown_text_has_a_newline_after_each_block() -> None:
    markdown = (
        "# Titel\n\nErster **Absatz**\nin zwei Zeilen.\n\n"
        "- eins\n- zwei\n\n> Zitat\n\n```\ncode()\n```\n\n"
        "| A | B |\n| --- | --- |\n| 1 | 2 |\n\n![Ein Bild](bild.png) und `x = 1`\n"
    )
    assert from_markdown(markdown, "text") == (
        "Titel\nErster Absatz\nin zwei Zeilen.\neins\nzwei\nZitat\ncode()\n"
        "A\tB\n1\t2\nEin Bild und x = 1"
    )


def test_from_markdown_of_nothing() -> None:
    for fmt in ("markdown", "text", "html"):
        assert from_markdown("  \n\f\n", fmt) == ""


# --- the reply and the parent ------------------------------------------------------------------


def test_reply_is_builtins_only(converter: Converter) -> None:
    payload = {
        "body": corpus("text.pdf"),
        "kind": "pdf",
        "format": "markdown",
        "metadata": True,
        "base": None,
        "content_language": "de",
        "max_pages": 100,
        "max_uncompressed": 200_000_000,
    }
    reply = converter.reply(payload)
    assert type(reply) is dict
    assert set(reply) == {
        "content",
        "title",
        "warnings",
        "pages",
        "pages_converted",
        "needs_ocr",
        "metadata",
    }
    assert type(reply["metadata"]) is dict
    assert reply["metadata"]["language"] == "de"


@pytest.mark.parametrize(
    "reply",
    [
        None,
        [],
        {"content": "x"},
        {
            "content": 1,
            "title": None,
            "warnings": [],
            "pages": None,
            "pages_converted": None,
            "needs_ocr": False,
            "metadata": None,
        },
        {
            "content": "x",
            "title": None,
            "warnings": ["a", 1],
            "pages": None,
            "pages_converted": None,
            "needs_ocr": False,
            "metadata": None,
        },
        {
            "content": "x",
            "title": None,
            "warnings": [],
            "pages": True,
            "pages_converted": None,
            "needs_ocr": False,
            "metadata": None,
        },
        {
            "content": "x",
            "title": None,
            "warnings": [],
            "pages": None,
            "pages_converted": None,
            "needs_ocr": 0,
            "metadata": None,
        },
        {
            "content": "x",
            "title": None,
            "warnings": [],
            "pages": None,
            "pages_converted": None,
            "needs_ocr": False,
            "metadata": "title",
        },
        {
            "content": "x",
            "title": None,
            "warnings": [],
            "pages": None,
            "pages_converted": None,
            "needs_ocr": False,
            "metadata": None,
            "extra": 1,
        },
    ],
)
def test_malformed_reply_is_refused(reply: Any) -> None:
    with pytest.raises(ValueError, match="conversion reply"):
        DocumentConversion.from_reply(reply)


def test_reply_round_trip() -> None:
    reply = {
        "content": "Text",
        "title": "Titel",
        "warnings": ["pages_truncated"],
        "pages": 12,
        "pages_converted": 5,
        "needs_ocr": False,
        "metadata": {"title": "Titel"},
    }
    assert DocumentConversion.from_reply(reply) == DocumentConversion(
        content="Text",
        title="Titel",
        warnings=["pages_truncated"],
        pages=12,
        pages_converted=5,
        needs_ocr=False,
        metadata={"title": "Titel"},
    )


def test_parent_side_modules_import_no_converter_libraries() -> None:
    code = (
        "import sys\n"
        "import text_extraction.documents.types, text_extraction.documents.formats\n"
        "heavy = [m for m in ('markitdown', 'pdfminer', 'pdfplumber', 'pandas', 'magika')"
        " if m in sys.modules]\n"
        "print(','.join(heavy))\n"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == ""


# --- hostile documents -------------------------------------------------------------------------

_SHEET_TYPES = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
    b'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
    b'<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
    b'officedocument.spreadsheetml.sheet.main+xml"/>'
    b'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.'
    b'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'
)
_SHEET_RELS = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    b'relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
)
_WORKBOOK = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    b'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    b'<sheets><sheet name="Daten" sheetId="1" r:id="rId1"/></sheets></workbook>'
)
_WORKBOOK_RELS = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    b'relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
)


def million_row_xlsx() -> bytes:
    """A workbook with one sheet of 1,000,000 rows of two numbers: about 14 MB, 86 MB
    unpacked, within every container limit; converting it takes minutes."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _SHEET_TYPES)
        archive.writestr("_rels/.rels", _SHEET_RELS)
        archive.writestr("xl/workbook.xml", _WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        with archive.open("xl/worksheets/sheet1.xml", "w") as sheet:
            sheet.write(
                b'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns='
                b'"http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            )
            for start in range(1, 1_000_001, 10_000):
                sheet.write(
                    b"".join(
                        b'<row r="%d"><c r="A%d"><v>%d</v></c><c r="B%d"><v>%d</v></c></row>'
                        % (row, row, row, row, row * 7)
                        for row in range(start, start + 10_000)
                    )
                )
            sheet.write(b"</sheetData></worksheet>")
    return out.getvalue()


def test_xlsx_million_rows_hits_deadline(converter: Converter) -> None:
    body = million_row_xlsx()
    assert len(body) < 20_000_000  # within DOCUMENT_MAX_BYTES
    with pytest.raises(ConversionTimeout):
        converter.convert(body, "xlsx", timeout=25.0)
    # The child was killed and replaced: the pool converts the next document normally.
    result = converter.convert(corpus("sheets.xlsx"), "xlsx")
    assert table_row("Anna", "1").search(result.content)
