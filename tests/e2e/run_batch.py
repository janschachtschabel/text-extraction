"""End-to-end batch run: 1,000 URLs across two worker processes, no host over its limit.

    uv run python -m tests.e2e.run_batch --summary-out e2e-results/summary.md

It starts the site farm (``tests/e2e/site_farm.py``, ten hosts on ``127.0.0.2`` … ``127.0.0.11``),
a local ``redis-server`` (or uses ``VALKEY_URL``, which must point at an empty database), one API
process and two worker processes, and waits for every ``/ready``. Then:

- client A (its own API key) submits 1,000 URLs: 400 on the busy host ``127.0.0.2`` and 600
  spread over the other nine;
- once A's first 50 items are done, client B (another API key) submits 20 URLs, all on the busy
  host. A keeps that host at its limit for the whole run, so B's items can only finish first if
  the queue gives B its fair share of the host's turns. A first-in-first-out queue would run them
  after A's remaining items there, and B would finish last;
- both jobs are polled until they complete (``--timeout``, 15 min).

Checks, all from the outside, on what the outbound gate guarantees (docs/configuration.md,
"Outbound limits per host"):

- both jobs ``completed`` with every result and none ``failed``;
- per host, at most ``OUTBOUND_MAX_CONCURRENCY_PER_HOST`` (2) requests in flight at once, over
  every request the host saw (pages and compliance files alike);
- per host, page requests start at least ``OUTBOUND_MIN_DELAY_MS`` (200 ms) minus 10 % apart. A
  page request starts a gate turn, unless the turn fetched the origin's compliance files first,
  and the spacing lies between turns. The farm's clock reads a request when it arrives, a little
  after the service sent it: hence the slack;
- per host, the compliance files (robots.txt, ``tdmrep.json``, ai.txt) never overlap each other,
  and the page of their turn starts after the last of them ended;
- each worker counted items in its ``text_extraction_job_items_total``;
- B's job finished before A's.

Prints a Markdown summary, writes it to ``--summary-out`` and exits 1 when a check fails. The
logs of every process and the farm's record (``farm-record.json``) go to ``--log-dir``.
"""

import argparse
import hashlib
import json
import os
import random
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import IO, Any

import httpx
import redis

from tests.e2e.site_farm import host_addresses

BUSY_HOST = "127.0.0.2"
HOSTS = 10
A_URLS_BUSY = 400
A_URLS_OTHERS = 600
B_URLS = 20
B_PAGE_OFFSET = 1000  # B's pages are /p/1000.html … on the busy host, distinct from A's
B_AFTER_DONE = 50  # B submits once this many of A's items are done
MAX_IN_FLIGHT = 2
MIN_DELAY_MS = 200
MIN_START_GAP = MIN_DELAY_MS / 1000 * 0.9  # 10 % clock slack
WORKERS = 2

READY_TIMEOUT_SECONDS = 120.0
STOP_TIMEOUT_SECONDS = 60.0
POLL_SECONDS = 0.5

SETTINGS = {
    "AUTH_MODE": "api_key",
    "FETCH_ALLOW_PRIVATE_NETWORKS": "true",  # the farm is on loopback addresses
    "COMPLIANCE_DEFAULT": "report",
    "OUTBOUND_MIN_DELAY_MS": str(MIN_DELAY_MS),
    "OUTBOUND_MAX_CONCURRENCY_PER_HOST": str(MAX_IN_FLIGHT),
    "WORKER_CONCURRENCY": "8",
    "RATE_LIMIT_ENABLED": "false",
    "PUBLIC_HOST": "127.0.0.1",
    "INTERNAL_HOST": "127.0.0.1",
    "LOG_FORMAT": "json",
    "LOG_LEVEL": "INFO",
    "SHUTDOWN_DRAIN_SECONDS": "0",
}

_ITEMS_TOTAL = re.compile(r'^text_extraction_job_items_total\{outcome="([^"]+)"\} (\S+)$', re.M)


# --- the checks on the farm's record ------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    """One request a farm host saw, with start and end on the farm's monotonic clock."""

    path: str
    start: float
    end: float
    status: int = 200


def max_overlap(requests: Iterable[Request]) -> int:
    """The most requests in flight at the same moment. A request that starts exactly when
    another ends does not overlap it."""
    events = sorted(
        [(request.start, 1) for request in requests] + [(request.end, -1) for request in requests]
    )  # at equal times an end (-1) sorts before a start (1)
    in_flight = most = 0
    for _, change in events:
        in_flight += change
        most = max(most, in_flight)
    return most


def spacing_violations(
    requests: Iterable[Request], *, min_gap: float
) -> list[tuple[Request, Request]]:
    """Each pair of consecutive request starts less than ``min_gap`` seconds apart."""
    ordered = sorted(requests, key=lambda request: request.start)
    return [
        (first, second)
        for first, second in pairwise(ordered)
        if second.start - first.start < min_gap
    ]


def min_start_gap(requests: Iterable[Request]) -> float | None:
    starts = sorted(request.start for request in requests)
    gaps = [later - earlier for earlier, later in pairwise(starts)]
    return min(gaps) if gaps else None


def is_page(request: Request) -> bool:
    return request.path.startswith("/p/")


def turn_violations(requests: Iterable[Request]) -> list[str]:
    """How a host's compliance files broke "one after another, before the page of their turn":
    files that overlap, or a page that started before the last file ended. The page of their
    turn is the first page that started after the last file did; a page of an earlier turn may
    still be in flight while the files load."""
    ordered = sorted(requests, key=lambda request: request.start)
    files = [request for request in ordered if not is_page(request)]
    problems = [
        f"{first.path} and {second.path} overlap"
        for first, second in pairwise(files)
        if second.start < first.end
    ]
    if files:
        last = files[-1]
        page = next((r for r in ordered if is_page(r) and r.start >= last.start), None)
        if page is not None and page.start < last.end:
            problems.append(f"{page.path} started before {last.path} ended")
    return problems


def read_record(path: Path) -> dict[str, list[Request]]:
    data = json.loads(path.read_text())
    return {
        host: [
            Request(
                path=entry["path"], start=entry["start"], end=entry["end"], status=entry["status"]
            )
            for entry in entries
        ]
        for host, entries in data["hosts"].items()
    }


# --- processes ----------------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass
class Processes:
    """The started processes, stopped in reverse order of start."""

    log_dir: Path
    started: list[tuple[str, subprocess.Popen[bytes], IO[bytes]]] = field(default_factory=list)

    def start(
        self, name: str, command: Sequence[str], env: dict[str, str] | None = None
    ) -> subprocess.Popen[bytes]:
        log = (self.log_dir / f"{name}.log").open("wb")
        process = subprocess.Popen(  # noqa: S603 - fixed commands of this harness
            command, env=env, stdout=log, stderr=subprocess.STDOUT
        )
        self.started.append((name, process, log))
        return process

    def stop(self, name: str) -> int | None:
        for entry in self.started:
            if entry[0] == name:
                self.started.remove(entry)
                return _terminate(*entry)
        return None

    def stop_all(self) -> None:
        while self.started:
            _terminate(*self.started.pop())

    def check_running(self) -> None:
        for name, process, _ in self.started:
            if process.poll() is not None:
                raise RuntimeError(f"{name} exited with code {process.returncode}")


def _terminate(name: str, process: subprocess.Popen[bytes], log: IO[bytes]) -> int | None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            print(
                f"{name} did not stop within {STOP_TIMEOUT_SECONDS:.0f} s; killed", file=sys.stderr
            )
            process.kill()
            process.wait()
    log.close()
    return process.returncode


def _entry_point(name: str) -> str:
    """The console script of the installed package, next to this interpreter."""
    found = shutil.which(name, path=str(Path(sys.executable).parent))
    if found is None:
        raise RuntimeError(f"entry point {name} not found next to {sys.executable}")
    return found


def _wait_until(what: str, ready: Any, processes: Processes, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        processes.check_running()
        if ready():
            return
        time.sleep(0.2)
    raise RuntimeError(f"{what} was not ready within {timeout:.0f} s")


def _http_ready(url: str) -> bool:
    try:
        return httpx.get(url, timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def _valkey_ready(url: str) -> bool:
    try:
        return bool(redis.Redis.from_url(url, socket_timeout=2).ping())
    except redis.RedisError:
        return False


# --- the scenario -------------------------------------------------------------------------------


@dataclass
class JobOutcome:
    """What the API reported about one job, and when."""

    name: str
    id: str
    total: int
    submitted: float  # monotonic, this process
    view: dict[str, Any] = field(default_factory=dict)
    statuses: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)
    results: int = 0

    @property
    def state(self) -> str:
        return str(self.view.get("state", "unknown"))

    @property
    def finished_at(self) -> datetime | None:
        value = self.view.get("finished_at")
        return datetime.fromisoformat(value) if value else None

    @property
    def done(self) -> int:
        return int(self.view.get("progress", {}).get("done", 0))

    @property
    def failed(self) -> int:
        return int(self.view.get("progress", {}).get("failed", 0))


def urls_of_client_a(port: int) -> list[str]:
    hosts = host_addresses(HOSTS)
    others = [host for host in hosts if host != BUSY_HOST]
    urls = [f"http://{BUSY_HOST}:{port}/p/{n}.html" for n in range(A_URLS_BUSY)]
    urls += [f"http://{others[n % len(others)]}:{port}/p/{n}.html" for n in range(A_URLS_OTHERS)]
    random.Random(8).shuffle(urls)  # noqa: S311 - the hosts arrive mixed, as in a real batch
    return urls


def urls_of_client_b(port: int) -> list[str]:
    return [f"http://{BUSY_HOST}:{port}/p/{B_PAGE_OFFSET + n}.html" for n in range(B_URLS)]


class Api:
    def __init__(self, base_url: str) -> None:
        self._http = httpx.Client(base_url=base_url, timeout=30)

    def close(self) -> None:
        self._http.close()

    def submit(self, key: str, urls: list[str]) -> str:
        answer = self._http.post(
            "/v1/batch",
            headers={"Authorization": f"Bearer {key}"},
            json={"urls": urls, "mode": "fast", "format": "markdown"},
        )
        if answer.status_code != 202:
            raise RuntimeError(f"POST /v1/batch answered {answer.status_code}: {answer.text[:500]}")
        return str(answer.json()["data"]["id"])

    def view(self, key: str, job_id: str, *, offset: int = 0, limit: int = 1) -> dict[str, Any]:
        answer = self._http.get(
            f"/v1/jobs/{job_id}",
            headers={"Authorization": f"Bearer {key}"},
            params={"offset": offset, "limit": limit},
        )
        answer.raise_for_status()
        data: dict[str, Any] = answer.json()["data"]
        return data

    def collect(self, key: str, job: JobOutcome) -> None:
        """Every item's result: counts by envelope status and error code."""
        offset: int | None = 0
        while offset is not None:
            page = self.view(key, job.id, offset=offset, limit=500)
            for item in page["items"]:
                result = item.get("result")
                if item["state"] != "done" or result is None:
                    continue
                job.results += 1
                job.statuses[result["status"]] += 1
                if result.get("error"):
                    job.errors[result["error"]["code"]] += 1
            offset = page["next_offset"]


def run_scenario(api: Api, keys: dict[str, str], port: int, timeout: float) -> list[JobOutcome]:
    a_urls = urls_of_client_a(port)
    a = JobOutcome("A", api.submit(keys["A"], a_urls), len(a_urls), time.monotonic())
    deadline = a.submitted + timeout
    while True:
        a.view = api.view(keys["A"], a.id)
        if a.done >= B_AFTER_DONE or a.state == "completed":
            break
        if time.monotonic() > deadline:
            raise RuntimeError("client A's job made no progress")
        time.sleep(0.1)
    b_urls = urls_of_client_b(port)
    b = JobOutcome("B", api.submit(keys["B"], b_urls), len(b_urls), time.monotonic())
    jobs = [a, b]
    while time.monotonic() < deadline:
        for job in jobs:
            if job.state != "completed":
                job.view = api.view(keys[job.name], job.id)
        if all(job.state == "completed" for job in jobs):
            break
        time.sleep(POLL_SECONDS)
    for job in jobs:
        api.collect(keys[job.name], job)
    return jobs


def worker_items(metrics_url: str) -> Counter[str]:
    text = httpx.get(metrics_url, timeout=5).text
    counts: Counter[str] = Counter()
    for outcome, value in _ITEMS_TOTAL.findall(text):
        counts[outcome] += int(float(value))
    return counts


# --- the report ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


def _ms(seconds: float | None) -> str:
    return "-" if seconds is None else f"{seconds * 1000:.0f}"


def host_rows(record: dict[str, list[Request]]) -> list[str]:
    rows = [
        "| Host | Pages | Compliance files | Max in flight (all) | Min page start gap ms | "
        f"Page gaps < {MIN_START_GAP * 1000:.0f} ms | Files serial before their page |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for host, requests in record.items():
        pages = [request for request in requests if is_page(request)]
        rows.append(
            f"| {host} | {len(pages)} | {len(requests) - len(pages)} | {max_overlap(requests)} | "
            f"{_ms(min_start_gap(pages))} | "
            f"{len(spacing_violations(pages, min_gap=MIN_START_GAP))} | "
            f"{'yes' if not turn_violations(requests) else 'NO'} |"
        )
    return rows


def evaluate(
    jobs: list[JobOutcome], record: dict[str, list[Request]], items: dict[str, Counter[str]]
) -> list[Check]:
    a, b = jobs
    checks = [
        Check(
            f"job {job.name} completed with {job.total} results, 0 failed",
            job.state == "completed" and job.results == job.total and job.failed == 0,
            f"state {job.state}, {job.results}/{job.total} results, statuses "
            f"{dict(job.statuses)}, errors {dict(job.errors)}",
        )
        for job in jobs
    ]
    overlaps = {host: max_overlap(requests) for host, requests in record.items()}
    checks.append(
        Check(
            f"per host at most {MAX_IN_FLIGHT} requests in flight (all requests)",
            all(value <= MAX_IN_FLIGHT for value in overlaps.values()),
            ", ".join(
                f"{host}: {value}" for host, value in overlaps.items() if value > MAX_IN_FLIGHT
            )
            or f"max {max(overlaps.values(), default=0)}",
        )
    )
    close = {
        host: spacing_violations([r for r in requests if is_page(r)], min_gap=MIN_START_GAP)
        for host, requests in record.items()
    }
    gaps = [
        gap
        for requests in record.values()
        if (gap := min_start_gap([r for r in requests if is_page(r)])) is not None
    ]
    checks.append(
        Check(
            f"per host page request starts >= {MIN_START_GAP * 1000:.0f} ms apart (turn starts)",
            not any(close.values()),
            "; ".join(
                f"{host}: {len(pairs)} gaps, e.g. "
                + ", ".join(
                    f"{first.path} -> {second.path} {(second.start - first.start) * 1000:.0f} ms"
                    for first, second in pairs[:3]
                )
                for host, pairs in close.items()
                if pairs
            )
            or f"min {_ms(min(gaps, default=None))} ms",
        )
    )
    turns = {host: turn_violations(requests) for host, requests in record.items()}
    files = sum(1 for requests in record.values() for r in requests if not is_page(r))
    checks.append(
        Check(
            "per host compliance files fetched one by one, before the page of their turn",
            not any(turns.values()),
            "; ".join(f"{host}: {', '.join(found)}" for host, found in turns.items() if found)
            or f"{files} files, none overlapping",
        )
    )
    for name, counts in items.items():
        checks.append(
            Check(
                f"{name} counted items in text_extraction_job_items_total",
                sum(counts.values()) > 0,
                str(dict(counts)),
            )
        )
    busy = record.get(BUSY_HOST, [])
    b_pages = [r.end for r in busy if is_page(r) and _page_number(r) >= B_PAGE_OFFSET]
    b_last = max(b_pages, default=None)
    a_before = (
        sum(1 for r in busy if is_page(r) and _page_number(r) < B_PAGE_OFFSET and r.end <= b_last)
        if b_last is not None
        else None
    )
    checks.append(
        Check(
            "job B (20 URLs on the busy host) finished before job A (fair shares)",
            a.finished_at is not None
            and b.finished_at is not None
            and b.finished_at < a.finished_at,
            f"B finished {b.finished_at}, A finished {a.finished_at}; A's pages on {BUSY_HOST} "
            f"fetched by B's last fetch: {a_before}/{A_URLS_BUSY}",
        )
    )
    return checks


def _page_number(request: Request) -> int:
    match = re.fullmatch(r"/p/(\d+)\.html", request.path)
    return int(match.group(1)) if match else -1


def summary(
    checks: list[Check],
    jobs: list[JobOutcome],
    record: dict[str, list[Request]],
    wall_seconds: float,
) -> str:
    lines = ["## End-to-end batch run", ""]
    lines += ["| Check | Result | Detail |", "| --- | --- | --- |"]
    lines += [
        f"| {check.name} | {'pass' if check.passed else 'FAIL'} | {check.detail} |"
        for check in checks
    ]
    lines += ["", "| Job | URLs | State | Created | Finished |", "| --- | ---: | --- | --- | --- |"]
    lines += [
        f"| {job.name} | {job.total} | {job.state} | {job.view.get('created_at')} | "
        f"{job.view.get('finished_at')} |"
        for job in jobs
    ]
    lines += ["", *host_rows(record), ""]
    lines.append(f"Wall time from A's submission to both jobs completed: {wall_seconds:.1f} s.")
    return "\n".join(lines) + "\n"


# --- main ---------------------------------------------------------------------------------------


def _api_keys() -> tuple[dict[str, str], str]:
    keys = {name: secrets.token_hex(32) for name in ("A", "B")}
    entries = [
        {
            "name": f"client-{name.lower()}",
            "role": "user",
            "sha256": hashlib.sha256(key.encode()).hexdigest(),
        }
        for name, key in keys.items()
    ]
    return keys, json.dumps(entries)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--log-dir", type=Path, default=Path("e2e-results"))
    parser.add_argument("--summary-out", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=900.0, help="seconds for both jobs")
    args = parser.parse_args(argv)
    log_dir: Path = args.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    record_path = log_dir / "farm-record.json"
    record_path.unlink(missing_ok=True)
    summary_path: Path = args.summary_out or log_dir / "summary.md"

    processes = Processes(log_dir)
    api: Api | None = None
    try:
        valkey_url = os.environ.get("VALKEY_URL")
        if not valkey_url:
            valkey_port = free_port()
            processes.start(
                "valkey",
                [
                    "redis-server",
                    "--port",
                    str(valkey_port),
                    "--bind",
                    "127.0.0.1",
                    "--save",
                    "",
                    "--appendonly",
                    "no",
                ],
            )
            valkey_url = f"redis://127.0.0.1:{valkey_port}/0"
        url = valkey_url
        _wait_until("Valkey", lambda: _valkey_ready(url), processes, READY_TIMEOUT_SECONDS)
        if redis.Redis.from_url(url).dbsize() != 0:
            raise RuntimeError("the Valkey database is not empty; the run needs a fresh one")

        farm_port = free_port()
        farm = subprocess.Popen(  # noqa: S603 - this harness's own module
            [
                sys.executable,
                "-m",
                "tests.e2e.site_farm",
                "--port",
                str(farm_port),
                "--hosts",
                str(HOSTS),
                "--record",
                str(record_path),
            ],
            stdout=subprocess.PIPE,
            stderr=(log_dir / "farm.log").open("wb"),
        )
        assert farm.stdout is not None
        processes.started.append(("farm", farm, farm.stdout))
        if farm.stdout.readline().strip() != b"ready":
            raise RuntimeError("the site farm did not start")

        keys, api_keys = _api_keys()
        env = {**os.environ, **SETTINGS, "VALKEY_URL": url, "API_KEYS": api_keys}
        public_port = free_port()
        internal_ports = {"api": free_port()}
        processes.start(
            "api",
            [_entry_point("text-extraction")],
            {**env, "PUBLIC_PORT": str(public_port), "INTERNAL_PORT": str(internal_ports["api"])},
        )
        for n in range(1, WORKERS + 1):
            name = f"worker-{n}"
            internal_ports[name] = free_port()
            processes.start(
                name,
                [_entry_point("text-extraction-worker")],
                {**env, "INTERNAL_PORT": str(internal_ports[name])},
            )
        for name, port in internal_ports.items():
            ready_url = f"http://127.0.0.1:{port}/ready"
            _wait_until(name, lambda u=ready_url: _http_ready(u), processes, READY_TIMEOUT_SECONDS)

        api = Api(f"http://127.0.0.1:{public_port}")
        started = time.monotonic()
        jobs = run_scenario(api, keys, farm_port, args.timeout)
        wall = time.monotonic() - started
        items = {
            name: worker_items(f"http://127.0.0.1:{port}/metrics")
            for name, port in internal_ports.items()
            if name.startswith("worker-")
        }
        for name in [n for n in internal_ports if n.startswith("worker-")]:
            processes.stop(name)
        processes.stop("api")
        processes.stop("farm")
    finally:
        if api is not None:
            api.close()
        processes.stop_all()

    record = read_record(record_path)
    checks = evaluate(jobs, record, items)
    text = summary(checks, jobs, record, wall)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(text)
    print(text)
    return 0 if all(check.passed for check in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
