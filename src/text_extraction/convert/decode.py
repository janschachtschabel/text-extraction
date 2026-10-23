"""Bytes to text for fetched HTML.

The charset comes from, in order: the Content-Type header, a byte order mark, a ``<meta>``
declaration in the first 4 KiB, and a guess. A declared charset that is unknown, or that cannot
decode the bytes, falls through to the next source - a wrong declaration must not turn a page
into mojibake or an error. Two corrections follow browsers and the real web:

* A Latin-1 family declaration (ISO-8859-1, ISO-8859-15, windows-1252, US-ASCII) yields to the
  bytes when they are valid UTF-8 with at least one non-ASCII character: servers send a default
  ISO-8859-1 header for UTF-8 pages all the time, and no Latin-1 text is valid UTF-8 by chance.
* ISO-8859-1, US-ASCII and windows-1252 all mean windows-1252 as the WHATWG Encoding Standard
  defines it: pages that say Latin-1 routinely contain curly quotes and the euro sign, and the
  five bytes that windows-1252 leaves undefined keep their Latin-1 code points.

A UTF-8 declaration (byte order mark, header or meta) is kept when a few bytes are not valid
UTF-8: the bad sequences become U+FFFD, as in browsers, and the rest of the page keeps its
umlauts. Only another declaration that decodes the bytes cleanly (a meta tag that contradicts a
UTF-8 header) wins over it.

The guess is strict UTF-8; then UTF-8 with replacement when the bytes are UTF-8 but for a few
stray bytes (at most ``_MAX_STRAY_SEQUENCES`` invalid sequences, and at least
``_MIN_UTF8_PER_STRAY`` valid non-ASCII characters for each of them, so a Latin-1 page whose
umlauts are all invalid UTF-8 never qualifies); then charset-normalizer, restricted to the
legacy encodings of the Encoding Standard and trusted only for a clearly non-Latin result (CJK,
Cyrillic, Greek, Hebrew, Arabic, Thai); everything else is windows-1252, which is what browsers
do for German and English. charset-normalizer alone mistakes short Latin-1 text with a few
umlauts for CJK or Arabic.
"""

import codecs
import re
import unicodedata

from charset_normalizer import from_bytes

_META_WINDOW = 4096
_HIGH_BYTES = re.compile(rb"[\x80-\xff]+")
_META_CHARSET = re.compile(rb"<meta\b[^>]*?\bcharset\s*=\s*[\"']?\s*([\w.:\-]+)", re.IGNORECASE)
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),  # before UTF-16 LE, whose BOM is a prefix of this one
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
_WHATWG = {"iso8859-1": "cp1252", "ascii": "cp1252"}  # codecs.lookup() names
_LATIN_FAMILY = {"cp1252", "iso8859-15"}

# The legacy encodings of the WHATWG Encoding Standard that charset-normalizer may choose from.
_LEGACY_LABELS = [
    "cp866",
    "iso8859-2",
    "iso8859-3",
    "iso8859-4",
    "iso8859-5",
    "iso8859-6",
    "iso8859-7",
    "iso8859-8",
    "iso8859-10",
    "iso8859-13",
    "iso8859-14",
    "iso8859-15",
    "iso8859-16",
    "koi8-r",
    "koi8-u",
    "mac-cyrillic",
    "cp874",
    "cp1250",
    "cp1251",
    "cp1252",
    "cp1253",
    "cp1254",
    "cp1255",
    "cp1256",
    "cp1257",
    "cp1258",
    "gb18030",
    "big5",
    "euc-jp",
    "shift_jis",
    "euc-kr",
]
# Of these, the ones whose text is not written in Latin letters.
_NON_LATIN_LABELS = [
    "cp866",
    "iso8859-5",
    "iso8859-6",
    "iso8859-7",
    "iso8859-8",
    "koi8-r",
    "koi8-u",
    "mac-cyrillic",
    "cp874",
    "cp1251",
    "cp1253",
    "cp1255",
    "cp1256",
    "gb18030",
    "big5",
    "euc-jp",
    "shift_jis",
    "euc-kr",
]
_SCRIPTS = [
    "CJK",
    "IDEOGRAPHIC",
    "HIRAGANA",
    "KATAKANA",
    "HANGUL",
    "BOPOMOFO",
    "FULLWIDTH",
    "CYRILLIC",
    "GREEK",
    "HEBREW",
    "ARABIC",
    "THAI",
]
_MIN_NON_ASCII_BYTES = 8  # fewer is too little evidence for a script
# Undeclared bytes count as UTF-8 with replacement with at most this many invalid sequences...
_MAX_STRAY_SEQUENCES = 8
# ... and at least this many valid non-ASCII characters per invalid sequence.
_MIN_UTF8_PER_STRAY = 4
_REPLACEMENT = "\ufffd"
_REPLACEMENT_BYTES = _REPLACEMENT.encode()
_MIN_SHARE = 0.8  # of non-ASCII bytes in runs, and of non-ASCII characters in the script
_C1_AS_CP1252 = {
    code: bytes([code]).decode("cp1252")
    for code in range(0x80, 0xA0)
    if code not in {0x81, 0x8D, 0x8F, 0x90, 0x9D}
}


def _lookup(labels: list[str]) -> frozenset[str]:
    return frozenset(codecs.lookup(label).name for label in labels)


_LEGACY = sorted(_lookup(_LEGACY_LABELS))
_NON_LATIN = _lookup(_NON_LATIN_LABELS)


def _codec(label: str) -> str | None:
    """The Python codec for a charset label, or None when the label is unknown."""
    try:
        name = codecs.lookup(label.strip().strip("\"'")).name
    except (LookupError, ValueError):
        return None
    return _WHATWG.get(name, name)


def _decode(data: bytes, codec: str) -> str | None:
    """Strict decoding; a character cut off at the very end (a body capped at a byte limit)
    is dropped instead of failing the whole page."""
    if codec == "cp1252":
        return data.decode("latin-1").translate(_C1_AS_CP1252)  # cannot fail
    try:
        return data.decode(codec)
    except UnicodeDecodeError as exc:
        if exc.reason != "unexpected end of data" or exc.end < len(data):
            return None
        try:
            return data[: exc.start].decode(codec)
        except UnicodeDecodeError:
            return None
    except LookupError:
        return None  # a codec that exists but is not a text encoding, such as "hex"


def _utf8_replacing(data: bytes) -> str:
    """UTF-8 with each invalid sequence as U+FFFD. A character cut off at the very end (a body
    capped at a byte limit) is dropped, as in strict decoding."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    return decoder.decode(data, final=False)


def _mostly_utf8(data: bytes) -> str | None:
    """The bytes as UTF-8 with replacement, when only a few stray bytes are invalid."""
    text = _utf8_replacing(data)
    invalid = text.count(_REPLACEMENT) - data.count(_REPLACEMENT_BYTES)
    if not 0 < invalid <= _MAX_STRAY_SEQUENCES:
        return None
    non_ascii = len(text) - len(text.encode("ascii", "ignore")) - invalid
    return text if non_ascii >= _MIN_UTF8_PER_STRAY * invalid else None


def _utf8_text(data: bytes) -> str | None:
    """The bytes as UTF-8 when they are valid and hold a non-ASCII character. A body that is
    ASCII but for half a character cut off at a byte limit does not count."""
    text = _decode(data, "utf-8")
    return text if text is not None and not text.isascii() else None


def _declared(data: bytes, codec: str) -> str | None:
    if codec in _LATIN_FAMILY:
        text = _utf8_text(data)
        if text is not None:
            return text
    return _decode(data, codec)


def _from_bom(data: bytes) -> str | None:
    for bom, codec in _BOMS:
        if data.startswith(bom):
            text = _decode(data, codec)
            if text is None and codec == "utf-8-sig":
                return _utf8_replacing(data)  # a UTF-8 BOM leaves no doubt about the encoding
            return text
    return None


def _meta_codec(data: bytes) -> str | None:
    match = _META_CHARSET.search(data[:_META_WINDOW])
    codec = _codec(match.group(1).decode("ascii", "replace")) if match else None
    if codec is not None and codec.startswith(("utf-16", "utf-32")):
        codec = "utf-8"  # the declaration was readable as ASCII, so the page cannot be UTF-16
    return codec


def _non_latin_script(data: bytes, text: str) -> bool:
    """Whether the decoded text is convincingly written in a non-Latin script.

    Latin-1 text with umlauts reads as plausible CJK or Arabic in some encoding, but its few
    non-ASCII bytes stand alone between ASCII letters; real text in these scripts is made of
    runs of non-ASCII bytes and, decoded, of characters of the script."""
    runs = _HIGH_BYTES.findall(data)
    total = sum(len(run) for run in runs)
    if total < _MIN_NON_ASCII_BYTES:
        return False
    if sum(len(run) for run in runs if len(run) >= 2) < _MIN_SHARE * total:
        return False
    letters = [char for char in text if not char.isascii() and char != "\ufffd"]
    in_script = sum(
        unicodedata.name(char, "").startswith(tuple(_SCRIPTS)) and not 0xFF61 <= ord(char) <= 0xFF9F
        for char in letters
    )
    return bool(letters) and in_script >= _MIN_SHARE * len(letters)


def _guess(data: bytes) -> str:
    # Valid UTF-8 with non-ASCII characters is UTF-8: no legacy encoding produces it by chance.
    # (Pure ASCII is UTF-8 as well, and cut-off characters at the end are dropped.)
    text = _decode(data, "utf-8") or _mostly_utf8(data)
    if text is not None:
        return text
    best = from_bytes(data, cp_isolation=_LEGACY).best()
    if best is not None and codecs.lookup(best.encoding).name in _NON_LATIN:
        text = data.decode(best.encoding, errors="replace")
        if _non_latin_script(data, text):
            return text
    return _decode(data, "cp1252") or ""


def decode_html(data: bytes, content_type_charset: str | None) -> str:
    """The text of an HTML document; never raises."""
    return _decode_html(data, content_type_charset).removeprefix("\ufeff")


def _decode_html(data: bytes, content_type_charset: str | None) -> str:
    header = _codec(content_type_charset) if content_type_charset else None
    if header and (text := _declared(data, header)) is not None:
        return text
    if (text := _from_bom(data)) is not None:
        return text
    meta = _meta_codec(data)
    if meta and (text := _declared(data, meta)) is not None:
        return text
    if "utf-8" in (header, meta):
        return _utf8_replacing(data)
    return _guess(data)
