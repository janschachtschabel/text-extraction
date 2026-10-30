"""Live site run: map real sites with ``POST /v1/site-structure`` and report what the crawl found.

    uv run python -m tests.live.run_site --urls tests/live/urls/sites.csv --depth 1 \
        --max-pages 25 --compliance enforce

Needs a started service with an extract worker (the crawl runs as a ``site`` job). For each
site it sends one request, waits for the job to complete, reads all its items from
``GET /v1/jobs/{id}`` and prints a Markdown summary: the job's state and duration, its items,
page items, sitemap files, further parts and entries, and histograms of the pages' links by
``region`` and by ``crawl`` decision, with the warnings and error codes. A report, never a gate:
the exit code is 0 whatever the sites answer. See `tests/live/README.md`.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from tests.live.run_live import domain_of, read_urls

DEFAULT_URLS = Path(__file__).parent / "urls" / "sites.csv"
DEFAULT_DEPTH = 1
DEFAULT_MAX_PAGES = 25
# One request's deadline; the crawl itself runs as a job and is waited for separately.
DEFAULT_CLIENT_TIMEOUT_SECONDS = 60.0
# A crawl of 25 pages takes minutes at the default per-host spacing; large sitemaps add to it.
DEFAULT_JOB_TIMEOUT_SECONDS = 1800.0
DEFAULT_POLL_SECONDS = 5.0
# Items per page of ``GET /v1/jobs/{id}`` (its maximum).
PAGE_SIZE = 500
# The column orders of the histograms: the API's own (``LinkRegion`` and ``Crawl`` in
# ``text_extraction.site.types``; a test keeps them in step), then any value it adds later,
# sorted. Spelled out, so the harness also runs against the service of another commit.
REGIONS = ("nav", "header", "footer", "aside", "main", "breadcrumb", "pagination", "none")
CRAWLS = (
    "listed",
    "file",
    "out_of_scope",
    "nofollow",
    "blocked",
    "depth",
    "queued",
    "seen",
    "max_pages",
    "max_files",
    "memory_guard",
)


@dataclass(frozen=True)
class SiteReport:
    """What one site's crawl gave; one JSONL line and one row of each summary table."""

    url: str
    state: str  # the job's state, or ``refused`` when the request got no job
    error: str | None  # why the run is incomplete: a refused request, a client error, a timeout
    seconds: float | None  # the job's duration (created to finished), else the client's wait
    items: int = 0
    pages: int = 0
    pages_ok: int = 0  # page items with status success
    failed: int = 0  # items of any role with status failed
    sitemap_files: int = 0
    sitemap_parts: int = 0
    entries: int = 0  # sitemap entries in the sitemap items and their further parts
    regions: dict[str, int] = field(default_factory=dict)
    crawls: dict[str, int] = field(default_factory=dict)
    warnings: dict[str, int] = field(default_factory=dict)
    error_codes: dict[str, int] = field(default_factory=dict)

    @classmethod
    def refused(cls, url: str, error: str) -> "SiteReport":
        """A site whose request was not accepted (no job)."""
        return cls(url=url, state="refused", error=error, seconds=None)


def _seconds(job: dict[str, Any]) -> float | None:
    created, finished = job.get("created_at"), job.get("finished_at")
    if not isinstance(created, str) or not isinstance(finished, str):
        return None
    try:
        delta = datetime.fromisoformat(finished) - datetime.fromisoformat(created)
    except ValueError:
        return None
    return round(delta.total_seconds(), 1)


def tally(
    url: str,
    job: dict[str, Any],
    items: Sequence[dict[str, Any]],
    *,
    error: str | None = None,
    elapsed: float | None = None,
) -> SiteReport:
    """Count a site job's items. ``elapsed`` (the client's wait) stands in for the duration of a
    job that has not finished."""
    roles: Counter[str] = Counter()
    regions: Counter[str] = Counter()
    crawls: Counter[str] = Counter()
    warnings: Counter[str] = Counter()
    codes: Counter[str] = Counter()
    pages_ok = failed = entries = 0
    for item in items:
        result = item.get("result") or {}
        data = result.get("data") or {}
        site = data.get("site") or {}
        role = site.get("role") or "unknown"
        roles[role] += 1
        status = result.get("status")
        if status == "failed":
            failed += 1
        if role == "page" and status == "success":
            pages_ok += 1
        if error_info := result.get("error"):
            codes[error_info.get("code") or "unknown"] += 1
        warnings.update(data.get("warnings") or [])
        for link in site.get("links") or []:
            regions[link.get("region") or "none"] += 1
            crawls[link.get("crawl") or "unknown"] += 1
        sitemap = site.get("sitemap") or {}
        entries += len(sitemap.get("entries") or [])
    seconds = _seconds(job)
    return SiteReport(
        url=url,
        state=str(job.get("state") or "unknown"),
        error=error,
        seconds=seconds if seconds is not None else elapsed,
        items=len(items),
        pages=roles["page"],
        pages_ok=pages_ok,
        failed=failed,
        sitemap_files=roles["sitemap"],
        sitemap_parts=roles["sitemap_part"],
        entries=entries,
        regions=dict(regions),
        crawls=dict(crawls),
        warnings=dict(warnings),
        error_codes=dict(codes),
    )


def _refusal(response: httpx.Response) -> str:
    try:
        error = response.json().get("error") or {}
        code = error.get("code")
    except (ValueError, AttributeError):
        code = None
    return f"HTTP {response.status_code} {code or 'no envelope'}"


async def _items(
    client: httpx.AsyncClient, status_url: str, page_size: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The job and all its items, page by page."""
    items: list[dict[str, Any]] = []
    offset: int | None = 0
    job: dict[str, Any] = {}
    while offset is not None:
        response = await client.get(status_url, params={"offset": offset, "limit": page_size})
        response.raise_for_status()
        job = response.json()["data"]
        items += job.get("items") or []
        following = job.get("next_offset")
        offset = following if isinstance(following, int) and following > offset else None
    return job, items


async def crawl(
    client: httpx.AsyncClient,
    url: str,
    *,
    depth: int,
    max_pages: int,
    compliance: str,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    job_timeout: float = DEFAULT_JOB_TIMEOUT_SECONDS,
    page_size: int = PAGE_SIZE,
) -> SiteReport:
    """Map one site: the request, the wait for its job, then all its items. Never raises for
    what the service or the site answers: a refusal or a client error is in the report."""
    started = time.monotonic()
    body = {
        "url": url,
        "depth": depth,
        "max_pages": max_pages,
        "compliance": compliance,
        "cache": False,
    }
    try:
        response = await client.post("/v1/site-structure", json=body)
        if response.status_code != 202:
            return SiteReport.refused(url, _refusal(response))
        status_url = response.json()["data"]["status_url"]
        error: str | None = None
        while True:
            polled = await client.get(status_url, params={"limit": 1})
            polled.raise_for_status()
            if polled.json()["data"]["state"] == "completed":
                break
            if time.monotonic() - started >= job_timeout:
                error = "the job did not complete in time"
                break
            await asyncio.sleep(poll_seconds)
        job, items = await _items(client, status_url, page_size)
        elapsed = round(time.monotonic() - started, 1)
        # Counted here, so an answer of an unexpected shape is this site's error only.
        return tally(url, job, items, error=error, elapsed=elapsed)
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as failure:
        return SiteReport(
            url=url,
            state="error",
            error=f"client {type(failure).__name__}",
            seconds=round(time.monotonic() - started, 1),
        )


async def run(
    urls: Sequence[str],
    client: httpx.AsyncClient,
    *,
    depth: int,
    max_pages: int,
    compliance: str,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    job_timeout: float = DEFAULT_JOB_TIMEOUT_SECONDS,
) -> list[SiteReport]:
    """All sites at once (each is a host of its own, so the per-host limits keep them apart);
    the reports in the order of ``urls``."""
    return list(
        await asyncio.gather(
            *(
                crawl(
                    client,
                    url,
                    depth=depth,
                    max_pages=max_pages,
                    compliance=compliance,
                    poll_seconds=poll_seconds,
                    job_timeout=job_timeout,
                )
                for url in urls
            )
        )
    )


def _columns(order: Sequence[str], counts: Sequence[dict[str, int]]) -> list[str]:
    found = {name for count in counts for name in count}
    return [name for name in order if name in found] + sorted(found - set(order))


def _table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return lines


def _histogram(
    title: str, order: Sequence[str], reports: Sequence[SiteReport], counts: list[dict[str, int]]
) -> list[str]:
    columns = _columns(order, counts)
    if not columns:
        return [f"### {title}", "", "No links.", ""]
    rows = [
        [domain_of(report.url), *(count.get(name, 0) for name in columns)]
        for report, count in zip(reports, counts, strict=True)
    ]
    return [f"### {title}", "", *_table(["Site", *columns], rows), ""]


def _listing(counts: dict[str, int]) -> str:
    ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return ", ".join(f"{name} {count}" for name, count in ranked) or "-"


def summarize(reports: Sequence[SiteReport], *, depth: int, max_pages: int, compliance: str) -> str:
    """The Markdown summary: a row per site, then the link histograms and the warnings."""
    sites = f"{len(reports)} site{'' if len(reports) == 1 else 's'}"
    lines = [
        f"## Site structure: {sites}, depth {depth}, max_pages {max_pages}, "
        f"compliance {compliance}",
        "",
    ]
    rows = [
        [
            domain_of(report.url),
            report.state if report.error is None else f"{report.state}: {report.error}",
            "-" if report.seconds is None else f"{report.seconds:.1f} s",
            report.items,
            report.pages,
            report.pages_ok,
            report.failed,
            report.sitemap_files,
            report.sitemap_parts,
            report.entries,
        ]
        for report in reports
    ]
    header = [
        "Site",
        "State",
        "Duration",
        "Items",
        "Pages",
        "Pages ok",
        "Failed",
        "Sitemap files",
        "Parts",
        "Entries",
    ]
    lines += [*_table(header, rows), ""]
    lines += _histogram("Links by region", REGIONS, reports, [r.regions for r in reports])
    lines += _histogram("Links by crawl decision", CRAWLS, reports, [r.crawls for r in reports])
    notes = [
        [domain_of(report.url), _listing(report.warnings), _listing(report.error_codes)]
        for report in reports
    ]
    lines += ["### Warnings and error codes", ""]
    lines += _table(["Site", "Warnings", "Error codes"], notes)
    return "\n".join(lines) + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--api-key", default=os.environ.get("LIVE_API_KEY"))
    parser.add_argument("--urls", type=Path, default=DEFAULT_URLS, help="CSV with a `url` column")
    parser.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument(
        "--compliance",
        choices=["enforce", "report", "ignore"],
        default="enforce",
        help="the request's compliance mode (default enforce: a crawl respects robots.txt and TDM)",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_CLIENT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--job-timeout",
        type=float,
        default=DEFAULT_JOB_TIMEOUT_SECONDS,
        help="seconds to wait for a site's job to complete",
    )
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--out", type=Path, default=None, help="JSONL file (default: results/...)")
    parser.add_argument(
        "--summary-out", type=Path, default=None, help="also write the summary here"
    )
    args = parser.parse_args(argv)
    if args.out is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        args.out = Path("results") / f"site_{stamp}.jsonl"
    return args


def _transport() -> httpx.AsyncBaseTransport | None:
    """The client's transport: the network (None); the tests put a fake service here."""
    return None


async def amain(args: argparse.Namespace) -> int:
    urls = read_urls(args.urls)
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    # trust_env off: the harness talks to the service only, never through a proxy.
    async with httpx.AsyncClient(
        base_url=args.base_url,
        headers=headers,
        timeout=args.timeout,
        trust_env=False,
        transport=_transport(),
    ) as client:
        reports = await run(
            urls,
            client,
            depth=args.depth,
            max_pages=args.max_pages,
            compliance=args.compliance,
            poll_seconds=args.poll_seconds,
            job_timeout=args.job_timeout,
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as sink:
        for report in reports:
            sink.write(json.dumps(asdict(report)) + "\n")
    summary = summarize(
        reports, depth=args.depth, max_pages=args.max_pages, compliance=args.compliance
    )
    print(summary)
    if args.summary_out is not None:
        args.summary_out.parent.mkdir(parents=True, exist_ok=True)
        args.summary_out.write_text(summary, encoding="utf-8")
    print(f"Results: {args.out}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(amain(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
