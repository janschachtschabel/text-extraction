"""Preview images of a result (ruling P10-S16, D7 a): the server fetches them through the vision
``ImageFetcher`` (the SSRF guard on the URL and every redirect, the caller's compliance mode, the
host's outbound turn, the service's user agent, the type sniffed from the bytes, at most
``UI_IMAGE_MAX_BYTES``) and the page shows them as ``data:`` URIs, so third-party hosts never
see the viewer and the CSP stays ``img-src 'self' data:``."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from prometheus_client import CollectorRegistry, Counter

from text_extraction.compliance import ComplianceMode
from text_extraction.vision.fetch import ImageFetcher, ImageSkipped
from text_extraction.vision.images import data_url

MAX_PREVIEWS: Final = 20
PREVIEW_CONCURRENCY: Final = 4
PREVIEW_TIMEOUT_SECONDS: Final = 10.0
SHOWN: Final = "shown"
OUTCOMES: Final = (
    SHOWN,
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
) -> list[Preview]:
    """The previews of the first ``MAX_PREVIEWS`` distinct URLs of ``images``, in order, fetched
    at most ``PREVIEW_CONCURRENCY`` at once with ``mode`` and ``max_bytes``, each within
    ``PREVIEW_TIMEOUT_SECONDS``."""
    urls = list(dict.fromkeys(images))[:MAX_PREVIEWS]
    limit = asyncio.Semaphore(PREVIEW_CONCURRENCY)

    async def one(url: str) -> Preview:
        async with limit:
            image = await fetcher.get(
                url, mode, timeout=PREVIEW_TIMEOUT_SECONDS, max_bytes=max_bytes
            )
        if isinstance(image, ImageSkipped):
            return Preview(url=url, data_uri=None, skipped=image.reason)
        return Preview(url=url, data_uri=data_url(image.data, image.media_type), skipped=None)

    return list(await asyncio.gather(*(one(url) for url in urls)))
