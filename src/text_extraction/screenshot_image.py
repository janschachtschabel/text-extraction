"""The stored form of a screenshot: the browser's PNG as the caller asked for it (B7).

The browser always captures a PNG. ``encode_screenshot`` turns it into the requested format
(PNG, JPEG or WebP) and scales it down to the requested width. It is pure and CPU-bound: the
pipeline runs it on a thread. A PNG that needs no change is passed through untouched. A WebP
image keeps at most ``WEBP_MAX_SIDE_PX`` rows (ruling P14-R10).
"""

import asyncio
import contextlib
import io
import struct
from collections import deque
from dataclasses import dataclass, field
from typing import Final
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

# Pixels of all encodes running at once in a process. An encode holds the decoded image and its
# copies in memory: for 1920 x 16,384 pixels about 330 MB as JPEG, about 470 MB as WebP and up to
# about 900 MB as WebP of a noisy page. So at most one encode of that size runs at once, and an
# encode weighs its pixels (a larger image weighs the whole budget, and runs alone).
ENCODE_PIXEL_BUDGET: Final = 1920 * 16_384
# Of a render's deadline: the time kept for encoding its screenshot (on top of the time the
# browser needs to take it), when the screenshot is encoded at all.
ENCODE_RESERVE_SECONDS: Final = 10.0


class _PixelBudget:
    """``ENCODE_PIXEL_BUDGET`` on one event loop, granted in the order it was asked for (a large
    image is not passed over by a stream of small ones)."""

    def __init__(self) -> None:
        self._free = ENCODE_PIXEL_BUDGET
        self._waiters: deque[tuple[int, asyncio.Future[None]]] = deque()

    async def acquire(self, pixels: int) -> None:
        if not self._waiters and pixels <= self._free:
            self._free -= pixels
            return
        waiter = asyncio.get_running_loop().create_future()
        entry = (pixels, waiter)
        self._waiters.append(entry)
        try:
            await waiter
        except asyncio.CancelledError:
            if waiter.done() and not waiter.cancelled():  # granted as the wait was cancelled
                self.release(pixels)
            else:
                with contextlib.suppress(ValueError):  # unless ``_grant`` dropped it already
                    self._waiters.remove(entry)
                self._grant()  # the next in line may fit now
            raise

    def release(self, pixels: int) -> None:
        self._free += pixels
        self._grant()

    def _grant(self) -> None:
        while self._waiters:
            pixels, waiter = self._waiters[0]
            if waiter.done():  # its wait was cancelled
                self._waiters.popleft()
                continue
            if pixels > self._free:
                return
            self._waiters.popleft()
            self._free -= pixels
            waiter.set_result(None)


# One budget per event loop (its waits belong to the loop); a worker runs one loop, so this is
# the process's budget.
_BUDGETS: WeakKeyDictionary[asyncio.AbstractEventLoop, _PixelBudget] = WeakKeyDictionary()


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
    if _passes_through(options):
        try:
            width, height = png_size(png)
        except struct.error as error:
            raise ScreenshotEncodeError("The screenshot is not a PNG.") from error
        return EncodedScreenshot(png, width, height, content_type, name)
    try:
        # The browser captures a PNG: any other format is refused, not decoded (audit A9).
        with Image.open(io.BytesIO(png), formats=("PNG",)) as source:
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


def encode_reserve_seconds(options: ScreenshotOptions | None) -> float:
    """The time a render keeps for encoding its screenshot: none for a PNG passed through."""
    if _passes_through(options):
        return 0.0
    return ENCODE_RESERVE_SECONDS


def _passes_through(options: ScreenshotOptions | None) -> bool:
    return options is None or (options.format == "png" and options.width is None)


def _weight(png: bytes) -> int:
    """The pixels an encode of ``png`` takes of the budget; a PNG without a readable header
    takes all of it."""
    try:
        width, height = png_size(png)
    except struct.error:
        return ENCODE_PIXEL_BUDGET
    return min(width * height, ENCODE_PIXEL_BUDGET)


async def encode_screenshot_limited(
    png: bytes, options: ScreenshotOptions | None
) -> EncodedScreenshot:
    """``encode_screenshot`` on a thread, within ``ENCODE_PIXEL_BUDGET``; a PNG passed through
    takes none of it. A cancel ends the wait, not the encode: its thread runs on with the image
    in memory, so its pixels stay taken until the thread ends."""
    if _passes_through(options):
        return encode_screenshot(png, options)
    loop = asyncio.get_running_loop()
    budget = _BUDGETS.get(loop)
    if budget is None:
        budget = _BUDGETS[loop] = _PixelBudget()
    pixels = _weight(png)
    await budget.acquire(pixels)
    try:
        encoding = loop.run_in_executor(None, encode_screenshot, png, options)
    except BaseException:
        budget.release(pixels)
        raise

    # The shield reads the encode's outcome, also when its wait was cancelled: an encode nobody
    # waits for any more logs no error.
    def ended(_done: asyncio.Future[EncodedScreenshot]) -> None:
        budget.release(pixels)

    encoding.add_done_callback(ended)
    return await asyncio.shield(encoding)
