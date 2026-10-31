"""Preview images of a result (ruling P10-S16, D7 a): the server fetches them through the vision
``ImageFetcher`` (the SSRF guard on the URL and every redirect, the caller's compliance mode, the
host's outbound turn, the service's user agent, the type sniffed from the bytes, at most
``UI_IMAGE_MAX_BYTES``) and the page shows them as ``data:`` URIs, so third-party hosts never
see the viewer and the CSP stays ``img-src 'self' data:``."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

import structlog
from prometheus_client import CollectorRegistry, Counter

from text_extraction.compliance import ComplianceMode
from text_extraction.vision.fetch import ImageBytes, ImageFetcher, ImageSkipped
from text_extraction.vision.images import data_url

_log = structlog.get_logger(__name__)

MAX_PREVIEWS: Final = 20
PREVIEW_CONCURRENCY: Final = 4
PREVIEW_TIMEOUT_SECONDS: Final = 10.0
# The whole view's time: images not fetched by then are skipped ("time"), so the view answers
# within the ops guide's ~30 s.
PREVIEW_DEADLINE_SECONDS: Final = 25.0
SHOWN: Final = "shown"
TIME: Final = "time"
FAILED: Final = "error"
OUTCOMES: Final = (
    SHOWN,
    TIME,
    "blocked",
    "refused",
    "too_large",
    "unsupported",
    "http_error",
    "error",
    "busy",
)


@dataclass(frozen=True)
class Preview:
    """One image: its ``data:`` URI when it was fetched, else why it was skipped."""

    url: str
    data_uri: str | None
    skipped: str | None


def _host_of(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def preview_counter(registry: CollectorRegistry) -> Counter:
    """``text_extraction_ui_image_previews_total{outcome}``: ``shown`` or the skip reason, each
    exported at 0 from the start."""
    counter = Counter(
        "text_extraction_ui_image_previews_total",
        "Preview images the UI fetched, by outcome (shown or the reason it was skipped).",
        ["outcome"],
        registry=registry,
    )
    for outcome in OUTCOMES:
        counter.labels(outcome)
    return counter


async def load_previews(
    images: Sequence[str],
    *,
    fetcher: ImageFetcher,
    mode: ComplianceMode,
    max_bytes: int,
    deadline_seconds: float = PREVIEW_DEADLINE_SECONDS,
) -> list[Preview]:
    """The previews of the first ``MAX_PREVIEWS`` distinct URLs of ``images``, in order, fetched
    at most ``PREVIEW_CONCURRENCY`` at once with ``mode`` and ``max_bytes``, each within
    ``PREVIEW_TIMEOUT_SECONDS`` and all within ``deadline_seconds``: an image not fetched by
    then is skipped as ``time``. An unexpected error of one fetch skips that image as
    ``error``; the others go on."""
    urls = list(dict.fromkeys(images))[:MAX_PREVIEWS]
    limit = asyncio.Semaphore(PREVIEW_CONCURRENCY)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_seconds

    async def fetched(url: str) -> ImageBytes | ImageSkipped | None:
        """The image, why it was skipped, or None when the deadline came first."""
        async with limit:
            left = deadline - loop.time()
            if left <= 0:
                return None
            timeout = min(PREVIEW_TIMEOUT_SECONDS, left)
            return await fetcher.get(url, mode, timeout=timeout, max_bytes=max_bytes)

    async def one(url: str) -> Preview:
        image: ImageBytes | ImageSkipped | None
        try:
            async with asyncio.timeout_at(deadline):
                image = await fetched(url)
        except TimeoutError:
            image = None
        except Exception:  # one image's failure is that image's, not the view's
            _log.exception("ui_preview_failed", host=_host_of(url))
            return Preview(url=url, data_uri=None, skipped=FAILED)
        if image is None:
            return Preview(url=url, data_uri=None, skipped=TIME)
        if isinstance(image, ImageSkipped):
            return Preview(url=url, data_uri=None, skipped=image.reason)
        return Preview(url=url, data_uri=data_url(image.data, image.media_type), skipped=None)

    return list(await asyncio.gather(*(one(url) for url in urls)))
