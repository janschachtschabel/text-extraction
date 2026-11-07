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
    links: tuple[
        tuple[str, str] | tuple[str, str, str | None] | tuple[str, str, str | None, str], ...
    ] = (),
    error: ErrorCode | None = None,
    page_type: str | None = None,
    external: tuple[tuple[str, str | None], ...] = (),
) -> JobItem:
    """A crawled page; ``links`` are (url, crawl) pairs, (url, crawl, category) triples (a
    pair, like an item stored before link categories, has no category) or (url, crawl, category,
    text) quadruples; ``page_type`` is the
    page's type (None: like a failed page or an item stored before page types); ``external`` are
    (url, category) pairs of external links (a None category: like an item stored before link
    categories)."""
    site = {
        "role": "page",
        "depth": depth,
        "parent": None if index == 0 else 0,
        "links": [
            {
                "url": link[0],
                "text": link[3] if len(link) == 4 else "",
                "kind": "internal",
                "source": "a",
                "rel": [],
                "crawl": link[1],
                "category": link[2] if len(link) >= 3 else None,
            }
            for link in links
        ]
        + [
            {
                "url": url_,
                "text": "",
                "kind": "external",
                "source": "a",
                "rel": [],
                "crawl": "out_of_scope",
                "category": category,
            }
            for url_, category in external
        ],
    }
    typed = {"type": page_type, "signals": []} if page_type is not None else None
    envelope: dict[str, Any] = {
        "status": "failed" if error else "success",
        "data": result_data(url, title=title, site=site, page_type=typed),
    }
    if error is not None:
        envelope["error"] = ErrorInfo(code=error, message="x", retryable=False).model_dump()
    return _item(index, url, envelope)


def robots_item(index: int, url: str) -> JobItem:
    site = {"role": "robots", "depth": 0, "parent": 0, "sitemaps": []}
    return _item(index, url, {"status": "success", "data": result_data(url, site=site)})


def sitemap_item(
    index: int,
    url: str,
    entries: list[dict[str, Any]],
    *,
    part: int | None = None,
    type_hint: str | None = None,
) -> JobItem:
    """A sitemap (or, with ``part``, a further part of one) listing ``entries``."""
    site = {
        "role": "sitemap" if part is None else "sitemap_part",
        "depth": 0,
        "parent": 1,
        "sitemap": {
            "format": "urlset",
            "entries": entries,
            "part": part or 1,
            "type_hint": type_hint,
        },
    }
    return _item(index, url, {"status": "success", "data": result_data(url, site=site)})


def pending_item(index: int, url: str) -> JobItem:
    return JobItem(index=index, url=url, state="pending", result=None)
