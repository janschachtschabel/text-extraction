"""The Valkey memory guard of the API pods (``memory_guard.py``) and what it holds back: new
batches, the hand-over of slow extractions to jobs, and the result cache's writes."""

import asyncio
import time
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any, cast

from fakeredis import FakeAsyncRedis, FakeServer
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from structlog.testing import capture_logs

from tests.fakes import PAGE
from tests.test_extract_api import AUTH, Handover
from tests.test_jobs_api import ALICE, Api, make_settings
from tests.test_result_cache import URL, make_entry
from text_extraction.app import create_public_app
from text_extraction.memory_guard import MemoryGuard
from text_extraction.result_cache import ResultCache, cache_key
from text_extraction.settings import Settings

MiB = 1024 * 1024


def scenario(test: Callable[[], Coroutine[Any, Any, None]]) -> Callable[[], None]:
    """Run an async test body on its own event loop (the suite has no async plugin)."""

    @wraps(test)
    def run() -> None:
        asyncio.run(test())

    return run


class FakeInfo:
    """Answers ``INFO memory`` with the values set, or fails while ``down`` is set."""

    def __init__(self, used: int = 10 * MiB, maximum: int = 100 * MiB) -> None:
        self.used = used
        self.maximum = maximum
        self.down = False
        self.calls = 0

    async def info(self, section: str) -> dict[str, Any]:
        assert section == "memory"
        self.calls += 1
        if self.down:
            raise RedisConnectionError("Connection refused")
        return {"used_memory": self.used, "maxmemory": self.maximum, "used_memory_human": "x"}


class StubGuard:
    """Stands in for the app's guard: ``high`` as the test sets it."""

    def __init__(self, high: bool) -> None:
        self.high = high

    def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


async def keys(valkey: FakeAsyncRedis, pattern: str) -> list[Any]:
    return list(await valkey.keys(pattern))


def make_guard(info: FakeInfo, registry: CollectorRegistry | None = None) -> MemoryGuard:
    return MemoryGuard(cast(Redis, info), 0.8, registry or CollectorRegistry(), interval=0.01)


def gauges(registry: CollectorRegistry) -> tuple[float | None, float | None]:
    return (
        registry.get_sample_value("text_extraction_valkey_used_memory_bytes"),
        registry.get_sample_value("text_extraction_valkey_max_memory_bytes"),
    )


# --- the guard -------------------------------------------------------------------------------


@scenario
async def test_guard_holds_at_the_ratio_and_exports_the_gauges() -> None:
    info = FakeInfo(used=79 * MiB, maximum=100 * MiB)
    registry = CollectorRegistry()
    guard = make_guard(info, registry)
    assert gauges(registry) == (None, None)  # nothing is known before the first read
    assert guard.high is False

    await guard.refresh()
    assert guard.high is False
    assert gauges(registry) == (79 * MiB, 100 * MiB)

    info.used = 80 * MiB  # exactly the ratio
    await guard.refresh()
    assert guard.high is True

    info.used = 50 * MiB
    await guard.refresh()
    assert guard.high is False


@scenario
async def test_no_guard_without_maxmemory() -> None:
    info = FakeInfo(used=900 * MiB, maximum=0)
    registry = CollectorRegistry()
    guard = make_guard(info, registry)

    await guard.refresh()

    assert guard.high is False
    assert gauges(registry) == (900 * MiB, 0)


@scenario
async def test_guard_keeps_its_last_values_while_valkey_fails_and_logs_once() -> None:
    info = FakeInfo(used=90 * MiB)
    registry = CollectorRegistry()
    guard = make_guard(info, registry)
    await guard.refresh()
    assert guard.high is True

    info.down = True
    with capture_logs() as logs:
        await guard.refresh()
        await guard.refresh()
        info.down = False
        info.used = 10 * MiB
        await guard.refresh()

    events = [entry["event"] for entry in logs]
    assert events == [
        "valkey_memory_unavailable",
        "valkey_memory_recovered",
        "valkey_memory_normal",
    ]
    assert guard.high is False
    assert gauges(registry) == (10 * MiB, 100 * MiB)


@scenario
async def test_guard_last_verdict_stays_during_an_outage() -> None:
    info = FakeInfo(used=95 * MiB)
    guard = make_guard(info)
    await guard.refresh()
    info.down = True

    await guard.refresh()

    assert guard.high is True


@scenario
async def test_guard_logs_each_change_of_its_verdict_once() -> None:
    info = FakeInfo(used=90 * MiB)
    guard = make_guard(info)
    with capture_logs() as logs:
        await guard.refresh()
        await guard.refresh()
        info.used = 10 * MiB
        await guard.refresh()
        await guard.refresh()

    assert [entry["event"] for entry in logs] == ["valkey_memory_high", "valkey_memory_normal"]
    assert logs[0]["log_level"] == "warning"


@scenario
async def test_guard_task_refreshes_until_stopped() -> None:
    info = FakeInfo(used=90 * MiB)
    guard = make_guard(info)
    guard.start()
    guard.start()  # a second start does nothing
    deadline = time.monotonic() + 10
    while info.calls < 3 and time.monotonic() < deadline:  # noqa: ASYNC110 - another task's state
        await asyncio.sleep(0.01)
    assert guard.running
    assert guard.high is True

    await guard.stop()
    calls = info.calls
    await asyncio.sleep(0.05)

    assert not guard.running
    assert info.calls == calls


def test_public_app_runs_the_guard_also_without_jobs() -> None:
    settings = make_settings(jobs_enabled=False)
    app = create_public_app(settings, FakeAsyncRedis(), CollectorRegistry())
    guard: MemoryGuard = app.state.memory_guard
    assert not guard.running

    with TestClient(app):
        assert guard.running

    assert not guard.running


# --- what the guard holds back ---------------------------------------------------------------


def test_batch_is_refused_while_valkey_memory_is_high() -> None:
    settings = make_settings()
    server_valkey = FakeAsyncRedis()
    app = create_public_app(settings, server_valkey, CollectorRegistry())
    app.state.memory_guard = StubGuard(high=True)
    with TestClient(app) as client:
        response = client.post("/v1/batch", json={"urls": ["https://example.org/"]}, headers=ALICE)

        assert response.status_code == 503
        error = response.json()["error"]
        assert error["code"] == "internal_error"
        assert error["retryable"] is True
        assert "memory" in error["message"]
        assert asyncio.run(keys(server_valkey, "job:*")) == []

        app.state.memory_guard.high = False
        accepted = client.post("/v1/batch", json={"urls": ["https://example.org/"]}, headers=ALICE)
        assert accepted.status_code == 202


def test_hand_over_keeps_waiting_inline_while_valkey_memory_is_high() -> None:
    case = Handover()
    case.app.state.memory_guard = StubGuard(high=True)
    with TestClient(case.app) as client:
        case.release_when(lambda: len(case.pipeline.tasks) == 1, after=0.1)

        response = client.post("/v1/extract", json={"url": PAGE, "wait_seconds": 0}, headers=AUTH)

        assert response.status_code == 200
        assert response.json()["status"] == "success"
        assert case.job_keys() == []


def test_worker_still_stores_results_while_valkey_memory_is_high() -> None:
    settings = make_settings()
    server = FakeServer()
    app = create_public_app(settings, FakeAsyncRedis(server=server), CollectorRegistry())
    with TestClient(app) as client:
        api = Api(client, server, settings)
        response = api.batch([PAGE])
        assert response.status_code == 202
        app.state.memory_guard = StubGuard(high=True)  # accepted before the memory ran high

        assert api.work() == 1

        job = api.job(response.json()["data"]["id"]).json()
        assert job["data"]["state"] == "completed"
        assert job["data"]["items"][0]["result"]["status"] == "success"


@scenario
async def test_result_cache_skips_its_writes_while_valkey_memory_is_high() -> None:
    valkey = FakeAsyncRedis()
    guard = StubGuard(high=True)
    cache = ResultCache(valkey, Settings(auth_mode="none"), guard=guard)
    key = cache_key(URL, {})

    with capture_logs() as logs:
        await cache.put(key, make_entry())
        await cache.put(key, make_entry())
        assert await valkey.exists(key) == 0
        guard.high = False
        await cache.put(key, make_entry())
        await cache.put(key, make_entry())

    assert await valkey.exists(key) == 1
    events = [entry["event"] for entry in logs]
    assert events == ["result_cache_writes_paused", "result_cache_writes_resumed"]


def test_api_pipeline_cache_uses_the_app_guard() -> None:
    app = create_public_app(make_settings(), FakeAsyncRedis(), CollectorRegistry())
    assert app.state.pipeline._cache._guard is app.state.memory_guard
