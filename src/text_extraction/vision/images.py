"""Image bytes for the vision call (rulings V-S8, V-S9): the type from the magic bytes, the data
URL, the cleaned description and its place in an image upload's content. No OpenAI SDK."""

import base64
import html
from typing import TYPE_CHECKING

from text_extraction.vision.types import (
    DESCRIPTION_MAX_CHARS,
    VISION_UPLOAD_TYPES,
    VisionMediaType,
)

if TYPE_CHECKING:  # the conversion libraries stay out of the LLM client's imports
    from text_extraction.convert import OutputFormat

__all__ = [
    "append_description",
    "clean_description",
    "data_url",
    "describable_upload",
    "sniff_image",
]


def sniff_image(body: bytes) -> VisionMediaType | None:
    """The vision endpoint's media type of ``body`` from its magic bytes, never from a declared
    type: PNG, JPEG, GIF and WebP; None for anything else (an SVG, HTML, a PDF, a TIFF, a BMP)."""
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    return None


def describable_upload(body: bytes, max_bytes: int) -> bool:
    """Whether the vision model can take an image upload's bytes (V-S7, V-S8): PNG, JPEG or
    WebP by their magic bytes, at most ``max_bytes`` (``LLM_VISION_MAX_IMAGE_BYTES``)."""
    return len(body) <= max_bytes and sniff_image(body) in VISION_UPLOAD_TYPES


def data_url(body: bytes, media_type: str) -> str:
    """``body`` as a base64 ``data:`` URL of ``media_type``."""
    return f"data:{media_type};base64,{base64.b64encode(body).decode('ascii')}"


def clean_description(value: str | None) -> str | None:
    """The model's description with white space collapsed, cut at the last space before
    ``DESCRIPTION_MAX_CHARS`` characters (a hard cut when there is none); None when nothing is
    left."""
    if value is None:
        return None
    text = " ".join(value.split())
    if len(text) > DESCRIPTION_MAX_CHARS:
        window = text[: DESCRIPTION_MAX_CHARS + 1]
        position = window.rfind(" ")
        text = window[:position] if position > 0 else window[:DESCRIPTION_MAX_CHARS]
        text = text.rstrip()
    return text or None


def append_description(content: str, description: str, fmt: "OutputFormat") -> str:
    """``content`` followed by the description of an uploaded image, as markdown (a quote), text
    or html (escaped). Content that is empty gets the part alone, without a leading line break."""
    if fmt == "markdown":
        part, lead = f"> Image description: {description}", "\n\n"
    elif fmt == "html":
        part, lead = f"<p>Image description: {html.escape(description)}</p>", "\n"
    else:
        part, lead = f"Image description: {description}", "\n\n"
    return content + lead + part if content else part
