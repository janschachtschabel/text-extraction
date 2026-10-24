# ruff: noqa: RUF001 - the tests use the characters whose handling they check
import codecs

import pytest

from text_extraction.convert import decode_html

GERMAN = "Größe und Maße: Für Übungen gibt es äußerst schöne Lösungen. "


def test_decode_latin1_without_declaration() -> None:
    data = ("<html><body><p>" + GERMAN * 5 + "</p></body></html>").encode("latin-1")

    text = decode_html(data, None)

    assert "Größe und Maße" in text
    assert "Für Übungen" in text
    assert "�" not in text


def test_decode_wrong_header_charset_falls_back() -> None:
    # The header says UTF-8, the bytes are ISO-8859-2: strict decoding fails, the meta tag wins.
    # (A statistical guess cannot tell this short text from other single-byte encodings.)
    data = '<meta charset="iso-8859-2"><p>ł</p>'.encode("iso-8859-2")

    assert decode_html(data, "utf-8") == '<meta charset="iso-8859-2"><p>ł</p>'


def test_decode_unknown_header_charset_falls_back() -> None:
    data = ("<p>" + GERMAN * 5 + "</p>").encode("utf-8")

    assert "Größe und Maße" in decode_html(data, "no-such-charset")


def test_decode_header_charset_wins_over_meta() -> None:
    data = ('<html><head><meta charset="utf-8"></head><body>Grüße</body></html>').encode(
        "iso-8859-2"
    )

    assert "Grüße" in decode_html(data, "iso-8859-2")


def test_decode_bom_wins_over_meta() -> None:
    body = '<meta charset="iso-8859-1"><p>€ Größe</p>'

    assert "€ Größe" in decode_html(codecs.BOM_UTF8 + body.encode("utf-8"), None)
    assert "€ Größe" in decode_html(codecs.BOM_UTF16_LE + body.encode("utf-16-le"), None)


def test_decode_http_equiv_meta_in_first_4_kib() -> None:
    head = '<meta http-equiv="Content-Type" content="text/html; charset=iso-8859-2">'
    data = (head + "<p>ł</p>").encode("iso-8859-2")

    assert decode_html(data, None) == head + "<p>ł</p>"


def test_decode_meta_declaration_wins_over_the_guess() -> None:
    # ISO-8859-2 bytes are not UTF-8; without the declaration they would be read as windows-1252.
    data = '<meta charset="iso-8859-2"><p>ł</p>'.encode("iso-8859-2")

    assert decode_html(data, None) == '<meta charset="iso-8859-2"><p>ł</p>'


@pytest.mark.parametrize("label", ["iso-8859-1", "windows-1252", "us-ascii", "iso-8859-15"])
def test_decode_header_in_latin_family_yields_to_utf8_bytes(label: str) -> None:
    # Servers send a default ISO-8859-1 header for UTF-8 pages all the time.
    data = ("<p>" + GERMAN + "</p>").encode()

    assert decode_html(data, label) == "<p>" + GERMAN + "</p>"


@pytest.mark.parametrize("label", ["iso-8859-1", "windows-1252", "us-ascii", "iso-8859-15"])
def test_decode_meta_in_latin_family_yields_to_utf8_bytes(label: str) -> None:
    page = f'<meta charset="{label}"><p>Grüße</p>'

    assert decode_html(page.encode(), None) == page


def test_decode_latin1_declaration_still_applies_to_non_utf8_bytes() -> None:
    page = '<meta charset="iso-8859-1"><p>Grüße</p>'

    assert decode_html(page.encode("latin-1"), None) == page
    assert decode_html(page.encode("latin-1"), "iso-8859-1") == page


@pytest.mark.parametrize("label", ["iso-8859-1", "windows-1252"])
def test_decode_lone_utf8_lead_byte_does_not_make_a_page_utf8(label: str) -> None:
    # A body cut at the byte limit may end in half a character, but nothing here is non-ASCII.
    assert decode_html(b"<p>abc</p>\xc3", label) == "<p>abc</p>\xc3"


def test_decode_cp1252_undefined_bytes_keep_their_code_point() -> None:
    # Python's cp1252 has no 0x81; the WHATWG decoder maps it to U+0081 instead of failing.
    data = ("<p>Größe und Übung " * 20).encode("latin-1") + b"\x81"

    text = decode_html(data, "iso-8859-1")

    assert text.startswith("<p>Größe und Übung <p>Größe")
    assert text.endswith("\x81")
    assert "�" not in text


def test_decode_latin1_declaration_reads_windows_1252() -> None:
    # Browsers read ISO-8859-1 as windows-1252, and pages rely on it for curly quotes.
    data = "<p>He said “hello” – 5 €</p>".encode("cp1252")

    assert decode_html(data, "iso-8859-1") == "<p>He said “hello” – 5 €</p>"


def test_decode_drops_a_character_cut_at_the_byte_limit() -> None:
    page = "<p>Größe</p>".encode()
    cut = page[: page.index("ö".encode()) + 1]  # ends after the first byte of a two-byte character

    assert decode_html(cut, "utf-8") == "<p>Gr"
    assert decode_html(cut, None) == "<p>Gr"


def test_decode_strips_a_leading_byte_order_mark_character() -> None:
    assert decode_html(codecs.BOM_UTF8 + b"<p>x</p>", "utf-8") == "<p>x</p>"


def test_decode_never_raises_on_garbage() -> None:
    assert isinstance(decode_html(bytes(range(256)) * 20, None), str)
    assert decode_html(b"", None) == ""


UNDECLARED_PAGES = [
    "<html><body><h1>Willkommen</h1><p>"
    + "Der Verein trifft sich jeden Montag im Gemeindehaus. " * 30
    + "Herr Müller leitet den Abend.</p></body></html>",
    "<p>Größe</p>",
    "<p>Über uns</p>",
    "<html><head><title>Kontakt</title></head><body><p>Bitte schreiben Sie an mueller@example.org "
    "oder rufen Sie uns an.</p><p>Öffnungszeiten: Mo-Fr 9-17 Uhr</p></body></html>",
    "<ul>"
    + "<li>Aufgabe zur Mathematik, Klasse 5, Bruchrechnung</li>" * 40
    + "<li>Lösung</li></ul>",
    "<p>Prüfung: 5 €, Zeit: 90 Minuten – Ort: Köln</p>",
    "<h2>Für alle</h2><p>"
    + "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 25
    + "Grüße</p>",
]


@pytest.mark.parametrize("page", UNDECLARED_PAGES)
def test_decode_undeclared_latin1_pages_with_few_umlauts(page: str) -> None:
    assert decode_html(page.encode("cp1252"), None) == page
    assert decode_html(page.encode("cp1252"), "iso-8859-1") == page


def test_decode_undeclared_shift_jis_page_is_still_detected() -> None:
    page = (
        "<html><body><h1>日本語のページ</h1><p>"
        + "これは日本語のテキストです。文字コードは宣言されていません。" * 8
        + "</p></body></html>"
    )

    assert decode_html(page.encode("shift_jis"), None) == page


def test_decode_undeclared_windows_1251_page_is_still_detected() -> None:
    page = (
        "<html><body><h1>Привет</h1><p>"
        + "Это страница на русском языке без объявления кодировки. " * 8
        + "</p></body></html>"
    )

    assert decode_html(page.encode("cp1251"), None) == page


def test_decode_undeclared_latin1_pages_over_a_varied_corpus() -> None:
    words = ["Müller", "Größe", "über", "Käse", "Übung", "schön", "Straße", "Köln", "für", "Öl"]
    filler = "Der Verein trifft sich jeden Montag und bespricht die Termine des Monats. "
    wrong = []
    for index, word in enumerate(words):
        for repeat in (0, 1, 3, 10, 40):
            page = f"<html><body><p>{filler * repeat}Herr {word} ist da.</p></body></html>"
            if index % 2:
                page = f"<p>{word}</p>" if repeat == 0 else page
            if decode_html(page.encode("latin-1"), None) != page:
                wrong.append(page[:60])

    assert wrong == []


def _utf8_page_with_stray_bytes(count: int, *, meta: bool) -> bytes:
    head = '<html><head><meta charset="utf-8"></head>' if meta else "<html><head></head>"
    body = "".join(f"<p>{GERMAN}</p>" for _ in range(10)).encode()
    stray = b"<p>caf\xe9</p>" * count  # a windows-1252 \xe9 pasted into a UTF-8 page
    return head.encode() + body + stray + b"</html>"


@pytest.mark.parametrize(("header", "meta"), [("utf-8", True), ("utf-8", False), (None, True)])
def test_decode_declared_utf8_with_a_stray_byte_keeps_the_umlauts(
    header: str | None, meta: bool
) -> None:
    text = decode_html(_utf8_page_with_stray_bytes(1, meta=meta), header)

    assert "Größe und Maße" in text
    assert "Ã" not in text
    assert text.count("�") == 1


def test_decode_utf8_bom_with_a_stray_byte_keeps_the_umlauts() -> None:
    text = decode_html(codecs.BOM_UTF8 + _utf8_page_with_stray_bytes(1, meta=False), None)

    assert "Größe und Maße" in text
    assert text.count("�") == 1


def test_decode_declared_utf8_still_drops_a_character_cut_at_the_byte_limit() -> None:
    data = _utf8_page_with_stray_bytes(1, meta=True) + "ö".encode()[:1]

    assert decode_html(data, "utf-8").endswith("</html>")


@pytest.mark.parametrize("count", [1, 8])
def test_decode_undeclared_utf8_with_a_few_stray_bytes_keeps_the_umlauts(count: int) -> None:
    text = decode_html(_utf8_page_with_stray_bytes(count, meta=False), None)

    assert "Größe und Maße" in text
    assert text.count("�") == count


def test_decode_undeclared_utf8_with_many_stray_bytes_is_guessed() -> None:
    text = decode_html(_utf8_page_with_stray_bytes(9, meta=False), None)

    assert "�" not in text
    assert "café" in text  # read as windows-1252, like a page of mixed encodings in a browser


def test_decode_undeclared_latin1_page_with_one_utf8_looking_pair_stays_windows_1252() -> None:
    page = "<p>Straße\xa0Nr. 5, Größe: groß, Übung für alle, schön und süß.</p>"

    assert decode_html(page.encode("cp1252"), None) == page


def test_decode_utf8_header_beats_a_stale_latin1_meta_declaration() -> None:
    page = (
        b'<html><head><meta charset="iso-8859-1"></head><body>'
        + ("<p>" + GERMAN + "</p>").encode() * 5
        + b"<p>K\xf6ln</p></body></html>"
    )

    text = decode_html(page, "utf-8")

    assert "Größe und Maße" in text
    assert "KÃ¶ln" not in text
    assert text.count("�") == 1


def test_decode_latin1_header_yields_to_mostly_utf8_bytes() -> None:
    # Apache's default ISO-8859-1 header on a UTF-8 page that holds one stray byte.
    page = ("<p>" + GERMAN + "</p>").encode() * 5 + b"<p>K\xf6ln</p>"

    text = decode_html(page, "iso-8859-1")

    assert "Größe und Maße" in text
    assert "KÃ¶ln" not in text
    assert text.count("�") == 1


def test_decode_latin1_meta_still_wins_over_a_wrong_utf8_header() -> None:
    # The bytes are not nearly UTF-8: every umlaut is an invalid sequence, so the meta decides.
    page = '<meta charset="iso-8859-1"><p>Größe, Käse, Übung, schön, Straße, Köln</p>'

    assert decode_html(page.encode("cp1252"), "utf-8") == page
