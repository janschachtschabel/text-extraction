"""The describe step of the post stage (rulings V-S4, V-S8, V-S11, V-S12): step 0 of
``structured.post``, before the structured LLM and chunking, so an upload's description is part
of the content those read.

**Which images** (``vision_targets``). A page's or a URL document's: the first ``max_images``
entries of ``metadata.images``, in their order (cover images first). An image upload
(``mode_used: upload``) of PNG, JPEG or WebP: one target, itself; a TIFF or BMP upload: one target
that is skipped (the vision endpoint takes png, jpeg, webp and gif only). Any other result has
none: an empty report, no warning, no LLM.

**Per image** (``describe_result``). A page image comes from the ``ImageSource`` (the image
fetcher: the SSRF guard, the job's compliance mode, the outbound gate), an upload's from its
stored bytes (``upload``; their type from the magic bytes, at most
``LLM_VISION_MAX_IMAGE_BYTES``). Then one vision call (``LlmClient.describe_image``, plus its
repair call) with ``VISION_SYSTEM`` and ``vision_text``: the alt text and the image are data.

* not fetched, refused, not an allowed type, too large, an input that is gone, a Valkey error
  of the fetcher: ``skipped``, warning ``image_not_described``; the others go on;
* refused by the endpoint (HTTP 400, 413, 422) or an answer that does not fit after the repair
  call (``LlmError.invalid_answer``, ruling R-V1), or ``description: null`` (nothing worth
  describing): ``failed``, the same warning; the others go on;
* any other ``LlmError`` (401, 403, 404, 429, 5xx, a timeout, no connection) stops the step:
  no image is fetched or sent after it, the images not done count ``failed``, and the post
  stage gets that error (``success`` becomes ``partial``/``llm_failed``);
* the step's budget (``seconds``, half the llm item's deadline) running out does the same with
  ``DESCRIBE_TIMEOUT_MESSAGE``, retryable. Each fetch gets ``min(FETCH_TIMEOUT_SECONDS, what is
  left)``.

``DESCRIBE_CONCURRENCY`` images run at once; the results keep the targets' order. A page image's
description goes on its entry (``metadata.images[i].description``); an upload's is appended to
``content`` by ``format`` and recorded on a new entry with ``url: null`` (V-S8). ``status``,
``quality``, ``word_count`` and ``fingerprint`` stay the extraction's.

``without_vision`` is the step without the LLM (``finish_post``: the memory guard, a hop that
failed, an exhausted item): every image that would be sent counts ``failed``, with the given
message, retryable; a TIFF or BMP upload is ``skipped`` as above.

Image bytes live only in this step's memory. Nothing here logs or counts by value an image byte,
URL, alt text, description or prompt: the counter ``text_extraction_vision_images_total`` has the
outcome as its only label, and logs carry error types.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal
from weakref import WeakKeyDictionary

import structlog
from prometheus_client import REGISTRY, CollectorRegistry, Counter
from redis.exceptions import RedisError

from text_extraction.compliance import ComplianceMode
from text_extraction.extract_models import ExtractResult
from text_extraction.llm.basics import LlmError
from text_extraction.llm.usage import LlmUsage
from text_extraction.metadata import Image, PageMetadata
from text_extraction.settings import Settings
from text_extraction.vision.fetch import ImageBytes, ImageSkipped, ImageSource
from text_extraction.vision.images import append_description, clean_description, sniff_image
from text_extraction.vision.prompts import DESCRIPTION_SCHEMA, VISION_SYSTEM, vision_text
from text_extraction.vision.types import (
    IMAGE_NOT_DESCRIBED_WARNING,
    IMAGE_UPLOAD_TYPES,
    VISION_UPLOAD_TYPES,
    DescribeImagesOptions,
    ImageDescriptionReport,
)

if TYPE_CHECKING:  # the OpenAI SDK only where a client is made (``llm/basics.py``)
    from text_extraction.llm.client import LlmClient

__all__ = [
    "DESCRIBE_CONCURRENCY",
    "DESCRIBE_TIMEOUT_MESSAGE",
    "DescribeOutcome",
    "ImageTarget",
    "describe_owed",
    "describe_result",
    "needs_vision",
    "vision_targets",
    "without_vision",
]

_log = structlog.get_logger(__name__)

DESCRIBE_CONCURRENCY: Final = 2  # images at once per result
DESCRIBE_TIMEOUT_MESSAGE: Final = "The image descriptions did not finish in time."
# The endpoint refuses this image (too large, a type it does not take): the others go on.
_IMAGE_REFUSED: Final = frozenset({400, 413, 422})

type _Outcome = Literal["described", "skipped", "failed"]


@dataclass(frozen=True)
class ImageTarget:
    """An image to describe: ``index`` into ``metadata.images`` and its ``url`` and ``alt``, or
    an upload itself (``index`` and ``url`` None)."""

    index: int | None
    url: str | None
    alt: str


@dataclass(frozen=True)
class DescribeOutcome:
    """What the step came to: ``data`` with the descriptions (and its report as
    ``image_descriptions``), the report, the warnings to add, the failure that makes a
    ``success`` ``partial`` (None: none) and the step's milliseconds."""

    data: ExtractResult
    report: ImageDescriptionReport
    warnings: list[str]
    failure: LlmError | None
    llm_ms: int


# --- which images ------------------------------------------------------------------------------


def _upload_type(data: ExtractResult) -> str | None:
    """The image type of an upload, or None for a page, a URL document or another upload."""
    if data.mode_used != "upload" or data.content_type is None:
        return None
    media_type = data.content_type.split(";")[0].strip().lower()
    return media_type if media_type in IMAGE_UPLOAD_TYPES else None


def vision_targets(data: ExtractResult, describe: DescribeImagesOptions) -> list[ImageTarget]:
    """The images of ``data`` to describe (V-S4; see the module docstring)."""
    if data.mode_used == "upload":
        return [ImageTarget(index=None, url=None, alt="")] if _upload_type(data) else []
    images = data.metadata.images if data.metadata is not None else []
    return [
        ImageTarget(index=index, url=image.url, alt=image.alt)
        for index, image in enumerate(images[: describe.max_images])
    ]


def describe_owed(data: ExtractResult, describe: DescribeImagesOptions | None) -> bool:
    """Whether ``data`` still owes its image descriptions: asked, and no report yet."""
    return describe is not None and data.image_descriptions is None


def _sent(data: ExtractResult) -> bool:
    """Whether a target of ``data`` would go to the vision model (not a TIFF or BMP upload)."""
    upload = _upload_type(data)
    return upload is None or upload in VISION_UPLOAD_TYPES


def needs_vision(data: ExtractResult, describe: DescribeImagesOptions | None) -> bool:
    """Whether the descriptions are owed and at least one target would go to the vision model."""
    if describe is None or not describe_owed(data, describe):
        return False
    return bool(vision_targets(data, describe)) and _sent(data)


# --- the counter -------------------------------------------------------------------------------

_COUNTERS: WeakKeyDictionary[CollectorRegistry, Counter] = WeakKeyDictionary()


def _counter_for(registry: CollectorRegistry) -> Counter:
    """The counter of ``registry``, registered once per registry (as ``llm/client``)."""
    counter = _COUNTERS.get(registry)
    if counter is None:
        counter = Counter(
            "text_extraction_vision_images_total",
            "Images of describe_images by outcome (described, skipped, failed).",
            ["outcome"],
            registry=registry,
        )
        _COUNTERS[registry] = counter
    return counter


# --- the step ----------------------------------------------------------------------------------


@dataclass
class _Done:
    outcome: _Outcome
    description: str | None = None


def _validate(parsed: Any) -> str | None:
    """The cleaned description of the model's answer; None when it described nothing."""
    if not isinstance(parsed, dict) or "description" not in parsed:
        raise ValueError("The answer must be a JSON object with a description.")
    value = parsed["description"]
    if value is not None and not isinstance(value, str):
        raise ValueError("description must be a string or null.")
    return clean_description(value)


def _language(data: ExtractResult) -> str | None:
    if data.metadata is not None and data.metadata.language:
        return data.metadata.language
    return data.language


@dataclass
class _Step:
    """One run of the step on ``data``."""

    data: ExtractResult
    llm: "LlmClient"
    images: ImageSource | None
    upload: bytes | None
    mode: ComplianceMode
    settings: Settings
    deadline: float
    failure: LlmError | None = None
    usage: LlmUsage | None = None

    def left(self) -> float:
        return self.deadline - time.monotonic()

    async def image(self, target: ImageTarget) -> ImageBytes | ImageSkipped:
        """The bytes of ``target``, or why it is skipped."""
        if target.index is None:  # the upload itself
            return self._uploaded()
        if self.images is None or target.url is None:
            return ImageSkipped("error")
        timeout = min(self.settings.fetch_timeout_seconds, self.left())
        if timeout <= 0:
            return ImageSkipped("error")
        try:
            return await self.images.get(target.url, self.mode, timeout=timeout)
        except RedisError as error:  # the gate or the robots cache: this image only
            _log.warning("image_fetch_failed", error=type(error).__name__)
            return ImageSkipped("error")

    def _uploaded(self) -> ImageBytes | ImageSkipped:
        if _upload_type(self.data) not in VISION_UPLOAD_TYPES:
            return ImageSkipped("unsupported")  # a TIFF or a BMP
        body = self.upload
        if body is None:  # the stored input is gone
            return ImageSkipped("error")
        if len(body) > self.settings.llm_vision_max_image_bytes:
            return ImageSkipped("too_large")
        media_type = sniff_image(body)
        if media_type is None or media_type not in VISION_UPLOAD_TYPES:
            return ImageSkipped("unsupported")
        return ImageBytes(data=body, media_type=media_type)

    async def describe(self, target: ImageTarget, gate: asyncio.Semaphore) -> _Done:
        async with gate:
            if self.failure is not None:
                return _Done("failed")
            image = await self.image(target)
            if isinstance(image, ImageSkipped):
                return _Done("skipped")
            if self.failure is not None:
                return _Done("failed")
            try:
                description, usage = await self.llm.describe_image(
                    VISION_SYSTEM,
                    vision_text(_language(self.data), target.alt),
                    image.data,
                    image.media_type,
                    DESCRIPTION_SCHEMA,
                    validate=_validate,
                )
            except LlmError as error:
                if error.status in _IMAGE_REFUSED or error.invalid_answer:
                    return _Done("failed")
                if self.failure is None:
                    self.failure = error
                return _Done("failed")
            finally:
                del image  # the bytes go with this image
            self.usage = usage if self.usage is None else _sum(self.usage, usage)
            if description is None:
                return _Done("failed")
            return _Done("described", description)


async def describe_result(
    data: ExtractResult,
    describe: DescribeImagesOptions,
    *,
    llm: "LlmClient",
    images: ImageSource | None,
    upload: bytes | None,
    mode: ComplianceMode,
    settings: Settings,
    seconds: float,
    registry: CollectorRegistry | None = None,
) -> DescribeOutcome:
    """The step with the vision model (see the module docstring): ``images`` fetches a page's
    images in the compliance ``mode`` of the job, ``upload`` is an image upload's bytes (None:
    gone), ``seconds`` the step's budget. LLM failures are in the outcome, never raised."""
    started = time.monotonic()
    targets = vision_targets(data, describe)
    step = _Step(data, llm, images, upload, mode, settings, deadline=started + seconds)
    done: list[_Done | None] = [None] * len(targets)
    gate = asyncio.Semaphore(DESCRIBE_CONCURRENCY)

    async def one(position: int, target: ImageTarget) -> None:
        done[position] = await step.describe(target, gate)

    try:
        async with asyncio.timeout(seconds):
            async with asyncio.TaskGroup() as group:
                for position, target in enumerate(targets):
                    group.create_task(one(position, target))
    except TimeoutError:
        _log.warning("image_descriptions_timed_out", seconds=seconds)
        if step.failure is None:
            step.failure = LlmError(DESCRIBE_TIMEOUT_MESSAGE, retryable=True)
    outcomes = [item if item is not None else _Done("failed") for item in done]
    llm_ms = round((time.monotonic() - started) * 1000)
    return _outcome(data, targets, outcomes, step.failure, step.usage, llm_ms, registry)


def without_vision(
    data: ExtractResult, describe: DescribeImagesOptions, *, message: str
) -> DescribeOutcome:
    """The step without the LLM work (V-S11): every target that would be sent counts
    ``failed`` with ``LlmError(message, retryable=True)``; a TIFF or BMP upload is
    ``skipped``; a result without targets gets its empty report."""
    targets = vision_targets(data, describe)
    sent = _sent(data)
    outcomes = [_Done("failed" if sent else "skipped") for _ in targets]
    failure = LlmError(message, retryable=True) if targets and sent else None
    return _outcome(data, targets, outcomes, failure, None, 0, None)


def _outcome(
    data: ExtractResult,
    targets: list[ImageTarget],
    outcomes: list[_Done],
    failure: LlmError | None,
    usage: LlmUsage | None,
    llm_ms: int,
    registry: CollectorRegistry | None,
) -> DescribeOutcome:
    counts = {"described": 0, "skipped": 0, "failed": 0}
    for item in outcomes:
        counts[item.outcome] += 1
    if targets:
        counter = _counter_for(registry if registry is not None else REGISTRY)
        for outcome, count in counts.items():
            if count:
                counter.labels(outcome).inc(count)
    report = ImageDescriptionReport(
        asked=len(targets),
        described=counts["described"],
        skipped=counts["skipped"],
        failed=counts["failed"],
        llm=usage,
    )
    warnings = [IMAGE_NOT_DESCRIBED_WARNING] if counts["skipped"] or counts["failed"] else []
    return DescribeOutcome(
        data=_with_descriptions(data, targets, outcomes, report),
        report=report,
        warnings=warnings,
        failure=failure,
        llm_ms=llm_ms,
    )


def _with_descriptions(
    data: ExtractResult,
    targets: list[ImageTarget],
    outcomes: list[_Done],
    report: ImageDescriptionReport,
) -> ExtractResult:
    """``data`` with the descriptions on its image entries (an upload's appended to the content
    and on a new entry, V-S8) and ``report`` as ``image_descriptions``."""
    update: dict[str, Any] = {"image_descriptions": report}
    metadata = data.metadata
    images = list(metadata.images) if metadata is not None else []
    changed = False
    for target, item in zip(targets, outcomes, strict=True):
        if item.description is None:
            continue
        changed = True
        if target.index is not None:
            images[target.index] = images[target.index].model_copy(
                update={"description": item.description}
            )
            continue
        images.append(Image(url=None, alt="", description=item.description))
        if data.content is not None:  # a content that was not stored stays so
            update["content"] = append_description(data.content, item.description, data.format)
    if changed:
        base = metadata if metadata is not None else PageMetadata()
        update["metadata"] = base.model_copy(update={"images": images})
    return data.model_copy(update=update)


def _sum(first: LlmUsage, second: LlmUsage) -> LlmUsage:
    return LlmUsage(
        model=first.model,
        calls=first.calls + second.calls,
        prompt_tokens=first.prompt_tokens + second.prompt_tokens,
        completion_tokens=first.completion_tokens + second.completion_tokens,
    )
