"""Queue gauges and the job counters of the API pods (``queue_metrics.py``)."""

import asyncio
import time
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from structlog.testing import capture_logs

from tests.test_jobs_api import ALICE, make_settings
from text_extraction import jobs_api
from text_extraction.app import create_public_app
from text_extraction.queue_metrics import QueueGauges, job_duration_for
from text_extraction.scheduler import FairQueue, QueueCounts
from text_extraction.settings import Settings

JOB_A = "a" * 32
JOB_B = "b" * 32
POOL = {"pool": "extract"}


def scenario(test: Callable[[], Coroutine[Any, Any, None]]) -> Callable[[], None]:
    """Run an async test body on its own event loop (the suite has no async plugin)."""

    @wraps(test)
    def run() -> None:
        asyncio.run(test())

    return run


async def until(ready: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while not ready() and time.monotonic() < deadline:  # noqa: ASYNC110 - state of another task
        await asyncio.sleep(0.01)


def gauge(registry: CollectorRegistry, name: str) -> float | None:
    return registry.get_sample_value(f"text_extraction_queue_{name}", POOL)


def snapshot(registry: CollectorRegistry) -> tuple[float | None, float | None, float | None]:
    return (
        gauge(registry, "pending_items"),
        gauge(registry, "leased_items"),
        gauge(registry, "clients"),
    )


class FlakyQueue:
    """Answers like a ``FairQueue`` until ``down`` is set; then Valkey does not answer."""

    pool = "extract"

    def __init__(self) -> None:
        self.counts_now = QueueCounts(pending=7, leased=2, clients=3)
        self.down = False
        self.calls: list[float] = []

    async def counts(self) -> QueueCounts:
        self.calls.append(time.monotonic())
        if self.down:
            raise RedisConnectionError("Connection refused")
        return self.counts_now


@scenario
async def test_gauges_follow_the_queue() -> None:
    settings = Settings(auth_mode="none")
    queue = FairQueue(FakeAsyncRedis(), settings)
    registry = CollectorRegistry()
    gauges = QueueGauges(queue, registry)
    assert snapshot(registry) == (None, None, None)  # nothing is known before the first read

    await gauges.refresh()
    assert snapshot(registry) == (0, 0, 0)

    await queue.enqueue(JOB_A, "alice", ["https://a.example/1", "https://b.example/1"])
    await queue.enqueue(JOB_B, "bob", ["https://a.example/2"])
    await gauges.refresh()
    assert snapshot(registry) == (3, 0, 2)

    claim = await queue.claim(60)
    assert claim.item is not None
    await gauges.refresh()
    assert snapshot(registry) == (2, 1, 2)

    await queue.complete(claim.item)
    await gauges.refresh()
    assert snapshot(registry) == (2, 0, 2)


@scenario
async def test_valkey_down_keeps_last_values_and_logs_once() -> None:
    queue = FlakyQueue()
    registry = CollectorRegistry()
    gauges = QueueGauges(queue, registry)
    await gauges.refresh()
    assert snapshot(registry) == (7, 2, 3)

    queue.down = True
    queue.counts_now = QueueCounts(pending=0, leased=0, clients=0)
    with capture_logs() as logs:
        for _ in range(3):
            await gauges.refresh()
    assert snapshot(registry) == (7, 2, 3)
    assert [(log["event"], log["log_level"]) for log in logs] == [
        ("queue_metrics_unavailable", "warning")
    ]
    assert logs[0]["pool"] == "extract"
    assert logs[0]["error_type"] == "ConnectionError"

    queue.down = False
    with capture_logs() as logs:
        await gauges.refresh()
        await gauges.refresh()
    assert snapshot(registry) == (0, 0, 0)
    assert [(log["event"], log["log_level"]) for log in logs] == [
        ("queue_metrics_recovered", "info")
    ]

    queue.down = True  # a second outage is logged again, once
    with capture_logs() as logs:
        await gauges.refresh()
        await gauges.refresh()
    assert [log["event"] for log in logs] == ["queue_metrics_unavailable"]


@scenario
async def test_no_values_are_exported_when_valkey_is_down_from_the_start() -> None:
    queue = FlakyQueue()
    queue.down = True
    registry = CollectorRegistry()
    gauges = QueueGauges(queue, registry)
    await gauges.refresh()
    assert snapshot(registry) == (None, None, None)


def last_success(registry: CollectorRegistry) -> float | None:
    return registry.get_sample_value(
        "text_extraction_queue_metrics_last_success_timestamp_seconds", POOL
    )


@scenario
async def test_last_success_timestamp_is_exported_from_the_first_read_and_kept_while_down() -> None:
    now = [1_700_000_000.0]
    queue = FlakyQueue()
    registry = CollectorRegistry()
    gauges = QueueGauges(queue, registry, clock=lambda: now[0])
    assert last_success(registry) is None  # no read yet

    queue.down = True
    await gauges.refresh()
    assert last_success(registry) is None  # a failed first read is not a success

    queue.down = False
    await gauges.refresh()
    assert last_success(registry) == 1_700_000_000.0

    queue.down = True
    now[0] += 120
    await gauges.refresh()
    await gauges.refresh()
    assert last_success(registry) == 1_700_000_000.0  # unchanged: now - it shows the staleness
    assert snapshot(registry) == (7, 2, 3)

    queue.down = False
    now[0] += 5
    await gauges.refresh()
    assert last_success(registry) == 1_700_000_125.0


@scenario
async def test_refresh_task_reads_at_most_every_interval_and_stops_cleanly() -> None:
    queue = FlakyQueue()
    registry = CollectorRegistry()
    gauges = QueueGauges(queue, registry, interval=0.05)
    gauges.start()
    gauges.start()  # a second start does not run a second loop
    await until(lambda: len(queue.calls) >= 3)
    await gauges.stop()
    assert len(queue.calls) >= 3
    gaps = [later - earlier for earlier, later in zip(queue.calls, queue.calls[1:], strict=False)]
    assert min(gaps) >= 0.045
    assert snapshot(registry) == (7, 2, 3)

    calls = len(queue.calls)
    await asyncio.sleep(0.15)
    assert len(queue.calls) == calls  # stopped
    await gauges.stop()  # stopping twice is fine


@scenario
async def test_refresh_task_survives_a_failing_read() -> None:
    queue = FlakyQueue()
    queue.down = True
    registry = CollectorRegistry()
    gauges = QueueGauges(queue, registry, interval=0.01)
    gauges.start()
    await until(lambda: len(queue.calls) >= 2)
    queue.down = False
    await until(lambda: gauge(registry, "pending_items") is not None)
    await gauges.stop()
    assert snapshot(registry) == (7, 2, 3)


def created(registry: CollectorRegistry, kind: str) -> float | None:
    return registry.get_sample_value("text_extraction_jobs_created_total", {"kind": kind})


def test_jobs_created_counter() -> None:
    registry = CollectorRegistry()
    settings = make_settings(batch_max_open_per_client=2)
    app = create_public_app(settings, FakeAsyncRedis(), registry)
    assert (created(registry, "batch"), created(registry, "extract")) == (0, 0)
    urls = ["https://a.example/1", "https://b.example/1"]
    with TestClient(app) as client:
        assert client.post("/v1/batch", json={"urls": urls}, headers=ALICE).status_code == 202
        assert created(registry, "batch") == 1

        # A refused request is not counted: invalid body, then the open-batch limit.
        assert client.post("/v1/batch", json={"urls": []}, headers=ALICE).status_code == 422
        assert client.post("/v1/batch", json={"urls": urls}, headers=ALICE).status_code == 202
        assert client.post("/v1/batch", json={"urls": urls}, headers=ALICE).status_code == 429
        assert created(registry, "batch") == 2
    assert created(registry, "batch") == 2
    assert created(registry, "extract") == 0


def test_batch_discarded_by_the_recount_is_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = CollectorRegistry()
    app = create_public_app(make_settings(batch_max_open_per_client=1), FakeAsyncRedis(), registry)
    urls = ["https://a.example/1"]
    with TestClient(app) as client:
        assert client.post("/v1/batch", json={"urls": urls}, headers=ALICE).status_code == 202
        assert created(registry, "batch") == 1

        # A concurrent request that passed the check before the other one was added: the
        # pre-check sees no open batch, but the recount after the add finds the limit exceeded.
        async def sees_none(*_args: Any, **_kwargs: Any) -> int:
            return 0

        monkeypatch.setattr(jobs_api, "_open_count", sees_none)
        response = client.post("/v1/batch", json={"urls": urls}, headers=ALICE)

        assert response.status_code == 429
        assert response.json()["error"]["retryable"] is True
    assert created(registry, "batch") == 1


def test_enqueue_failure_is_not_counted() -> None:
    registry = CollectorRegistry()
    app = create_public_app(make_settings(), FakeAsyncRedis(), registry)

    async def fail(*_args: Any, **_kwargs: Any) -> None:
        raise RedisError("enqueue failed")

    app.state.job_queue.enqueue = fail
    with TestClient(app) as client:
        response = client.post("/v1/batch", json={"urls": ["https://a.example/1"]}, headers=ALICE)
    assert response.status_code == 503
    assert created(registry, "batch") == 0


def test_job_duration_is_shared_with_the_worker_by_registry() -> None:
    registry = CollectorRegistry()
    assert job_duration_for(registry) is job_duration_for(registry)
    assert job_duration_for(registry) is not job_duration_for(CollectorRegistry())


def test_the_api_app_runs_the_queue_gauges_between_startup_and_shutdown() -> None:
    registry = CollectorRegistry()
    app = create_public_app(make_settings(), FakeAsyncRedis(), registry)
    gauges: QueueGauges = app.state.queue_gauges
    assert not gauges.running  # created with the app, started by the lifespan
    with TestClient(app):
        assert gauges.running
        deadline = time.monotonic() + 10
        while gauge(registry, "pending_items") is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert snapshot(registry) == (0, 0, 0)
    assert not gauges.running


def test_the_api_app_does_not_run_the_queue_gauges_with_jobs_disabled() -> None:
    registry = CollectorRegistry()
    app = create_public_app(make_settings(jobs_enabled=False), FakeAsyncRedis(), registry)
    gauges: QueueGauges = app.state.queue_gauges
    with TestClient(app):
        assert not gauges.running
    assert not gauges.running
    assert snapshot(registry) == (None, None, None)
    assert last_success(registry) is None


def test_gauges_are_described_when_the_collector_is_registered() -> None:
    registry = CollectorRegistry()
    QueueGauges(FlakyQueue(), registry)
    with pytest.raises(ValueError, match="Duplicated"):
        QueueGauges(FlakyQueue(), registry)
