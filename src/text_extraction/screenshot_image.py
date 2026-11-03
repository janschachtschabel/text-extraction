"""The stored form of a screenshot: the browser's PNG as the caller asked for it (B7).

The browser always captures a PNG. ``encode_screenshot`` turns it into the requested format
(PNG, JPEG or WebP) and scales it down to the requested width. It is pure and CPU-bound: the
pipeline runs it on a thread. A PNG that needs no change is passed through untouched. A WebP
image keeps at most ``WEBP_MAX_SIDE_PX`` rows (ruling P14-R10).
"""

import asyncio
import io
import struct
from dataclasses import dataclass, field
from weakref import WeakKeyDictionary

from PIL import Image

from text_extraction.browser_options import ScreenshotOptions

# Format -> (Pillow's name, content type, file name).
_FORMATS = {
    "png": ("PNG", "image/png", "screenshot.png"),
    "jpeg": ("JPEG", "image/jpeg", "screenshot.jpg"),
    "webp": ("WEBP", "image/webp", "screenshot.webp"),
}

# libwebp encodes at most 16,383 pixels a side. The width never gets there (viewport and
# ``width`` stop at 3,840), the full-page height can (``SCREENSHOT_MAX_HEIGHT_PX``, 16,384).
WEBP_MAX_SIDE_PX = 16383
# The warning of a capture cut short: at SCREENSHOT_MAX_HEIGHT_PX, or at the WebP limit.
SCREENSHOT_CLIPPED_WARNING = "screenshot_clipped"

# Encodes running at once in a process. One holds the decoded image and its copies in memory:
# about 180 MB for a 1920 x 16,384 JPEG, so concurrent renders must not each start one.
ENCODE_CONCURRENCY = 2
# One set of slots per event loop (a semaphore belongs to the loop it waits on); a worker runs
# one loop, so this is the process's limit.
_ENCODE_SLOTS: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = WeakKeyDictionary()


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
    clipped: bool = False  # rows were cut for the format (WebP height limit)


def encode_screenshot(png: bytes, options: ScreenshotOptions | None) -> EncodedScreenshot:
    """``png`` in the format and width of ``options``; ``None`` keeps the PNG as it is.

    The image is scaled down to ``options.width`` (never up) keeping its aspect ratio. A WebP
    image taller than ``WEBP_MAX_SIDE_PX`` after that loses its bottom rows (``clipped``).
    Raises ``ScreenshotEncodeError`` when the PNG cannot be decoded or encoded."""
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
            clipped = fmt == "webp" and image.height > WEBP_MAX_SIDE_PX
            if clipped:
                image = image.crop((0, 0, image.width, WEBP_MAX_SIDE_PX))
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
    return EncodedScreenshot(buffer.getvalue(), width, height, content_type, name, clipped)


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


async def encode_screenshot_limited(
    png: bytes, options: ScreenshotOptions | None
) -> EncodedScreenshot:
    """``encode_screenshot`` on a thread, at most ``ENCODE_CONCURRENCY`` at once."""
    loop = asyncio.get_running_loop()
    slots = _ENCODE_SLOTS.get(loop)
    if slots is None:
        slots = _ENCODE_SLOTS[loop] = asyncio.Semaphore(ENCODE_CONCURRENCY)
    async with slots:
        return await asyncio.to_thread(encode_screenshot, png, options)
