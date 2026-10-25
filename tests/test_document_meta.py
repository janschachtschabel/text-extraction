import io
import zipfile
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer

from text_extraction.documents.meta import _pdf_date, document_metadata
from text_extraction.documents.types import DocumentKind
from text_extraction.metadata import PageMetadata

CORPUS = Path(__file__).parent / "documents" / "corpus"
GERMAN = (
    "Die Photosynthese ist der Vorgang, bei dem Pflanzen Lichtenergie in chemische Energie "
    "umwandeln. Dabei nehmen die Blätter Kohlenstoffdioxid aus der Luft auf und geben "
    "Sauerstoff ab. Der Vorgang läuft in den Chloroplasten ab und braucht Wasser, das die "
    "Wurzeln aus dem Boden holen. Größere Pflanzen speichern den Zucker als Stärke."
)
EMPTY_LANGUAGE_ONLY = PageMetadata()


def corpus(name: str) -> bytes:
    return (CORPUS / name).read_bytes()


def zipped(entries: dict[str, bytes], *, first: tuple[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if first:
            archive.writestr(*first)
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def meta(
    body: bytes, kind: DocumentKind, *, header: str | None = None, text: str = ""
) -> PageMetadata:
    return document_metadata(body, kind, None, header, text)


def test_xmp_cc_license() -> None:
    result = meta(corpus("xmp-cc-by-sa.pdf"), "pdf")

    assert result.license is not None
    assert result.license.id == "CC-BY-SA-4.0"
    assert result.license.source == "xmp"
    assert result.license.url == "https://creativecommons.org/licenses/by-sa/4.0/"
    assert result.license.confidence == "high"
    assert result.licenses[0] == result.license
    assert [item.id for item in result.licenses].count("CC-BY-SA-4.0") == 1


def test_xmp_fields() -> None:
    result = meta(corpus("xmp-cc-by-sa.pdf"), "pdf")

    assert result.title == "Lizenzierte Handreichung"
    assert result.authors == ["Erika Mustermann"]
    assert result.description == "Ein frei lizenziertes Arbeitsblatt."
    assert result.keywords == ["Biologie", "OER"]
    assert result.published == "2024-03-15T09:30:00+01:00"
    assert result.modified == "2024-04-01T12:00:00Z"
    assert result.language == "de"
    assert any(item.label == "Copyright 2024 Erika Mustermann" for item in result.licenses)


def test_pdf_info_title_author_dates() -> None:
    result = meta(corpus("text.pdf"), "pdf")

    assert result.title == "Photosynthese im Überblick"
    assert result.authors == ["Erika Mustermann"]
    assert result.description == "Biologie, Klasse 7"
    assert result.keywords == ["Photosynthese", "Chlorophyll", "Pflanzen"]
    assert result.published == "2000-01-01T00:00:00+00:00"
    assert result.modified == "2000-01-01T00:00:00+00:00"
    assert result.license is None
    assert result.licenses == []


@pytest.mark.parametrize(
    ("raw", "iso"),
    [
        ("D:20240315093000+01'00'", "2024-03-15T09:30:00+01:00"),
        ("D:20240315093000Z", "2024-03-15T09:30:00Z"),
        ("D:20240315093000", "2024-03-15T09:30:00"),
        ("D:202403", "2024-03-01"),
        ("D:2024", "2024-01-01"),
        ("D:20241315093000", None),
        ("last Tuesday", None),
        ("", None),
    ],
)
def test_pdf_date_to_iso(raw: str, iso: str | None) -> None:
    assert _pdf_date(raw) == iso


def test_docx_core_properties() -> None:
    result = meta(corpus("core.docx"), "docx")

    assert result.title == "Photosynthese im Überblick"
    assert result.authors == ["Erika Mustermann"]
    assert result.description == "Ein Arbeitsblatt für die siebte Klasse."
    assert result.keywords == ["Photosynthese", "Chlorophyll", "Pflanzen"]
    assert result.published == "2024-03-15T09:30:00Z"
    assert result.modified == "2024-04-01T12:00:00Z"
    assert result.language == "de"


def test_pptx_and_xlsx_share_the_core_properties_part() -> None:
    for kind in ("pptx", "xlsx"):
        assert meta(corpus("core.docx"), kind).title == "Photosynthese im Überblick"


def test_odf_meta_xml() -> None:
    meta_xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<office:document-meta xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
        b' xmlns:dc="http://purl.org/dc/elements/1.1/"'
        b' xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0"><office:meta>'
        b"<dc:title>Tabelle zur Photosynthese</dc:title><dc:description>Messwerte</dc:description>"
        b"<meta:initial-creator>Erika Mustermann</meta:initial-creator>"
        b"<dc:creator>Max Beispiel</dc:creator>"
        b"<meta:creation-date>2024-03-15T09:30:00</meta:creation-date>"
        b"<dc:date>2024-04-01T12:00:00</dc:date><dc:language>de-DE</dc:language>"
        b"<meta:keyword>Biologie</meta:keyword><meta:keyword>Messung</meta:keyword>"
        b"</office:meta></office:document-meta>"
    )
    body = zipped({"meta.xml": meta_xml})

    for kind in ("odt", "ods", "odp"):
        result = meta(body, kind)
        assert result.title == "Tabelle zur Photosynthese"
        assert result.authors == ["Erika Mustermann", "Max Beispiel"]
        assert result.published == "2024-03-15T09:30:00"
        assert result.modified == "2024-04-01T12:00:00"
        assert result.keywords == ["Biologie", "Messung"]
        assert result.language == "de"


def test_epub_package_document() -> None:
    container = (
        b'<?xml version="1.0"?><container version="1.0"'
        b' xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
        b'<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
        b"</rootfiles></container>"
    )
    package = (
        b'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        b'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        b"<dc:title>Das Buch</dc:title><dc:creator>Erika Mustermann</dc:creator>"
        b"<dc:creator>Max Beispiel</dc:creator><dc:language>en-GB</dc:language>"
        b"<dc:date>2023-05-01</dc:date><dc:subject>Biologie</dc:subject>"
        b"<dc:publisher>Freier Verlag</dc:publisher>"
        b"<dc:rights>Creative Commons Namensnennung 4.0 International</dc:rights>"
        b'<meta property="dcterms:modified">2024-01-02T03:04:05Z</meta>'
        b"</metadata></package>"
    )
    body = zipped(
        {"META-INF/container.xml": container, "OEBPS/content.opf": package},
        first=("mimetype", b"application/epub+zip"),
    )

    result = meta(body, "epub")

    assert result.title == "Das Buch"
    assert result.authors == ["Erika Mustermann", "Max Beispiel"]
    assert result.language == "en"
    assert result.published == "2023-05-01"
    assert result.modified == "2024-01-02T03:04:05Z"
    assert result.keywords == ["Biologie"]
    assert result.site_name == "Freier Verlag"
    assert result.license is not None
    assert result.license.id == "CC-BY-4.0"
    assert result.license.source == "meta"


def test_language_comes_from_the_document_then_the_header_then_the_text() -> None:
    xmp_pdf = corpus("xmp-cc-by-sa.pdf")  # dc:language de-DE
    assert meta(xmp_pdf, "pdf", header="en-US", text=GERMAN).language == "de"
    assert meta(corpus("text.pdf"), "pdf", header="fr-CA, en;q=0.5").language == "fr"
    assert meta(corpus("text.pdf"), "pdf", text=GERMAN).language == "de"
    assert meta(corpus("text.pdf"), "pdf", text="zu kurz").language is None


@pytest.mark.parametrize("kind", ["csv", "xls", "png", "jpeg", "tiff", "webp", "bmp"])
def test_other_kinds_give_the_language_only(kind: DocumentKind) -> None:
    result = meta(b"anything", kind, header="de-DE")

    assert result == PageMetadata(language="de")


def test_metadata_never_resolves_entities(httpserver: HTTPServer, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-CONTENT")
    for payload in (
        f'<!ENTITY xxe SYSTEM "{httpserver.url_for("/xxe")}">',
        f'<!ENTITY xxe SYSTEM "{secret.as_uri()}">',
        '<!ENTITY xxe "inline">',
    ):
        core = (
            f'<?xml version="1.0"?><!DOCTYPE cp [{payload}]>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
            'metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>&xxe;</dc:title></cp:coreProperties>"
        ).encode()

        result = meta(zipped({"docProps/core.xml": core}), "docx")

        assert result == EMPTY_LANGUAGE_ONLY
    assert httpserver.log == []


def test_entity_expansion_bomb_gives_empty_metadata() -> None:
    laughs = "".join(
        f'<!ENTITY l{n} "{f"&l{n - 1};" * 10}">' if n else '<!ENTITY l0 "lol">' for n in range(9)
    )
    core = (
        f'<?xml version="1.0"?><!DOCTYPE cp [{laughs}]>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
        'core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<dc:title>&l8;</dc:title></cp:coreProperties>"
    ).encode()

    assert meta(zipped({"docProps/core.xml": core}), "docx") == EMPTY_LANGUAGE_ONLY


def test_xmp_with_a_dtd_is_ignored_but_the_info_dictionary_still_counts() -> None:
    body = corpus("xmp-cc-by-sa.pdf").replace(
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/">',
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><!DOCTYPE x [<!ENTITY a "b">]>',
        1,
    )

    result = meta(body, "pdf")

    assert result.license is None
    assert result.title is None  # this file has no info dictionary: nothing else to read


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        (b"", "pdf"),
        (b"%PDF-1.7 garbage", "pdf"),
        (corpus("encrypted.pdf"), "pdf"),
        (b"not a zip", "docx"),
        (zipped({"other.xml": b"<a/>"}), "docx"),
        (zipped({"docProps/core.xml": b"<broken"}), "docx"),
        (zipped({"meta.xml": b"\x00\x01"}), "odt"),
        (zipped({"META-INF/container.xml": b"<container/>"}), "epub"),
        (zipped({"META-INF/container.xml": b"<c><rootfile full-path='x.opf'/></c>"}), "epub"),
    ],
)
def test_unreadable_sources_give_empty_metadata(body: bytes, kind: DocumentKind) -> None:
    assert meta(body, kind) == EMPTY_LANGUAGE_ONLY


def test_zip_entries_are_read_up_to_a_limit() -> None:
    padding = b"<!-- " + b"x" * (2 << 20) + b" -->"
    core = (
        b'<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
        b'core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">'
        + padding
        + b"<dc:title>Zu spaet</dc:title></cp:coreProperties>"
    )

    assert meta(zipped({"docProps/core.xml": core}), "docx") == EMPTY_LANGUAGE_ONLY


def test_values_are_clipped() -> None:
    title = "T" * 5000
    core = (
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
        'core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:title>{title}</dc:title><cp:keywords>{','.join(f'k{n}' for n in range(500))}"
        "</cp:keywords></cp:coreProperties>"
    ).encode()

    result = meta(zipped({"docProps/core.xml": core}), "docx")

    assert result.title is not None
    assert len(result.title) == 1000
    assert len(result.keywords) == 50
