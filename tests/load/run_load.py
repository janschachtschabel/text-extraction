"""The load generator: run the load profile (``scenarios.py``) against a running stack, one
scenario after another, while ``docker stats`` is sampled (rulings P11-S1 to P11-S4).

    API_KEY=... uv run python -m tests.load.run_load --base-url http://127.0.0.1:8080 \
        --farm-url http://farm:18000 --out-dir load-results --selector-processes 1

Each scenario runs alone: its requests go out at most ``concurrency`` at once, an answer that
is a job (202 with a ``status_url``: a batch, a crawl, an audio upload, a slow request) is read
every 2 s until it completes, and its window runs from the first request to the last answer
plus the scenario's settle time. Before the first scenario an idle window of 20 s records the
stack at rest (the models a container keeps loaded show there).

The API key comes from the environment variable named by ``--api-key-env`` (``API_KEY``) and is
never printed. ``--internal-urls`` (comma-separated, e.g. ``http://127.0.0.1:9090``) are waited
for (``/ready``) before the run, and their ``/metrics`` are saved after each scenario under
``metrics/``. With ``--fake-llm-url`` the fake LLM's byte counts (``fake_llm.py``) per scenario
go into the summary. ``--selector-processes`` only labels the run.

Writes to ``--out-dir``: ``results.jsonl`` (one line per request), ``stats.jsonl`` (the
``docker stats`` samples), ``summary.md`` (also printed) and ``sizing.json`` (requests and
limits per container, P11-S3). Exits 1 when a request failed or was refused.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, TextIO
from urllib.parse import urlsplit

import httpx

from tests.load import stats
from tests.load.report import ScenarioResult, markdown, percentile, sizing, summarise
from tests.load.scenarios import REPO_ROOT, SCENARIOS, Scenario

Outcome = Literal["success", "partial", "failed", "error"]
OUTCOMES: tuple[Outcome, ...] = ("success", "partial", "failed", "error")

IDLE_SECONDS = 20
POLL_SECONDS = 2.0
# One request is bounded well above EXTRACT_TIMEOUT_SECONDS (25), so a slow answer is recorded
# as the service's; a job (a batch of 1,000 pages, a crawl, a transcription) gets an hour.
REQUEST_TIMEOUT_SECONDS = 120.0
JOB_TIMEOUT_SECONDS = 3600.0
READY_TIMEOUT_SECONDS = 300.0
STATS_INTERVAL_SECONDS = stats.INTERVAL_SECONDS
STATS_COMMAND: Sequence[str] = stats.DOCKER_STATS
MCP_ACCEPT = "application/json, text/event-stream"


@dataclass(frozen=True)
class Record:
    """One line of ``results.jsonl``: one request and its outcome."""

    scenario: str
    index: int
    http_status: int | None
    outcome: Outcome
    error_code: str | None
    latency_ms: int


def _transport() -> httpx.AsyncBaseTransport | None:
    """The API client's transport: the network (None); the tests put a fake service here."""
    return None


def _envelope_error(payload: object) -> str | None:
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def _envelope_outcome(payload: object) -> tuple[Outcome, str | None]:
    status = payload.get("status") if isinstance(payload, dict) else None
    if status in ("success", "partial", "failed"):
        return status, _envelope_error(payload)
    return "error", "no_envelope"


def _job_outcome(job: Mapping[str, Any]) -> Outcome:
    """A completed job by its items: failed when all failed, partial when some failed or are
    partial, else success."""
    progress = job.get("progress") or {}
    total, failed, partial = (int(progress.get(key) or 0) for key in ("total", "failed", "partial"))
    if total and failed == total:
        return "failed"
    return "partial" if failed or partial else "success"


def _mcp_outcome(payload: object) -> tuple[Outcome, str | None]:
    if not isinstance(payload, dict):
        return "error", "no_json_rpc"
    if "error" in payload:
        error = payload["error"]
        code = error.get("code") if isinstance(error, dict) else None
        return "error", f"jsonrpc_{code}"
    result = payload.get("result")
    if isinstance(result, dict) and result.get("isError"):
        return "failed", None
    return "success", None


def _status_url(http_status: int, payload: object) -> str | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    status_url = data.get("status_url") if isinstance(data, dict) else None
    return status_url if http_status == 202 and isinstance(status_url, str) else None


async def _send(
    client: httpx.AsyncClient, scenario: Scenario, body: dict[str, object]
) -> httpx.Response:
    if scenario.kind == "upload":
        fields = dict(body)
        path = REPO_ROOT / str(fields.pop("file"))
        content = await asyncio.to_thread(path.read_bytes)
        files = {"file": (path.name, content, "application/octet-stream")}
        data = {name: str(value) for name, value in fields.items()}
        return await client.post(scenario.endpoint, files=files, data=data)
    if scenario.kind == "mcp":
        return await client.post(scenario.endpoint, json=body, headers={"Accept": MCP_ACCEPT})
    return await client.post(scenario.endpoint, json=body)


async def _follow(
    client: httpx.AsyncClient, status_url: str, poll_seconds: float
) -> tuple[Outcome, str | None]:
    """Read the job until it is ``completed``; its outcome by its items."""
    while True:
        response = await client.get(status_url, params={"limit": 1})
        response.raise_for_status()
        job = response.json().get("data") or {}
        if job.get("state") == "completed":
            return _job_outcome(job), None
        await asyncio.sleep(poll_seconds)


async def request_once(
    client: httpx.AsyncClient,
    scenario: Scenario,
    index: int,
    farm_url: str,
    *,
    poll_seconds: float = POLL_SECONDS,
    job_timeout: float = JOB_TIMEOUT_SECONDS,
) -> Record:
    """One request of a scenario, followed to its end. Never raises: a client failure is the
    record's ``client_<ExceptionName>``, a job past ``job_timeout`` its ``job_timeout``."""
    started = time.monotonic()
    http_status: int | None = None
    outcome: Outcome
    code: str | None
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
            response = await _send(client, scenario, scenario.body(index, farm_url))
        http_status = response.status_code
        try:
            payload: object = response.json()
        except ValueError:
            payload = None
        status_url = _status_url(http_status, payload)
        if scenario.kind == "mcp" and http_status == 200:
            outcome, code = _mcp_outcome(payload)
        elif status_url is not None:
            try:
                async with asyncio.timeout(job_timeout):
                    outcome, code = await _follow(client, status_url, poll_seconds)
            except TimeoutError:
                outcome, code = "error", "job_timeout"
        elif http_status == 200:
            outcome, code = _envelope_outcome(payload)
        else:
            outcome, code = "error", _envelope_error(payload) or f"http_{http_status}"
    except Exception as error:  # one request's failure is its record, not the run's end
        outcome, code = "error", f"client_{type(error).__name__}"
    latency_ms = round((time.monotonic() - started) * 1000)
    return Record(scenario.name, index, http_status, outcome, code, latency_ms)


async def run_scenario(
    client: httpx.AsyncClient,
    scenario: Scenario,
    farm_url: str,
    *,
    sink: TextIO,
    poll_seconds: float = POLL_SECONDS,
    job_timeout: float = JOB_TIMEOUT_SECONDS,
) -> ScenarioResult:
    """All requests of one scenario, at most ``concurrency`` at once, each record written to
    ``sink`` when it is done; then the settle time. The window is in epoch seconds."""
    slots = asyncio.Semaphore(scenario.concurrency)
    records: list[Record] = []

    async def one(index: int) -> None:
        async with slots:
            record = await request_once(
                client,
                scenario,
                index,
                farm_url,
                poll_seconds=poll_seconds,
                job_timeout=job_timeout,
            )
        records.append(record)
        sink.write(json.dumps(asdict(record)) + "\n")
        sink.flush()

    start = time.time()
    async with asyncio.TaskGroup() as group:
        for index in range(scenario.count):
            group.create_task(one(index))
    end = time.time()
    await asyncio.sleep(scenario.settle_seconds)
    settle_end = time.time()

    counts = Counter(record.outcome for record in records)
    latencies = [float(record.latency_ms) for record in records]
    return ScenarioResult(
        name=scenario.name,
        kind=scenario.kind,
        start=start,
        end=end,
        settle_end=settle_end,
        requests=len(records),
        outcomes={outcome: counts[outcome] for outcome in OUTCOMES if counts[outcome]},
        latency_p50_ms=round(percentile(latencies, 50)),
        latency_p95_ms=round(percentile(latencies, 95)),
        latency_max_ms=round(max(latencies)),
        throughput_per_s=round(len(records) / max(end - start, 1e-3), 2),
    )


async def _wait_ready(client: httpx.AsyncClient, urls: Sequence[str]) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    for url in urls:
        while True:
            try:
                if (await client.get(f"{url}/ready")).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{url}/ready did not answer 200 in time")
            await asyncio.sleep(1)


async def _save_metrics(
    client: httpx.AsyncClient, urls: Sequence[str], out_dir: Path, scenario: str
) -> None:
    for url in urls:
        name = urlsplit(url).netloc.replace(":", "_")
        try:
            response = await client.get(f"{url}/metrics")
        except httpx.HTTPError as error:
            print(f"metrics of {url}: {type(error).__name__}", file=sys.stderr)
            continue
        target = out_dir / "metrics" / f"{scenario}.{name}.prom"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(response.text, encoding="utf-8")


async def _llm_counts(client: httpx.AsyncClient, url: str | None) -> dict[str, dict[str, int]]:
    if url is None:
        return {}
    response = await client.get(f"{url.rstrip('/')}/stats")
    response.raise_for_status()
    counts: dict[str, dict[str, int]] = response.json()
    return counts


def _llm_table(usage: Mapping[str, tuple[int, Mapping[str, Mapping[str, int]]]]) -> str:
    """The fake LLM's counts per scenario: per route the calls, bytes, and the tokens per
    scenario request, approximated as UTF-8 bytes / 4 (P11-S6)."""
    lines = [
        "",
        "### LLM calls (fake server)",
        "",
        "Tokens are approximated as UTF-8 bytes / 4: prompt tokens from the request bodies, "
        "answer tokens from the fake's answers (not a real model's).",
        "",
        "| Scenario | Route | Calls | Request bytes | Answer bytes | Prompt tokens per request "
        "| Answer tokens per request |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for scenario, (requests, routes) in usage.items():
        for route, count in routes.items():
            if not count["requests"]:
                continue
            per_request = max(requests, 1) * 4
            lines.append(
                f"| {scenario} | {route} | {count['requests']} | {count['request_bytes']} "
                f"| {count['response_bytes']} | {count['request_bytes'] // per_request} "
                f"| {count['response_bytes'] // per_request} |"
            )
    return "\n".join(lines) + "\n"


def _delta(
    before: Mapping[str, Mapping[str, int]], after: Mapping[str, Mapping[str, int]]
) -> dict[str, dict[str, int]]:
    return {
        route: {key: value - before.get(route, {}).get(key, 0) for key, value in counts.items()}
        for route, counts in after.items()
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument(
        "--internal-urls",
        default="",
        help="comma-separated internal base URLs (port 9090): /ready before, /metrics after",
    )
    parser.add_argument("--farm-url", required=True, help="the site farm's base URL")
    parser.add_argument(
        "--api-key-env", default="API_KEY", help="the environment variable with the API key"
    )
    parser.add_argument(
        "--only", action="append", default=None, metavar="NAME", help="run only this scenario"
    )
    parser.add_argument("--out-dir", type=Path, default=Path("load-results"))
    parser.add_argument(
        "--selector-processes",
        type=int,
        default=None,
        metavar="N",
        help="the stack's STRUCTURED_SELECTOR_PROCESSES, as a label of the run",
    )
    parser.add_argument("--fake-llm-url", default=None, help="the fake LLM's base URL")
    parser.add_argument("--poll-seconds", type=float, default=POLL_SECONDS)
    parser.add_argument("--job-timeout", type=float, default=JOB_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    known = {scenario.name for scenario in SCENARIOS}
    unknown = sorted(set(args.only or []) - known)
    if unknown:
        parser.error(f"unknown scenario: {', '.join(unknown)} (known: {', '.join(sorted(known))})")
    if not os.environ.get(args.api_key_env):
        parser.error(f"the environment variable {args.api_key_env} with the API key is not set")
    args.internal_urls = [
        url.strip().rstrip("/") for url in args.internal_urls.split(",") if url.strip()
    ]
    return args


async def amain(args: argparse.Namespace) -> int:
    plan = [scenario for scenario in SCENARIOS if not args.only or scenario.name in args.only]
    out_dir: Path = args.out_dir
    headers = {"Authorization": f"Bearer {os.environ[args.api_key_env]}"}
    limits = httpx.Limits(max_connections=max(scenario.concurrency for scenario in plan) + 5)
    windows: dict[str, tuple[float, float]] = {}
    results: list[ScenarioResult] = []
    llm_usage: dict[str, tuple[int, dict[str, dict[str, int]]]] = {}
    # trust_env off: the generator talks to the stack only, never through a proxy.
    async with (
        httpx.AsyncClient(
            base_url=args.base_url,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
            limits=limits,
            trust_env=False,
            transport=_transport(),
        ) as client,
        httpx.AsyncClient(timeout=30, trust_env=False) as side,
    ):
        await _wait_ready(side, args.internal_urls)
        with (out_dir / "results.jsonl").open("w", encoding="utf-8") as sink:
            async with stats.Sampler(
                out_dir / "stats.jsonl", interval=STATS_INTERVAL_SECONDS, command=STATS_COMMAND
            ) as sampler:
                idle_start = time.time()
                await asyncio.sleep(IDLE_SECONDS)
                windows["idle"] = (idle_start, time.time())
                for scenario in plan:
                    print(f"{scenario.name}: {scenario.count} requests", file=sys.stderr)
                    before = await _llm_counts(side, args.fake_llm_url)
                    result = await run_scenario(
                        client,
                        scenario,
                        args.farm_url,
                        sink=sink,
                        poll_seconds=args.poll_seconds,
                        job_timeout=args.job_timeout,
                    )
                    windows[scenario.name] = (result.start, result.settle_end)
                    results.append(result)
                    if args.fake_llm_url is not None:
                        after = await _llm_counts(side, args.fake_llm_url)
                        llm_usage[scenario.name] = (result.requests, _delta(before, after))
                    await _save_metrics(side, args.internal_urls, out_dir, scenario.name)
                samples = list(sampler.samples)

    rows = summarise(samples, windows)
    label = args.selector_processes
    heading = "Load test" + (f", {label} selector processes" if label is not None else "")
    summary = markdown(rows, results, heading=heading)
    if llm_usage:
        summary += _llm_table(llm_usage)
    (out_dir / "summary.md").write_text(summary, encoding="utf-8")
    resources = sizing(rows)
    report = {
        "selector_processes": label,
        "windows": windows,
        "containers": {name: asdict(value) for name, value in resources.items()},
    }
    (out_dir / "sizing.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(summary)
    bad = {
        result.name: result.outcomes
        for result in results
        if result.outcomes.get("failed") or result.outcomes.get("error")
    }
    if bad:
        print(f"FAILED: requests failed or were refused in {', '.join(bad)}", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
