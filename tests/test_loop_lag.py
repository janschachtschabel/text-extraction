"""The event-loop lag of the extract worker (``loop_lag.py``): a histogram sampled every 0.5 s,
exported on the internal port and absent from the public OpenAPI spec (package 11, P11-S7)."""

import asyncio
import time
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any

from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from tests.test_app import make_settings
from text_extraction.app import create_public_app
from text_extraction.internal import Readiness, create_internal_app
from text_extraction.loop_lag import LAG_METRIC, SAMPLE_SECONDS, LoopLag

COUNT = f"{LAG_METRIC}_count"
SUM = f"{LAG_METRIC}_sum"


def scenario(test: Callable[[], Coroutine[Any, Any, None]]) -> Callable[[], None]:
    """Run an async test body on its own event loop (the suite has no async plugin)."""

    @wraps(test)
    def run() -> None:
        asyncio.run(test())

    return run


def samples(registry: CollectorRegistry) -> float:
    return registry.get_sample_value(COUNT) or 0.0


async def until_samples(registry: CollectorRegistry, at_least: float) -> None:
    deadline = time.monotonic() + 5
    while samples(registry) < at_least:
        assert time.monotonic() < deadline, "no samples"
        await asyncio.sleep(0.005)


def test_the_lag_is_sampled_every_half_second() -> None:
    assert LAG_METRIC == "text_extraction_event_loop_lag_seconds"
    assert SAMPLE_SECONDS == 0.5


@scenario
async def test_a_blocked_loop_shows_as_lag() -> None:
    registry = CollectorRegistry()
    lag = LoopLag(registry, interval=0.01)
    lag.start()
    try:
        await until_samples(registry, 3)
        before = registry.get_sample_value(SUM) or 0.0
        time.sleep(0.3)  # noqa: ASYNC251 - the point: hold the loop like a CPU-bound step
        await until_samples(registry, samples(registry) + 1)
        assert (registry.get_sample_value(SUM) or 0.0) - before >= 0.25
        over = registry.get_sample_value(f"{LAG_METRIC}_bucket", {"le": "0.1"}) or 0.0
        assert samples(registry) - over >= 1  # one sample above 100 ms
    finally:
        await lag.stop()
    assert not lag.running


@scenario
async def test_an_idle_loop_has_next_to_no_lag() -> None:
    registry = CollectorRegistry()
    lag = LoopLag(registry, interval=0.01)
    lag.start()
    assert lag.running
    await until_samples(registry, 5)
    await lag.stop()

    under = registry.get_sample_value(f"{LAG_METRIC}_bucket", {"le": "0.1"})
    assert under == samples(registry)  # every sample under 100 ms


@scenario
async def test_stop_before_start_and_twice_is_harmless() -> None:
    lag = LoopLag(CollectorRegistry())
    await lag.stop()
    lag.start()
    await lag.stop()
    await lag.stop()
    assert not lag.running


def test_two_monitors_share_one_registry() -> None:
    """Several workers (tests) may share a registry, as the other worker metrics do."""
    registry = CollectorRegistry()
    LoopLag(registry)
    LoopLag(registry)
    assert LAG_METRIC in registry._names_to_collectors


def test_the_lag_is_on_the_internal_port_and_not_in_the_openapi_spec() -> None:
    registry = CollectorRegistry()
    LoopLag(registry)
    settings = make_settings()
    readiness = Readiness()
    readiness.mark_listening()
    internal_app = create_internal_app(settings, FakeAsyncRedis(), registry, readiness=readiness)
    with TestClient(internal_app) as internal:
        metrics = internal.get("/metrics")

    assert metrics.status_code == 200
    assert f"# TYPE {LAG_METRIC} histogram" in metrics.text
    with TestClient(create_public_app(settings, FakeAsyncRedis(), registry)) as public:
        spec = public.get("/openapi.json")
        assert public.get("/metrics").status_code == 404
    assert spec.status_code == 200
    assert LAG_METRIC not in spec.text
    assert "/metrics" not in spec.json()["paths"]
