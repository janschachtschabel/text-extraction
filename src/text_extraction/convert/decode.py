"""Bytes to text for fetched HTML.

The charset comes from, in order: the Content-Type header, a byte order mark, a ``<meta>``
declaration in the first 4 KiB, and a guess (strict UTF-8, then charset-normalizer). A declared
charset that is unknown, or that cannot decode the bytes, falls through to the next source - a
wrong declaration must not turn a page into mojibake or an error.

ISO-8859-1 and ASCII declarations are read as windows-1252, as browsers do (WHATWG Encoding
Standard): pages that say Latin-1 routinely contain curly quotes and the euro sign.
"""

import codecs
import re

from charset_normalizer import from_bytes

_META_WINDOW = 4096
_META_CHARSET = re.compile(rb"<meta\b[^>]*?\bcharset\s*=\s*[\"']?\s*([\w.:\-]+)", re.IGNORECASE)
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),  # before UTF-16 LE, whose BOM is a prefix of this one
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
_WHATWG = {"iso8859-1": "cp1252", "ascii": "cp1252"}  # codecs.lookup() names


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


def _from_bom(data: bytes) -> str | None:
    for bom, codec in _BOMS:
        if data.startswith(bom):
            return _decode(data, codec)
    return None


def _from_meta(data: bytes) -> str | None:
    match = _META_CHARSET.search(data[:_META_WINDOW])
    codec = _codec(match.group(1).decode("ascii", "replace")) if match else None
    if codec is None:
        return None
    if codec.startswith(("utf-16", "utf-32")):
        codec = "utf-8"  # the declaration was readable as ASCII, so the page cannot be UTF-16
    return _decode(data, codec)


def _guess(data: bytes) -> str:
    # Valid UTF-8 with non-ASCII characters is UTF-8: no legacy encoding produces it by chance,
    # while the statistical guess is unreliable on short input.
    text = _decode(data, "utf-8")
    if text is not None:
        return text
    best = from_bytes(data).best()
    if best is not None:
        return str(best)
    return data.decode("utf-8", errors="replace")


def decode_html(data: bytes, content_type_charset: str | None) -> str:
    """The text of an HTML document; never raises."""
    if content_type_charset:
        codec = _codec(content_type_charset)
        text = _decode(data, codec) if codec else None
        if text is not None:
            return text.removeprefix("\ufeff")
    for source in (_from_bom, _from_meta):
        text = source(data)
        if text is not None:
            return text.removeprefix("\ufeff")
    return _guess(data).removeprefix("\ufeff")
