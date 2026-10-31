"""The load test's report (`tests/load/report.py`): per scenario and container the CPU and
memory of the samples inside the scenario's window, the Markdown summary and the Helm sizing
rule (P11-S3)."""

from datetime import UTC, datetime

import pytest

from tests.load.report import (
    Resources,
    Row,
    ScenarioResult,
    markdown,
    percentile,
    service_of,
    sizing,
    summarise,
)
from tests.load.stats import StatSample

START = datetime(2026, 10, 31, 12, 0, tzinfo=UTC).timestamp()


def sample(t: float, container: str, cpu: float, mem: float) -> StatSample:
    return StatSample(t=START + t, container=container, cpu_percent=cpu, mem_mib=mem)


def row(container: str, *, cpu_p95: float, cpu_max: float, mem_p95: float, mem_max: float) -> Row:
    return Row(
        scenario="sync-fast-c10",
        container=container,
        cpu_mean=cpu_p95,
        cpu_p95=cpu_p95,
        cpu_max=cpu_max,
        mem_p95=mem_p95,
        mem_max=mem_max,
    )


def test_percentile_p95_of_known_series() -> None:
    series = [float(value) for value in range(1, 21)]  # 1 … 20

    # Nearest rank: the smallest value with at least 95 % of the series at or below it.
    assert percentile(series, 95) == 19.0
    assert percentile(list(reversed(series)), 95) == 19.0  # order does not matter
    assert percentile(series, 100) == 20.0
    assert percentile(series, 50) == 10.0
    assert percentile([7.0], 95) == 7.0
    with pytest.raises(ValueError, match="no values"):
        percentile([], 95)


def test_summarise_uses_only_samples_inside_the_window() -> None:
    samples = [sample(t, "api", cpu=10.0 * t, mem=100.0 + t) for t in range(11)]
    samples += [sample(t, "worker", cpu=1.0, mem=50.0) for t in (3, 9)]
    windows = {"first": (START + 2, START + 4), "second": (START + 6, START + 8)}

    rows = summarise(samples, windows)

    assert rows == [
        Row(
            "first", "api", cpu_mean=30.0, cpu_p95=40.0, cpu_max=40.0, mem_p95=104.0, mem_max=104.0
        ),
        Row("first", "worker", cpu_mean=1.0, cpu_p95=1.0, cpu_max=1.0, mem_p95=50.0, mem_max=50.0),
        Row(
            "second", "api", cpu_mean=70.0, cpu_p95=80.0, cpu_max=80.0, mem_p95=108.0, mem_max=108.0
        ),
    ]


def test_summarise_skips_a_window_without_samples() -> None:
    rows = summarise([sample(1, "api", 1.0, 1.0)], {"empty": (START + 5, START + 6)})

    assert rows == []


def test_sizing_rounds_request_and_limit() -> None:
    rows = [row("api", cpu_p95=37.0, cpu_max=160.0, mem_p95=300.0, mem_max=900.0)]

    resources = sizing(rows)

    # P11-S3: request = p95 rounded up to 64Mi / 50m; limit = 1.5 * peak up to 256Mi / 250m.
    assert resources["api"].memory_request == "320Mi"
    assert resources["api"].memory_limit == "1536Mi"  # 1.5 * 900 = 1350 → 1536
    assert resources["api"].cpu_request == "400m"  # 0.37 cores → 370m → 400m
    assert resources["api"].cpu_limit == "2500m"  # 1.6 cores * 1.5 = 2.4 → 2500m


def test_sizing_takes_the_highest_p95_and_peak_over_the_scenarios() -> None:
    rows = [
        row("api", cpu_p95=10.0, cpu_max=90.0, mem_p95=200.0, mem_max=210.0),
        row("api", cpu_p95=50.0, cpu_max=60.0, mem_p95=100.0, mem_max=700.0),
        row("worker", cpu_p95=0.0, cpu_max=0.0, mem_p95=0.0, mem_max=0.0),
    ]

    resources = sizing(rows)

    assert resources["api"] == Resources(
        cpu_request="500m",  # 0.5 cores
        cpu_limit="1500m",  # 1.5 * 0.9 cores = 1.35 → 1500m
        memory_request="256Mi",  # 200 → 256
        memory_limit="1280Mi",  # 1.5 * 700 = 1050 → 1280
        cpu_p95_cores=0.5,
        cpu_max_cores=0.9,
        mem_p95_mib=200.0,
        mem_max_mib=700.0,
    )
    # An idle container still gets the smallest step, never 0.
    assert resources["worker"].cpu_request == "50m"
    assert resources["worker"].cpu_limit == "250m"
    assert resources["worker"].memory_request == "64Mi"
    assert resources["worker"].memory_limit == "256Mi"


def test_markdown_names_each_window() -> None:
    results = [
        ScenarioResult(
            name="sync-fast-c10",
            kind="sync",
            start=START,
            end=START + 30,
            settle_end=START + 40,
            requests=300,
            outcomes={"success": 299, "error": 1},
            latency_p50_ms=420,
            latency_p95_ms=900,
            latency_max_ms=1500,
            throughput_per_s=10.0,
        ),
        ScenarioResult(
            name="batch-1000",
            kind="batch",
            start=START + 40,
            end=START + 340,
            settle_end=START + 350,
            requests=1,
            outcomes={"success": 1},
            latency_p50_ms=300000,
            latency_p95_ms=300000,
            latency_max_ms=300000,
            throughput_per_s=0.0,
        ),
    ]
    rows = [
        Row(
            "sync-fast-c10",
            "api",
            cpu_mean=20.0,
            cpu_p95=35.5,
            cpu_max=60.0,
            mem_p95=300.0,
            mem_max=310.0,
        ),
    ]

    text = markdown(rows, results, heading="Load test, 2 selector processes")

    assert text.startswith("## Load test, 2 selector processes")
    assert "| sync-fast-c10 | sync | 2026-10-31T12:00:00Z | 2026-10-31T12:00:40Z |" in text
    assert "| batch-1000 | batch | 2026-10-31T12:00:40Z | 2026-10-31T12:05:50Z |" in text
    assert "success 299, error 1" in text
    assert "| sync-fast-c10 | api | 20.0 | 35.5 | 60.0 | 300 | 310 |" in text
    assert "No `docker stats` samples" not in text


def test_markdown_says_when_there_were_no_samples() -> None:
    assert "No `docker stats` samples" in markdown([], [])


@pytest.mark.parametrize(
    ("container", "service"),
    [
        ("text-extraction-api-1", "api"),
        ("text-extraction-browser-worker-2", "browser-worker"),
        ("text-extraction_llm-worker_1", "llm-worker"),  # Compose v1 names
        ("api", "api"),
        ("other-project-api-1", "other-project-api"),  # not our project: the name stays
    ],
)
def test_service_of_strips_the_project_and_the_replica(container: str, service: str) -> None:
    assert service_of(container, project="text-extraction") == service


def test_sizing_is_keyed_by_service_without_the_test_containers() -> None:
    rows = [
        row("text-extraction-worker-1", cpu_p95=40.0, cpu_max=50.0, mem_p95=300.0, mem_max=400.0),
        row("text-extraction-worker-2", cpu_p95=60.0, cpu_max=90.0, mem_p95=200.0, mem_max=500.0),
        row("text-extraction-api-1", cpu_p95=5.0, cpu_max=10.0, mem_p95=100.0, mem_max=120.0),
        row("text-extraction-farm-1", cpu_p95=90.0, cpu_max=99.0, mem_p95=50.0, mem_max=60.0),
        row("text-extraction-fake-llm-1", cpu_p95=1.0, cpu_max=2.0, mem_p95=40.0, mem_max=50.0),
        row("text-extraction-valkey-1", cpu_p95=3.0, cpu_max=4.0, mem_p95=30.0, mem_max=35.0),
    ]

    resources = sizing(rows, project="text-extraction")

    # The farm and the fake LLM are the test's, Valkey is sized by its own chart.
    assert sorted(resources) == ["api", "worker"]
    # Two replicas of one service: the highest p95 and the peak of both.
    assert resources["worker"].cpu_p95_cores == 0.6
    assert resources["worker"].cpu_max_cores == 0.9
    assert resources["worker"].mem_p95_mib == 300.0
    assert resources["worker"].mem_max_mib == 500.0


def test_markdown_counts_the_unexpected_outcomes() -> None:
    result = ScenarioResult(
        name="upload-docs-50",
        kind="upload",
        start=START,
        end=START + 10,
        settle_end=START + 20,
        requests=50,
        outcomes={"success": 48, "partial": 2},
        latency_p50_ms=500,
        latency_p95_ms=900,
        latency_max_ms=1000,
        throughput_per_s=5.0,
        unexpected=2,
    )

    text = markdown([], [result])

    assert "| Outcomes | Unexpected |" in text
    assert "| success 48, partial 2 | 2 |" in text
