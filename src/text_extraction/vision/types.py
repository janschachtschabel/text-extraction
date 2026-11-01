"""The vision step's small shared types (rulings V-S1, V-S3, V-S4, V-S7): the option, the
report, the media types and the stored-input meta. No OpenAI SDK, no conversion library: the
request models and the worker parents import it."""

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from text_extraction.llm.usage import LlmUsage

__all__ = [
    "DESCRIPTION_MAX_CHARS",
    "IMAGE_NOT_DESCRIBED_WARNING",
    "IMAGE_UPLOAD_TYPES",
    "VISION_UPLOAD_TYPES",
    "DescribeImagesOptions",
    "ImageDescriptionReport",
    "StoredInputMeta",
    "VisionMediaType",
]

# The types the vision endpoint takes (png, jpeg, webp and gif).
VisionMediaType = Literal["image/png", "image/jpeg", "image/gif", "image/webp"]
# The uploaded image types that are sent to the vision model (a gif is fetched for a page's
# image, never uploaded: the document routes do not take it).
VISION_UPLOAD_TYPES: Final = frozenset({"image/png", "image/jpeg", "image/webp"})
# Every image type an upload may have; a TIFF or a BMP is not described (it is skipped).
IMAGE_UPLOAD_TYPES: Final = VISION_UPLOAD_TYPES | {"image/tiff", "image/bmp"}
DESCRIPTION_MAX_CHARS: Final = 300  # of one description
IMAGE_NOT_DESCRIBED_WARNING: Final = "image_not_described"


class DescribeImagesOptions(BaseModel, strict=True):
    """The ``describe_images`` request option: describe the page's images (the first
    ``max_images`` of ``metadata.images``) or an uploaded image with the vision model. A post-stage
    option; never part of a cache key."""

    model_config = ConfigDict(extra="forbid")

    max_images: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Most images to describe (the first ones of `metadata.images`, in order).",
    )


class ImageDescriptionReport(BaseModel):
    """What the image descriptions of a result came to; ``asked == described + skipped +
    failed``. It also marks the step as done (a result without it still owes the
    descriptions)."""

    asked: int = Field(description="Images taken, at most `max_images`.")
    described: int = Field(description="Images that got a description.")
    skipped: int = Field(
        description="Images not fetched, refused, not an allowed type or too large."
    )
    failed: int = Field(description="Images the model refused or never got.")
    llm: LlmUsage | None = Field(
        default=None, description="The vision calls' model, calls and tokens."
    )


class StoredInputMeta(BaseModel):
    """What the llm worker reads of a job item's stored input: the meta of either model that
    holds an upload's bytes (``UploadInputMeta``, ``DocumentInputMeta``); any other field is
    ignored."""

    model_config = ConfigDict(extra="ignore")

    content_type: str | None = None
    file_name: str | None = None
