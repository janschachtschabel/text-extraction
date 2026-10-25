"""The extract worker (``worker.py``): slot loops over the fair queue, the job store and the
pipeline, with fakeredis (Lua), a fake fetcher, an injected clock and an injected sleep."""

import asyncio
import contextlib
import os
import signal
import socket
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from functools import wraps
from typing import Any, cast

import httpx
import pytest
import structlog
from fakeredis import FakeAsyncRedis
from prometheus_client import CollectorRegistry
from redis.exceptions import ConnectionError as RedisConnectionError
from structlog.testing import capture_logs

from tests.fakes import PARAGRAPH, Answer, FakeFetcher, RefusingGate, fetched
from tests.test_pipeline_render import CHALLENGE_PAGE, SHELL, SHELL_WITH_WORDS
from text_extraction import worker as worker_module
from text_extraction.asr_dispatch import asr_queue
from text_extraction.auth import Principal
from text_extraction.compliance import ComplianceChecker, ComplianceReport
from text_extraction.docling_dispatch import docling_queue
from text_extraction.documents.routing import ocr_available
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.errors import ServiceError
from text_extraction.extract_models import ExtractRequest, ExtractResult, Timings
from text_extraction.fetch import Fetcher, FetchResult, Validators
from text_extraction.file_models import UploadInputMeta
from text_extraction.fingerprint import FingerprintStore
from text_extraction.internal import Readiness, create_internal_app
from text_extraction.job_models import JobItem, JobOptions
from text_extraction.jobs import JobLost, JobRecord, JobStore, ResultWrite
from text_extraction.memory_guard import MemoryGuard
from text_extraction.outbound import CircuitOpen, DomainGate, OutboundBusy
from text_extraction.pipeline import Pipeline
from text_extraction.pipeline_documents import OCR_UNAVAILABLE_WARNING, DocumentInputMeta
from text_extraction.pipeline_media import ASR_UNAVAILABLE_WARNING, MediaInputMeta, asr_available
from text_extraction.result_cache import ResultCache
from text_extraction.scheduler import Claim, FairQueue, QueueItem
from text_extraction.server import _server
from text_extraction.settings import Settings
from text_extraction.worker import ExtractRunner, Worker, _serve, build_worker, main

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


class Pressure:
    """A memory guard whose verdict the test sets."""

    high = False


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
        document_pool: Any = None,
        media_fetcher: Any = None,
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
            document_pool=document_pool,
            media_fetcher=media_fetcher,
        )
        self.store = JobStore(self.valkey, self.settings, clock=self.clock)
        self.queue = (make_queue or default_queue)(self.valkey, self.settings, self.clock)
        # The browser pool's queue, which escalating ``auto`` items move to (B5).
        self.browser_queue = FairQueue(self.valkey, self.settings, "browser", clock=self.clock)
        # The Docling pool's queue, which items that need OCR move to (F13).
        self.docling_queue = docling_queue(self.valkey, self.settings)
        # The ASR pool's queue, which items that need a transcription move to (M9).
        self.asr_queue = asr_queue(self.valkey, self.settings)
        self.guard = Pressure()
        self.runner = ExtractRunner(
            self.pipeline,
            self.store,
            self.settings,
            self.browser_queue if self.settings.browser_enabled else None,
            docling_queue=self.docling_queue if ocr_available(self.settings) else None,
            asr_queue=self.asr_queue if asr_available(self.settings) else None,
            guard=self.guard,
            registry=self.registry,
        )
        self.worker = Worker(
            self.settings,
            self.valkey,
            self.runner,
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


# --- the trace id of the job's creator -------------------------------------------------------


@scenario
async def test_worker_logs_and_stores_under_the_creators_trace_id() -> None:
    url = page("example.org")
    harness = Harness({url: fetched(url)})
    job_id = await harness.store.create("batch", OWNER, OPTIONS, [url], trace_id="trace-batch-1")
    await harness.queue.enqueue(job_id, OWNER.name, [url])
    claim = await harness.claim()

    with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as logs:
        await harness.worker.process(claim.item)  # type: ignore[arg-type]

    done = [entry for entry in logs if entry["event"] == "job_item_done"]
    assert [entry.get("trace_id") for entry in done] == ["trace-batch-1"]
    [item] = await harness.items(job_id)
    assert item.result is not None
    assert item.result.trace_id == "trace-batch-1"
    assert structlog.contextvars.get_contextvars().get("trace_id") is None  # unbound again


@scenario
async def test_worker_without_a_stored_trace_id_binds_none() -> None:
    url = page("example.org")
    harness = Harness({url: fetched(url)})
    job_id = await harness.submit([url])
    claim = await harness.claim()

    with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as logs:
        await harness.worker.process(claim.item, exhausted=True)  # type: ignore[arg-type]

    assert logs
    assert all("trace_id" not in entry for entry in logs)
    assert all(entry["job_id"] == job_id for entry in logs)
    [item] = await harness.items(job_id)
    assert item.result is not None
    assert item.result.trace_id is None


# --- the Valkey memory guard in the worker process (R31) ---------------------------------------


class HighGuard:
    high = True


@scenario
async def test_worker_stores_results_but_skips_cache_writes_while_memory_is_high() -> None:
    url = page("example.org")
    harness = Harness({url: fetched(url)})
    valkey, settings = harness.valkey, harness.settings
    fetcher = cast(Fetcher, harness.fetcher)
    pipeline = Pipeline(
        fetcher,
        DomainGate(valkey, settings),
        ComplianceChecker(fetcher, valkey, settings),
        ResultCache(valkey, settings, guard=HighGuard()),
        FingerprintStore(valkey, settings),
        settings,
        harness.registry,
    )
    harness.worker = Worker(
        settings,
        valkey,
        ExtractRunner(pipeline, harness.store, settings, None),
        harness.store,
        harness.queue,
        harness.registry,
    )
    job_id = await harness.submit([url])

    await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]

    [item] = await harness.items(job_id)
    assert item.result is not None
    assert item.result.status == "success"  # the job's result write is not guarded
    assert await valkey.keys("res:*") == []  # the cache's second copy is skipped


def test_worker_process_wires_one_guard_into_cache_and_registry() -> None:
    registry = CollectorRegistry()
    parts = build_worker(make_settings(), FakeAsyncRedis(), registry)
    try:
        runner = parts.worker._runner
        assert isinstance(runner, ExtractRunner)
        assert runner._pipeline._cache._guard is parts.guard
        assert parts.guard in registry._collector_to_names
        assert "text_extraction_valkey_used_memory_bytes" in registry._names_to_collectors
    finally:
        parts.pool.shutdown()


@scenario
async def test_serve_runs_the_guard_while_the_worker_runs() -> None:
    harness = Harness({}, worker_concurrency=1)
    guard = MemoryGuard(harness.valkey, 0.8, CollectorRegistry(), interval=0.01)
    readiness = Readiness()
    app = create_internal_app(
        harness.settings, harness.valkey, harness.registry, readiness=readiness
    )
    internal = _server(app, "127.0.0.1", free_port())

    serving = asyncio.create_task(
        _serve(harness.worker, internal, readiness, graceful_seconds=1, guard=guard)
    )
    deadline = time.monotonic() + 20
    while not readiness.ready:
        assert time.monotonic() < deadline, "the worker never started"
        await asyncio.sleep(0.01)
    assert guard.running

    os.kill(os.getpid(), signal.SIGTERM)
    async with asyncio.timeout(20):
        await serving

    assert not guard.running


# --- auto escalation to the browser pool (B5) --------------------------------------------------


ADMIN = Principal(name="root", role="admin", rate_limit_per_minute=None)


async def browser_list(valkey: FakeAsyncRedis, domain: str, client: str = "alice") -> list[str]:
    raw = await valkey.lrange(f"q:{{browser}}:c:{client}:d:{domain}", 0, -1)
    return [os.fsdecode(value) for value in raw]


@scenario
async def test_existing_worker_tests_pass_with_extract_runner() -> None:
    """The harness above runs every package-3 scenario through the ``ExtractRunner``; its lease
    is the extract pool's."""
    harness = Harness()

    assert isinstance(harness.worker._runner, ExtractRunner)
    assert harness.runner.lease_seconds == harness.settings.extract_timeout_seconds * 2 + 10
    assert harness.worker._lease_seconds == harness.runner.lease_seconds


@scenario
async def test_auto_item_needing_browser_moves_to_browser_pool_with_draft() -> None:
    plain, shell = page("example.org", 0), page("example.org", 1)
    harness = Harness({plain: fetched(plain), shell: fetched(shell, SHELL)}, browser_enabled=True)
    job_id = await harness.submit([plain, shell])

    for _ in range(2):
        await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]

    # Completed in ``extract`` without a result ...
    counts = await harness.queue.counts()
    assert (counts.pending, counts.leased) == (0, 0)
    first, second = await harness.items(job_id)
    assert first.state == "done"
    assert (second.state, second.result) == ("pending", None)
    assert not await harness.completed(job_id)
    # ... and queued in ``browser`` with the same index, under the job's client.
    assert await browser_list(harness.valkey, "example.org") == [f"{job_id}:1:0"]
    draft = await harness.store.draft(job_id, 1)
    assert draft is not None
    assert draft.data is not None
    assert (draft.data.mode_used, draft.data.url) == ("fast", shell)
    assert draft.data.quality is not None
    assert draft.data.quality.browser_recommended
    assert await harness.store.draft(job_id, 0) is None
    assert outcome_count(harness, "escalated") == 1
    assert outcome_count(harness, "success") == 1
    # B5: a result that escalates is neither cached nor fingerprinted by the fast run.
    assert len(await harness.valkey.keys("res:*")) == 1


@pytest.mark.parametrize("stored", ["hit", "revalidated"])
@scenario
async def test_a_stored_fast_winner_does_not_move(stored: str) -> None:
    shell = page("example.org", 1)

    async def answer(validators: Validators | None) -> FetchResult:
        if validators is not None and validators.etag == '"v1"':
            return fetched(shell, b"", status=304, headers={"etag": '"v1"'})
        return fetched(shell, SHELL_WITH_WORDS, headers={"etag": '"v1"'})

    ttl = 3600 if stored == "hit" else 0
    harness = Harness({shell: answer}, browser_enabled=True, result_cache_ttl_seconds=ttl)
    # What the browser worker stores when the fast result wins (B5); browser mode off stores
    # the same entry.
    off = make_settings(result_cache_ttl_seconds=ttl)
    fetcher = cast(Fetcher, harness.fetcher)
    seeding = Pipeline(
        fetcher,
        DomainGate(harness.valkey, off),
        ComplianceChecker(fetcher, harness.valkey, off),
        ResultCache(harness.valkey, off),
        FingerprintStore(harness.valkey, off),
        off,
        CollectorRegistry(),
    )
    seed = await seeding.run(ExtractRequest(url=shell, compliance="enforce"), OWNER)
    assert seed.data is not None
    assert seed.data.quality is not None
    assert seed.data.quality.browser_recommended
    job_id = await harness.submit([shell])

    await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]

    [item] = await harness.items(job_id)
    assert item.result is not None
    assert item.result.data is not None
    assert (item.result.data.cache, item.result.data.mode_used) == (stored, "fast")
    assert await harness.completed(job_id)
    assert (await harness.browser_queue.counts()).pending == 0
    assert outcome_count(harness, "escalated") == 0


@scenario
async def test_no_move_when_browser_disabled() -> None:
    shell = page("example.org", 1)
    harness = Harness({shell: fetched(shell, SHELL)})
    job_id = await harness.submit([shell])

    await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]

    [item] = await harness.items(job_id)
    assert item.result is not None
    assert item.result.data is not None
    assert item.result.data.mode_used == "fast"
    assert await harness.completed(job_id)
    assert (await harness.browser_queue.counts()).pending == 0
    assert await harness.store.draft(job_id, 0) is None
    assert outcome_count(harness, "escalated") == 0


@pytest.mark.parametrize("mode", ["fast", "browser"])
@scenario
async def test_only_auto_items_move(mode: str) -> None:
    shell = page("example.org", 1)
    harness = Harness({shell: fetched(shell, SHELL)}, browser_enabled=True)
    options = JobOptions(mode=mode, compliance="enforce")  # type: ignore[arg-type]
    job_id = await harness.store.create("batch", OWNER, options, [shell])
    await harness.queue.enqueue(job_id, OWNER.name, [shell])

    await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]

    assert await harness.completed(job_id)
    assert (await harness.browser_queue.counts()).pending == 0


@pytest.mark.parametrize(
    ("owner", "compliance", "unblock_enabled", "moves"),
    [
        (ADMIN, "ignore", True, True),
        (ADMIN, "ignore", False, False),
        (ADMIN, "enforce", True, False),
        # R9: unblock needs an admin owner, even where the operator's default is ``ignore``.
        (OWNER, "ignore", True, False),
    ],
)
@scenario
async def test_bot_wall_moves_only_for_admin_ignore_jobs_with_unblock(
    owner: Principal, compliance: str, unblock_enabled: bool, moves: bool
) -> None:
    wall = page("example.org", 2)
    harness = Harness(
        {wall: fetched(wall, CHALLENGE_PAGE, status=403)},
        browser_enabled=True,
        browser_unblock_enabled=unblock_enabled,
        compliance_default=compliance,
    )
    options = JobOptions(mode="auto", compliance=compliance)  # type: ignore[arg-type]
    job_id = await harness.store.create("batch", owner, options, [wall])
    await harness.queue.enqueue(job_id, owner.name, [wall])

    await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]

    assert (await harness.browser_queue.counts()).pending == (1 if moves else 0)
    assert await harness.completed(job_id) is not moves
    if not moves:  # the fast answer stays: the bot wall's 403, not a refused compliance mode
        [item] = await harness.items(job_id)
        assert error_of(item)[0] == ErrorCode.TARGET_HTTP_ERROR


@scenario
async def test_move_of_an_expired_job_drops_the_item() -> None:
    harness = Harness(browser_enabled=True)
    shell = page("example.org", 1)

    class Expiring:
        """Runs the real pipeline on a shell page, while the job expires meanwhile."""

        async def run(self, req: ExtractRequest, principal: Principal, **kwargs: Any) -> Any:
            await harness.store.delete(job_id)
            return await real.run(req, principal, **kwargs)

    real = Harness({shell: fetched(shell, SHELL)}, browser_enabled=True).pipeline
    harness.runner._pipeline = Expiring()  # type: ignore[assignment]
    job_id = await harness.submit([shell])

    await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]

    assert (await harness.browser_queue.counts()).pending == 0
    assert (await harness.queue.counts()).leased == 0
    assert outcome_count(harness, "lost") == 1


@scenario
async def test_move_after_a_lost_lease_is_not_counted() -> None:
    shell = page("example.org", 1)
    harness = Harness({shell: fetched(shell, SHELL)}, browser_enabled=True)
    await harness.submit([shell])
    stale = (await harness.claim()).item
    harness.clock.now += harness.lease_seconds + 1
    current = (await harness.claim()).item
    assert stale is not None
    assert current is not None

    await harness.worker.process(current)
    await harness.worker.process(stale)  # wakes up late and moves it as well

    assert outcome_count(harness, "escalated") == 1
    counts = await harness.queue.counts()
    assert (counts.pending, counts.leased) == (0, 0)
    # Both holders queued it on ``browser``: the browser worker renders only the first copy
    # (test_browser_worker.py, test_an_item_moved_twice_renders_once).
    [member] = {*await browser_list(harness.valkey, "example.org")}
    assert await browser_list(harness.valkey, "example.org") == [member, member]


@scenario
async def test_item_that_has_its_result_is_completed_without_running_again() -> None:
    url = page("example.org")
    harness = Harness({url: fetched(url)})
    uncached = JobOptions(compliance="enforce", cache=False)  # no cache hit either
    job_id = await harness.store.create("batch", OWNER, uncached, [url])
    await harness.queue.enqueue(job_id, OWNER.name, [url])
    stale = (await harness.claim()).item
    harness.clock.now += harness.lease_seconds + 1
    current = (await harness.claim()).item
    assert stale is not None
    assert current is not None
    await harness.worker.process(stale)  # writes the result although its lease is gone

    await harness.worker.process(current)  # finds the result: no second fetch

    assert [called for called, _ in harness.fetcher.calls if called == url] == [url]
    counts = await harness.queue.counts()
    assert (counts.pending, counts.leased) == (0, 0)
    record = await harness.store.get(job_id)
    assert record is not None
    assert (record.state, record.progress.done) == ("completed", 1)
    assert outcome_count(harness, "success") == 1


@scenario
async def test_shutdown_waits_only_briefly_for_a_start_up_that_does_not_unwind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_module, "_START_UNWIND_SECONDS", 0.2)
    harness = Harness({}, worker_concurrency=1)
    readiness = Readiness()
    app = create_internal_app(
        harness.settings, harness.valkey, harness.registry, readiness=readiness
    )
    internal = _server(app, "127.0.0.1", free_port())
    started = asyncio.Event()
    release = asyncio.Event()

    async def stuck() -> None:
        started.set()
        while not release.is_set():  # a driver call that swallows the cancellation
            with contextlib.suppress(asyncio.CancelledError):
                await release.wait()

    with capture_logs() as logs:
        serving = asyncio.create_task(
            _serve(harness.worker, internal, readiness, graceful_seconds=1, startup=stuck)
        )
        await started.wait()
        os.kill(os.getpid(), signal.SIGTERM)
        async with asyncio.timeout(5):
            await serving
    release.set()

    assert not readiness.ready
    assert any(log["event"] == "worker_start_not_unwound" for log in logs)


# --- documents that need OCR (F13) ---------------------------------------------------------


SCAN_URL = "https://docs.example.org/scan.pdf"
SCAN_BODY = b"%PDF-1.7\nscan\n%%EOF\n"


class OcrPool:
    """MarkItDown's children: every PDF has a text layer too thin to keep (it needs OCR)."""

    async def start(self) -> None:
        pass

    async def run(self, function_name: str, payload: dict[str, Any], deadline: float) -> Any:
        return {
            "content": "Thin text layer.",
            "title": "Scan",
            "warnings": [],
            "pages": 1,
            "pages_converted": 1,
            "needs_ocr": True,
            "metadata": None,
        }

    async def shutdown(self) -> None:
        pass


def ocr_harness(**overrides: Any) -> Harness:
    scan = fetched(SCAN_URL, SCAN_BODY, content_type="application/pdf")
    return Harness({SCAN_URL: scan}, document_pool=OcrPool(), docling_enabled=True, **overrides)


async def docling_members(
    harness: Harness, client: str = "alice", domain: str = "docs.example.org"
) -> list[str]:
    raw = await harness.valkey.lrange(f"q:{{docling}}:c:{client}:d:{domain}", 0, -1)
    return [os.fsdecode(value) for value in raw]


@scenario
async def test_batch_item_moves_to_docling() -> None:
    harness = ocr_harness()
    job_id = await harness.submit([SCAN_URL])
    steps: list[str] = []
    store, queue = harness.store, harness.docling_queue
    real_input, real_draft, real_enqueue = store.put_input, store.put_draft, queue.enqueue_items

    async def put_input(*args: Any, **kwargs: Any) -> None:
        steps.append("input")
        await real_input(*args, **kwargs)

    async def put_draft(*args: Any, **kwargs: Any) -> None:
        steps.append("draft")
        await real_draft(*args, **kwargs)

    failures = [RedisConnectionError("valkey is down")]

    async def enqueue_items(*args: Any, **kwargs: Any) -> None:
        steps.append("queue")
        if failures:  # the pod dies after the draft
            raise failures.pop()
        await real_enqueue(*args, **kwargs)

    store.put_input = put_input  # type: ignore[method-assign]
    store.put_draft = put_draft  # type: ignore[method-assign]
    queue.enqueue_items = enqueue_items  # type: ignore[method-assign]
    real_complete = harness.queue.complete

    async def complete(item: QueueItem) -> bool:
        steps.append("complete")
        return await real_complete(item)

    harness.queue.complete = complete  # type: ignore[method-assign]

    with pytest.raises(RedisConnectionError):
        await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]
    # Still leased here, and nothing reached the Docling queue.
    assert (await harness.queue.counts()).leased == 1
    assert (await queue.counts()).pending == 0
    harness.clock.now += harness.lease_seconds + 1
    await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]

    assert steps == ["input", "draft", "queue", "input", "draft", "queue", "complete"]
    # Moved once: completed in ``extract`` without a result, queued on ``docling`` with the same
    # job, index and client.
    counts = await harness.queue.counts()
    assert (counts.pending, counts.leased) == (0, 0)
    assert await docling_members(harness) == [f"{job_id}:0:0"]
    (item,) = await harness.items(job_id)
    assert (item.state, item.result) == ("pending", None)
    stored = await store.get_input(job_id, 0, DocumentInputMeta)
    assert stored is not None
    body, meta = stored
    assert body == SCAN_BODY
    assert (meta.kind, meta.final_url, meta.needs_ocr) == ("pdf", SCAN_URL, True)
    ttl = await harness.valkey.ttl(f"job:{{{job_id}}}:input:0")
    assert 0 < ttl <= harness.settings.docling_input_ttl_seconds
    draft = await store.draft(job_id, 0)
    assert draft is not None
    assert draft.data is not None
    assert draft.data.content == "Thin text layer."
    assert outcome_count(harness, "escalated") == 1
    # The draft is neither cached nor fingerprinted: the Docling worker stores the kept one.
    assert await harness.valkey.keys("res:*") == []


@scenario
async def test_batch_item_keeps_its_draft_while_memory_is_high() -> None:
    harness = ocr_harness()
    harness.guard.high = True
    job_id = await harness.submit([SCAN_URL])

    await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]
    counted = harness.sample("text_extraction_docling_dispatch_total", {"outcome": "memory_guard"})
    assert counted == 1

    (item,) = await harness.items(job_id)
    assert item.result is not None
    assert item.result.status == "partial"  # the draft's own verdict: too few words
    assert item.result.data is not None
    assert OCR_UNAVAILABLE_WARNING in item.result.data.warnings
    assert item.result.data.fingerprint is None
    assert (await harness.docling_queue.counts()).pending == 0
    assert await harness.valkey.keys("job:*:input:*") == []


@scenario
async def test_batch_item_keeps_its_draft_while_the_input_budget_is_full() -> None:
    """All inputs together would exceed DOCLING_INPUT_BUDGET_BYTES: the item keeps its draft
    with ``ocr_unavailable``, unstored, and nothing moves (final review I3)."""
    harness = ocr_harness(docling_input_budget_bytes=10_000, document_max_bytes=10_000)
    other = await harness.store.create("batch", OWNER, OPTIONS, [SCAN_URL])
    meta = DocumentInputMeta(kind="pdf", content_type="application/pdf", raw_sha256="0" * 64)
    size = 10_000 - len(meta.model_dump_json()) - 10  # 10 bytes are left
    await harness.store.put_input(other, 0, b"f" * size, meta, 600)
    job_id = await harness.submit([SCAN_URL])

    await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]

    (item,) = await harness.items(job_id)
    assert item.result is not None
    assert item.result.status == "partial"  # the draft's own verdict: too few words
    assert item.result.data is not None
    assert OCR_UNAVAILABLE_WARNING in item.result.data.warnings
    assert item.result.data.fingerprint is None
    assert (await harness.docling_queue.counts()).pending == 0
    assert await harness.valkey.keys(f"job:{{{job_id}}}:input:*") == []
    assert await harness.store.draft(job_id, 0) is None
    counted = harness.sample("text_extraction_docling_dispatch_total", {"outcome": "input_budget"})
    assert counted == 1


@scenario
async def test_no_move_while_docling_is_off() -> None:
    scan = fetched(SCAN_URL, SCAN_BODY, content_type="application/pdf")
    harness = Harness({SCAN_URL: scan}, document_pool=OcrPool())
    job_id = await harness.submit([SCAN_URL])

    await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]

    (item,) = await harness.items(job_id)
    assert item.result is not None
    assert item.result.data is not None
    assert OCR_UNAVAILABLE_WARNING in item.result.data.warnings
    assert (await harness.docling_queue.counts()).pending == 0


async def submit_upload(harness: Harness, body: bytes, content_type: str) -> str:
    options = JobOptions(mode="fast", compliance="enforce", cache=False, format="text")
    job_id = await harness.store.create("file", OWNER, options, [None])
    meta = UploadInputMeta(file_name="notes.txt", content_type=content_type)
    await harness.store.put_input(job_id, 0, body, meta, 600)
    await harness.queue.enqueue_items(job_id, OWNER.name, [(0, None)])
    return job_id


@scenario
async def test_file_item_runs_from_its_input_and_deletes_it() -> None:
    harness = Harness()
    job_id = await submit_upload(harness, (PARAGRAPH * 4).encode(), "text/plain")

    await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]

    (item,) = await harness.items(job_id)
    assert item.url is None
    assert item.result is not None
    assert item.result.status == "success"
    assert item.result.data is not None
    assert (item.result.data.mode_used, item.result.data.url) == ("upload", None)
    assert item.result.data.format == "text"
    assert await harness.valkey.keys("job:*:input:*") == []
    assert harness.fetcher.calls == []


@scenario
async def test_file_item_whose_input_expired_fails_timeout() -> None:
    harness = Harness()
    job_id = await submit_upload(harness, (PARAGRAPH * 4).encode(), "text/plain")
    await harness.store.delete_input(job_id, 0)

    await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]

    (item,) = await harness.items(job_id)
    code, retryable, _ = error_of(item)
    assert (code, retryable) == (ErrorCode.TIMEOUT, True)


@scenario
async def test_file_item_that_needs_ocr_moves_to_docling() -> None:
    harness = Harness(document_pool=OcrPool(), docling_enabled=True)
    job_id = await submit_upload(harness, SCAN_BODY, "application/pdf")

    await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]

    assert await docling_members(harness, domain="-") == [f"{job_id}:0:0"]
    stored = await harness.store.get_input(job_id, 0, DocumentInputMeta)
    assert stored is not None
    assert stored[1].file_name == "notes.txt"
    assert stored[1].needs_ocr is True


@scenario
async def test_docling_worker_deletes_the_input_with_the_outcome() -> None:
    """The ``Worker`` of pool ``docling`` (Task 9's runner) deletes an item's input once the
    item's result is written."""
    harness = ocr_harness()
    job_id = await harness.submit([SCAN_URL])
    await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]

    class Answering:
        lease_seconds = 60.0

        async def run(self, job: JobRecord, item: QueueItem, url: str, **kwargs: Any) -> Any:
            assert await harness.store.get_input(job.id, item.index, DocumentInputMeta)
            return Envelope[ExtractResult](status="failed")

    docling = Worker(
        harness.settings,
        harness.valkey,
        Answering(),
        harness.store,
        harness.docling_queue,
        harness.registry,
        clock=harness.clock,
    )
    claim = await harness.docling_queue.claim(60)
    assert claim.item is not None
    await docling.process(claim.item)

    assert await harness.completed(job_id)
    assert await harness.valkey.keys("job:*:input:*") == []


# --- media that needs a transcription (M9) ------------------------------------------------


def asr_harness(**overrides: Any) -> Harness:
    from tests.test_pipeline_media import FakeMediaFetcher, probe

    return Harness(media_fetcher=FakeMediaFetcher(probe(track=None)), asr_enabled=True, **overrides)


async def submit_transcription(harness: Harness, urls: list[str]) -> str:
    options = JobOptions(compliance="enforce", transcribe=True)
    job_id = await harness.store.create("batch", OWNER, options, urls)
    await harness.queue.enqueue(job_id, OWNER.name, urls)
    return job_id


async def asr_members(
    harness: Harness, client: str = "alice", domain: str = "www.youtube.com"
) -> list[str]:
    raw = await harness.valkey.lrange(f"q:{{asr}}:c:{client}:d:{domain}", 0, -1)
    return [os.fsdecode(value) for value in raw]


YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@scenario
async def test_batch_item_moves_to_asr() -> None:
    harness = asr_harness()
    job_id = await submit_transcription(harness, [YOUTUBE])
    steps: list[str] = []
    store, queue = harness.store, harness.asr_queue
    real_input, real_draft, real_enqueue = store.put_input, store.put_draft, queue.enqueue_items

    async def put_input(*args: Any, **kwargs: Any) -> None:
        steps.append("input")
        await real_input(*args, **kwargs)

    async def put_draft(*args: Any, **kwargs: Any) -> None:
        steps.append("draft")
        await real_draft(*args, **kwargs)

    failures = [RedisConnectionError("valkey is down")]

    async def enqueue_items(*args: Any, **kwargs: Any) -> None:
        steps.append("queue")
        if failures:  # the pod dies after the draft
            raise failures.pop()
        await real_enqueue(*args, **kwargs)

    store.put_input = put_input  # type: ignore[method-assign]
    store.put_draft = put_draft  # type: ignore[method-assign]
    queue.enqueue_items = enqueue_items  # type: ignore[method-assign]
    real_complete = harness.queue.complete

    async def complete(item: QueueItem) -> bool:
        steps.append("complete")
        return await real_complete(item)

    harness.queue.complete = complete  # type: ignore[method-assign]

    with pytest.raises(RedisConnectionError):
        await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]
    # Still leased here, and nothing reached the ASR queue.
    assert (await harness.queue.counts()).leased == 1
    assert (await queue.counts()).pending == 0
    harness.clock.now += harness.lease_seconds + 1
    await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]

    assert steps == ["input", "draft", "queue", "input", "draft", "queue", "complete"]
    # Moved once: completed in ``extract`` without a result, queued on ``asr`` with the same
    # job, index and client.
    counts = await harness.queue.counts()
    assert (counts.pending, counts.leased) == (0, 0)
    assert await asr_members(harness) == [f"{job_id}:0:0"]
    (item,) = await harness.items(job_id)
    assert (item.state, item.result) == ("pending", None)
    stored = await store.get_input(job_id, 0, MediaInputMeta)
    assert stored is not None
    body, meta = stored
    assert body == b""
    assert (meta.source, meta.platform, meta.url) == ("platform", "youtube", YOUTUBE)
    ttl = await harness.valkey.ttl(f"job:{{{job_id}}}:input:0")
    assert 0 < ttl <= harness.settings.asr_input_ttl_seconds
    # The rerun replaced the first input's reservation: the budget holds its bytes once.
    reserved = len(body) + len(meta.model_dump_json().encode())
    sizes = await harness.valkey.hgetall("inputs:{budget}:size")
    assert sizes == {f"{job_id}:0".encode(): str(reserved).encode()}
    assert await harness.valkey.get("inputs:{budget}:total") == str(reserved).encode()
    draft = await store.draft(job_id, 0)
    assert draft is not None
    assert draft.data is not None
    assert draft.data.media is not None
    assert draft.data.media.transcript_source == "metadata_only"
    assert outcome_count(harness, "escalated") == 1
    # The draft is neither cached nor fingerprinted: the ASR worker stores the kept one.
    assert await harness.valkey.keys("res:*") == []


@scenario
async def test_batch_item_keeps_its_draft_while_memory_is_high_instead_of_asr() -> None:
    harness = asr_harness()
    harness.guard.high = True
    job_id = await submit_transcription(harness, [YOUTUBE])

    await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]

    counted = harness.sample("text_extraction_asr_dispatch_total", {"outcome": "memory_guard"})
    assert counted == 1
    (item,) = await harness.items(job_id)
    assert item.result is not None
    assert item.result.status == "partial"
    assert item.result.data is not None
    assert ASR_UNAVAILABLE_WARNING in item.result.data.warnings
    assert item.result.data.fingerprint is None
    assert (await harness.asr_queue.counts()).pending == 0
    assert await harness.valkey.keys("job:*:input:*") == []


@scenario
async def test_batch_item_keeps_its_draft_while_the_input_budget_is_full_instead_of_asr() -> None:
    harness = asr_harness(
        docling_input_budget_bytes=1_000_000,
        media_upload_max_bytes=1_000_000,
        document_max_bytes=50_000,
    )
    other = await harness.store.create("batch", OWNER, OPTIONS, [YOUTUBE])
    meta = UploadInputMeta()
    await harness.store.put_input(
        other, 0, b"f" * (1_000_000 - len(meta.model_dump_json()) - 10), meta, 600
    )
    job_id = await submit_transcription(harness, [YOUTUBE])

    await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]

    (item,) = await harness.items(job_id)
    assert item.result is not None
    assert item.result.data is not None
    assert ASR_UNAVAILABLE_WARNING in item.result.data.warnings
    assert (await harness.asr_queue.counts()).pending == 0
    assert await harness.store.draft(job_id, 0) is None
    counted = harness.sample("text_extraction_asr_dispatch_total", {"outcome": "input_budget"})
    assert counted == 1


@scenario
async def test_no_move_while_asr_is_off() -> None:
    from tests.test_pipeline_media import FakeMediaFetcher, probe

    harness = Harness(media_fetcher=FakeMediaFetcher(probe(track=None)))
    job_id = await harness.store.create("batch", OWNER, OPTIONS, [YOUTUBE])
    await harness.queue.enqueue(job_id, OWNER.name, [YOUTUBE])

    await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]

    (item,) = await harness.items(job_id)
    assert item.result is not None
    assert item.result.status == "partial"  # metadata only, nothing asked for
    assert (await harness.asr_queue.counts()).pending == 0


OGG = b"OggS\x00\x02" + b"\x00" * 64


@scenario
async def test_file_item_of_audio_moves_to_asr_with_its_bytes() -> None:
    harness = asr_harness()
    job_id = await submit_upload(harness, OGG, "audio/ogg")

    await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]

    assert await asr_members(harness, domain="-") == [f"{job_id}:0:0"]
    stored = await harness.store.get_input(job_id, 0, MediaInputMeta)
    assert stored is not None
    body, meta = stored
    assert body == OGG
    assert (meta.source, meta.kind, meta.file_name) == ("upload", "ogg", "notes.txt")


class AsrAnswering:
    """A stand-in for the ASR worker's runner: checks what it reads, answers ``answer``."""

    lease_seconds = 60.0

    def __init__(self, harness: Harness, answer: Envelope[ExtractResult]) -> None:
        self._harness = harness
        self._answer = answer

    async def run(self, job: JobRecord, item: QueueItem, url: str, **kwargs: Any) -> Any:
        assert await self._harness.store.get_input(job.id, item.index, MediaInputMeta)
        return self._answer


def asr_worker(harness: Harness, runner: Any) -> Worker:
    return Worker(
        harness.settings,
        harness.valkey,
        runner,
        harness.store,
        harness.asr_queue,
        harness.registry,
        clock=harness.clock,
    )


def metadata_draft() -> Envelope[ExtractResult]:
    """A platform video's metadata result, as the API stores it as the item's draft."""
    result = ExtractResult(
        url=YOUTUBE,
        final_url=YOUTUBE,
        upstream_status=200,
        content_type="text/html",
        mode_used="fast",
        attempts=1,
        language="en",
        word_count=3,
        title="A lesson",
        format="markdown",
        content="# A lesson",
        quality=None,
        timings=Timings(total_ms=1, compliance_ms=0, fetch_ms=1, convert_ms=0),
        compliance=ComplianceReport(mode="enforce", decision="allowed"),
        metadata=None,
        links=None,
        fingerprint=None,
        cache="miss",
        warnings=[],
    )
    error = ErrorInfo(code=ErrorCode.NO_TRANSCRIPT_AVAILABLE, message="none", retryable=False)
    return Envelope[ExtractResult](status="partial", data=result, error=error)


async def queue_one_transcription(harness: Harness, *, key: str | None = None) -> str:
    """A sync request's one-item job on pool ``asr``, as the API's dispatch leaves it."""
    options = JobOptions(compliance="enforce", transcribe=True)
    job_id = await harness.store.create("extract", OWNER, options, [YOUTUBE])
    meta = MediaInputMeta(
        source="platform",
        platform="youtube",
        url=YOUTUBE,
        extractor="youtube",
        kind=None,
        file_name=None,
        duration=None,
        languages=[],
        compliance=None,
        attempts=1,
    )
    await harness.store.put_input(job_id, 0, b"", meta, 600)
    if key is not None:
        await harness.store.put_asr_key(job_id, key)
    await harness.store.put_draft(job_id, 0, metadata_draft())
    await harness.asr_queue.enqueue_items(job_id, "alice#live", [(0, YOUTUBE)])
    return job_id


@scenario
async def test_asr_worker_deletes_input_draft_and_key_with_the_outcome() -> None:
    harness = asr_harness()
    job_id = await queue_one_transcription(harness, key="sk-caller")
    worker = asr_worker(harness, AsrAnswering(harness, Envelope[ExtractResult](status="failed")))

    claim = await harness.asr_queue.claim(60)
    assert claim.item is not None
    await worker.process(claim.item)

    assert await harness.completed(job_id)
    assert await harness.valkey.keys("job:*:input:*") == []
    assert await harness.store.draft(job_id, 0) is None
    assert await harness.store.asr_key(job_id) is None


@scenario
async def test_exhausted_asr_item_answers_its_draft_with_asr_unavailable() -> None:
    harness = asr_harness()
    job_id = await queue_one_transcription(harness, key="sk-caller")
    worker = asr_worker(harness, AsrAnswering(harness, Envelope[ExtractResult](status="failed")))

    claim = await harness.asr_queue.claim(60)
    assert claim.item is not None
    await worker.process(claim.item, exhausted=True)

    (item,) = await harness.items(job_id)
    assert item.result is not None
    assert item.result.status == "partial"  # the draft, not a lost item's internal_error
    assert item.result.data is not None
    assert ASR_UNAVAILABLE_WARNING in item.result.data.warnings
    assert item.result.data.fingerprint is None  # neither cached nor deduplicated
    assert await harness.store.asr_key(job_id) is None
    assert await harness.valkey.keys("job:*:input:*") == []
