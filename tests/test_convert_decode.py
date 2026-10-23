# ruff: noqa: RUF001 - the tests use the characters whose handling they check
import codecs

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


def test_decode_meta_declaration_wins_over_the_utf8_guess() -> None:
    # Bytes that are valid UTF-8 but declared as Latin-1 are read as declared, like a browser does.
    data = '<meta charset="iso-8859-1"><p>ü</p>'.encode()

    assert decode_html(data, None) == '<meta charset="iso-8859-1"><p>Ã¼</p>'
    assert decode_html(data.replace(b"iso-8859-1", b"utf-8"), None).endswith("<p>ü</p>")


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
