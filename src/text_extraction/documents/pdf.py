"""What a PDF is before it is converted: its pages, whether it has a text layer, and a cut to
its first pages.

Runs in a ``DocumentPool`` child, next to the converter, so that a hostile file costs the child
and not the pod. Built on pdfium (``pypdfium2``, BSD-3-Clause/Apache-2.0); it imports neither
MarkItDown, pdfminer nor pandas. The probe is cheap: it opens the file, counts the pages and
counts the characters of the first ``PROBE_PAGES`` pages without laying any text out.
"""

import io
import re
from dataclasses import dataclass

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw

PROBE_PAGES = 50  # pages whose text layer is looked at
THIN_PAGE_CHARS = 50  # a page with fewer characters has no usable text layer
_BROKEN_TOKEN_SHARE = 20  # more than 1 token in 20 (5 %) broken: the text layer is no good
_BROKEN_TOKEN = re.compile(r"\(cid:\d+\)|\ufffd")


def _escaped(name: str) -> bytes:
    """A regex for a PDF name in which any character may be written ``#`` and two hex digits."""
    parts = []
    for character in name:
        digits = "".join(
            f"[{digit.lower()}{digit.upper()}]" if digit.isalpha() else digit
            for digit in f"{ord(character):02x}"
        )
        parts.append(f"(?:{re.escape(character)}|#{digits})")
    return ("/" + "".join(parts)).encode()


# ``/Encrypt`` in the trailer or the xref stream dictionary, also as ``/Encr#79pt``.
_ENCRYPT_NAME = re.compile(_escaped("Encrypt"))


class PasswordProtected(Exception):  # noqa: N818 - the name is part of the task's interface
    """The PDF needs a password to open (a user password; an empty one does not count)."""


class PdfUnreadable(Exception):  # noqa: N818 - the name is part of the task's interface
    """pdfium cannot open the bytes as a PDF: damaged, truncated or not a PDF at all."""


@dataclass(frozen=True, slots=True)
class PdfProbe:
    pages: int  # in the document
    probed: int  # pages whose characters were counted (at most PROBE_PAGES)
    thin_pages: int  # of the probed pages, those with fewer than THIN_PAGE_CHARS characters
    encrypted: bool  # encrypted, but opens with an empty password


def _open(body: bytes) -> pdfium.PdfDocument | None:
    """The opened document; None for a PDF without pages, which pdfium refuses to open.

    pdfium reports a missing page tree as a password error or as a failure without a code, so a
    failure is a password problem only where the file carries an ``/Encrypt`` entry, whose name
    may be written with ``#xx`` escapes."""
    try:
        return pdfium.PdfDocument(body)
    except pdfium.PdfiumError as error:
        code = error.err_code
        if code == pdfium_raw.FPDF_ERR_PASSWORD and _ENCRYPT_NAME.search(body):
            raise PasswordProtected from None
        if code in (0, pdfium_raw.FPDF_ERR_PASSWORD) and b"%PDF-" in body[:1024]:
            return None
        raise PdfUnreadable from None
    except (ValueError, OverflowError, MemoryError):
        raise PdfUnreadable from None


def _characters(pdf: pdfium.PdfDocument, index: int) -> int:
    """Characters in the text layer of a page; a page that cannot be loaded has none."""
    try:
        page = pdf.get_page(index)
    except pdfium.PdfiumError:
        return 0
    try:
        text_page = page.get_textpage()
        try:
            return int(text_page.count_chars())
        finally:
            text_page.close()
    except pdfium.PdfiumError:
        return 0
    finally:
        page.close()


def probe_pdf(body: bytes) -> PdfProbe:
    """Pages and text-layer density of a PDF.

    Raises ``PasswordProtected`` for a PDF that needs a password and ``PdfUnreadable`` for bytes
    that are no PDF pdfium can open."""
    pdf = _open(body)
    if pdf is None:
        return PdfProbe(pages=0, probed=0, thin_pages=0, encrypted=False)
    try:
        pages = len(pdf)
        probed = min(pages, PROBE_PAGES)
        thin = sum(1 for index in range(probed) if _characters(pdf, index) < THIN_PAGE_CHARS)
        encrypted = pdfium_raw.FPDF_GetSecurityHandlerRevision(pdf.raw) != -1
        return PdfProbe(pages=pages, probed=probed, thin_pages=thin, encrypted=encrypted)
    finally:
        pdf.close()


def truncate_pdf(body: bytes, max_pages: int) -> bytes:
    """The first ``max_pages`` pages as a PDF of their own; the bytes unchanged when the
    document is not longer. The cut keeps the pages, not the document's metadata: read that
    from the original."""
    pdf = _open(body)
    if pdf is None:
        return body
    try:
        if len(pdf) <= max_pages:
            return body
        cut = pdfium.PdfDocument.new()
        try:
            cut.import_pages(pdf, list(range(max_pages)))
            output = io.BytesIO()
            cut.save(output)
            return output.getvalue()
        finally:
            cut.close()
    except pdfium.PdfiumError:
        raise PdfUnreadable from None
    finally:
        pdf.close()


def scan_verdict(probe: PdfProbe) -> bool:
    """True when the PDF looks like a scan: at least half of the probed pages are thin. A PDF
    without pages is none."""
    return probe.probed > 0 and probe.thin_pages * 2 >= probe.probed


def broken_text(markdown: str) -> bool:
    """True when the converted text shows a broken text layer: more than 5 % of its
    whitespace-separated tokens hold a ``(cid:N)`` glyph reference or U+FFFD."""
    tokens = markdown.split()
    if not tokens:
        return False
    broken = sum(1 for token in tokens if _BROKEN_TOKEN.search(token))
    return broken * _BROKEN_TOKEN_SHARE > len(tokens)
