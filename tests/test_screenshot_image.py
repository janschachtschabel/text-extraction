"""``encode_screenshot`` (``screenshot_image.py``): the stored file in its format and width."""

import asyncio
import io
import threading
import time

import pytest
from PIL import Image

from text_extraction import screenshot_image
from text_extraction.browser_options import ScreenshotOptions, Viewport
from text_extraction.screenshot_image import WEBP_MAX_SIDE_PX, EncodedScreenshot, encode_screenshot


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


def test_encodes_run_at_most_encode_concurrency_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each encode holds a full image in memory (~180 MB for a 1920x16384 JPEG): a process runs
    # at most ENCODE_CONCURRENCY of them, however many renders finish together.
    running = 0
    peak = 0
    lock = threading.Lock()

    def slow_encode(png: bytes, options: ScreenshotOptions | None) -> str:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with lock:
            running -= 1
        return "encoded"

    monkeypatch.setattr(screenshot_image, "encode_screenshot", slow_encode)

    async def main() -> list[object]:
        return await asyncio.gather(
            *(screenshot_image.encode_screenshot_limited(b"", None) for _ in range(6))
        )

    assert asyncio.run(main()) == ["encoded"] * 6
    assert peak == screenshot_image.ENCODE_CONCURRENCY == 2
    assert asyncio.run(main()) == ["encoded"] * 6  # a new event loop gets its own slots
