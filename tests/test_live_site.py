"""The live site run (`tests/live/run_site.py`): the requests it sends, how it pages a site job,
and its Markdown summary, against a fake service."""

import asyncio
import json
from pathlib import Path
from typing import Any, get_args

import httpx
import pytest

from tests.live import run_site
from tests.live.run_live import read_urls
from tests.live.run_site import CRAWLS, REGIONS, SiteReport, summarize, tally
from text_extraction.site.types import Crawl, LinkRegion

SITES = [
    "https://www.lehrer-online.de/",
    "https://www.planet-schule.de/",
    "https://klexikon.zum.de/",
    "https://www.leifiphysik.de/",
    "https://de.serlo.org/",
]
START = "https://lernen.example.org/"


def page(index: int, links: list[tuple[str | None, str]], *, depth: int = 0, **result: Any) -> Any:
    """A page item with links of the given (region, crawl)."""
    return {
        "index": index,
        "url": f"{START}p{index}",
        "state": "done",
        "result": {
            "status": result.get("status", "success"),
            "error": result.get("error"),
            "data": {
                "warnings": result.get("warnings", []),
                "site": {
                    "role": "page",
                    "depth": depth,
                    "links": [
                        {"url": f"{START}x{n}", "region": region, "crawl": crawl}
                        for n, (region, crawl) in enumerate(links)
                    ],
                },
            },
        },
    }


def sitemap(index: int, entries: int, *, role: str = "sitemap", **result: Any) -> Any:
    return {
        "index": index,
        "url": f"{START}sitemap.xml",
        "state": "done",
        "result": {
            "status": result.get("status", "success"),
            "error": result.get("error"),
            "data": {
                "warnings": result.get("warnings", []),
                "site": {
                    "role": role,
                    "depth": 0,
                    "links": None,
                    "sitemap": {"entries": [{"loc": f"{START}e{n}"} for n in range(entries)]},
                },
            },
        },
    }


def robots(index: int) -> Any:
    return {
        "index": index,
        "url": f"{START}robots.txt",
        "state": "done",
        "result": {
            "status": "success",
            "error": None,
            "data": {"warnings": [], "site": {"role": "robots", "depth": 0, "links": None}},
        },
    }


ITEMS = [
    page(0, [("nav", "queued"), ("nav", "queued"), (None, "listed"), ("footer", "out_of_scope")]),
    robots(1),
    page(2, [("nav", "seen"), ("main", "depth")], depth=1),
    sitemap(3, 4, warnings=["sitemap_urls_truncated"]),
    sitemap(4, 2, role="sitemap_part"),
    page(
        5,
        [],
        depth=1,
        status="failed",
        error={"code": "target_http_error", "message": "x", "retryable": False},
    ),
]
JOB = {
    "id": "j1",
    "kind": "site",
    "state": "completed",
    "created_at": "2026-10-30T12:00:00Z",
    "finished_at": "2026-10-30T12:01:05.500000Z",
    "progress": {"total": len(ITEMS)},
}


def test_the_shipped_site_list_has_the_five_sites() -> None:
    assert read_urls(Path(run_site.__file__).parent / "urls" / "sites.csv") == SITES


def test_histogram_columns_follow_the_api() -> None:
    assert (*get_args(LinkRegion), "none") == REGIONS
    assert get_args(Crawl) == CRAWLS


def test_tally_counts_items_entries_regions_and_crawl_decisions() -> None:
    report = tally(START, JOB, ITEMS)

    assert report == SiteReport(
        url=START,
        state="completed",
        error=None,
        seconds=65.5,
        items=6,
        pages=3,
        pages_ok=2,
        failed=1,
        sitemap_files=1,
        sitemap_parts=1,
        entries=6,
        regions={"nav": 3, "main": 1, "footer": 1, "none": 1},
        crawls={"queued": 2, "seen": 1, "depth": 1, "listed": 1, "out_of_scope": 1},
        warnings={"sitemap_urls_truncated": 1},
        error_codes={"target_http_error": 1},
    )


def test_summary_is_markdown_with_one_row_per_site() -> None:
    reports = [
        tally(START, JOB, ITEMS),
        SiteReport.refused("https://down.example/", "HTTP 503 internal_error"),
    ]

    summary = summarize(reports, depth=1, max_pages=25, compliance="report")

    lines = summary.splitlines()
    assert lines[0] == "## Site structure: 2 sites, depth 1, max_pages 25, compliance report"
    assert (
        "| Site | State | Duration | Items | Pages | Pages ok | Failed | Sitemap files "
        "| Parts | Entries |"
    ) in lines
    assert "| lernen.example.org | completed | 65.5 s | 6 | 3 | 2 | 1 | 1 | 1 | 6 |" in lines
    refused = "| down.example | refused: HTTP 503 internal_error | - | 0 | 0 | 0 | 0 | 0 | 0 | 0 |"
    assert refused in lines
    # The histograms: a column per value that occurs, in the API's order, `none` last.
    assert "### Links by region" in lines
    assert "| Site | nav | footer | main | none |" in lines
    assert "| lernen.example.org | 3 | 1 | 1 | 1 |" in lines
    assert "### Links by crawl decision" in lines
    assert "| Site | listed | out_of_scope | depth | queued | seen |" in lines
    assert "| lernen.example.org | 1 | 1 | 1 | 2 | 1 |" in lines
    assert "| down.example | 0 | 0 | 0 | 0 | 0 |" in lines
    assert "### Warnings and error codes" in lines
    assert "| lernen.example.org | sitemap_urls_truncated 1 | target_http_error 1 |" in lines
    assert summary.endswith("\n")


def fake_service(
    items: list[Any], *, states: list[str] | None = None, accept: int = 202
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """A service that accepts a site job and reports it with ``states`` (the last one stays),
    paging ``items`` as ``GET /v1/jobs/{id}`` does."""
    seen: list[httpx.Request] = []
    remaining = list(states or ["completed"])

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            if accept != 202:
                error = {"code": "internal_error", "message": "x", "retryable": True}
                return httpx.Response(accept, json={"status": "failed", "error": error})
            data = {"id": "j1", "kind": "site", "state": "queued", "total": 1}
            return httpx.Response(202, json={"data": {**data, "status_url": "/v1/jobs/j1"}})
        state = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params.get("limit", 100))
        chunk = items[offset : offset + limit]
        following = offset + limit if state == "completed" and offset + limit < len(items) else None
        data = {**JOB, "state": state, "items": chunk, "next_offset": following}
        return httpx.Response(200, json={"status": "success", "data": data})

    return httpx.MockTransport(handle), seen


def crawl_with(transport: httpx.MockTransport, **options: Any) -> SiteReport:
    async def scenario() -> SiteReport:
        async with httpx.AsyncClient(transport=transport, base_url="http://service") as client:
            return await run_site.crawl(
                client,
                START,
                depth=options.get("depth", 1),
                max_pages=options.get("max_pages", 25),
                compliance=options.get("compliance", "enforce"),
                poll_seconds=0,
                job_timeout=options.get("job_timeout", 5),
                page_size=options.get("page_size", 2),
            )

    return asyncio.run(scenario())


def test_crawl_sends_the_documented_request_and_pages_the_items() -> None:
    transport, seen = fake_service(ITEMS, states=["queued", "running", "completed"])

    report = crawl_with(transport)

    post = seen[0]
    assert (post.method, post.url.path) == ("POST", "/v1/site-structure")
    assert json.loads(post.content) == {
        "url": START,
        "depth": 1,
        "max_pages": 25,
        "compliance": "enforce",
        "cache": False,
    }
    assert report == tally(START, JOB, ITEMS)
    pages = [request.url.params for request in seen[1:] if request.url.params.get("offset")]
    assert [(params["offset"], params["limit"]) for params in pages] == [
        ("0", "2"),
        ("2", "2"),
        ("4", "2"),
    ]


def test_a_refused_request_is_reported_not_raised() -> None:
    transport, _ = fake_service(ITEMS, accept=503)

    report = crawl_with(transport)

    assert report == SiteReport.refused(START, "HTTP 503 internal_error")


def test_a_job_that_does_not_complete_in_time_is_reported_with_its_items() -> None:
    transport, _ = fake_service(ITEMS[:2], states=["running"])

    report = crawl_with(transport, job_timeout=0)

    assert report.state == "running"
    assert report.error == "the job did not complete in time"
    assert report.items == 2  # what it had so far


def test_a_malformed_time_falls_back_to_the_clients_wait() -> None:
    report = tally(START, {**JOB, "finished_at": "not a time"}, ITEMS, elapsed=12.5)

    assert (report.seconds, report.pages) == (12.5, 3)


def test_a_malformed_answer_is_reported_for_its_site_only() -> None:
    broken = [{"index": 0, "url": START, "state": "done", "result": ["not", "an", "envelope"]}]
    transport, _ = fake_service(broken)

    async def scenario() -> list[SiteReport]:
        async with httpx.AsyncClient(transport=transport, base_url="http://service") as client:
            return await run_site.run(
                [START, START],
                client,
                depth=1,
                max_pages=25,
                compliance="enforce",
                poll_seconds=0,
                job_timeout=5,
            )

    reports = asyncio.run(scenario())

    assert [(report.state, report.error) for report in reports] == [
        ("error", "client AttributeError"),
        ("error", "client AttributeError"),
    ]


def test_main_writes_the_summary_and_never_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    urls = tmp_path / "sites.csv"
    urls.write_text(f"url\n{START}\n")
    transport, seen = fake_service(ITEMS)
    monkeypatch.setattr(run_site, "_transport", lambda: transport)
    summary_out = tmp_path / "summary.md"
    out = tmp_path / "sites.jsonl"

    code = run_site.main(
        [
            "--urls",
            str(urls),
            "--summary-out",
            str(summary_out),
            "--out",
            str(out),
            "--poll-seconds",
            "0",
        ]
    )

    assert code == 0
    # The defaults: one click deep, 25 pages, and robots.txt and TDM opt-outs respected.
    sent = json.loads(seen[0].content)
    assert (sent["depth"], sent["max_pages"], sent["compliance"]) == (1, 25, "enforce")
    printed = capsys.readouterr().out
    assert summary_out.read_text(encoding="utf-8") in printed
    assert summary_out.read_text(encoding="utf-8").startswith("## Site structure: 1 site, depth 1,")
    [line] = out.read_text(encoding="utf-8").splitlines()
    assert json.loads(line)["url"] == START
    assert json.loads(line)["pages"] == 3
