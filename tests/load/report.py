"""The load test's report: CPU and memory per scenario and container (ruling P11-S2), the
Markdown summary and the Helm requests and limits (ruling P11-S3).

A scenario's window runs from its first request to its last answer plus its settle time; only
the samples inside it count for the scenario. CPU is in percent of one core (docker's
``CPUPerc``), memory in MiB. Percentiles are nearest-rank: the p95 is the smallest sample with
at least 95 % of the samples at or below it.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from tests.load.stats import StatSample

# P11-S3: request = p95 rounded up to the next 64Mi or 50m; limit = 1.5 * the peak rounded up
# to the next 256Mi or 250m.
REQUEST_MEMORY_STEP_MIB = 64
REQUEST_CPU_STEP_MILLICORES = 50
LIMIT_MEMORY_STEP_MIB = 256
LIMIT_CPU_STEP_MILLICORES = 250
LIMIT_FACTOR = 1.5


@dataclass(frozen=True)
class Row:
    """CPU (percent of one core) and memory (MiB) of one container in one scenario's window."""

    scenario: str
    container: str
    cpu_mean: float
    cpu_p95: float
    cpu_max: float
    mem_p95: float
    mem_max: float


@dataclass(frozen=True)
class ScenarioResult:
    """What one scenario did: its window (epoch seconds: first request, last answer, end of the
    settle time), its requests by outcome, latencies and throughput."""

    name: str
    kind: str
    start: float
    end: float
    settle_end: float
    requests: int
    outcomes: Mapping[str, int]
    latency_p50_ms: int
    latency_p95_ms: int
    latency_max_ms: int
    throughput_per_s: float


@dataclass(frozen=True)
class Resources:
    """The Helm requests and limits of one container (P11-S3) and the measured values they
    come from: the highest p95 and the peak over all scenarios."""

    cpu_request: str
    cpu_limit: str
    memory_request: str
    memory_limit: str
    cpu_p95_cores: float
    cpu_max_cores: float
    mem_p95_mib: float
    mem_max_mib: float


def percentile(values: Sequence[float], q: float) -> float:
    """The nearest-rank ``q``-th percentile (0 < q <= 100) of ``values``; ``ValueError`` when
    there are none."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    rank = max(1, math.ceil(round(q / 100 * len(ordered), 9)))
    return ordered[rank - 1]


def summarise(
    samples: Sequence[StatSample], windows: Mapping[str, tuple[float, float]]
) -> list[Row]:
    """One row per scenario and container with samples inside the scenario's window (both ends
    included), in the order of ``windows``, containers by name."""
    rows: list[Row] = []
    for scenario, (start, end) in windows.items():
        inside: dict[str, list[StatSample]] = {}
        for sample in samples:
            if start <= sample.t <= end:
                inside.setdefault(sample.container, []).append(sample)
        for container in sorted(inside):
            cpu = [sample.cpu_percent for sample in inside[container]]
            mem = [sample.mem_mib for sample in inside[container]]
            rows.append(
                Row(
                    scenario=scenario,
                    container=container,
                    cpu_mean=round(sum(cpu) / len(cpu), 2),
                    cpu_p95=percentile(cpu, 95),
                    cpu_max=max(cpu),
                    mem_p95=percentile(mem, 95),
                    mem_max=max(mem),
                )
            )
    return rows


def _round_up(value: float, step: int) -> int:
    """``value`` rounded up to a multiple of ``step``, at least one step. Rounded to 6 places
    first, so that a float such as 370.00000000000006 stays 370."""
    return max(1, math.ceil(round(value / step, 6))) * step


def sizing(rows: Sequence[Row]) -> dict[str, Resources]:
    """P11-S3 per container, over all its rows: the request from the highest p95 of any
    scenario (the steady state of the busiest scenario), the limit from 1.5 * the peak."""
    by_container: dict[str, list[Row]] = {}
    for row in rows:
        by_container.setdefault(row.container, []).append(row)
    resources: dict[str, Resources] = {}
    for container in sorted(by_container):
        own = by_container[container]
        cpu_p95 = max(row.cpu_p95 for row in own) / 100  # cores
        cpu_max = max(row.cpu_max for row in own) / 100
        mem_p95 = max(row.mem_p95 for row in own)
        mem_max = max(row.mem_max for row in own)
        resources[container] = Resources(
            cpu_request=f"{_round_up(cpu_p95 * 1000, REQUEST_CPU_STEP_MILLICORES)}m",
            cpu_limit=f"{_round_up(LIMIT_FACTOR * cpu_max * 1000, LIMIT_CPU_STEP_MILLICORES)}m",
            memory_request=f"{_round_up(mem_p95, REQUEST_MEMORY_STEP_MIB)}Mi",
            memory_limit=f"{_round_up(LIMIT_FACTOR * mem_max, LIMIT_MEMORY_STEP_MIB)}Mi",
            cpu_p95_cores=round(cpu_p95, 4),
            cpu_max_cores=round(cpu_max, 4),
            mem_p95_mib=mem_p95,
            mem_max_mib=mem_max,
        )
    return resources


def _utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _outcomes(outcomes: Mapping[str, int]) -> str:
    return ", ".join(f"{name} {count}" for name, count in outcomes.items()) or "none"


def markdown(
    rows: Sequence[Row], results: Sequence[ScenarioResult], *, heading: str = "Load test"
) -> str:
    """The summary: one table of the scenarios with their windows (UTC; the window ends after
    the settle time) and one of CPU and memory per scenario and container."""
    lines = [
        f"## {heading}",
        "",
        "Each scenario ran alone. Its window runs from its first request to its last answer "
        "plus the settle time; only `docker stats` samples inside it count for it. CPU is in % "
        "of one core, memory in MiB (`MemUsage`), percentiles are nearest-rank.",
        "",
        "### Scenarios",
        "",
        "| Scenario | Kind | Window start | Window end | Requests | Outcomes | p50 ms | p95 ms "
        "| max ms | Requests/s |",
        "| --- | --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: |",
    ]
    for result in results:
        lines.append(
            f"| {result.name} | {result.kind} | {_utc(result.start)} | {_utc(result.settle_end)} "
            f"| {result.requests} | {_outcomes(result.outcomes)} | {result.latency_p50_ms} "
            f"| {result.latency_p95_ms} | {result.latency_max_ms} "
            f"| {result.throughput_per_s:.2f} |"
        )
    lines += [
        "",
        "### Resources per scenario and container",
        "",
        "| Scenario | Container | CPU mean % | CPU p95 % | CPU max % | Memory p95 MiB "
        "| Memory max MiB |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row.scenario} | {row.container} | {row.cpu_mean:.1f} | {row.cpu_p95:.1f} "
            f"| {row.cpu_max:.1f} | {row.mem_p95:.0f} | {row.mem_max:.0f} |"
        )
    return "\n".join(lines) + "\n"
