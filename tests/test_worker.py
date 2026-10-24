"""The extract worker (``worker.py``): slot loops over the fair queue, the job store and the
pipeline, with fakeredis (Lua), a fake fetcher, an injected clock and an injected sleep."""

import asyncio
import os
import signal
import socket
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from functools import wraps
from typing import Any, cast

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from prometheus_client import CollectorRegistry
from redis.exceptions import ConnectionError as RedisConnectionError
from structlog.testing import capture_logs

from tests.fakes import Answer, FakeFetcher, RefusingGate, fetched
from text_extraction.auth import Principal
from text_extraction.compliance import ComplianceChecker
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.extract_models import ExtractRequest, ExtractResult
from text_extraction.fetch import Fetcher, FetchResult, Validators
from text_extraction.fingerprint import FingerprintStore
from text_extraction.internal import Readiness, create_internal_app
from text_extraction.job_models import JobItem, JobOptions
from text_extraction.jobs import JobLost, JobStore, ResultWrite
from text_extraction.outbound import CircuitOpen, DomainGate, OutboundBusy
from text_extraction.pipeline import Pipeline
from text_extraction.result_cache import ResultCache
from text_extraction.scheduler import Claim, FairQueue, QueueItem
from text_extraction.server import _server
from text_extraction.settings import Settings
from text_extraction.worker import Worker, _serve, main

START = 1_000_000.0
OWNER = Principal(name="alice", role="user", rate_limit_per_minute=None)
OPTIONS = JobOptions(compliance="enforce")
LOST_MESSAGE = "The item was lost by a worker too often."


def scenario(test: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., None]:
    """Run an async test body on its own event loop (the suite has no async plugin)."""

    @wraps(test)
    def run(*args: Any, **kwargs: Any) -> None:
        asyncio.run(test(*args, **kwargs))

    return run


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        return self.now


class Sleeps:
    """The worker's injected sleep: records each delay, then only yields to the loop."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        await asyncio.sleep(0)


def page(host: str, n: int = 0) -> str:
    return f"https://{host}/page-{n}"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"auth_mode": "none", "outbound_min_delay_ms": 0}
    values.update(overrides)
    return Settings(**values)


def default_queue(valkey: FakeAsyncRedis, settings: Settings, clock: Clock) -> FairQueue:
    return FairQueue(valkey, settings, clock=clock)


class Harness:
    """A worker on fakeredis with the real job store, queue and pipeline (fake fetcher)."""

    def __init__(
        self,
        answers: dict[str, Answer] | None = None,
        *,
        gate: object | None = None,
        make_queue: Callable[[FakeAsyncRedis, Settings, Clock], Any] | None = None,
        pipeline: Any = None,
        random: Callable[[], float] = lambda: 0.5,  # no jitter
        **overrides: Any,
    ) -> None:
        self.valkey = FakeAsyncRedis()
        self.clock = Clock()
        self.sleeps = Sleeps()
        self.settings = make_settings(**overrides)
        self.fetcher = FakeFetcher(answers or {})
        self.registry = CollectorRegistry()
        fetcher = cast(Fetcher, self.fetcher)
        self.pipeline = pipeline or Pipeline(
            fetcher,
            cast(DomainGate, gate) if gate is not None else DomainGate(self.valkey, self.settings),
            ComplianceChecker(fetcher, self.valkey, self.settings),
            ResultCache(self.valkey, self.settings),
            FingerprintStore(self.valkey, self.settings),
            self.settings,
            self.registry,
        )
        self.store = JobStore(self.valkey, self.settings, clock=self.clock)
        self.queue = (make_queue or default_queue)(self.valkey, self.settings, self.clock)
        self.worker = Worker(
            self.settings,
            self.valkey,
            self.pipeline,
            self.store,
            self.queue,
            self.registry,
            clock=self.clock,
            sleep=self.sleeps,
            random=random,
        )

    async def submit(self, urls: list[str]) -> str:
        job_id = await self.store.create("batch", OWNER, OPTIONS, urls)
        await self.queue.enqueue(job_id, OWNER.name, urls)
        return job_id

    async def claim(self) -> Claim:
        claim: Claim = await self.queue.claim(self.lease_seconds)
        assert claim.item is not None
        return claim

    @property
    def lease_seconds(self) -> float:
        return self.settings.extract_timeout_seconds * 2 + 10

    async def items(self, job_id: str) -> list[JobItem]:
        return await self.store.items(job_id, 0, 100)

    async def completed(self, job_id: str) -> bool:
        record = await self.store.get(job_id)
        return record is not None and record.state == "completed"

    async def run_until(self, done: Callable[[], Awaitable[bool]], *, within: float = 20) -> None:
        """Run the worker's slot loops until ``done``, then stop them and wait for the end."""
        stop = asyncio.Event()
        running = asyncio.create_task(self.worker.run(stop))
        try:
            async with asyncio.timeout(within):
                while not await done():
                    assert not running.done(), "the worker stopped by itself"
                    await asyncio.sleep(0.01)
        finally:
            stop.set()
            async with asyncio.timeout(within):
                await running

    def sample(self, name: str, labels: dict[str, str] | None = None) -> float:
        return self.registry.get_sample_value(name, labels or {}) or 0.0


def outcome_count(harness: Harness, outcome: str) -> float:
    return harness.sample("text_extraction_job_items_total", {"outcome": outcome})


def error_of(item: JobItem) -> tuple[ErrorCode, bool, str]:
    assert item.result is not None
    assert item.result.status == "failed"
    assert item.result.error is not None
    error = item.result.error
    return error.code, error.retryable, error.message


async def queue_list(valkey: FakeAsyncRedis, domain: str, client: str = "alice") -> list[str]:
    raw = await valkey.lrange(f"q:{{extract}}:c:{client}:d:{domain}", 0, -1)
    return [os.fsdecode(value) for value in raw]


# --- processing --------------------------------------------------------------------------------


@scenario
async def test_worker_processes_a_batch_and_completes_the_job() -> None:
    urls = [page("example.org", n) for n in range(3)] + [page("other.example", n) for n in range(2)]
    harness = Harness({url: fetched(url) for url in urls})
    job_id = await harness.submit(urls)

    await harness.run_until(lambda: harness.completed(job_id))

    record = await harness.store.get(job_id)
    assert record is not None
    assert record.state == "completed"
    assert record.progress.success == 5
    items = await harness.items(job_id)
    assert [item.index for item in items] == [0, 1, 2, 3, 4]
    for item in items:
        assert item.result is not None
        assert item.result.status == "success"
        assert item.result.data is not None
        assert item.result.data.url == urls[item.index]
    counts = await harness.queue.counts()
    assert (counts.pending, counts.leased) == (0, 0)


@scenario
async def test_first_item_marks_the_job_running_with_the_owner_and_options() -> None:
    seen: list[tuple[ExtractRequest, Principal, str]] = []

    class Recording:
        async def run(
            self, req: ExtractRequest, principal: Principal, *, gate_refusals: str = "fail"
        ) -> Envelope[ExtractResult]:
            seen.append((req, principal, gate_refusals))
            return Envelope[ExtractResult](status="failed", error=None)

    options = JobOptions(
        mode="fast", format="text", metadata=False, links=True, compliance="report"
    )
    harness = Harness(pipeline=Recording())
    urls = [page("example.org", 0), page("example.org", 1)]
    job_id = await harness.store.create("batch", OWNER, options, urls)
    await harness.queue.enqueue(job_id, OWNER.name, urls)

    await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]

    record = await harness.store.get(job_id)
    assert record is not None
    assert record.state == "running"
    [(req, principal, gate_refusals)] = seen
    assert req == ExtractRequest(
        url=urls[0], mode="fast", format="text", metadata=False, links=True, compliance="report"
    )
    assert principal == Principal(name="alice", role="user", rate_limit_per_minute=None)
    assert gate_refusals == "raise"


@pytest.mark.parametrize(
    ("raised", "code", "retryable"),
    [
        (RuntimeError("boom"), ErrorCode.INTERNAL_ERROR, False),
        (ServiceError(ErrorCode.FORBIDDEN, "Not allowed.", 403), ErrorCode.FORBIDDEN, False),
    ],
)
@scenario
async def test_pipeline_errors_fail_the_item(
    raised: Exception, code: ErrorCode, retryable: bool
) -> None:
    class Failing:
        async def run(self, *args: Any, **kwargs: Any) -> Envelope[ExtractResult]:
            raise raised

    harness = Harness(pipeline=Failing())
    job_id = await harness.submit([page("example.org")])

    await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]

    [item] = await harness.items(job_id)
    assert error_of(item)[:2] == (code, retryable)
    assert await harness.completed(job_id)
    assert (await harness.queue.counts()).leased == 0


@scenario
async def test_expired_job_drops_the_item() -> None:
    harness = Harness()
    job_id = await harness.submit([page("example.org")])
    claim = await harness.claim()
    await harness.valkey.delete(f"job:{{{job_id}}}:meta", f"job:{{{job_id}}}:urls")

    with capture_logs() as logs:
        await harness.worker.process(claim.item)  # type: ignore[arg-type]

    assert harness.fetcher.calls == []
    counts = await harness.queue.counts()
    assert (counts.pending, counts.leased) == (0, 0)
    assert outcome_count(harness, "lost") == 1
    assert any(log["event"] == "job_item_dropped" for log in logs)


# --- deferrals ---------------------------------------------------------------------------------


@scenario
async def test_gate_refusal_defers_the_item() -> None:
    harness = Harness(gate=RefusingGate(OutboundBusy(3.0)))
    job_id = await harness.submit([page("example.org")])
    claim = await harness.claim()

    await harness.worker.process(claim.item)  # type: ignore[arg-type]

    assert await queue_list(harness.valkey, "example.org") == [f"{job_id}:0:1"]
    not_before = await harness.valkey.hget("q:{extract}:notbefore", "example.org")
    assert not_before is not None
    assert float(not_before) == pytest.approx(START + 3.0)
    [item] = await harness.items(job_id)
    assert item.state == "pending"
    record = await harness.store.get(job_id)
    assert record is not None
    assert record.progress.done == 0
    again = await harness.queue.claim(harness.lease_seconds)
    assert again.item is None
    assert again.retry_in == pytest.approx(3.0)
    assert outcome_count(harness, "deferred") == 1


@pytest.mark.parametrize(
    ("refusal", "code"),
    [(OutboundBusy(3.0), ErrorCode.RATE_LIMITED), (CircuitOpen(30.0), ErrorCode.CONNECTION_ERROR)],
)
@scenario
async def test_item_deferred_too_often_fails_with_the_gate_code(
    refusal: Exception, code: ErrorCode
) -> None:
    harness = Harness(gate=RefusingGate(refusal), job_item_max_attempts=2)
    job_id = await harness.submit([page("example.org")])

    attempts = []
    for _ in range(3):
        claim = await harness.claim()
        assert claim.item is not None
        attempts.append(claim.item.attempts)
        await harness.worker.process(claim.item)
        harness.clock.now += 60  # past the not-before of the deferral

    assert attempts == [0, 1, 2]
    [item] = await harness.items(job_id)
    error_code, retryable, _ = error_of(item)
    assert (error_code, retryable) == (code, True)
    assert item.result is not None
    assert item.result.data is not None  # the pipeline's own envelope
    assert item.result.data.url == page("example.org")
    assert await harness.completed(job_id)
    counts = await harness.queue.counts()
    assert (counts.pending, counts.leased) == (0, 0)
    assert outcome_count(harness, "deferred") == 2
    assert outcome_count(harness, code.value) == 1


# --- lost leases -------------------------------------------------------------------------------


@scenario
async def test_exhausted_items_fail_with_internal_error() -> None:
    harness = Harness(job_item_max_attempts=1)
    job_id = await harness.submit([page("example.org")])
    for _ in range(2):  # two workers die with the item: attempts 0, then 1
        await harness.claim()
        harness.clock.now += harness.lease_seconds + 1

    await harness.run_until(lambda: harness.completed(job_id))

    [item] = await harness.items(job_id)
    assert error_of(item) == (ErrorCode.INTERNAL_ERROR, True, LOST_MESSAGE)
    assert harness.fetcher.calls == []
    counts = await harness.queue.counts()
    assert (counts.pending, counts.leased) == (0, 0)
    assert outcome_count(harness, "internal_error") == 1


async def exhausted_claim(harness: Harness) -> Claim:
    """Two workers die with the only item (max attempts 1): the next claim is exhausted."""
    await harness.submit([page("example.org")])
    for _ in range(2):
        await harness.claim()
        harness.clock.now += harness.lease_seconds + 1
    claim = await harness.claim()
    assert claim.exhausted
    return claim


@scenario
async def test_exhausted_item_is_completed_when_its_job_was_lost_while_recording() -> None:
    harness = Harness(job_item_max_attempts=1)
    claim = await exhausted_claim(harness)

    async def lost(job_id: str, index: int, envelope: Envelope[ExtractResult]) -> ResultWrite:
        raise JobLost(job_id)

    harness.store.write_result = lost  # type: ignore[method-assign]
    await harness.worker.process(claim.item, exhausted=True)  # type: ignore[arg-type]

    counts = await harness.queue.counts()
    assert (counts.pending, counts.leased) == (0, 0)
    assert outcome_count(harness, "lost") == 1


@pytest.mark.parametrize("failing", ["get", "write_result"])
@pytest.mark.parametrize("raised", [RedisConnectionError("down"), TimeoutError("slow")])
@scenario
async def test_exhausted_item_stays_leased_when_valkey_fails_and_is_recorded_later(
    failing: str, raised: Exception
) -> None:
    harness = Harness(job_item_max_attempts=1)
    claim = await exhausted_claim(harness)
    job_id = claim.item.job_id  # type: ignore[union-attr]
    original = getattr(harness.store, failing)

    async def broken(*args: Any) -> Any:
        raise raised

    setattr(harness.store, failing, broken)
    with pytest.raises(type(raised)):
        await harness.worker.process(claim.item, exhausted=True)  # type: ignore[arg-type]

    counts = await harness.queue.counts()
    assert (counts.pending, counts.leased) == (0, 1)  # not completed: it comes back
    assert outcome_count(harness, "lost") == 0

    setattr(harness.store, failing, original)  # Valkey is back
    harness.clock.now += harness.lease_seconds + 1
    again = await harness.claim()
    assert again.exhausted
    await harness.worker.process(again.item, exhausted=True)  # type: ignore[arg-type]

    [item] = await harness.items(job_id)
    assert error_of(item) == (ErrorCode.INTERNAL_ERROR, True, LOST_MESSAGE)
    assert await harness.completed(job_id)
    counts = await harness.queue.counts()
    assert (counts.pending, counts.leased) == (0, 0)
    assert outcome_count(harness, "internal_error") == 1


@scenario
async def test_exhausted_item_of_an_expired_job_is_completed() -> None:
    harness = Harness(job_item_max_attempts=1)
    job_id = await harness.submit([page("example.org")])
    for _ in range(2):
        await harness.claim()
        harness.clock.now += harness.lease_seconds + 1
    claim = await harness.claim()
    await harness.valkey.delete(f"job:{{{job_id}}}:meta", f"job:{{{job_id}}}:urls")

    await harness.worker.process(claim.item, exhausted=True)  # type: ignore[arg-type]

    counts = await harness.queue.counts()
    assert (counts.pending, counts.leased) == (0, 0)
    assert outcome_count(harness, "lost") == 1


@scenario
async def test_rerun_after_lost_lease_counts_once() -> None:
    url = page("example.org")
    harness = Harness({url: fetched(url)})
    job_id = await harness.submit([url])
    first = (await harness.claim()).item  # this worker hangs past its lease ...
    harness.clock.now += harness.lease_seconds + 1
    second = (await harness.claim()).item  # ... so another one takes the item over
    assert first is not None
    assert second is not None
    assert (first.attempts, second.attempts) == (0, 1)

    await harness.worker.process(second)
    await harness.worker.process(first)  # the first worker wakes up and finishes too

    record = await harness.store.get(job_id)
    assert record is not None
    assert record.state == "completed"
    assert (record.progress.done, record.progress.success) == (1, 1)
    counts = await harness.queue.counts()
    assert (counts.pending, counts.leased) == (0, 0)
    assert harness.sample("text_extraction_job_duration_seconds_count", {"kind": "batch"}) == 1
    assert outcome_count(harness, "success") == 1  # the repeat write is not counted


@scenario
async def test_stale_worker_that_writes_first_counts_once() -> None:
    url = page("example.org")
    harness = Harness({url: fetched(url)})
    job_id = await harness.submit([url])
    stale = (await harness.claim()).item
    harness.clock.now += harness.lease_seconds + 1
    current = (await harness.claim()).item
    assert stale is not None
    assert current is not None

    await harness.worker.process(stale)  # writes the result, but its lease is gone
    assert await harness.queue.complete(stale) is False
    assert (await harness.queue.counts()).leased == 1  # the current holder still has it
    await harness.worker.process(current)  # a repeat write, then the current lease ends

    record = await harness.store.get(job_id)
    assert record is not None
    assert record.state == "completed"
    assert (record.progress.done, record.progress.success) == (1, 1)
    counts = await harness.queue.counts()
    assert (counts.pending, counts.leased) == (0, 0)
    assert harness.sample("text_extraction_job_duration_seconds_count", {"kind": "batch"}) == 1
    assert outcome_count(harness, "success") == 1


@scenario
async def test_deferral_after_a_lost_lease_is_not_counted() -> None:
    harness = Harness(gate=RefusingGate(OutboundBusy(3.0)))
    job_id = await harness.submit([page("example.org")])
    stale = (await harness.claim()).item
    harness.clock.now += harness.lease_seconds + 1
    current = (await harness.claim()).item
    assert stale is not None
    assert current is not None

    await harness.worker.process(stale)

    assert outcome_count(harness, "deferred") == 0
    counts = await harness.queue.counts()
    assert (counts.pending, counts.leased) == (0, 1)  # still the current holder's
    assert await queue_list(harness.valkey, "example.org") == []
    [item] = await harness.items(job_id)
    assert item.state == "pending"


@pytest.mark.parametrize("raised", [TimeoutError("Valkey is slow"), ConnectionResetError()])
@scenario
async def test_os_errors_from_the_pipeline_back_off_instead_of_failing_the_item(
    raised: Exception,
) -> None:
    class Failing:
        async def run(self, *args: Any, **kwargs: Any) -> Envelope[ExtractResult]:
            raise raised

    harness = Harness(pipeline=Failing())
    job_id = await harness.submit([page("example.org")])

    with pytest.raises(type(raised)):
        await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]

    [item] = await harness.items(job_id)
    assert item.state == "pending"
    assert (await harness.queue.counts()).leased == 1


# --- Valkey outages and idle sleeps ------------------------------------------------------------


class FlakyQueue(FairQueue):
    """A real queue whose first ``failures`` claims raise a connection error."""

    def __init__(self, *args: Any, failures: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.failures = failures

    async def claim(self, lease_seconds: float) -> Claim:
        if self.failures > 0:
            self.failures -= 1
            raise RedisConnectionError("Valkey is down")
        return await super().claim(lease_seconds)


@pytest.mark.parametrize(("failures", "backoff"), [(3, [1, 2, 4]), (7, [1, 2, 4, 8, 16, 30, 30])])
@scenario
async def test_valkey_outage_backs_off_and_recovers(failures: int, backoff: list[float]) -> None:
    urls = [page("example.org", n) for n in range(3)]

    def flaky(valkey: FakeAsyncRedis, settings: Settings, clock: Clock) -> FlakyQueue:
        return FlakyQueue(valkey, settings, clock=clock, failures=failures)

    harness = Harness({url: fetched(url) for url in urls}, make_queue=flaky, worker_concurrency=1)
    job_id = await harness.submit(urls)

    with capture_logs() as logs:
        await harness.run_until(lambda: harness.completed(job_id))

    assert harness.sleeps.delays[:failures] == backoff
    warnings = [log for log in logs if log["event"] == "worker_valkey_unavailable"]
    assert len(warnings) == failures
    assert all(log["error_type"] == "ConnectionError" for log in warnings)
    record = await harness.store.get(job_id)
    assert record is not None
    assert record.progress.success == 3


class ScriptedQueue:
    """Answers claims from a script (a Claim, or an exception to raise); sets ``stop`` at its
    end. Items in it belong to no job, so the worker drops them."""

    def __init__(self, script: list[Claim | Exception], stop: asyncio.Event) -> None:
        self.script = list(script)
        self.stop = stop
        self.completed: list[QueueItem] = []

    async def claim(self, lease_seconds: float) -> Claim:
        if not self.script:
            self.stop.set()
            return Claim(item=None)
        answer = self.script.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def complete(self, item: QueueItem) -> bool:
        self.completed.append(item)
        return True


def orphan(index: int = 0) -> Claim:
    return Claim(item=QueueItem("f" * 32, index, "alice", "example.org", 0, "lease"))


SCAN = Claim(item=None, retry_in=0.05, scan_incomplete=True)


@scenario
async def test_idle_sleeps_back_off_on_incomplete_scans_and_follow_retry_in() -> None:
    stop = asyncio.Event()
    script: list[Claim | Exception] = [
        *[SCAN] * 7,
        orphan(0),
        SCAN,
        SCAN,
        Claim(item=None, retry_in=0.3),
        SCAN,
        Claim(item=None, retry_in=None),
        Claim(item=None, retry_in=5.0),
        RedisConnectionError("down"),
        RedisConnectionError("down"),
        orphan(1),
        RedisConnectionError("down"),
    ]
    queue = ScriptedQueue(script, stop)
    harness = Harness(make_queue=lambda *_: queue, worker_concurrency=1)

    async with asyncio.timeout(10):
        await harness.worker.run(stop)

    assert harness.sleeps.delays == pytest.approx(
        [
            *[0.05, 0.1, 0.2, 0.4, 0.8, 1.0, 1.0],  # consecutive incomplete scans
            *[0.05, 0.1],  # an item was claimed: from the start again
            *[0.3, 0.05],  # retry_in; another answer ends the run of incomplete scans
            *[1.0, 1.0],  # no retry_in, and retry_in capped at the idle sleep
            *[1, 2],  # Valkey outage
            1,  # the claimed item reset the outage backoff
        ]
    )
    assert [item.index for item in queue.completed] == [0, 1]


@pytest.mark.parametrize(("random", "factor"), [(0.0, 0.5), (0.999, 1.499)])
@scenario
async def test_idle_sleep_has_plus_minus_half_jitter(random: float, factor: float) -> None:
    stop = asyncio.Event()
    queue = ScriptedQueue([Claim(item=None), SCAN], stop)
    harness = Harness(make_queue=lambda *_: queue, worker_concurrency=1, random=lambda: random)

    async with asyncio.timeout(10):
        await harness.worker.run(stop)

    assert harness.sleeps.delays == pytest.approx([1.0 * factor, 0.05 * factor])


@scenario
async def test_gate_backend_outage_backs_off_and_keeps_the_lease() -> None:
    harness = Harness(gate=RefusingGate(RedisConnectionError("down")), worker_concurrency=1)
    job_id = await harness.submit([page("example.org")])

    async def backed_off() -> bool:
        return len(harness.sleeps.delays) >= 2

    with capture_logs() as logs:
        await harness.run_until(backed_off)

    assert harness.sleeps.delays[0] == 1  # backoff, then idle: the item is still leased
    assert any(log["event"] == "worker_valkey_unavailable" for log in logs)
    [item] = await harness.items(job_id)
    assert item.state == "pending"
    assert (await harness.queue.counts()).leased == 1


# --- shutdown and readiness --------------------------------------------------------------------


class Started:
    """The page fetches that began, in order; ``reach`` waits for a number of them."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self._changed = asyncio.Event()

    def add(self, url: str) -> None:
        self.urls.append(url)
        self._changed.set()

    async def reach(self, count: int) -> None:
        async with asyncio.timeout(20):
            while len(self.urls) < count:
                self._changed.clear()
                await self._changed.wait()


def slow_answers(urls: list[str], started: Started, release: asyncio.Event) -> dict[str, Answer]:
    """Answers that hold each fetch until ``release`` is set."""

    def slow(url: str) -> Callable[[Validators | None], Awaitable[FetchResult]]:
        async def answer(_validators: Validators | None) -> FetchResult:
            started.add(url)
            await release.wait()
            return fetched(url)

        return answer

    return {url: slow(url) for url in urls}


@scenario
async def test_stop_finishes_in_flight_items_and_claims_no_more() -> None:
    urls = [page("a.example"), page("b.example"), page("c.example")]
    started = Started()
    release = asyncio.Event()
    harness = Harness(slow_answers(urls, started, release), worker_concurrency=2)
    job_id = await harness.submit(urls)
    stop = asyncio.Event()

    running = asyncio.create_task(harness.worker.run(stop))
    await started.reach(2)
    assert harness.sample("text_extraction_worker_busy_slots") == 2
    stop.set()
    await asyncio.sleep(0.1)
    assert not running.done()  # the items in flight are still running
    release.set()
    async with asyncio.timeout(20):
        await running

    assert len(started.urls) == 2
    record = await harness.store.get(job_id)
    assert record is not None
    assert record.progress.done == 2
    counts = await harness.queue.counts()
    assert (counts.pending, counts.leased) == (1, 0)
    assert harness.sample("text_extraction_worker_busy_slots") == 0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def status_of(client: httpx.AsyncClient, url: str) -> int | None:
    try:
        return (await client.get(url, timeout=2)).status_code
    except httpx.TransportError:
        return None  # not listening


@scenario
async def test_worker_ready_endpoint() -> None:
    urls = [page("a.example"), page("b.example")]
    started = Started()
    release = asyncio.Event()
    harness = Harness(slow_answers(urls, started, release), worker_concurrency=1)
    job_id = await harness.submit(urls)
    readiness = Readiness()
    app = create_internal_app(
        harness.settings, harness.valkey, harness.registry, readiness=readiness
    )
    port = free_port()
    internal = _server(app, "127.0.0.1", port)
    ready_url = f"http://127.0.0.1:{port}/ready"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as local:
        before = (await local.get("http://worker/ready")).status_code
    serving = asyncio.create_task(_serve(harness.worker, internal, readiness, graceful_seconds=30))
    async with httpx.AsyncClient(trust_env=False) as client:
        deadline = time.monotonic() + 20
        while await status_of(client, ready_url) != 200:
            assert time.monotonic() < deadline, "the worker never became ready"
            await asyncio.sleep(0.05)
        await started.reach(1)  # the loops run and hold one item

        os.kill(os.getpid(), signal.SIGTERM)
        deadline = time.monotonic() + 20
        while (draining := await status_of(client, ready_url)) == 200:
            assert time.monotonic() < deadline, "/ready did not follow SIGTERM"
            await asyncio.sleep(0.05)
        assert not serving.done()  # the item in flight holds the shutdown
        release.set()
        async with asyncio.timeout(30):
            await serving
        after = await status_of(client, ready_url)

    assert before == 503
    assert draining == 503
    assert after is None  # the internal server stopped last
    assert len(started.urls) == 1  # nothing was claimed after the signal
    [first, second] = await harness.items(job_id)
    assert (first.state, second.state) == ("done", "pending")


@scenario
async def test_shutdown_cancels_items_that_outlive_the_grace_period() -> None:
    urls = [page("a.example")]
    started = Started()
    release = asyncio.Event()  # never set
    harness = Harness(slow_answers(urls, started, release), worker_concurrency=1)
    await harness.submit(urls)
    readiness = Readiness()
    app = create_internal_app(
        harness.settings, harness.valkey, harness.registry, readiness=readiness
    )
    internal = _server(app, "127.0.0.1", free_port())

    with capture_logs() as logs:
        serving = asyncio.create_task(
            _serve(harness.worker, internal, readiness, graceful_seconds=0.2)
        )
        await started.reach(1)
        os.kill(os.getpid(), signal.SIGTERM)
        async with asyncio.timeout(20):
            await serving

    assert any(log["event"] == "worker_shutdown_timeout" for log in logs)
    assert (await harness.queue.counts()).leased == 1  # the lease expires and the item returns


@pytest.fixture(autouse=True)
def _restore_sigterm() -> Iterator[None]:
    previous = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, previous)


def test_main_exits_2_on_invalid_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("WORKER_CONCURRENCY", "0")

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 2


# --- metrics -----------------------------------------------------------------------------------


@scenario
async def test_metrics_count_outcomes() -> None:
    good = page("example.org")
    harness = Harness({good: fetched(good)})
    job_id = await harness.submit([good, "ftp://example.org/file.txt"])
    harness.clock.now += 7

    await harness.run_until(lambda: harness.completed(job_id))

    deferred = Harness(gate=RefusingGate(OutboundBusy(3.0)))
    await deferred.submit([page("example.org")])
    await deferred.worker.process((await deferred.claim()).item)  # type: ignore[arg-type]

    assert outcome_count(harness, "success") == 1
    assert outcome_count(harness, "invalid_url") == 1
    assert outcome_count(deferred, "deferred") == 1
    assert harness.sample("text_extraction_job_item_seconds_count") == 2
    assert harness.sample("text_extraction_worker_busy_slots") == 0
    assert harness.sample("text_extraction_job_duration_seconds_count", {"kind": "batch"}) == 1
    assert harness.sample("text_extraction_job_duration_seconds_sum", {"kind": "batch"}) == 7
