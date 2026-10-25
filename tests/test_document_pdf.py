import io
import subprocess
import sys
from pathlib import Path

import pytest
from reportlab.lib import pdfencrypt
from reportlab.pdfgen import canvas

from text_extraction.documents.pdf import (
    PROBE_PAGES,
    PasswordProtected,
    PdfProbe,
    PdfUnreadable,
    broken_text,
    probe_pdf,
    scan_verdict,
    truncate_pdf,
)

CORPUS = Path(__file__).parent / "documents" / "corpus"


def corpus(name: str) -> bytes:
    return (CORPUS / name).read_bytes()


def numbered_pdf(pages: int, *, encrypt: pdfencrypt.StandardEncryption | None = None) -> bytes:
    """A PDF whose page N says "Seite N" in a sentence long enough to count as text."""
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, invariant=1, encrypt=encrypt)
    for number in range(1, pages + 1):
        pdf.drawString(72, 770, f"Seite {number} dieses Dokuments mit genug Text für die Probe.")
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def page_texts(body: bytes) -> list[str]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(body)
    try:
        return [pdf.get_page(i).get_textpage().get_text_range() for i in range(len(pdf))]
    finally:
        pdf.close()


def test_probe_counts_pages_and_thin_pages() -> None:
    assert probe_pdf(corpus("text.pdf")) == PdfProbe(
        pages=3, probed=3, thin_pages=0, encrypted=False
    )
    assert probe_pdf(corpus("scan.pdf")) == PdfProbe(
        pages=2, probed=2, thin_pages=2, encrypted=False
    )


def test_probe_looks_at_the_first_pages_only() -> None:
    probe = probe_pdf(numbered_pdf(PROBE_PAGES + 10))

    assert (probe.pages, probe.probed, probe.thin_pages) == (PROBE_PAGES + 10, PROBE_PAGES, 0)


def test_scan_verdict() -> None:
    assert scan_verdict(probe_pdf(corpus("scan.pdf"))) is True
    assert scan_verdict(probe_pdf(corpus("text.pdf"))) is False


@pytest.mark.parametrize(
    ("probed", "thin", "verdict"),
    [(4, 2, True), (4, 1, False), (3, 2, True), (3, 1, False), (1, 1, True), (0, 0, False)],
)
def test_scan_verdict_is_at_least_half(probed: int, thin: int, verdict: bool) -> None:
    probe = PdfProbe(pages=probed, probed=probed, thin_pages=thin, encrypted=False)

    assert scan_verdict(probe) is verdict


def test_password_protected() -> None:
    with pytest.raises(PasswordProtected):
        probe_pdf(corpus("encrypted.pdf"))
    with pytest.raises(PasswordProtected):
        truncate_pdf(corpus("encrypted.pdf"), 1)


def test_encrypted_pdf_with_an_empty_password_opens() -> None:
    body = numbered_pdf(
        2, encrypt=pdfencrypt.StandardEncryption("", ownerPassword="eigentümer", strength=128)
    )

    probe = probe_pdf(body)

    assert (probe.pages, probe.thin_pages, probe.encrypted) == (2, 0, True)


def test_zero_pages() -> None:
    probe = probe_pdf(corpus("empty.pdf"))

    assert probe == PdfProbe(pages=0, probed=0, thin_pages=0, encrypted=False)
    assert scan_verdict(probe) is False
    assert truncate_pdf(corpus("empty.pdf"), 5) == corpus("empty.pdf")


@pytest.mark.parametrize(
    "name",
    [b"/Encr#79pt", b"/#45ncrypt", b"/E#6Ecrypt", b"/E#6ec#72ypt", b"/#45#6e#63#72#79#70#74"],
)
def test_password_protected_with_an_escaped_encrypt_name(name: bytes) -> None:
    body = corpus("encrypted.pdf").replace(b"/Encrypt", name)

    with pytest.raises(PasswordProtected):
        probe_pdf(body)
    with pytest.raises(PasswordProtected):
        truncate_pdf(body, 1)


@pytest.mark.parametrize(
    "body",
    [b"", b"not a pdf at all", b"%PDF-1.7\ngarbage that is no document", corpus("text.pdf")[:300]],
)
def test_malformed_pdf_is_unreadable(body: bytes) -> None:
    with pytest.raises(PdfUnreadable):
        probe_pdf(body)
    with pytest.raises(PdfUnreadable):
        truncate_pdf(body, 1)


def test_truncate_keeps_first_pages() -> None:
    body = numbered_pdf(12)

    cut = truncate_pdf(body, 5)

    assert probe_pdf(cut).pages == 5
    texts = page_texts(cut)
    assert texts[0].startswith("Seite 1 ")
    assert texts[4].startswith("Seite 5 ")
    assert probe_pdf(body).pages == 12


def test_truncate_returns_a_short_document_unchanged() -> None:
    body = numbered_pdf(3)

    assert truncate_pdf(body, 3) is body
    assert truncate_pdf(body, 100) is body


def test_broken_text_rule() -> None:
    assert broken_text("(cid:12)(cid:13) word") is True
    assert (
        broken_text("Die Photosynthese wandelt Licht in Zucker um. Größe, Äpfel, Übung.") is False
    )


def test_broken_text_counts_replacement_characters() -> None:
    assert broken_text("Gr��e und �pfel sind kaputt") is True


def test_broken_text_threshold_is_more_than_five_percent() -> None:
    ordinary = ["wort"] * 19
    assert broken_text(" ".join([*ordinary, "(cid:7)"])) is False  # 1 of 20: exactly 5 %
    assert broken_text(" ".join([*ordinary[:18], "(cid:7)", "x(cid:8)y"])) is True  # 2 of 20


def test_broken_text_of_nothing_is_fine() -> None:
    assert broken_text("") is False
    assert broken_text("  \n\t ") is False


def test_pdf_and_meta_modules_import_no_converter_libraries() -> None:
    code = (
        "import sys\n"
        "import text_extraction.documents.pdf, text_extraction.documents.meta\n"
        "banned = {'markitdown', 'pdfminer', 'pdfplumber', 'pandas'}\n"
        "print(sorted(banned & set(sys.modules)))\n"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == "[]"
