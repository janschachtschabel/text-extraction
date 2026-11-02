"""The stored form of a screenshot: the browser's PNG as the caller asked for it (B7).

The browser always captures a PNG. ``encode_screenshot`` turns it into the requested format
(PNG, JPEG or WebP) and scales it down to the requested width. It is pure and CPU-bound: the
pipeline runs it on a thread. A PNG that needs no change is passed through untouched.
"""

import io
import struct
from dataclasses import dataclass, field

from PIL import Image

from text_extraction.browser_options import ScreenshotOptions

# Format -> (Pillow's name, content type, file name).
_FORMATS = {
    "png": ("PNG", "image/png", "screenshot.png"),
    "jpeg": ("JPEG", "image/jpeg", "screenshot.jpg"),
    "webp": ("WEBP", "image/webp", "screenshot.webp"),
}


def png_size(png: bytes) -> tuple[int, int]:
    """Width and height from a PNG's header."""
    width, height = struct.unpack(">II", png[16:24])
    return int(width), int(height)


class ScreenshotEncodeError(Exception):
    """The browser's PNG could not be read or encoded (damaged, or too many pixels)."""


@dataclass(frozen=True)
class EncodedScreenshot:
    """A screenshot as it is stored: the file, its size in pixels and how it is served."""

    data: bytes = field(repr=False)
    width: int
    height: int
    content_type: str
    name: str


def encode_screenshot(png: bytes, options: ScreenshotOptions | None) -> EncodedScreenshot:
    """``png`` in the format and width of ``options``; ``None`` keeps the PNG as it is.

    The image is scaled down to ``options.width`` (never up) keeping its aspect ratio. Raises
    ``ScreenshotEncodeError`` when the PNG cannot be decoded."""
    options = options or ScreenshotOptions()
    fmt, target = options.format, options.width
    pillow_format, content_type, name = _FORMATS[fmt]
    if fmt == "png" and target is None:
        try:
            width, height = png_size(png)
        except struct.error as error:
            raise ScreenshotEncodeError("The screenshot is not a PNG.") from error
        return EncodedScreenshot(png, width, height, content_type, name)
    try:
        with Image.open(io.BytesIO(png)) as source:
            image = _scaled(source, target)
            buffer = io.BytesIO()
            if fmt == "png":
                image.save(buffer, format=pillow_format, optimize=False)
            elif fmt == "jpeg":
                image.convert("RGB").save(
                    buffer,
                    format=pillow_format,
                    quality=options.effective_quality(),
                    progressive=True,
                )
            else:
                image.save(
                    buffer, format=pillow_format, quality=options.effective_quality(), method=4
                )
            width, height = image.size
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise ScreenshotEncodeError(f"The screenshot could not be encoded: {error}") from error
    return EncodedScreenshot(buffer.getvalue(), width, height, content_type, name)


def _scaled(image: Image.Image, width: int | None) -> Image.Image:
    """``image`` scaled down to ``width`` pixels (height rounded, at least 1); it is returned as
    it is when ``width`` is not given or not smaller."""
    if image.mode not in ("RGB", "RGBA", "L"):
        image = image.convert("RGBA" if "transparency" in image.info else "RGB")
    if width is None or width >= image.width:
        image.load()
        return image
    height = max(1, round(image.height * width / image.width))
    return image.resize((width, height), Image.Resampling.LANCZOS)
