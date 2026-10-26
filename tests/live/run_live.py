"""Live regression harness: send a list of real URLs to a running service and bucket the answers.

    uv run python -m tests.live.run_live --urls tests/live/urls/wlo_200.csv --mode auto \
        --min-ok-rate 80
    uv run python -m tests.live.run_live --urls tests/live/urls/media.csv
    uv run python -m tests.live.run_live --upload tests/media/corpus/speech_de.ogg

Writes one JSON line per URL and prints a Markdown summary. A row of a list may ask for
``transcribe`` (the media set); an answer that is a job (202, a transcription) is followed until
the job completes, and its item's result is what counts. See `tests/live/README.md`.
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
# Domains named per error code in the summary.
TOP_CODE_DOMAINS = 3
# The warning of an ``auto`` answer whose browser worker did not answer in time.
BROWSER_UNAVAILABLE = "browser_unavailable"
MODES = ("fast", "auto", "browser")
# The mode of the gate: the service decides per URL, as it does for callers who leave it out.
DEFAULT_MODE = "auto"
# A transcription is a job (202): the harness reads the job every few seconds until it is done,
# for at most an hour (an hour of audio at the spike's real-time factor of 0.88, and a queue).
DEFAULT_JOB_TIMEOUT_SECONDS = 3600.0
DEFAULT_POLL_SECONDS = 5.0
# The transcript sources of `data.media`, in the order of the media table.
TRANSCRIPT_SOURCES = ("manual_subtitles", "auto_subtitles", "asr", "metadata_only")
SUBTITLES = frozenset({"manual_subtitles", "auto_subtitles"})
_TRUE = frozenset({"true", "yes", "1"})
_FALSE = frozenset({"", "false", "no", "0"})


@dataclass(frozen=True)
class Row:
    """One row of a URL list: the URL and, in the media set, whether to send ``transcribe``,
    the platform and the transcript source it should get. An upload row has the file instead
    of a URL (``url`` is ``upload:<file name>``)."""

    url: str
    transcribe: bool = False
    platform: str | None = None
    expected_source: str | None = None
    upload: Path | None = None

    @classmethod
    def for_upload(cls, path: Path) -> "Row":
        """An audio or video file for ``POST /v1/extract/file``: always transcribed."""
        return cls(f"upload:{path.name}", platform="upload", expected_source="asr", upload=path)


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
    mode_used: str | None  # "fast" or "browser": what produced the answer; None without data
    warnings: list[str]  # data.warnings (for example browser_unavailable); empty without data
    elapsed_ms: int
    # The media fields: data.media.platform (else the row's platform), data.media.
    # transcript_source, the row's expected source and whether the request said transcribe.
    platform: str | None = None
    transcript_source: str | None = None
    expected_source: str | None = None
    transcribe: bool = False


def read_urls(path: Path) -> list[str]:
    """The ``url`` column of a CSV file with a header row; order and duplicates are kept.

    A row without a URL (too short, or the field is empty) is skipped with a warning on stderr.
    """
    urls: list[str] = []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "url" not in reader.fieldnames:
            raise ValueError(f"{path}: the header row needs a `url` column")
        for row in reader:
            url = (row.get("url") or "").strip()
            if url:
                urls.append(url)
            else:
                print(f"{path}: line {reader.line_num} has no url, skipped", file=sys.stderr)
    return urls


def read_rows(path: Path) -> list[Row]:
    """The rows of a CSV file with a header row: the ``url`` column (as ``read_urls``) and, where
    the file has them, ``transcribe`` (``true``/``false``, empty is false), ``platform`` and
    ``expected_source``."""
    rows: list[Row] = []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "url" not in reader.fieldnames:
            raise ValueError(f"{path}: the header row needs a `url` column")
        for row in reader:
            url = (row.get("url") or "").strip()
            if not url:
                print(f"{path}: line {reader.line_num} has no url, skipped", file=sys.stderr)
                continue
            flag = (row.get("transcribe") or "").strip().lower()
            if flag not in _TRUE | _FALSE:
                raise ValueError(f"{path}: line {reader.line_num}: transcribe is true or false")
            rows.append(
                Row(
                    url,
                    transcribe=flag in _TRUE,
                    platform=(row.get("platform") or "").strip() or None,
                    expected_source=(row.get("expected_source") or "").strip() or None,
                )
            )
    return rows


def domain_of(url: str) -> str:
    """The lower-case host of a URL; empty when it has none."""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


async def extract_one(
    client: httpx.AsyncClient,
    url: str | Row,
    *,
    compliance: str,
    min_words: int,
    mode: str = DEFAULT_MODE,
    deadline_seconds: float = DEFAULT_CLIENT_TIMEOUT_SECONDS,
    transcribe: bool = True,
    job_timeout_seconds: float = DEFAULT_JOB_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> Record:
    """Ask the service for one URL (or upload one file) and bucket the answer.

    ``deadline_seconds`` bounds the request (httpx's own timeouts are per phase, so a server
    that trickles bytes could hold one forever). A row that asks for ``transcribe`` sends it,
    unless ``transcribe`` is off (a service without ASR answers it 400). An answer that is a
    job (202 with a ``status_url``) is read every ``poll_seconds`` until it completes, within
    ``job_timeout_seconds``; its first item's result is the answer. Any failure on the client
    side is recorded as ``client_<ExceptionName>``: one URL must not end the run.
    """
    row = url if isinstance(url, Row) else Row(url)
    sends_transcribe = row.transcribe and transcribe and row.upload is None
    started = time.monotonic()
    http_status: int | None = None
    payload: object = None
    client_error: str | None = None
    try:
        async with asyncio.timeout(deadline_seconds):
            response = await _send(client, row, sends_transcribe, mode, compliance)
        http_status = response.status_code
        try:
            payload = response.json()
        except ValueError:
            payload = None  # not JSON: counts as a service error
        status_url = _status_url(http_status, payload)
        if status_url is not None:
            payload = None  # the job's acceptance is no result
            async with asyncio.timeout(job_timeout_seconds):
                payload = await _job_result(client, status_url, poll_seconds)
            http_status = 200  # the item's result, as GET /v1/jobs/{id} answered it
    except Exception as error:  # any client failure is this URL's record, not the run's end
        client_error = f"client_{type(error).__name__}"
    elapsed_ms = round((time.monotonic() - started) * 1000)

    fields = read_envelope(payload)
    media = _media(payload)
    platform = media.get("platform")
    source = media.get("transcript_source")
    return Record(
        url=row.url,
        domain=domain_of(row.url),
        bucket=classify(http_status, payload, min_words=min_words),
        http_status=http_status,
        status=fields.status if fields else None,
        error_code=(fields.error_code if fields else None) or client_error,
        word_count=fields.word_count if fields else None,
        compliance_decision=fields.compliance_decision if fields else None,
        mode_used=fields.mode_used if fields else None,
        warnings=_warnings(payload),
        elapsed_ms=elapsed_ms,
        platform=platform if isinstance(platform, str) else row.platform,
        transcript_source=source if isinstance(source, str) else None,
        expected_source=row.expected_source,
        transcribe=sends_transcribe or row.upload is not None,
    )


async def _send(
    client: httpx.AsyncClient, row: Row, transcribe: bool, mode: str, compliance: str
) -> httpx.Response:
    """``POST /v1/extract`` of the row's URL, or ``POST /v1/extract/file`` of its file."""
    if row.upload is not None:
        content = await asyncio.to_thread(row.upload.read_bytes)
        files = {"file": (row.upload.name, content, "application/octet-stream")}
        return await client.post("/v1/extract/file", files=files, data={"format": "text"})
    body: dict[str, object] = {
        "url": row.url,
        "mode": mode,
        "format": "text",
        "cache": False,
        "compliance": compliance,
    }
    if transcribe:
        body["transcribe"] = True
    return await client.post("/v1/extract", json=body)


def _status_url(http_status: int, payload: object) -> str | None:
    """The job's ``status_url`` of a 202 answer; None for any other answer."""
    data = payload.get("data") if isinstance(payload, dict) else None
    status_url = data.get("status_url") if isinstance(data, dict) else None
    return status_url if http_status == 202 and isinstance(status_url, str) else None


async def _job_result(client: httpx.AsyncClient, status_url: str, poll_seconds: float) -> object:
    """The first item's result envelope once the job is ``completed``."""
    while True:
        response = await client.get(status_url)
        response.raise_for_status()
        data = response.json().get("data") or {}
        if data.get("state") == "completed":
            items = data.get("items") or [{}]
            return items[0].get("result")
        await asyncio.sleep(poll_seconds)


def _media(payload: object) -> dict[str, object]:
    """``data.media`` of an answer; empty when it has none."""
    data = payload.get("data") if isinstance(payload, dict) else None
    media = data.get("media") if isinstance(data, dict) else None
    return media if isinstance(media, dict) else {}


def _warnings(payload: object) -> list[str]:
    """The string entries of ``data.warnings``; empty when the answer has none."""
    data = payload.get("data") if isinstance(payload, dict) else None
    warnings = data.get("warnings") if isinstance(data, dict) else None
    if not isinstance(warnings, list):
        return []
    return [warning for warning in warnings if isinstance(warning, str)]


async def run(
    urls: Sequence[str | Row],
    client: httpx.AsyncClient,
    *,
    concurrency: int,
    compliance: str,
    min_words: int,
    sink: TextIO,
    progress: TextIO | None = None,
    mode: str = DEFAULT_MODE,
    deadline_seconds: float = DEFAULT_CLIENT_TIMEOUT_SECONDS,
    transcribe: bool = True,
    job_timeout_seconds: float = DEFAULT_JOB_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> list[Record]:
    """Extract every URL (a plain URL or a ``Row``); write each record to ``sink`` as soon as it
    is done.

    Each domain has its own queue worked off one request at a time, so a host never sees two of
    our requests at once (a transcription job holds its domain's turn until it completes). All
    domains share ``concurrency`` slots; the semaphore wakes waiters in arrival order, so the
    domains take turns.
    """
    queues: dict[str, deque[str | Row]] = defaultdict(deque)
    for url in urls:
        queues[domain_of(url.url if isinstance(url, Row) else url)].append(url)

    slots = asyncio.Semaphore(concurrency)
    records: list[Record] = []

    async def work_off(queue: deque[str | Row]) -> None:
        while queue:
            url = queue.popleft()
            async with slots:
                record = await extract_one(
                    client,
                    url,
                    compliance=compliance,
                    min_words=min_words,
                    mode=mode,
                    deadline_seconds=deadline_seconds,
                    transcribe=transcribe,
                    job_timeout_seconds=job_timeout_seconds,
                    poll_seconds=poll_seconds,
                )
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
    """The Markdown summary: buckets overall, ok rate, would-be blocks, answers whose browser
    worker did not answer in time, results per mode that produced them, buckets per domain, the
    transcript sources per platform (a list with expected sources: the media set), the error
    codes of the answers and every URL that is not ok."""
    total = len(records)
    counts = Counter(record.bucket for record in records)
    lines = [
        "## Live regression summary",
        "",
        f"URLs: {total}",
        f"Ok rate: {ok_rate(records):.1f} % ({counts['ok']} of {total})",
        "Would be blocked under `enforce`: "
        f"{sum(record.compliance_decision == 'reported' for record in records)}",
        f"Answers with the warning `{BROWSER_UNAVAILABLE}`: "
        f"{sum(BROWSER_UNAVAILABLE in record.warnings for record in records)}",
        "",
        "| Bucket | URLs | Share |",
        "| --- | ---: | ---: |",
    ]
    lines += [
        f"| {bucket} | {counts[bucket]} | {_percent(counts[bucket], total)} |"
        for bucket in BUCKETS
        if counts[bucket]
    ]

    modes = Counter(record.mode_used for record in records)
    if modes:
        lines += [
            "",
            "| Mode used | URLs | Ok |",
            "| --- | ---: | ---: |",
        ]
        # Answers without data (no mode) come last.
        for used in sorted(modes, key=lambda name: (name is None, name or "")):
            ok = sum(r.bucket == "ok" for r in records if r.mode_used == used)
            lines.append(f"| {used or '(none)'} | {modes[used]} | {ok} |")

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
    lines += _media_table(records)
    lines += _error_codes(records)
    lines += _not_ok(records)
    return "\n".join(lines) + "\n"


def as_expected(record: Record) -> bool:
    """Whether a row got the transcript source it expected; a row that expects subtitles takes
    either kind (a platform moves a track between manual and automatic captions)."""
    expected, got = record.expected_source, record.transcript_source
    if expected in SUBTITLES:
        return got in SUBTITLES
    return expected is not None and got == expected


def _media_table(records: Sequence[Record]) -> list[str]:
    """The transcript sources per platform, with how many rows got what they expected: only for
    a list that names expected sources (the media set). Reported, never a gate; the URLs are in
    the JSONL and, when not ok, in the table of URLs not ok."""
    expecting = [r for r in records if r.expected_source is not None]
    if not expecting:
        return []
    per_platform: dict[str | None, list[Record]] = defaultdict(list)
    for record in records:
        per_platform[record.platform].append(record)
    lines = [
        "",
        "### Media",
        "",
        f"Media rows as expected: {sum(map(as_expected, expecting))} of {len(expecting)}",
        "",
        "| Platform | URLs | " + " | ".join(TRANSCRIPT_SOURCES) + " | none | As expected |",
        "| --- | ---: |" + " ---: |" * (len(TRANSCRIPT_SOURCES) + 2),
    ]
    for platform in sorted(per_platform, key=lambda name: (name is None, name or "")):
        group = per_platform[platform]
        counts = Counter(r.transcript_source for r in group)
        wanted = [r for r in group if r.expected_source is not None]
        cells = [str(counts[source]) for source in TRANSCRIPT_SOURCES] + [str(counts[None])]
        lines.append(
            f"| {platform or '(none)'} | {len(group)} | "
            + " | ".join(cells)
            + f" | {sum(map(as_expected, wanted))} of {len(wanted)} |"
        )
    return lines


def _not_ok(records: Sequence[Record]) -> list[str]:
    """Every URL whose bucket is not ``ok``, by domain and URL: two runs can be compared URL by
    URL, which the domain table cannot show."""
    failed = sorted((r for r in records if r.bucket != "ok"), key=lambda r: (r.domain, r.url))
    if not failed:
        return []
    lines = ["", "### URLs not ok", "", "| URL | Bucket | Code | Mode | Words |"]
    lines.append("| --- | --- | --- | --- | ---: |")
    lines += [
        f"| {r.url} | {r.bucket} | {r.error_code or '-'} | {r.mode_used or '-'} | "
        f"{'-' if r.word_count is None else r.word_count} |"
        for r in failed
    ]
    return lines


def _error_codes(records: Sequence[Record]) -> list[str]:
    """The ``error.code`` of failed and partial answers and of every ``connection_error``, most
    frequent first, each with the domains that gave it most: the per-cause view of a run."""
    per_code: dict[str, Counter[str]] = defaultdict(Counter)
    for record in records:
        answered = record.status in ("failed", "partial") or record.bucket == "connection_error"
        if answered and record.status is not None and record.error_code is not None:
            per_code[record.error_code][record.domain] += 1
    if not per_code:
        return []
    lines = ["", "### Error codes", "", "| Code | URLs | Top domains |", "| --- | ---: | --- |"]
    for code, domains in sorted(per_code.items(), key=lambda item: (-item[1].total(), item[0])):
        top = sorted(domains.items(), key=lambda item: (-item[1], item[0]))[:TOP_CODE_DOMAINS]
        named = ", ".join(f"{domain or '(none)'} {count}" for domain, count in top)
        lines.append(f"| {code} | {domains.total()} | {named} |")
    return lines


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--api-key", default=os.environ.get("LIVE_API_KEY"))
    parser.add_argument(
        "--urls",
        type=Path,
        default=None,
        help=f"CSV with a `url` column (default: {DEFAULT_URLS.name}, none with --upload)",
    )
    parser.add_argument(
        "--upload",
        type=Path,
        action="append",
        default=None,
        help="an audio or video file to upload and transcribe (repeatable)",
    )
    parser.add_argument(
        "--no-transcribe",
        dest="transcribe",
        action="store_false",
        help="never send `transcribe`, whatever a row says (a service without ASR answers 400)",
    )
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--min-words", type=int, default=50)
    parser.add_argument(
        "--mode",
        choices=MODES,
        default=DEFAULT_MODE,
        help="the request's mode: fast, auto (default) or browser",
    )
    parser.add_argument("--compliance", choices=["enforce", "report", "ignore"], default="report")
    parser.add_argument("--min-ok-rate", type=float, default=None, help="exit 1 below this percent")
    parser.add_argument("--out", type=Path, default=None, help="JSONL file (default: results/...)")
    parser.add_argument(
        "--summary-out", type=Path, default=None, help="also write the summary here"
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_CLIENT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--job-timeout",
        type=float,
        default=DEFAULT_JOB_TIMEOUT_SECONDS,
        help="seconds to wait for a job (a transcription) to complete",
    )
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    args = parser.parse_args(argv)
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    args.upload = args.upload or []
    if args.urls is None and not args.upload:
        args.urls = DEFAULT_URLS
    if args.out is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        args.out = Path("results") / f"live_{stamp}.jsonl"
    return args


async def amain(args: argparse.Namespace) -> int:
    rows = read_rows(args.urls) if args.urls is not None else []
    rows += [Row.for_upload(path) for path in args.upload]
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
                rows,
                client,
                concurrency=args.concurrency,
                compliance=args.compliance,
                min_words=args.min_words,
                mode=args.mode,
                sink=sink,
                progress=sys.stderr,
                deadline_seconds=args.timeout,
                transcribe=args.transcribe,
                job_timeout_seconds=args.job_timeout,
                poll_seconds=args.poll_seconds,
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
