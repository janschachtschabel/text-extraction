"""``encode_screenshot`` (``screenshot_image.py``): the stored file in its format and width."""

import asyncio
import gc
import io
import struct
import threading
import time
from collections.abc import Callable

import pytest
from PIL import Image

from text_extraction import screenshot_image
from text_extraction.browser_options import ScreenshotOptions, Viewport
from text_extraction.screenshot_image import (
    WEBP_MAX_SIDE_PX,
    EncodedScreenshot,
    ScreenshotEncodeError,
    encode_screenshot,
)


def _png(width: int, height: int) -> bytes:
    image = Image.effect_noise((width, height), 64).convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _options(**overrides: object) -> ScreenshotOptions:
    return ScreenshotOptions.model_validate(
        {"viewport": Viewport(width=1200, height=630), **overrides}
    )


def test_png_without_width_is_passed_through() -> None:
    png = _png(800, 600)

    out = encode_screenshot(png, None)

    assert out.data is png
    assert (out.width, out.height) == (800, 600)
    assert (out.content_type, out.name) == ("image/png", "screenshot.png")
    assert encode_screenshot(png, _options()).data is png  # default options: png, no width


@pytest.mark.parametrize(
    ("fmt", "mime", "name", "magic"),
    [
        ("jpeg", "image/jpeg", "screenshot.jpg", b"\xff\xd8\xff"),
        ("webp", "image/webp", "screenshot.webp", b"RIFF"),
    ],
)
def test_formats(fmt: str, mime: str, name: str, magic: bytes) -> None:
    out = encode_screenshot(_png(800, 600), _options(format=fmt))

    assert out.data.startswith(magic)
    assert (out.content_type, out.name) == (mime, name)
    assert (out.width, out.height) == (800, 600)
    with Image.open(io.BytesIO(out.data)) as reopened:
        assert reopened.size == (800, 600)
        assert reopened.format == fmt.upper()


def test_jpeg_flattens_transparency() -> None:
    buffer = io.BytesIO()
    Image.new("RGBA", (40, 30), (255, 0, 0, 0)).save(buffer, format="PNG")

    out = encode_screenshot(buffer.getvalue(), _options(format="jpeg"))

    with Image.open(io.BytesIO(out.data)) as reopened:
        assert reopened.mode == "RGB"


def test_width_scales_down_keeping_ratio() -> None:
    out = encode_screenshot(_png(1200, 630), _options(format="webp", width=600))

    assert (out.width, out.height) == (600, 315)
    with Image.open(io.BytesIO(out.data)) as reopened:
        assert reopened.size == (600, 315)


def test_width_on_png_re_encodes_as_png() -> None:
    out = encode_screenshot(_png(1200, 630), _options(width=300))

    assert out.data.startswith(b"\x89PNG")
    assert (out.width, out.height, out.name) == (300, 158, "screenshot.png")  # 157.5 rounds up


def test_height_is_at_least_one() -> None:
    out = encode_screenshot(
        _png(2000, 2), _options(width=16, viewport=Viewport(width=2000, height=240))
    )

    assert (out.width, out.height) == (16, 1)


def test_width_is_never_scaled_up() -> None:
    out = encode_screenshot(_png(400, 300), _options(format="webp", width=1000))

    assert (out.width, out.height) == (400, 300)


def test_quality_changes_size() -> None:
    png = _png(800, 600)
    for fmt in ("jpeg", "webp"):
        low = encode_screenshot(png, _options(format=fmt, quality=20))
        high = encode_screenshot(png, _options(format=fmt, quality=95))
        assert len(low.data) < len(high.data), fmt


def test_repr_does_not_show_the_bytes() -> None:
    out = encode_screenshot(_png(20, 20), None)

    assert isinstance(out, EncodedScreenshot)
    assert "x89PNG" not in repr(out)
    assert "20" in repr(out)


def _flat_png(width: int, height: int) -> bytes:
    """A one-colour PNG: small and fast to encode, even 16,384 rows high."""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_webp_is_cut_to_the_webp_height_limit() -> None:
    # libwebp encodes at most 16,383 pixels a side; the full-page capture can be 16,384 rows.
    out = encode_screenshot(_flat_png(1920, 16384), _options(format="webp"))

    assert (out.width, out.height) == (1920, WEBP_MAX_SIDE_PX)
    assert out.clipped is True
    with Image.open(io.BytesIO(out.data)) as reopened:
        assert reopened.size == (1920, 16383)


def test_webp_scaled_below_the_limit_is_not_cut() -> None:
    out = encode_screenshot(_flat_png(1920, 16384), _options(format="webp", width=960))

    assert (out.width, out.height) == (960, 8192)
    assert out.clipped is False


@pytest.mark.parametrize("fmt", ["png", "jpeg"])
def test_other_formats_keep_all_rows(fmt: str) -> None:
    out = encode_screenshot(_flat_png(64, 16384), _options(format=fmt, width=32))

    assert (out.width, out.height) == (32, 8192)
    assert out.clipped is False
    tall = encode_screenshot(_flat_png(16, 16500), _options(format=fmt, width=16))
    assert (tall.height, tall.clipped) == (16500, False)


_real_encode = encode_screenshot
ENCODED = EncodedScreenshot(b"", 1, 1, "image/webp", "screenshot.webp")


class _BlockingEncode:
    """Stands in for ``encode_screenshot``: blocks until ``go`` is set, counts the encodes that
    entered and how many ran at once. A PNG passed through goes to the real function."""

    def __init__(self) -> None:
        self.go = threading.Event()
        self.entered = 0
        self.running = 0
        self.peak = 0
        self.delay = 0.0  # seconds each encode takes once it may go on
        self._lock = threading.Lock()

    def __call__(self, png: bytes, options: ScreenshotOptions | None) -> EncodedScreenshot:
        if options is None or (options.format == "png" and options.width is None):
            return _real_encode(png, options)
        with self._lock:
            self.entered += 1
            self.running += 1
            self.peak = max(self.peak, self.running)
        self.go.wait(timeout=10)
        time.sleep(self.delay)
        with self._lock:
            self.running -= 1
        return ENCODED


def _png_header(width: int, height: int) -> bytes:
    """The start of a PNG of ``width`` x ``height``: all the limiter reads (``png_size``)."""
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", width, height) + bytes(5)


WEBP = ScreenshotOptions(format="webp")
MAX_SIZE = _png_header(1920, 16384)  # the largest full-page capture


async def _until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not condition():  # noqa: ASYNC110 - counted by the encode threads
            await asyncio.sleep(0.01)


def test_cancelled_encode_keeps_its_pixels_until_the_thread_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The extraction deadline cancels the awaiting coroutine; the thread runs on and still holds
    # its image in memory, so its pixels stay taken until the thread ends (audit A1).
    encode = _BlockingEncode()
    monkeypatch.setattr(screenshot_image, "encode_screenshot", encode)

    async def main() -> None:
        first = asyncio.create_task(screenshot_image.encode_screenshot_limited(MAX_SIZE, WEBP))
        await _until(lambda: encode.entered == 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(screenshot_image.encode_screenshot_limited(MAX_SIZE, WEBP))
        await asyncio.sleep(0.1)
        assert encode.entered == 1  # the first thread still runs
        encode.go.set()
        assert await second == ENCODED

    asyncio.run(main())
    assert (encode.entered, encode.peak) == (2, 1)


def test_small_encodes_share_the_pixel_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    encode = _BlockingEncode()
    monkeypatch.setattr(screenshot_image, "encode_screenshot", encode)
    small = _png_header(1920, 5000)
    assert 3 * 1920 * 5000 <= screenshot_image.ENCODE_PIXEL_BUDGET < 4 * 1920 * 5000

    async def main() -> list[EncodedScreenshot]:
        tasks = [
            asyncio.create_task(screenshot_image.encode_screenshot_limited(small, WEBP))
            for _ in range(4)
        ]
        await _until(lambda: encode.entered == 3)
        await asyncio.sleep(0.1)
        assert encode.entered == 3  # the fourth waits for pixels
        encode.go.set()
        return await asyncio.gather(*tasks)

    assert asyncio.run(main()) == [ENCODED] * 4
    assert encode.peak == 3


def test_an_image_larger_than_the_budget_runs_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    encode = _BlockingEncode()
    monkeypatch.setattr(screenshot_image, "encode_screenshot", encode)
    huge = _png_header(3840, 16384)  # twice the budget: weighed as the whole budget
    assert screenshot_image.ENCODE_PIXEL_BUDGET < 3840 * 16384

    async def main() -> list[EncodedScreenshot]:
        first = asyncio.create_task(screenshot_image.encode_screenshot_limited(huge, WEBP))
        await _until(lambda: encode.entered == 1)  # no deadlock: it gets the whole budget
        small = asyncio.create_task(
            screenshot_image.encode_screenshot_limited(_png_header(16, 16), WEBP)
        )
        await asyncio.sleep(0.1)
        assert encode.entered == 1
        encode.go.set()
        return [await first, await small]

    assert asyncio.run(main()) == [ENCODED] * 2
    assert encode.peak == 1


def test_unreadable_pngs_take_the_whole_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    encode = _BlockingEncode()
    encode.go.set()
    encode.delay = 0.05
    monkeypatch.setattr(screenshot_image, "encode_screenshot", encode)

    async def main() -> list[EncodedScreenshot]:
        return await asyncio.gather(
            *(screenshot_image.encode_screenshot_limited(b"", WEBP) for _ in range(6))
        )

    assert asyncio.run(main()) == [ENCODED] * 6
    assert asyncio.run(main()) == [ENCODED] * 6  # a new event loop gets its own budget
    assert encode.peak == 1


def test_png_passthrough_takes_no_pixels(monkeypatch: pytest.MonkeyPatch) -> None:
    encode = _BlockingEncode()
    monkeypatch.setattr(screenshot_image, "encode_screenshot", encode)
    png = _png(40, 30)

    async def main() -> tuple[EncodedScreenshot, EncodedScreenshot]:
        busy = [
            asyncio.create_task(screenshot_image.encode_screenshot_limited(MAX_SIZE, WEBP))
            for _ in range(2)
        ]
        await _until(lambda: encode.entered >= 1)
        await asyncio.sleep(0.05)  # the whole budget is taken, and a second encode waits
        try:
            async with asyncio.timeout(1):
                return (
                    await screenshot_image.encode_screenshot_limited(png, None),
                    await screenshot_image.encode_screenshot_limited(png, _options()),
                )
        finally:
            encode.go.set()
            await asyncio.gather(*busy)

    plain, default = asyncio.run(main())
    assert plain.data is png
    assert default.data is png


def test_a_failed_encode_nobody_waits_for_logs_no_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # The deadline cancels the wait before the encode fails: shield drops its own callback on
    # the encode then, so the limiter itself has to read the exception (audit A1).
    def failing(png: bytes, options: ScreenshotOptions | None) -> EncodedScreenshot:
        time.sleep(0.2)
        raise ScreenshotEncodeError("boom")

    monkeypatch.setattr(screenshot_image, "encode_screenshot", failing)
    reported: list[dict[str, object]] = []

    async def main() -> None:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: reported.append(context))
        waiting = asyncio.create_task(screenshot_image.encode_screenshot_limited(b"x" * 100, WEBP))
        await asyncio.sleep(0.05)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await asyncio.sleep(0.4)  # the thread ends and its future gets the error
        gc.collect()
        await asyncio.sleep(0.05)

    asyncio.run(main())
    assert reported == []


BUDGET = screenshot_image.ENCODE_PIXEL_BUDGET


def _assert_idle(budget: screenshot_image._PixelBudget, free: int = BUDGET) -> None:
    assert budget._free == free
    assert not budget._waiters


def test_a_cancelled_queued_wait_leaves_the_budget_as_it_was() -> None:
    async def main() -> None:
        budget = screenshot_image._PixelBudget()
        await budget.acquire(BUDGET)
        waiting = asyncio.create_task(budget.acquire(1))
        await asyncio.sleep(0)
        assert len(budget._waiters) == 1
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        _assert_idle(budget, free=0)
        budget.release(BUDGET)
        _assert_idle(budget)

    asyncio.run(main())


def test_a_wait_granted_and_cancelled_before_it_wakes_gives_its_pixels_back() -> None:
    async def main() -> None:
        budget = screenshot_image._PixelBudget()
        await budget.acquire(BUDGET)
        waiting = asyncio.create_task(budget.acquire(100))
        await asyncio.sleep(0)
        budget.release(BUDGET)  # grants the waiter its 100 pixels ...
        assert budget._free == BUDGET - 100
        waiting.cancel()  # ... and its task is cancelled before it runs again
        with pytest.raises(asyncio.CancelledError):
            await waiting
        _assert_idle(budget)

    asyncio.run(main())


def test_a_cancelled_large_head_wait_lets_the_smaller_one_behind_it_go() -> None:
    async def main() -> None:
        budget = screenshot_image._PixelBudget()
        await budget.acquire(BUDGET - 10)
        large = asyncio.create_task(budget.acquire(BUDGET))
        await asyncio.sleep(0)
        small = asyncio.create_task(budget.acquire(5))  # fits, but waits behind ``large``
        await asyncio.sleep(0)
        assert not small.done()
        large.cancel()
        with pytest.raises(asyncio.CancelledError):
            await large
        async with asyncio.timeout(1):
            await small
        _assert_idle(budget, free=5)
        budget.release(5)
        budget.release(BUDGET - 10)
        _assert_idle(budget)

    asyncio.run(main())


@pytest.mark.parametrize(
    ("options", "seconds"),
    [
        (None, 0.0),
        (ScreenshotOptions(), 0.0),
        (ScreenshotOptions(width=800), 10.0),
        (ScreenshotOptions(format="jpeg"), 10.0),
        (ScreenshotOptions(format="webp"), 10.0),
    ],
)
def test_encode_reserve_seconds(options: ScreenshotOptions | None, seconds: float) -> None:
    assert screenshot_image.encode_reserve_seconds(options) == seconds
    assert screenshot_image.ENCODE_RESERVE_SECONDS == 10.0


def test_encode_refuses_a_non_png_capture() -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), (10, 20, 30)).save(buffer, format="JPEG")

    with pytest.raises(ScreenshotEncodeError):
        encode_screenshot(buffer.getvalue(), _options(format="webp"))
