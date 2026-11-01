"""Checks of an image before the Docling child decodes it (Review Focus 1).

An image is refused from its headers alone when a frame has more than ``MAX_IMAGE_PIXELS``
pixels (``DocumentTooLarge``), and is ``DocumentUnreadable`` when Pillow cannot read them. Every
frame the conversion reads is checked (a multi-page TIFF converts its first ``max_pages``
pages), so a small first page cannot hide a bomb behind it; frames after them are not read.

Light: Pillow only, no Docling, so the default test suite checks it. Only the Docling child
imports it (``documents/docling_convert.py``).
"""

import io
import warnings
from typing import Final

from PIL import Image

from text_extraction.documents.sniff import DocumentTooLarge, DocumentUnreadable

# A frame with more pixels is not decoded: Pillow's own bomb limit (about 9,500 by 9,500
# pixels), far above a 600 dpi A4 scan (about 5,000 by 7,000).
MAX_IMAGE_PIXELS: Final = 89_478_485
_TOO_LARGE: Final = "The image has too many pixels."
# The formats the sniffer routes to OCR (documents/sniff.py); Pillow's other decoders are not
# tried on hostile input (defence in depth: Pillow 13 fixes the advisories of 12.3.0 in its
# rarer decoders, and the next advisory may hit one of them again).
_FORMATS: Final = ("PNG", "JPEG", "TIFF", "WEBP", "BMP")


def check_image(body: bytes, *, max_frames: int) -> None:
    """Refuse an image whose first ``max_frames`` frames are not all small enough to decode,
    from their headers alone."""
    try:
        with warnings.catch_warnings():
            # Pillow only warns between its limit and twice it: refused here as well.
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(body), formats=_FORMATS) as image:
                for frame in range(max_frames):
                    if frame:
                        try:
                            image.seek(frame)
                        except EOFError:
                            return  # no more frames
                    width, height = image.size
                    if width * height > MAX_IMAGE_PIXELS:
                        raise DocumentTooLarge(_TOO_LARGE)
    except DocumentTooLarge:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise DocumentTooLarge(_TOO_LARGE) from exc
    except Exception as exc:  # a parser boundary on hostile input
        raise DocumentUnreadable("The image could not be read.") from exc
