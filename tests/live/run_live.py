"""Live regression harness: send a list of real URLs to a running service and bucket the answers.

    uv run python -m tests.live.run_live --urls tests/live/urls/wlo_200.csv --min-ok-rate 68

Writes one JSON line per URL and prints a Markdown summary. See `tests/live/README.md`.
"""

import argparse
import asyncio
import csv
import json
import os
import sys
import time
from collections import Counter, defaultdict, deque
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit

import httpx

from tests.live.classify import BUCKETS, Bucket, classify, read_envelope

DEFAULT_URLS = Path(__file__).parent / "urls" / "wlo_200.csv"
# The service answers within EXTRACT_TIMEOUT_SECONDS (default 25); the client waits well longer
# so that a slow answer is recorded as the service's, not cut off by the harness.
DEFAULT_CLIENT_TIMEOUT_SECONDS = 90.0
TOP_DOMAINS = 10


@dataclass(frozen=True)
class Record:
    """One line of the JSONL output."""

    url: str
    domain: str
    bucket: Bucket
    http_status: int | None
    status: str | None
    error_code: str | None
    word_count: int | None
    compliance_decision: str | None
    elapsed_ms: int


def read_urls(path: Path) -> list[str]:
    """The ``url`` column of a CSV file with a header row; order and duplicates are kept."""
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "url" not in reader.fieldnames:
            raise ValueError(f"{path}: the header row needs a `url` column")
        return [row["url"].strip() for row in reader if row["url"].strip()]


def domain_of(url: str) -> str:
    """The lower-case host of a URL; empty when it has none."""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


async def extract_one(
    client: httpx.AsyncClient, url: str, *, compliance: str, min_words: int
) -> Record:
    """Ask the service for one URL and bucket the answer."""
    body = {"url": url, "mode": "auto", "format": "text", "cache": False, "compliance": compliance}
    started = time.monotonic()
    http_status: int | None = None
    payload: object = None
    client_error: str | None = None
    try:
        response = await client.post("/v1/extract", json=body)
        http_status = response.status_code
        try:
            payload = response.json()
        except ValueError:
            payload = None  # not JSON: counts as a service error
    except httpx.HTTPError as error:
        client_error = f"client_{type(error).__name__}"
    elapsed_ms = round((time.monotonic() - started) * 1000)

    fields = read_envelope(payload)
    return Record(
        url=url,
        domain=domain_of(url),
        bucket=classify(http_status, payload, min_words=min_words),
        http_status=http_status,
        status=fields.status if fields else None,
        error_code=(fields.error_code if fields else None) or client_error,
        word_count=fields.word_count if fields else None,
        compliance_decision=fields.compliance_decision if fields else None,
        elapsed_ms=elapsed_ms,
    )


async def run(
    urls: Sequence[str],
    client: httpx.AsyncClient,
    *,
    concurrency: int,
    compliance: str,
    min_words: int,
    sink: TextIO,
    progress: TextIO | None = None,
) -> list[Record]:
    """Extract every URL; write each record to ``sink`` as soon as it is done.

    Each domain has its own queue worked off one request at a time, so a host never sees two of
    our requests at once. All domains share ``concurrency`` slots; the semaphore wakes waiters in
    arrival order, so the domains take turns.
    """
    queues: dict[str, deque[str]] = defaultdict(deque)
    for url in urls:
        queues[domain_of(url)].append(url)

    slots = asyncio.Semaphore(concurrency)
    records: list[Record] = []

    async def work_off(queue: deque[str]) -> None:
        while queue:
            url = queue.popleft()
            async with slots:
                record = await extract_one(client, url, compliance=compliance, min_words=min_words)
            records.append(record)
            sink.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
            sink.flush()
            if progress is not None and len(records) % 25 == 0:
                print(f"{len(records)}/{len(urls)} done", file=progress, flush=True)

    async with asyncio.TaskGroup() as group:
        for queue in queues.values():
            group.create_task(work_off(queue))
    return records


def ok_rate(records: Sequence[Record]) -> float:
    """Percent of all URLs in the ``ok`` bucket; 0 for an empty run."""
    if not records:
        return 0.0
    return 100 * sum(record.bucket == "ok" for record in records) / len(records)


def _percent(count: int, total: int) -> str:
    return f"{100 * count / total:.1f} %" if total else "0.0 %"


def summarize(records: Sequence[Record], *, top_domains: int = TOP_DOMAINS) -> str:
    """The Markdown summary: buckets overall, ok rate, would-be blocks, buckets per domain."""
    total = len(records)
    counts = Counter(record.bucket for record in records)
    lines = [
        "## Live regression summary",
        "",
        f"URLs: {total}",
        f"Ok rate: {ok_rate(records):.1f} % ({counts['ok']} of {total})",
        "Would be blocked under `enforce`: "
        f"{sum(record.compliance_decision == 'reported' for record in records)}",
        "",
        "| Bucket | URLs | Share |",
        "| --- | ---: | ---: |",
    ]
    lines += [
        f"| {bucket} | {counts[bucket]} | {_percent(counts[bucket], total)} |"
        for bucket in BUCKETS
        if counts[bucket]
    ]

    per_domain: dict[str, Counter[str]] = defaultdict(Counter)
    for record in records:
        per_domain[record.domain][record.bucket] += 1
    # Most URLs first; equal counts alphabetically, so the table is stable.
    top = sorted(per_domain.items(), key=lambda item: (-item[1].total(), item[0]))[:top_domains]
    if top:
        shown = [bucket for bucket in BUCKETS if any(c[bucket] for _, c in top)]
        lines += [
            "",
            f"### Top {len(top)} domains by URLs",
            "",
            "| Domain | URLs | " + " | ".join(shown) + " |",
            "| --- | ---: |" + " ---: |" * len(shown),
        ]
        lines += [
            f"| {domain or '(none)'} | {c.total()} | " + " | ".join(str(c[b]) for b in shown) + " |"
            for domain, c in top
        ]
    return "\n".join(lines) + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--api-key", default=os.environ.get("LIVE_API_KEY"))
    parser.add_argument("--urls", type=Path, default=DEFAULT_URLS, help="CSV with a `url` column")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--min-words", type=int, default=50)
    parser.add_argument("--compliance", choices=["enforce", "report", "ignore"], default="report")
    parser.add_argument("--min-ok-rate", type=float, default=None, help="exit 1 below this percent")
    parser.add_argument("--out", type=Path, default=None, help="JSONL file (default: results/...)")
    parser.add_argument(
        "--summary-out", type=Path, default=None, help="also write the summary here"
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_CLIENT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    if args.out is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        args.out = Path("results") / f"live_{stamp}.jsonl"
    return args


async def amain(args: argparse.Namespace) -> int:
    urls = read_urls(args.urls)
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    limits = httpx.Limits(max_connections=args.concurrency)
    with args.out.open("w", encoding="utf-8") as sink:
        # trust_env off: the harness talks to the service only, never through a proxy.
        async with httpx.AsyncClient(
            base_url=args.base_url,
            headers=headers,
            timeout=args.timeout,
            limits=limits,
            trust_env=False,
        ) as client:
            records = await run(
                urls,
                client,
                concurrency=args.concurrency,
                compliance=args.compliance,
                min_words=args.min_words,
                sink=sink,
                progress=sys.stderr,
            )
    summary = summarize(records)
    print(summary)
    if args.summary_out is not None:
        args.summary_out.parent.mkdir(parents=True, exist_ok=True)
        args.summary_out.write_text(summary, encoding="utf-8")
    print(f"Results: {args.out}")
    rate = ok_rate(records)
    if args.min_ok_rate is not None and rate < args.min_ok_rate:
        print(f"FAILED: ok rate {rate:.1f} % is below {args.min_ok_rate:.1f} %", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(amain(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
