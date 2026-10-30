import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis, FakeServer

from text_extraction.envelope import ErrorCode
from text_extraction.fetch import FetchError
from text_extraction.outbound import (
    CircuitOpen,
    DomainGate,
    HostTurn,
    OutboundBusy,
    parse_retry_after,
    record_fetch_failure,
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
    return int(await valkey.zcard(f"out:inflight:{{{host}}}"))


async def settle() -> None:
    """Let background tasks (releases that outlive a cancelled caller) finish."""
    for _ in range(20):
        await asyncio.sleep(0)


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
    async def scenario() -> tuple[list[float], list[float]]:
        # The clock moves only once all three sleep, so all three book at the same moment.
        clock = VirtualClock()
        gate = DomainGate(
            FakeAsyncRedis(),
            Settings(
                auth_mode="none",
                outbound_max_concurrency_per_host=3,
                outbound_min_delay_ms=1000,
            ),
            clock=clock.clock,
            sleep=clock.sleep,
        )
        starts: list[float] = []

        async def one() -> None:
            async with gate.slot(HOST):
                starts.append(clock.now - START)

        await clock.run(*(asyncio.create_task(one()) for _ in range(3)))
        return clock.sleeps, starts

    waits, starts = asyncio.run(scenario())

    assert sorted(waits) == [1.0, 2.0]
    assert starts == [0.0, 1.0, 2.0]


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


def test_a_shorter_pause_never_replaces_a_longer_one() -> None:
    async def scenario() -> float:
        gate = make_gate(FakeTime(), FakeAsyncRedis())
        await gate.record(HOST, status=429, retry_after=60.0)
        await gate.record(HOST, status=503, retry_after=10.0)
        with pytest.raises(CircuitOpen) as info:
            async with gate.slot(HOST):
                pytest.fail("the host is cooling down")
        return info.value.retry_after

    assert asyncio.run(scenario()) == pytest.approx(60.0)


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
        ("1" + "0" * 400, None),  # a header the target controls must never crash us
        ("1" * 5000, None),
        ("\u0663\u0664", None),  # Arabic-Indic digits are no delta-seconds
        ("9999999999", 9999999999.0),
        ("Fri, 99 Foo 2026 99:99:99 GMT", None),
    ],
)
def test_parse_retry_after_seconds_and_garbage(value: str | None, expected: float | None) -> None:
    assert parse_retry_after(value, now=START) == expected


def test_503_without_retry_after_uses_default_backoff() -> None:
    async def scenario() -> float:
        fake_time = FakeTime()
        gate = make_gate(fake_time, FakeAsyncRedis(), outbound_backoff_seconds=30)
        await gate.record(HOST, status=503, retry_after=None)
        with pytest.raises(CircuitOpen) as info:
            async with gate.slot(HOST):
                pytest.fail("the host is cooling down")
        fake_time.now += 31.0
        async with gate.slot(HOST):  # the pause is over after 30 s
            pass
        return info.value.retry_after

    assert asyncio.run(scenario()) == pytest.approx(30.0)


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
        async with gate.slot(HOST):  # the circuit is still closed after one failure and a 429
            pass
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


def test_lease_has_a_safety_expiry() -> None:
    async def scenario() -> tuple[float, int]:
        valkey = FakeAsyncRedis()
        gate = make_gate(FakeTime(), valkey, outbound_min_delay_ms=0)
        async with gate.slot(HOST):
            key = f"out:inflight:{{{HOST}}}"
            members = await valkey.zrange(key, 0, -1, withscores=True)
            return float(members[0][1]), int(await valkey.ttl(key))

    expires_at, ttl = asyncio.run(scenario())

    assert expires_at == START + 120
    assert ttl > 100  # the key outlives its last lease; it is only cleanup


@pytest.mark.parametrize(
    ("overrides", "expiry"),
    [
        ({}, 120),  # the defaults' deadlines (25, 30, 60 s) are well within it
        ({"browser_unblock_timeout_seconds": 180}, 210),
        ({"browser_timeout_seconds": 120}, 150),
        ({"extract_timeout_seconds": 300}, 330),
    ],
)
def test_a_lease_outlives_the_longest_turn(overrides: dict[str, float], expiry: float) -> None:
    # M-4: a render holds its turn for its whole deadline (B3); a lease that ran out earlier
    # would let another turn on the same host in, past the per-host concurrency limit.
    async def scenario() -> float:
        valkey = FakeAsyncRedis()
        gate = make_gate(FakeTime(), valkey, outbound_min_delay_ms=0, **overrides)
        async with gate.slot(HOST):
            members = await valkey.zrange(f"out:inflight:{{{HOST}}}", 0, -1, withscores=True)
            return float(members[0][1])

    assert asyncio.run(scenario()) == START + expiry


def test_leaked_lease_frees_the_slot_after_its_expiry() -> None:
    async def scenario() -> tuple[int, int]:
        fake_time = FakeTime()
        valkey = FakeAsyncRedis()
        gate = make_gate(
            fake_time,
            valkey,
            outbound_max_concurrency_per_host=1,
            outbound_min_delay_ms=0,
            outbound_max_wait_seconds=1,
        )
        leaker = gate.slot(HOST)
        await leaker.__aenter__()  # never exited: the pod died
        with pytest.raises(OutboundBusy):
            async with gate.slot(HOST):
                pytest.fail("the only lease is held")
        # Refused polls must not keep the dead lease alive.
        fake_time.now += 117.0
        with pytest.raises(OutboundBusy):
            async with gate.slot(HOST):
                pytest.fail("the lease has not expired yet")
        fake_time.now += 2.0
        async with gate.slot(HOST):
            held = await inflight(valkey)
        return held, await inflight(valkey)

    assert asyncio.run(scenario()) == (1, 0)  # only the new lease, which was released


def test_cancel_during_the_spacing_wait_releases_the_lease() -> None:
    async def scenario() -> tuple[int, int]:
        fake_time = FakeTime()
        valkey = FakeAsyncRedis()
        waiting = asyncio.Event()

        async def stuck_sleep(seconds: float) -> None:
            waiting.set()
            await asyncio.Event().wait()

        gate = DomainGate(
            valkey,
            Settings(auth_mode="none", outbound_min_delay_ms=1000),
            clock=fake_time.clock,
            sleep=stuck_sleep,
        )
        async with gate.slot(HOST):
            pass

        async def second() -> None:
            async with gate.slot(HOST):
                pytest.fail("cancelled before its turn")

        task = asyncio.create_task(second())
        await waiting.wait()
        held = await inflight(valkey)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await settle()
        return held, await inflight(valkey)

    assert asyncio.run(scenario()) == (1, 0)


class StallingRedis(FakeAsyncRedis):
    """Runs the lease script, then never answers: the caller is cancelled mid-flight."""

    def __init__(self) -> None:
        super().__init__()
        self.stall = asyncio.Event()
        self.stalled = asyncio.Event()

    async def evalsha(self, sha: str, numkeys: int, *keys_and_args: Any) -> Any:
        result = await super().evalsha(sha, numkeys, *keys_and_args)
        if "inflight" in str(keys_and_args[0]) and not self.stall.is_set():
            self.stalled.set()
            await asyncio.Event().wait()
        return result


def test_cancel_during_the_acquire_releases_the_lease() -> None:
    async def scenario() -> tuple[int, int]:
        valkey = StallingRedis()
        gate = make_gate(FakeTime(), valkey, outbound_min_delay_ms=0)

        async def request() -> None:
            async with gate.slot(HOST):
                pytest.fail("cancelled before the answer arrived")

        task = asyncio.create_task(request())
        await valkey.stalled.wait()
        valkey.stall.set()  # later calls behave normally
        held = await inflight(valkey)  # the lease exists in Valkey, the caller never saw it
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await settle()
        return held, await inflight(valkey)

    assert asyncio.run(scenario()) == (1, 0)


def test_gates_of_two_pods_share_the_concurrency_limit() -> None:
    async def scenario() -> list[int]:
        fake_time = FakeTime()
        server = FakeServer()
        settings = {
            "outbound_max_concurrency_per_host": 2,
            "outbound_min_delay_ms": 0,
            "outbound_max_wait_seconds": 0,
        }
        pod_a = make_gate(fake_time, FakeAsyncRedis(server=server), **settings)
        pod_b = make_gate(fake_time, FakeAsyncRedis(server=server), **settings)
        refused = []
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(pod_a.slot(HOST))
            await stack.enter_async_context(pod_b.slot(HOST))
            for pod in (pod_a, pod_b):
                with pytest.raises(OutboundBusy):
                    async with pod.slot(HOST):
                        pytest.fail("two requests are in flight across the pods")
                refused.append(1)
        async with pod_b.slot(HOST):  # everything released: a pod may go again
            refused.append(2)
        return refused

    assert asyncio.run(scenario()) == [1, 1, 2]


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

    assert asyncio.run(scenario()) == {"out:circuit:{xn--bcher-kva.example}"}


class VirtualClock:
    """A clock that moves only when every task waits on a sleep: then it jumps to the earliest
    wake-up. ``late`` makes the n-th sleep (counted from 0) wake that many seconds late, as a
    stalled event loop would."""

    def __init__(self, late: dict[int, float] | None = None) -> None:
        self.now = START
        self.sleeps: list[float] = []
        self._late = late or {}
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        extra = self._late.get(len(self.sleeps), 0.0)
        self.sleeps.append(seconds)
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.now + seconds + extra, len(self.sleeps), future))
        await future

    async def run(self, *tasks: asyncio.Task[None]) -> None:
        """Run the tasks to the end, moving the clock whenever they all sleep."""
        while not all(task.done() for task in tasks):
            for _ in range(50):  # let every task get as far as it can without time passing
                await asyncio.sleep(0)
            if self._sleepers:
                self._sleepers.sort(key=lambda sleeper: sleeper[:2])
                wake_at, _, future = self._sleepers.pop(0)
                self.now = max(self.now, wake_at)
                future.set_result(None)
        for task in tasks:
            task.result()


def _spaced_starts(late: dict[int, float]) -> tuple[list[float], list[float]]:
    """Three requests, the last two concurrent; returns their real starts and the sleeps."""

    async def scenario() -> tuple[list[float], list[float]]:
        clock = VirtualClock(late)
        gate = DomainGate(
            FakeAsyncRedis(),
            Settings(
                auth_mode="none",
                outbound_max_concurrency_per_host=2,
                outbound_min_delay_ms=1000,
                outbound_max_wait_seconds=10,
            ),
            clock=clock.clock,
            sleep=clock.sleep,
        )
        starts: list[float] = []

        async def request() -> None:
            async with gate.slot(HOST):
                await gate.before_request(HOST)
                starts.append(clock.now - START)

        await clock.run(asyncio.create_task(request()))
        second = asyncio.create_task(request())
        await asyncio.sleep(0)  # the second books its turn first
        await clock.run(second, asyncio.create_task(request()))
        return starts, clock.sleeps

    return asyncio.run(scenario())


def test_a_late_waker_does_not_shorten_the_gap_after_it() -> None:
    # The second request's sleep wakes 0.5 s late: its real start is 1.5 s. The third booked
    # 2.0 s, but must not start before 1.5 s + the 1 s delay.
    starts, _ = _spaced_starts({0: 0.5})

    assert starts == [0.0, 1.5, 2.5]


def test_requests_on_time_wait_no_longer_than_their_turn() -> None:
    starts, sleeps = _spaced_starts({})

    assert starts == [0.0, 1.0, 2.0]
    assert sleeps == [1.0, 2.0]  # no extra wait once a booked turn has come


class CountingRedis(FakeAsyncRedis):
    """Counts the script calls whose first key is ``out:starts``: the start confirmations and
    the send reports."""

    def __init__(self) -> None:
        super().__init__()
        self.confirms = 0

    async def evalsha(self, sha: str, numkeys: int, *keys_and_args: Any) -> Any:
        if str(keys_and_args[0]).startswith("out:starts:"):
            self.confirms += 1
        return await super().evalsha(sha, numkeys, *keys_and_args)


def test_a_turn_that_is_due_costs_no_extra_round_trip() -> None:
    async def scenario() -> tuple[int, list[float]]:
        fake_time = FakeTime()
        valkey = CountingRedis()
        gate = make_gate(fake_time, valkey, outbound_min_delay_ms=1000)
        for n in range(3):
            if n == 1:
                valkey.confirms = 0  # the first calls also load the scripts
            async with gate.slot(HOST):
                await gate.before_request(HOST)
                await gate.mark_sent(HOST)
            fake_time.now += 5  # the next request comes after its turn
        return valkey.confirms, fake_time.sleeps

    # Per turn: the request's own confirmation and its send report, no check of the turn's own.
    assert asyncio.run(scenario()) == (4, [])


def test_a_request_sent_late_moves_the_next_start_on() -> None:
    async def scenario() -> list[float]:
        clock = VirtualClock()
        gate = DomainGate(
            FakeAsyncRedis(),
            Settings(auth_mode="none", outbound_min_delay_ms=1000, outbound_max_wait_seconds=10),
            clock=clock.clock,
            sleep=clock.sleep,
        )
        starts: list[float] = []

        async def first() -> None:
            async with gate.slot(HOST):
                await gate.before_request(HOST)
                starts.append(clock.now - START)
                await clock.sleep(0.5)  # a stall between the check and the real send
                await gate.mark_sent(HOST)

        async def second() -> None:
            async with gate.slot(HOST):
                await gate.before_request(HOST)
                starts.append(clock.now - START)

        one = asyncio.create_task(first())
        await asyncio.sleep(0)
        await clock.run(one, asyncio.create_task(second()))
        return starts

    # The first request went out at 0.5 s: the second may not start before 1.5 s.
    assert asyncio.run(scenario()) == [0.0, 1.5]


def test_mark_sent_never_moves_the_latest_start_back() -> None:
    async def scenario() -> list[float]:
        fake_time = FakeTime()
        valkey = FakeAsyncRedis()
        gate = make_gate(fake_time, valkey, outbound_min_delay_ms=1000)
        fake_time.now = START + 5
        await gate.mark_sent(HOST)
        fake_time.now = START + 2  # another pod's late report of an earlier send
        await gate.mark_sent(HOST)
        fake_time.now = START + 5.5
        async with gate.slot(HOST):
            pass
        return fake_time.sleeps

    assert asyncio.run(scenario()) == [0.5]  # spaced from 5 s, not from 2 s


def test_the_wait_after_the_turn_respects_the_max_wait() -> None:
    async def scenario() -> tuple[list[float], float]:
        fake_time = FakeTime()
        valkey = FakeAsyncRedis()
        gate = make_gate(
            fake_time,
            valkey,
            outbound_min_delay_ms=200,
            outbound_max_crawl_delay_seconds=30,
            outbound_max_wait_seconds=1,
        )
        async with gate.slot(HOST):  # the first turn on the host, booked without a Crawl-delay
            await gate.before_request(HOST)
            await gate.mark_sent(HOST)
        fake_time.now += 1
        # The next request knows the Crawl-delay of 20 s: its turn is due at once by the booking,
        # but the real-start check would wait 19 s more, beyond the 1 s it may wait.
        with pytest.raises(OutboundBusy) as busy:
            async with gate.slot(HOST, 20.0):
                pytest.fail("the turn is 20 s away")
        return fake_time.sleeps, busy.value.retry_after

    sleeps, retry_after = asyncio.run(scenario())

    assert sleeps == []
    assert retry_after == pytest.approx(19.0)


# --- R25: spacing between the requests of different turns, back to back within a turn ---------


def r25_gate(clock: VirtualClock, **overrides: object) -> DomainGate:
    values: dict[str, object] = {
        "auth_mode": "none",
        "outbound_max_concurrency_per_host": 2,
        "outbound_min_delay_ms": 1000,
        "outbound_max_wait_seconds": 10,
    }
    values.update(overrides)
    return DomainGate(
        FakeAsyncRedis(),
        Settings(**values),  # type: ignore[arg-type]
        clock=clock.clock,
        sleep=clock.sleep,
    )


async def send(gate: DomainGate, clock: VirtualClock, starts: list[float], took: float) -> None:
    """One request in the current turn: confirmed, sent, answered after ``took`` seconds."""
    await gate.before_request(HOST)
    starts.append(round(clock.now - START, 6))
    await gate.mark_sent(HOST)
    if took:
        await clock.sleep(took)


def test_the_requests_of_one_turn_go_back_to_back() -> None:
    # A cold origin: three compliance files and the page, each answered after 0.3 s.
    async def scenario() -> tuple[list[float], list[float]]:
        clock = VirtualClock()
        gate = r25_gate(clock)
        starts: list[float] = []

        async def turn() -> None:
            async with gate.slot(HOST):
                for _ in range(4):
                    await send(gate, clock, starts, 0.3)

        await clock.run(asyncio.create_task(turn()))
        return starts, clock.sleeps

    starts, sleeps = asyncio.run(scenario())

    # First contact costs no spacing: the delay lies only between different turns.
    assert starts == [0.0, 0.3, 0.6, 0.9]
    assert sleeps == [0.3, 0.3, 0.3, 0.3]


def test_another_turns_request_keeps_the_spacing_to_them() -> None:
    async def scenario() -> tuple[list[float], list[float]]:
        clock = VirtualClock()
        gate = r25_gate(clock)
        first: list[float] = []
        second: list[float] = []

        async def turn_a() -> None:
            async with gate.slot(HOST):
                await send(gate, clock, first, 0.3)
                await send(gate, clock, first, 0.3)
                await send(gate, clock, first, 0.0)

        async def turn_b() -> None:
            async with gate.slot(HOST):  # booked 1.0 s after A's turn
                await send(gate, clock, second, 0.0)

        a = asyncio.create_task(turn_a())
        await asyncio.sleep(0)
        await clock.run(a, asyncio.create_task(turn_b()))
        return first, second

    first, second = asyncio.run(scenario())

    assert first == [0.0, 0.3, 0.6]
    assert second == [1.6]  # 1 s after A's latest request, not after A's turn start


def test_a_turn_waits_for_a_request_another_turn_sent_in_between() -> None:
    async def scenario() -> tuple[list[float], list[float]]:
        clock = VirtualClock()
        gate = r25_gate(clock)
        first: list[float] = []
        second: list[float] = []

        async def turn_a() -> None:
            async with gate.slot(HOST):
                await send(gate, clock, first, 1.2)  # a slow answer
                await send(gate, clock, first, 0.0)

        async def turn_b() -> None:
            async with gate.slot(HOST):
                await send(gate, clock, second, 0.0)

        a = asyncio.create_task(turn_a())
        await asyncio.sleep(0)
        await clock.run(a, asyncio.create_task(turn_b()))
        return first, second

    first, second = asyncio.run(scenario())

    assert second == [1.0]
    assert first == [0.0, 2.0]  # B sent at 1.0 s, so A's next request waits until 2.0 s


def test_a_late_report_never_moves_a_turns_start_back() -> None:
    async def scenario() -> tuple[float | None, list[float]]:
        clock = VirtualClock()
        gate = r25_gate(clock)
        valkey = gate._valkey
        starts: list[float] = []
        score: float | None = None

        async def turn_a() -> None:
            nonlocal score
            async with gate.slot(HOST):
                clock.now = START + 5
                await send(gate, clock, starts, 0.0)
                clock.now = START + 2  # a late report of an earlier send, as from a slow pod
                await gate.mark_sent(HOST)
                key = f"out:starts:{{{HOST}}}"
                members = cast(
                    list[tuple[bytes, float]], await valkey.zrange(key, 0, -1, withscores=True)
                )
                (score,) = [value for _, value in members]

        async def turn_b() -> None:
            async with gate.slot(HOST):
                await send(gate, clock, starts, 0.0)

        await clock.run(asyncio.create_task(turn_a()))
        clock.now = START + 5.5
        await clock.run(asyncio.create_task(turn_b()))
        return score, starts

    score, starts = asyncio.run(scenario())

    assert score == START + 5
    assert starts == [5.0, 6.0]  # spaced from 5 s, not from 2 s


def test_a_request_outside_any_turn_counts_for_every_turn() -> None:
    async def scenario() -> list[float]:
        clock = VirtualClock()
        gate = r25_gate(clock)
        starts: list[float] = []
        await gate.mark_sent(HOST)  # sent without a turn: recorded under a fresh token

        async def turn() -> None:
            async with gate.slot(HOST):
                await send(gate, clock, starts, 0.0)

        await clock.run(asyncio.create_task(turn()))
        return starts

    assert asyncio.run(scenario()) == [1.0]


def test_a_request_to_another_host_is_not_confirmed_in_this_turn() -> None:
    async def scenario() -> list[float]:
        clock = VirtualClock()
        gate = r25_gate(clock)

        async def turn() -> None:
            async with gate.slot(HOST):
                await gate.before_request("other.example")  # not this turn's host: no wait

        await clock.run(asyncio.create_task(turn()))
        return clock.sleeps

    assert asyncio.run(scenario()) == []


def test_the_confirm_before_a_request_respects_the_max_wait() -> None:
    async def scenario() -> tuple[float, list[float]]:
        clock = VirtualClock()
        gate = r25_gate(
            clock,
            outbound_min_delay_ms=200,
            outbound_max_wait_seconds=1,
            outbound_max_crawl_delay_seconds=30,
        )
        starts: list[float] = []
        retry_after = 0.0

        async def turn_a() -> None:
            async with gate.slot(HOST):
                await clock.sleep(0.3)
                await send(gate, clock, starts, 0.0)

        async def turn_b() -> None:
            nonlocal retry_after
            async with gate.slot(HOST, 20.0):  # due at 0.2 s: nothing was sent yet
                await clock.sleep(0.3)
                # A sent at 0.3 s; with a Crawl-delay of 20 s this request would wait 19.8 s.
                with pytest.raises(OutboundBusy) as busy:
                    await gate.before_request(HOST)
                retry_after = busy.value.retry_after

        a = asyncio.create_task(turn_a())
        await asyncio.sleep(0)
        await clock.run(a, asyncio.create_task(turn_b()))
        return retry_after, starts

    retry_after, starts = asyncio.run(scenario())

    assert starts == [0.3]
    assert retry_after == pytest.approx(19.8)


def test_the_starts_set_stays_small() -> None:
    async def scenario() -> int:
        clock = VirtualClock()
        gate = r25_gate(clock, outbound_max_crawl_delay_seconds=5)
        starts: list[float] = []

        async def turn() -> None:
            async with gate.slot(HOST):
                await send(gate, clock, starts, 0.0)

        for _ in range(60):
            await clock.run(asyncio.create_task(turn()))
        return int(await gate._valkey.zcard(f"out:starts:{{{HOST}}}"))

    # One start per second over 60 s; only the last few delays' worth are kept.
    assert asyncio.run(scenario()) <= 12


# --- the turn that moves along a redirect chain, and the host failure rule ----------------------


class NotingGate:
    """Notes the turns taken and given back, and each outcome recorded."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, float | None]] = []
        self.records: list[tuple[str, int | None]] = []

    @asynccontextmanager
    async def slot(self, host: str, crawl_delay: float | None = None) -> AsyncIterator[None]:
        self.events.append(("take", host, crawl_delay))
        try:
            yield
        finally:
            self.events.append(("give back", host, None))

    async def record(self, host: str, *, status: int | None, retry_after: float | None) -> None:
        self.records.append((host, status))


def test_host_turn_keeps_its_host_and_gives_back_before_another() -> None:
    async def scenario() -> tuple[NotingGate, list[str | None], bool]:
        gate = NotingGate()
        turn = HostTurn(cast(DomainGate, gate))
        answering: list[str | None] = []
        await turn.take("Example.COM", None)
        answering.append(turn.answering)
        await turn.take("example.com", 2.0)  # held already: kept, no second turn
        holds = turn.holds("EXAMPLE.com")
        await turn.take("cdn.example.net", 1.0)
        answering.append(turn.answering)
        await turn.give_back()
        answering.append(turn.answering)
        await turn.give_back()  # nothing held: nothing happens
        return gate, answering, holds

    gate, answering, holds = asyncio.run(scenario())

    assert gate.events == [
        ("take", "Example.COM", None),
        ("give back", "Example.COM", None),
        ("take", "cdn.example.net", 1.0),
        ("give back", "cdn.example.net", None),
    ]
    assert answering == ["example.com", "cdn.example.net", None]
    assert holds is True


@pytest.mark.parametrize(
    ("error", "budget", "recorded"),
    [
        (FetchError(ErrorCode.TOO_LARGE, "x", upstream_status=200), 0.1, [(HOST, 200)]),
        (FetchError(ErrorCode.DNS_ERROR, "x"), 0.1, [(HOST, None)]),
        (FetchError(ErrorCode.CONNECTION_ERROR, "x"), 0.1, [(HOST, None)]),
        (FetchError(ErrorCode.TIMEOUT, "x"), 10.0, [(HOST, None)]),  # half of 20 s: fair
        (FetchError(ErrorCode.TIMEOUT, "x"), 9.9, []),  # cut short: says little about the host
        (FetchError(ErrorCode.BLOCKED_URL, "x"), 20.0, []),
        (FetchError(ErrorCode.UNSUPPORTED_CONTENT_TYPE, "x"), 20.0, []),
    ],
)
def test_record_fetch_failure_counts_only_what_says_something_about_the_host(
    error: FetchError, budget: float, recorded: list[tuple[str, int | None]]
) -> None:
    gate = NotingGate()

    asyncio.run(
        record_fetch_failure(
            cast(DomainGate, gate), HOST, error, budget=budget, fetch_timeout_seconds=20
        )
    )

    assert gate.records == recorded
