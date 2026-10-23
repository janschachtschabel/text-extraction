import asyncio
import os
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from email.utils import format_datetime

import pytest
from fakeredis import FakeAsyncRedis

from text_extraction.outbound import (
    CircuitOpen,
    DomainGate,
    OutboundBusy,
    parse_retry_after,
)
from text_extraction.settings import Settings

START = 1_000_000.0
HOST = "example.com"


class FakeTime:
    """Injected clock and sleep: sleeping moves the clock, nothing really waits."""

    def __init__(self) -> None:
        self.now = START
        self.sleeps: list[float] = []
        self.on_sleep: Callable[[], Awaitable[None]] | None = None

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            hook, self.on_sleep = self.on_sleep, None
            await hook()
        await asyncio.sleep(0)


def make_gate(fake_time: FakeTime, valkey: FakeAsyncRedis, **overrides: object) -> DomainGate:
    settings = Settings(auth_mode="none", **overrides)  # type: ignore[arg-type]
    return DomainGate(valkey, settings, clock=fake_time.clock, sleep=fake_time.sleep)


async def inflight(valkey: FakeAsyncRedis, host: str = HOST) -> int:
    value = await valkey.get(f"out:inflight:{host}")
    return int(value) if value is not None else 0


def test_concurrency_limit_per_host() -> None:
    async def scenario() -> tuple[float, int, int]:
        fake_time = FakeTime()
        valkey = FakeAsyncRedis()
        gate = make_gate(
            fake_time,
            valkey,
            outbound_max_concurrency_per_host=2,
            outbound_min_delay_ms=0,
            outbound_max_wait_seconds=1,
        )
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(gate.slot(HOST))
            await stack.enter_async_context(gate.slot(HOST))
            held = await inflight(valkey)
            with pytest.raises(OutboundBusy) as busy:
                async with gate.slot(HOST):
                    pytest.fail("a third request must not get in while two are held")
            after_refusal = await inflight(valkey)
        return busy.value.retry_after, held, after_refusal

    retry_after, held, after_refusal = asyncio.run(scenario())

    assert held == 2
    assert after_refusal == 2  # the refused request gave its lease back
    assert retry_after > 0


def test_waiting_request_gets_in_when_a_lease_is_released() -> None:
    async def scenario() -> list[float]:
        fake_time = FakeTime()
        valkey = FakeAsyncRedis()
        gate = make_gate(
            fake_time,
            valkey,
            outbound_max_concurrency_per_host=1,
            outbound_min_delay_ms=0,
            outbound_max_wait_seconds=5,
        )
        holder = gate.slot(HOST)
        await holder.__aenter__()

        async def release() -> None:
            await holder.__aexit__(None, None, None)

        fake_time.on_sleep = release  # the holder finishes during the waiter's first sleep
        async with gate.slot(HOST):
            assert await inflight(valkey) == 1
        assert await inflight(valkey) == 0
        return fake_time.sleeps

    sleeps = asyncio.run(scenario())

    assert len(sleeps) == 1


def test_min_delay_spaces_requests() -> None:
    async def scenario() -> tuple[list[float], float]:
        fake_time = FakeTime()
        gate = make_gate(fake_time, FakeAsyncRedis(), outbound_min_delay_ms=1000)
        for _ in range(3):
            async with gate.slot(HOST):
                pass
        return fake_time.sleeps, fake_time.now - START

    sleeps, elapsed = asyncio.run(scenario())

    assert sleeps == [1.0, 1.0]  # the first request starts at once
    assert elapsed == 2.0


def test_concurrent_requests_get_distinct_slots() -> None:
    async def scenario() -> list[float]:
        fake_time = FakeTime()
        waits: list[float] = []

        async def record_only(seconds: float) -> None:
            waits.append(seconds)  # the clock stays put, so all three decide at the same time

        gate = DomainGate(
            FakeAsyncRedis(),
            Settings(
                auth_mode="none",
                outbound_max_concurrency_per_host=3,
                outbound_min_delay_ms=1000,
            ),
            clock=fake_time.clock,
            sleep=record_only,
        )

        async def one() -> None:
            async with gate.slot(HOST):
                pass

        await asyncio.gather(one(), one(), one())
        return waits

    assert sorted(asyncio.run(scenario())) == [1.0, 2.0]


@pytest.mark.parametrize(
    ("crawl_delay", "expected_gap"),
    [
        (5.0, 5.0),  # a robots.txt Crawl-delay above the minimum wins
        (60.0, 10.0),  # ... but is capped
        (0.2, 1.0),  # below the minimum it changes nothing
        (None, 1.0),
    ],
)
def test_crawl_delay_used_and_capped(crawl_delay: float | None, expected_gap: float) -> None:
    async def scenario() -> list[float]:
        fake_time = FakeTime()
        gate = make_gate(
            fake_time,
            FakeAsyncRedis(),
            outbound_min_delay_ms=1000,
            outbound_max_crawl_delay_seconds=10,
            outbound_max_wait_seconds=30,
        )
        for _ in range(2):
            async with gate.slot(HOST, crawl_delay):
                pass
        return fake_time.sleeps

    assert asyncio.run(scenario()) == [expected_gap]


def test_wait_longer_than_max_raises_busy() -> None:
    async def scenario() -> tuple[float, list[float], float]:
        fake_time = FakeTime()
        valkey = FakeAsyncRedis()
        gate = make_gate(
            fake_time, valkey, outbound_min_delay_ms=1000, outbound_max_wait_seconds=0.5
        )
        async with gate.slot(HOST):
            pass
        with pytest.raises(OutboundBusy) as busy:
            async with gate.slot(HOST):
                pytest.fail("the slot is 1 s away, more than the 0.5 s allowed")
        sleeps_after_refusal = list(fake_time.sleeps)
        assert await inflight(valkey) == 0
        # The refusal did not use up the slot: once the second has passed, the next goes at once.
        fake_time.now += 1.0
        async with gate.slot(HOST):
            pass
        return busy.value.retry_after, sleeps_after_refusal, fake_time.now - START

    retry_after, sleeps_after_refusal, elapsed = asyncio.run(scenario())

    assert retry_after == pytest.approx(1.0)
    assert sleeps_after_refusal == []
    assert elapsed == 1.0


def test_429_with_retry_after_sets_cooldown() -> None:
    async def scenario() -> tuple[float, float, float]:
        fake_time = FakeTime()
        gate = make_gate(fake_time, FakeAsyncRedis(), outbound_min_delay_ms=0)
        await gate.record(HOST, status=429, retry_after=12.0)
        with pytest.raises(CircuitOpen) as during:
            async with gate.slot(HOST):
                pytest.fail("the host is cooling down")
        fake_time.now += 11.0
        with pytest.raises(CircuitOpen) as nearly:
            async with gate.slot(HOST):
                pytest.fail("the host is still cooling down")
        fake_time.now += 1.5
        async with gate.slot(HOST):
            pass
        return during.value.retry_after, nearly.value.retry_after, fake_time.now - START

    first, second, elapsed = asyncio.run(scenario())

    assert first == pytest.approx(12.0)
    assert second == pytest.approx(1.0)
    assert elapsed == 12.5


def test_retry_after_is_capped_at_the_max_backoff() -> None:
    async def scenario() -> float:
        gate = make_gate(FakeTime(), FakeAsyncRedis(), outbound_max_backoff_seconds=300)
        await gate.record(HOST, status=429, retry_after=86_400.0)
        with pytest.raises(CircuitOpen) as info:
            async with gate.slot(HOST):
                pytest.fail("the host is cooling down")
        return info.value.retry_after

    assert asyncio.run(scenario()) == pytest.approx(300.0)


def test_retry_after_http_date() -> None:
    now = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC).timestamp()
    in_90_seconds = format_datetime(datetime.fromtimestamp(now + 90, UTC), usegmt=True)
    an_hour_ago = format_datetime(datetime.fromtimestamp(now - 3600, UTC), usegmt=True)

    assert parse_retry_after(in_90_seconds, now=now) == pytest.approx(90.0)
    assert parse_retry_after(an_hour_ago, now=now) == 0.0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("120", 120.0),
        (" 7 ", 7.0),
        ("0", 0.0),
        ("-5", 0.0),
        (None, None),
        ("", None),
        ("soon", None),
        ("1.5", None),
        ("Fri, 99 Foo 2026 99:99:99 GMT", None),
    ],
)
def test_parse_retry_after_seconds_and_garbage(value: str | None, expected: float | None) -> None:
    assert parse_retry_after(value, now=START) == expected


def test_503_without_retry_after_uses_default_backoff() -> None:
    async def scenario() -> tuple[float, float]:
        fake_time = FakeTime()
        gate = make_gate(fake_time, FakeAsyncRedis(), outbound_backoff_seconds=30)
        await gate.record(HOST, status=503, retry_after=None)
        with pytest.raises(CircuitOpen) as info:
            async with gate.slot(HOST):
                pytest.fail("the host is cooling down")
        fake_time.now += 31.0
        async with gate.slot(HOST):
            pass
        return info.value.retry_after, fake_time.now - START

    retry_after, _ = asyncio.run(scenario())

    assert retry_after == pytest.approx(30.0)


def test_circuit_opens_after_threshold_and_closes_after_timeout() -> None:
    async def scenario() -> float:
        fake_time = FakeTime()
        gate = make_gate(
            fake_time,
            FakeAsyncRedis(),
            circuit_failure_threshold=3,
            circuit_open_seconds=60,
            outbound_min_delay_ms=0,
        )
        await gate.record(HOST, status=None, retry_after=None)  # connection failure
        await gate.record(HOST, status=500, retry_after=None)
        async with gate.slot(HOST):  # two failures: still closed
            pass
        await gate.record(HOST, status=502, retry_after=None)
        with pytest.raises(CircuitOpen) as info:
            async with gate.slot(HOST):
                pytest.fail("the circuit is open")
        fake_time.now += 59.0
        with pytest.raises(CircuitOpen):
            async with gate.slot(HOST):
                pytest.fail("the circuit is still open")
        fake_time.now += 2.0
        async with gate.slot(HOST):  # closed again
            pass
        # A closed circuit starts counting from zero.
        await gate.record(HOST, status=500, retry_after=None)
        async with gate.slot(HOST):
            pass
        return info.value.retry_after

    assert asyncio.run(scenario()) == pytest.approx(60.0)


@pytest.mark.parametrize("good_status", [200, 301, 404])
def test_success_resets_failures(good_status: int) -> None:
    async def scenario() -> None:
        gate = make_gate(
            FakeTime(), FakeAsyncRedis(), circuit_failure_threshold=3, outbound_min_delay_ms=0
        )
        await gate.record(HOST, status=500, retry_after=None)
        await gate.record(HOST, status=None, retry_after=None)
        await gate.record(HOST, status=good_status, retry_after=None)
        await gate.record(HOST, status=500, retry_after=None)
        await gate.record(HOST, status=500, retry_after=None)
        async with gate.slot(HOST):  # 2 failures since the reset, threshold 3
            pass

    asyncio.run(scenario())


def test_429_neither_counts_as_failure_nor_resets_the_count() -> None:
    async def scenario() -> None:
        fake_time = FakeTime()
        gate = make_gate(
            fake_time, FakeAsyncRedis(), circuit_failure_threshold=2, outbound_min_delay_ms=0
        )
        await gate.record(HOST, status=500, retry_after=None)
        await gate.record(HOST, status=429, retry_after=0.0)  # no pause, no change to the count
        await gate.record(HOST, status=500, retry_after=None)
        with pytest.raises(CircuitOpen):
            async with gate.slot(HOST):
                pytest.fail("two failures with a 429 between them open the circuit")

    asyncio.run(scenario())


def test_lease_released_on_exception() -> None:
    async def scenario() -> tuple[int, int]:
        valkey = FakeAsyncRedis()
        gate = make_gate(FakeTime(), valkey, outbound_min_delay_ms=0)
        held = 0

        async def failing_fetch() -> None:
            nonlocal held
            async with gate.slot(HOST):
                held = await inflight(valkey)
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await failing_fetch()
        return held, await inflight(valkey)

    assert asyncio.run(scenario()) == (1, 0)


def test_lease_has_a_safety_ttl() -> None:
    async def scenario() -> int:
        valkey = FakeAsyncRedis()
        gate = make_gate(FakeTime(), valkey, outbound_min_delay_ms=0)
        async with gate.slot(HOST):
            return int(await valkey.ttl(f"out:inflight:{HOST}"))

    assert 100 < asyncio.run(scenario()) <= 120


def test_hosts_are_independent() -> None:
    async def scenario() -> list[float]:
        fake_time = FakeTime()
        gate = make_gate(
            fake_time,
            FakeAsyncRedis(),
            outbound_max_concurrency_per_host=1,
            circuit_failure_threshold=1,
            outbound_min_delay_ms=1000,
        )
        await gate.record("broken.example", status=500, retry_after=None)
        await gate.record("slow.example", status=429, retry_after=60.0)
        async with gate.slot("busy.example"):  # holds the only lease of its host
            async with gate.slot(HOST):
                pass
            async with gate.slot("other.example"):
                pass
        return fake_time.sleeps

    assert asyncio.run(scenario()) == []  # nobody waited for another host


def test_hosts_are_lower_cased_idna() -> None:
    async def scenario() -> set[str]:
        valkey = FakeAsyncRedis()
        gate = make_gate(FakeTime(), valkey, circuit_failure_threshold=1)
        await gate.record("Bücher.Example", status=500, retry_after=None)
        keys = {os.fsdecode(key) for key in await valkey.keys("out:*")}
        with pytest.raises(CircuitOpen):
            async with gate.slot("xn--bcher-kva.example"):
                pytest.fail("the same host in another spelling")
        return keys

    assert asyncio.run(scenario()) == {"out:circuit:xn--bcher-kva.example"}
