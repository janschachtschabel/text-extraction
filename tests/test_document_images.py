"""The image checks of the Docling child (``documents/images.py``), without Docling: an image
too large to decode is refused from its headers, in every frame the conversion would read
(Review Focus 1: an image of 30,000 by 30,000 pixels)."""

import io
import struct
import subprocess
import sys
import warnings
import zlib

import pytest
from PIL import Image

from text_extraction.documents.images import MAX_IMAGE_PIXELS, check_image
from text_extraction.documents.sniff import DocumentTooLarge, DocumentUnreadable


def png_header(width: int, height: int) -> bytes:
    """A PNG that declares ``width`` x ``height`` pixels and holds none: only its headers."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def tiff_with_frames(*sizes: tuple[int, int]) -> bytes:
    """A multi-frame TIFF whose frames declare ``sizes``. Pillow writes small frames; the width
    and height tags of each IFD are then set, so no frame holds its declared pixels."""
    frames = [Image.new("L", (8, 8)) for _ in sizes]
    buffer = io.BytesIO()
    frames[0].save(buffer, format="TIFF", save_all=True, append_images=frames[1:])
    data = bytearray(buffer.getvalue())
    assert data[:2] == b"II"
    (offset,) = struct.unpack_from("<I", data, 4)
    for width, height in sizes:
        (count,) = struct.unpack_from("<H", data, offset)
        for entry in range(count):
            at = offset + 2 + entry * 12
            tag, kind = struct.unpack_from("<HH", data, at)
            if tag in (256, 257):  # ImageWidth, ImageLength
                value = width if tag == 256 else height
                struct.pack_into("<H" if kind == 3 else "<I", data, at + 8, value)
        (offset,) = struct.unpack_from("<I", data, offset + 2 + count * 12)
    return bytes(data)


def test_an_image_of_30000_by_30000_pixels_is_refused() -> None:
    with pytest.raises(DocumentTooLarge, match="too many pixels"):
        check_image(png_header(30_000, 30_000), max_frames=100)


def test_an_image_just_over_the_limit_is_refused_without_a_warning() -> None:
    """Between Pillow's warning and its error (one to two times its limit): refused, and no
    ``DecompressionBombWarning`` reaches the log."""
    side = int(MAX_IMAGE_PIXELS**0.5) + 100
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(DocumentTooLarge):
            check_image(png_header(side, side), max_frames=100)
    assert caught == []


def test_a_scan_of_a_normal_size_passes() -> None:
    check_image(png_header(5_000, 7_000), max_frames=100)  # A4 at 600 dpi


def test_every_frame_is_checked() -> None:
    """A TIFF whose first page is small and whose second is a bomb is refused."""
    body = tiff_with_frames((100, 100), (20_000, 20_000))

    with pytest.raises(DocumentTooLarge):
        check_image(body, max_frames=100)
    check_image(body, max_frames=1)  # the conversion reads the first page only


def test_frames_after_the_limit_are_not_read() -> None:
    body = tiff_with_frames((100, 100), (100, 100), (20_000, 20_000))

    check_image(body, max_frames=2)
    with pytest.raises(DocumentTooLarge):
        check_image(body, max_frames=3)


def test_an_unreadable_image_is_unreadable() -> None:
    with pytest.raises(DocumentUnreadable):
        check_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, max_frames=100)


def test_the_module_imports_no_docling() -> None:
    code = (
        "import sys, text_extraction.documents.images\n"
        "assert not any(name.split('.')[0] in ('docling', 'torch') for name in sys.modules)\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)  # noqa: S603 - our own code


def test_only_the_sniffed_image_formats_are_opened() -> None:
    # Pillow 12 has open advisories in its rarer decoders; the check opens nothing but the five
    # formats the sniffer routes here (PNG, JPEG, TIFF, WebP, BMP).
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buffer, format="GIF")
    gif = buffer.getvalue()

    with pytest.raises(DocumentUnreadable):
        check_image(gif, max_frames=1)
