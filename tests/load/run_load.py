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
go into the summary. ``--selector-processes``, ``--asr-model`` and ``--pii-model-size`` (the
stack's ``STRUCTURED_SELECTOR_PROCESSES``, ``ASR_MODEL`` and ``PII_MODEL_SIZE``) only label the
run: they go into the summary's heading and into ``sizing.json``.

Writes to ``--out-dir``: ``results.jsonl`` (one line per request), ``stats.jsonl`` (the
``docker stats`` samples), ``summary.md`` (also printed) and ``sizing.json`` (the requests and
limits the measurement gives each compose service, without the farm, the fake LLM and Valkey:
the memory request from the highest memory p95, the CPU request from the highest CPU mean, the
limits from 1.5 x the peaks; P11-S3 as read by R-P11-10). ``sizing.json`` is not the chart's
sizing by itself: the R-P11-7 memory floors (a limit never below the value that backs a child
process's address-space budget), Valkey's own sizing (R-P11-11) and the llm worker's 1Gi floor
(R-P11-12) apply on top and are not computed here; configuration.md, "Resource baselines", gives
the whole rule and the values that follow from it. The summary and the sizing are written even
when the run stops on an error. Exits 1 when a request's
outcome is not one its scenario expects (an error never is).

``--farm-url`` must be local: a loopback or private IP address, or a single-label host name
such as the compose service ``farm``. Anything else is refused before a request goes out.

The site structure items (ruling P11-S7) go into the summary too:

- **Site crawls**: per crawl its items (the job's ``total``), its duration (the job's
  ``finished_at - created_at``, and the request's latency, read every ``--poll-seconds``) and
  the Valkey bytes of its keys. With
  ``--valkey-cli`` (a command line, e.g. ``docker compose ... exec -T valkey valkey-cli``,
  which authenticates from the container's ``REDISCLI_AUTH``) the generator lists the crawl's
  keys (``job:{<id>}:*``) once it completed, when it holds every result, and sums their
  ``MEMORY USAGE ... SAMPLES 0``. A failed read leaves the bytes unknown.
- **Event-loop lag**: ``text_extraction_event_loop_lag_seconds`` (the extract worker's
  histogram) per scenario and internal URL, from the ``/metrics`` read before and after it:
  samples, mean, total, samples over 100 ms and the bound of the highest one. It is unknown
  (``-``) when the read before failed, no sample was taken or the counts went backwards.
- A ``MEMORY USAGE`` answer that is no number (an error) leaves a crawl's Valkey bytes unknown.
"""

import argparse
import asyncio
import ipaddress
import json
import math
import os
import shlex
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TextIO
from urllib.parse import urlsplit

import httpx
from prometheus_client.parser import text_string_to_metric_families

from tests.load import stats
from tests.load.fake_llm import BYTES_PER_TOKEN
from tests.load.report import ScenarioResult, markdown, percentile, sizing, summarise
from tests.load.scenarios import SCENARIOS, Scenario, upload_bytes

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
LAG_METRIC = "text_extraction_event_loop_lag_seconds"
VALKEY_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class Record:
    """One line of ``results.jsonl``: one request and its outcome."""

    scenario: str
    index: int
    http_status: int | None
    outcome: Outcome
    error_code: str | None
    latency_ms: int
    expected: bool  # the outcome is one the scenario expects
    job_id: str | None = None  # an answer that was a job
    items: int | None = None  # the completed job's items (``progress.total``)
    valkey_bytes: int | None = None  # a crawl's keys at completion (``--valkey-cli``)
    job_seconds: float | None = None  # the job's ``finished_at - created_at``


@dataclass(frozen=True)
class Lag:
    """The event-loop lag samples of one scenario window: how many, their mean, how many were
    over 100 ms, the bucket bound of the highest (``inf``: above the last bound) and their sum.
    A sample is how much later than asked the sampler woke from its 0.5 s sleep, so the sum is
    the delay the samples saw: a lower bound of the time the loop was held, because a block
    longer than one interval is counted up to 0.5 s short."""

    samples: int
    mean_ms: int
    over_100ms: int
    max_bound_s: float
    total_s: float


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


def _job_id(payload: object) -> str | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    job_id = data.get("id") if isinstance(data, dict) else None
    return job_id if isinstance(job_id, str) else None


def valkey_job_bytes(command: Sequence[str], job_id: str) -> int:
    """The ``MEMORY USAGE`` (every element counted: ``SAMPLES 0``) summed over the keys of the
    job, through ``command`` (a ``valkey-cli`` that reaches the stack's Valkey). Raises
    ``subprocess.CalledProcessError`` or ``subprocess.TimeoutExpired`` when it fails."""
    scan = subprocess.run(  # noqa: S603 - the operator's command line, no shell
        [*command, "--scan", "--pattern", f"job:{{{job_id}}}:*"],
        capture_output=True,
        text=True,
        check=True,
        timeout=VALKEY_TIMEOUT_SECONDS,
    )
    keys = [key for key in scan.stdout.split() if key]
    if not keys:
        return 0
    usage = subprocess.run(  # noqa: S603 - the operator's command line, no shell
        list(command),
        input="".join(f"MEMORY USAGE {key} SAMPLES 0\n" for key in keys),
        capture_output=True,
        text=True,
        check=True,
        timeout=VALKEY_TIMEOUT_SECONDS,
    )
    # One answer a line: a number (``(integer) N`` on a terminal), or nothing (``(nil)``) for a
    # key gone. Anything else is an error answer (an ACL denial, say): the sum would be low.
    total = 0
    for line in usage.stdout.splitlines():
        answer = line.strip().removeprefix("(integer)").strip()
        if answer.isdigit():
            total += int(answer)
        elif answer not in ("", "(nil)"):
            raise ValueError("a MEMORY USAGE answer is no number")
    return total


async def _valkey_bytes(command: Sequence[str] | None, job_id: str | None) -> int | None:
    if command is None or job_id is None:
        return None
    try:
        return await asyncio.to_thread(valkey_job_bytes, command, job_id)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        # The command line and its output hold no secret; the error's type is enough.
        print(f"valkey bytes of a crawl: {type(error).__name__}", file=sys.stderr)
        return None


async def _send(
    client: httpx.AsyncClient, scenario: Scenario, body: dict[str, object]
) -> httpx.Response:
    if scenario.kind == "upload":
        fields = dict(body)
        file = str(fields.pop("file"))
        content = await asyncio.to_thread(upload_bytes, file)
        files = {"file": (PurePosixPath(file).name, content, "application/octet-stream")}
        data = {name: str(value) for name, value in fields.items()}
        return await client.post(scenario.endpoint, files=files, data=data)
    if scenario.kind == "mcp":
        return await client.post(scenario.endpoint, json=body, headers={"Accept": MCP_ACCEPT})
    return await client.post(scenario.endpoint, json=body)


def _job_seconds(job: Mapping[str, Any]) -> float | None:
    """The job's ``finished_at - created_at`` in seconds: the service's own times."""
    try:
        created = datetime.fromisoformat(job["created_at"])
        finished = datetime.fromisoformat(job["finished_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return round((finished - created).total_seconds(), 3)


async def _follow(
    client: httpx.AsyncClient, status_url: str, poll_seconds: float
) -> tuple[Outcome, int | None, float | None]:
    """Read the job until it is ``completed``; its outcome by its items, how many, and how
    long it took."""
    while True:
        response = await client.get(status_url, params={"limit": 1})
        response.raise_for_status()
        job = response.json().get("data") or {}
        if job.get("state") == "completed":
            total = (job.get("progress") or {}).get("total")
            items = total if isinstance(total, int) else None
            return _job_outcome(job), items, _job_seconds(job)
        await asyncio.sleep(poll_seconds)


async def request_once(
    client: httpx.AsyncClient,
    scenario: Scenario,
    index: int,
    farm_url: str,
    *,
    poll_seconds: float = POLL_SECONDS,
    job_timeout: float = JOB_TIMEOUT_SECONDS,
    valkey_cli: Sequence[str] | None = None,
) -> Record:
    """One request of a scenario, followed to its end. Never raises: a client failure is the
    record's ``client_<ExceptionName>``, a job past ``job_timeout`` its ``job_timeout``. A
    completed crawl's keys are measured through ``valkey_cli`` (after its latency is taken)."""
    started = time.monotonic()
    http_status: int | None = None
    outcome: Outcome
    code: str | None
    job_id: str | None = None
    items: int | None = None
    job_seconds: float | None = None
    completed = False
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
            job_id, code = _job_id(payload), None
            try:
                async with asyncio.timeout(job_timeout):
                    outcome, items, job_seconds = await _follow(client, status_url, poll_seconds)
                completed = True
            except TimeoutError:
                outcome, code = "error", "job_timeout"
        elif http_status == 200:
            outcome, code = _envelope_outcome(payload)
        else:
            outcome, code = "error", _envelope_error(payload) or f"http_{http_status}"
    except Exception as error:  # one request's failure is its record, not the run's end
        outcome, code = "error", f"client_{type(error).__name__}"
    latency_ms = round((time.monotonic() - started) * 1000)
    expected = outcome in scenario.expected
    crawl = scenario.kind == "site" and completed
    valkey_bytes = await _valkey_bytes(valkey_cli if crawl else None, job_id)
    return Record(
        scenario.name,
        index,
        http_status,
        outcome,
        code,
        latency_ms,
        expected,
        job_id,
        items,
        valkey_bytes,
        job_seconds,
    )


async def run_scenario(
    client: httpx.AsyncClient,
    scenario: Scenario,
    farm_url: str,
    *,
    sink: TextIO,
    poll_seconds: float = POLL_SECONDS,
    job_timeout: float = JOB_TIMEOUT_SECONDS,
    valkey_cli: Sequence[str] | None = None,
    collected: list[Record] | None = None,
) -> ScenarioResult:
    """All requests of one scenario, at most ``concurrency`` at once, each record written to
    ``sink`` (and added to ``collected``) when it is done; then the settle time. The window is
    in epoch seconds."""
    slots = asyncio.Semaphore(scenario.concurrency)
    records: list[Record] = [] if collected is None else collected
    first = len(records)

    async def one(index: int) -> None:
        async with slots:
            record = await request_once(
                client,
                scenario,
                index,
                farm_url,
                poll_seconds=poll_seconds,
                job_timeout=job_timeout,
                valkey_cli=valkey_cli,
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

    records = records[first:]
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
        unexpected=sum(not record.expected for record in records),
    )


async def _idle() -> ScenarioResult:
    """The window of the stack at rest, before any load: what the containers hold idle (the
    models they keep loaded, for instance)."""
    start = time.time()
    await asyncio.sleep(IDLE_SECONDS)
    end = time.time()
    return ScenarioResult(
        name="idle",
        kind="idle",
        start=start,
        end=end,
        settle_end=end,
        requests=0,
        outcomes={},
        latency_p50_ms=0,
        latency_p95_ms=0,
        latency_max_ms=0,
        throughput_per_s=0.0,
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
) -> dict[str, str]:
    """Save each URL's ``/metrics`` under ``metrics/``; the texts by netloc."""
    texts: dict[str, str] = {}
    for url in urls:
        netloc = urlsplit(url).netloc
        try:
            response = await client.get(f"{url}/metrics")
        except httpx.HTTPError as error:
            print(f"metrics of {url}: {type(error).__name__}", file=sys.stderr)
            continue
        target = out_dir / "metrics" / f"{scenario}.{netloc.replace(':', '_')}.prom"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(response.text, encoding="utf-8")
        texts[netloc] = response.text
    return texts


def _histogram(text: str, name: str) -> tuple[dict[float, float], float] | None:
    """The cumulative bucket counts (by upper bound) and the sum of histogram ``name``."""
    for family in text_string_to_metric_families(text):
        if family.name != name or family.type != "histogram":
            continue
        buckets: dict[float, float] = {}
        total = 0.0
        for sample in family.samples:
            if sample.name == f"{name}_bucket":
                buckets[float(sample.labels["le"])] = sample.value
            elif sample.name == f"{name}_sum":
                total = sample.value
        return buckets, total
    return None


def exports_lag(text: str) -> bool:
    """Whether a ``/metrics`` text has the lag histogram."""
    return _histogram(text, LAG_METRIC) is not None


def lag_between(before: str, after: str) -> Lag | None:
    """The lag samples taken between two ``/metrics`` texts (``before`` without the histogram:
    from zero). None when ``after`` has no lag histogram, when no sample was taken, and when a
    count went backwards (the process restarted between the reads)."""
    later = _histogram(after, LAG_METRIC)
    if later is None:
        return None
    earlier = _histogram(before, LAG_METRIC) or ({}, 0.0)
    buckets = {
        bound: count - earlier[0].get(bound, 0.0) for bound, count in sorted(later[0].items())
    }
    total = later[1] - earlier[1]
    if any(count < 0 for count in buckets.values()) or total < 0:
        return None
    samples = round(buckets.get(math.inf, 0.0))
    if not samples:
        return None
    over = samples - round(buckets.get(0.1, samples))
    bound = next((bound for bound, count in buckets.items() if round(count) >= samples), math.inf)
    return Lag(
        samples=samples,
        mean_ms=round(total * 1000 / samples),
        over_100ms=over,
        max_bound_s=bound,
        total_s=round(total, 3),
    )


def scenario_lags(before: Mapping[str, str], after: Mapping[str, str]) -> dict[str, Lag | None]:
    """A scenario's lag per internal URL whose read after it has the histogram, from the
    reads before and after the window (by netloc). None (unknown) when the read before
    failed, as ``lag_between`` gives it otherwise."""
    return {
        url: lag_between(before[url], text) if url in before else None
        for url, text in after.items()
        if exports_lag(text)
    }


def _site_table(records: Sequence[Record], crawls: Collection[str]) -> str:
    """The table of the jobs of the ``crawls`` scenarios."""
    rows = [record for record in records if record.scenario in crawls and record.job_id]
    if not rows:
        return ""
    lines = [
        "",
        "### Site crawls",
        "",
        "Job s: the job's finished_at - created_at (compare with JOB_TTL_SECONDS, which "
        "nothing renews mid-crawl). Request s: from the request to the poll that saw the job "
        "completed (polls every 2 s). Valkey: MEMORY USAGE of the job's keys at completion; - "
        "when not read.",
        "",
        "| Scenario | Items | Job s | Request s | Valkey bytes | Valkey MiB |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for record in rows:
        size = record.valkey_bytes
        mib = "-" if size is None else f"{size / 2**20:.1f}"
        seconds = "-" if record.job_seconds is None else f"{record.job_seconds:.1f}"
        items = "-" if record.items is None else record.items
        lines.append(
            f"| {record.scenario} | {items} | {seconds} | {record.latency_ms / 1000:.1f} "
            f"| {'-' if size is None else size} | {mib} |"
        )
    return "\n".join(lines) + "\n"


def _lag_table(lags: Mapping[str, Mapping[str, Lag | None]]) -> str:
    rows = [
        (scenario, url, lag) for scenario, by_url in lags.items() for url, lag in by_url.items()
    ]
    if not rows:
        return ""
    lines = [
        "",
        "### Event-loop lag",
        "",
        f"{LAG_METRIC}, sampled every 0.5 s, in the scenario's window (the reads before and "
        "after it). Total: the sum of the samples, the delay the samples saw beyond their "
        "0.5 s sleeps (a lower bound of the time the loop was held: a block longer than one "
        "interval counts up to 0.5 s short). Max: the bucket "
        "bound of the highest sample. - : unknown (no read before the window, no sample, or "
        "the worker restarted).",
        "",
        "| Scenario | Internal URL | Samples | Mean ms | Total s | Over 100 ms | Max s |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for scenario, url, lag in rows:
        if lag is None:
            lines.append(f"| {scenario} | {url} | - | - | - | - | - |")
            continue
        bound = "> last bound" if math.isinf(lag.max_bound_s) else f"{lag.max_bound_s:g}"
        lines.append(
            f"| {scenario} | {url} | {lag.samples} | {lag.mean_ms} | {lag.total_s:.1f} "
            f"| {lag.over_100ms} | {bound} |"
        )
    return "\n".join(lines) + "\n"


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
        "Tokens are approximated as UTF-8 bytes / 4. Prompt: a chat request's whole body, an "
        "embedding request's input texts. Completion: the answer's message content only (the "
        "fake's minimal JSON, not a real model's answer); an embedding has none.",
        "",
        "| Scenario | Route | Calls | Prompt bytes | Completion bytes | Prompt tokens per request "
        "| Completion tokens per request |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for scenario, (requests, routes) in usage.items():
        for route, count in routes.items():
            if not count["requests"]:
                continue
            per_request = max(requests, 1) * BYTES_PER_TOKEN
            completion_bytes = count["completion_bytes"]
            # An embedding answer is vectors: it has no completion tokens.
            completion = "-" if route == "embeddings" else str(completion_bytes // per_request)
            lines.append(
                f"| {scenario} | {route} | {count['requests']} | {count['prompt_bytes']} "
                f"| {completion_bytes} | {count['prompt_bytes'] // per_request} "
                f"| {completion} |"
            )
    return "\n".join(lines) + "\n"


def _delta(
    before: Mapping[str, Mapping[str, int]], after: Mapping[str, Mapping[str, int]]
) -> dict[str, dict[str, int]]:
    return {
        route: {key: value - before.get(route, {}).get(key, 0) for key, value in counts.items()}
        for route, counts in after.items()
    }


def is_local_url(url: str) -> bool:
    """Whether ``url`` is http(s) on a loopback or private IP literal or on a single-label host
    name (``farm``, ``localhost``: a name only a local resolver or compose network knows).

    Link-local addresses (``169.254.0.0/16``, ``fe80::/10``) are refused although Python counts
    them as private: that range holds the cloud metadata service, never the farm."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host
    return not address.is_link_local and (address.is_loopback or address.is_private)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument(
        "--internal-urls",
        default="",
        help="comma-separated internal base URLs (port 9090): /ready before, /metrics after",
    )
    parser.add_argument(
        "--farm-url",
        required=True,
        help="the site farm's base URL: a loopback or private IP address, or a single-label "
        "host name (a compose service such as farm)",
    )
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
    parser.add_argument(
        "--asr-model",
        default=None,
        metavar="NAME",
        help="the stack's ASR_MODEL, as a label of the run",
    )
    parser.add_argument(
        "--pii-model-size",
        default=None,
        metavar="SIZE",
        help="the stack's PII_MODEL_SIZE, as a label of the run",
    )
    parser.add_argument("--fake-llm-url", default=None, help="the fake LLM's base URL")
    parser.add_argument(
        "--valkey-cli",
        default=None,
        type=shlex.split,
        metavar="COMMAND",
        help="a valkey-cli command line that reaches the stack's Valkey, for the Valkey bytes "
        "of each crawl",
    )
    parser.add_argument("--poll-seconds", type=float, default=POLL_SECONDS)
    parser.add_argument("--job-timeout", type=float, default=JOB_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    known = {scenario.name for scenario in SCENARIOS}
    unknown = sorted(set(args.only or []) - known)
    if unknown:
        parser.error(f"unknown scenario: {', '.join(unknown)} (known: {', '.join(sorted(known))})")
    if not is_local_url(args.farm_url):
        parser.error(
            "--farm-url must be an http(s) URL of a loopback or private IP address or of a "
            "single-label host name: the load goes to our own farm only"
        )
    if not os.environ.get(args.api_key_env):
        parser.error(f"the environment variable {args.api_key_env} with the API key is not set")
    args.internal_urls = [
        url.strip().rstrip("/") for url in args.internal_urls.split(",") if url.strip()
    ]
    return args


async def amain(args: argparse.Namespace) -> int:
    plan = [scenario for scenario in SCENARIOS if not args.only or scenario.name in args.only]
    crawls = {scenario.name for scenario in plan if scenario.kind == "site"}
    out_dir: Path = args.out_dir
    headers = {"Authorization": f"Bearer {os.environ[args.api_key_env]}"}
    limits = httpx.Limits(max_connections=max(scenario.concurrency for scenario in plan) + 5)
    windows: dict[str, tuple[float, float]] = {}
    results: list[ScenarioResult] = []
    llm_usage: dict[str, tuple[int, dict[str, dict[str, int]]]] = {}
    records: list[Record] = []
    lags: dict[str, dict[str, Lag | None]] = {}
    sampler = stats.Sampler(
        out_dir / "stats.jsonl", interval=STATS_INTERVAL_SECONDS, command=STATS_COMMAND
    )
    try:
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
                async with sampler:
                    idle = await _idle()
                    windows[idle.name] = (idle.start, idle.settle_end)
                    results.append(idle)
                    metrics = await _save_metrics(side, args.internal_urls, out_dir, idle.name)
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
                            valkey_cli=args.valkey_cli,
                            collected=records,
                        )
                        windows[scenario.name] = (result.start, result.settle_end)
                        results.append(result)
                        if args.fake_llm_url is not None:
                            after = await _llm_counts(side, args.fake_llm_url)
                            llm_usage[scenario.name] = (result.requests, _delta(before, after))
                        texts = await _save_metrics(
                            side, args.internal_urls, out_dir, scenario.name
                        )
                        lags[scenario.name] = scenario_lags(metrics, texts)
                        metrics = texts
    finally:
        # Also after a failure: the scenarios that ran keep their summary and sizing.
        summary = _write_reports(
            out_dir,
            sampler.samples,
            windows,
            results,
            llm_usage,
            args.selector_processes,
            asr_model=args.asr_model,
            pii_model_size=args.pii_model_size,
            extra=_site_table(records, crawls) + _lag_table(lags),
        )
    print(summary)
    unexpected = [result.name for result in results if result.unexpected]
    if unexpected:
        print(
            f"FAILED: requests with an unexpected outcome in {', '.join(unexpected)}",
            file=sys.stderr,
        )
        return 1
    return 0


def _write_reports(
    out_dir: Path,
    samples: Sequence[stats.StatSample],
    windows: Mapping[str, tuple[float, float]],
    results: Sequence[ScenarioResult],
    llm_usage: Mapping[str, tuple[int, Mapping[str, Mapping[str, int]]]],
    selector_processes: int | None,
    *,
    asr_model: str | None = None,
    pii_model_size: str | None = None,
    extra: str = "",
) -> str:
    """Write ``summary.md`` (with the ``extra`` tables) and ``sizing.json``, both labelled with
    the run's selector processes and models where known; the summary's text."""
    rows = summarise(samples, windows)
    heading = "Load test"
    if selector_processes is not None:
        heading += f", {selector_processes} selector process"
        heading += "" if selector_processes == 1 else "es"
    if asr_model is not None:
        heading += f", ASR model {asr_model}"
    if pii_model_size is not None:
        heading += f", PII model size {pii_model_size}"
    summary = markdown(rows, results, heading=heading)
    if llm_usage:
        summary += _llm_table(llm_usage)
    summary += extra
    (out_dir / "summary.md").write_text(summary, encoding="utf-8")
    report = {
        "selector_processes": selector_processes,
        "asr_model": asr_model,
        "pii_model_size": pii_model_size,
        "windows": windows,
        "services": {name: asdict(value) for name, value in sizing(rows).items()},
    }
    (out_dir / "sizing.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
