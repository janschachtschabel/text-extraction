"""Job items of a ``site`` job for the UI's tree tests: pages, robots.txt and sitemap parts, as
``GET /v1/jobs/{id}`` shows them."""

from typing import Any

from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.extract_models import ExtractResult
from text_extraction.job_models import JobItem


def result_data(url: str, **overrides: Any) -> dict[str, Any]:
    """The ``data`` of a fast result for ``url``."""
    data: dict[str, Any] = {
        "url": url,
        "final_url": url,
        "upstream_status": 200,
        "content_type": "text/html",
        "mode_used": "fast",
        "attempts": 1,
        "language": "en",
        "word_count": 3,
        "title": None,
        "format": "text",
        "content": "Some text.",
        "quality": None,
        "timings": {"total_ms": 40, "compliance_ms": 3, "fetch_ms": 20, "convert_ms": 10},
        "compliance": {"mode": "enforce", "decision": "allowed", "robots": "allowed"},
        "metadata": None,
        "links": None,
        "fingerprint": None,
        "cache": "miss",
        "warnings": [],
    }
    data.update(overrides)
    return data


def _item(index: int, url: str, envelope: dict[str, Any]) -> JobItem:
    return JobItem(
        index=index,
        url=url,
        state="done",
        result=Envelope[ExtractResult].model_validate(envelope),
    )


def page_item(
    index: int,
    url: str,
    *,
    title: str | None = None,
    depth: int = 1,
    links: tuple[tuple[str, str], ...] = (),
    error: ErrorCode | None = None,
) -> JobItem:
    """A crawled page; ``links`` are (url, crawl) pairs."""
    site = {
        "role": "page",
        "depth": depth,
        "parent": None if index == 0 else 0,
        "links": [
            {"url": link, "text": "", "kind": "internal", "source": "a", "rel": [], "crawl": crawl}
            for link, crawl in links
        ],
    }
    envelope: dict[str, Any] = {
        "status": "failed" if error else "success",
        "data": result_data(url, title=title, site=site),
    }
    if error is not None:
        envelope["error"] = ErrorInfo(code=error, message="x", retryable=False).model_dump()
    return _item(index, url, envelope)


def robots_item(index: int, url: str) -> JobItem:
    site = {"role": "robots", "depth": 0, "parent": 0, "sitemaps": []}
    return _item(index, url, {"status": "success", "data": result_data(url, site=site)})


def sitemap_item(
    index: int, url: str, entries: list[dict[str, Any]], *, part: int | None = None
) -> JobItem:
    """A sitemap (or, with ``part``, a further part of one) listing ``entries``."""
    site = {
        "role": "sitemap" if part is None else "sitemap_part",
        "depth": 0,
        "parent": 1,
        "sitemap": {"format": "urlset", "entries": entries, "part": part or 1},
    }
    return _item(index, url, {"status": "success", "data": result_data(url, site=site)})


def pending_item(index: int, url: str) -> JobItem:
    return JobItem(index=index, url=url, state="pending", result=None)
