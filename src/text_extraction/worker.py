"""The extract worker (entry point ``text-extraction-worker``): runs batch items from the fair
queue through the fast pipeline and stores their results in the job.

A worker pod runs ``WORKER_CONCURRENCY`` slot loops. Each loop:

1. claims the next item, leased for ``EXTRACT_TIMEOUT_SECONDS * 2 + 10`` seconds. An item that
   lost too many leases (``exhausted``) fails with ``internal_error``, retryable, and is completed
   once that is recorded or its job is gone. A Valkey error leaves it leased: it comes back after
   the lease, so its batch still gets a result for it;
2. with nothing to claim, sleeps ``min(retry_in or 1, 1)`` seconds, or backs off from 0.05 s to
   1 s while claims keep answering that their scan budget ran out; every idle sleep has ±50 %
   jitter, so the loops of many pods do not claim in step;
3. rebuilds the request from the job (its options and its owner as the principal) and runs the
   pipeline. When the outbound gate refuses the host (busy or paused), the item is deferred to
   the gate's retry time, which frees the slot for other domains. An item at
   ``JOB_ITEM_MAX_ATTEMPTS`` is not deferred again: it runs once more with the refusal answered
   as the pipeline's ``rate_limited`` or ``connection_error`` envelope, which is stored;
4. stores the result (only the first result of an item counts), then completes the item. An item
   whose job expired is completed and dropped. A worker whose lease was taken back (it hung past
   it) still stores its result, but its repeat write or deferral is not counted in the metrics.

A Valkey error, an ``OSError`` (a Valkey call timing out) or the gate's 503 backs the loop off
for 1, 2, 4 … 30 s; the lease of the item in hand then expires and the item is claimed again.
No error ends a loop.

The process serves only the internal port (``/health``, ``/ready``, ``/metrics``). ``/ready``
answers 200 once the loops run, while Valkey answers. On SIGTERM or SIGINT it turns 503, the
loops stop claiming, the items in flight get ``graceful_shutdown_seconds`` to finish (any still
running then are cancelled; their leases expire and other workers take them over), and the
internal server stops last. A claim already sent when the signal arrives is still processed
within that time, since its item is leased to this worker already.
"""

import asyncio
import contextlib
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal
from weakref import WeakKeyDictionary

import structlog
from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram
from redis.asyncio import Redis
from redis.exceptions import RedisError

from text_extraction.auth import Principal
from text_extraction.conversion_pool import ConversionPool
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.errors import ServiceError
from text_extraction.extract_models import ExtractRequest, ExtractResult
from text_extraction.fetch import Fetcher
from text_extraction.internal import Readiness, create_internal_app
from text_extraction.jobs import JobLost, JobRecord, JobStore
from text_extraction.metadata import load_language_model
from text_extraction.outbound import CircuitOpen, OutboundBusy
from text_extraction.pipeline import Pipeline, create_pipeline
from text_extraction.queue_metrics import job_duration_for
from text_extraction.scheduler import FairQueue, QueueItem
from text_extraction.server import (
    _HANDLED_SIGNALS,
    _Server,
    _server,
    configured_settings,
    graceful_shutdown_seconds,
)
from text_extraction.settings import Settings
from text_extraction.valkey import create_valkey

_log = structlog.get_logger(__name__)

# Idle sleep without an item: the most a loop waits before it claims again.
_IDLE_SECONDS = 1.0
# First sleep after a claim that ran out of its scan budget; it doubles up to the idle sleep.
_SCAN_BACKOFF_SECONDS = 0.05
# Backoff while Valkey fails: 1, 2, 4 … seconds, at most this.
_MAX_VALKEY_BACKOFF_SECONDS = 30.0
# Pause after an unexpected error in a loop, so a bug cannot spin it.
_ERROR_PAUSE_SECONDS = 1.0

_LOST_MESSAGE = "The item was lost by a worker too often."
_INTERNAL_MESSAGE = "Internal server error."

# Outcomes of an item besides its envelope's: given back to the queue, or its job is gone.
_DEFERRED = "deferred"
_LOST = "lost"
# Logged, not counted: the item had its result already, or another worker holds it now.
_SUPERSEDED = "superseded"

_ITEM_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 20, 25, 30, 45, 60)


@dataclass(frozen=True)
class _Metrics:
    items: Counter
    item_seconds: Histogram
    busy: Gauge
    job_duration: Histogram


# One set of metrics per registry, so several workers (tests) can share one.
_METRICS: WeakKeyDictionary[CollectorRegistry, _Metrics] = WeakKeyDictionary()


def _metrics_for(registry: CollectorRegistry) -> _Metrics:
    metrics = _METRICS.get(registry)
    if metrics is None:
        metrics = _Metrics(
            items=Counter(
                "text_extraction_job_items_total",
                "Job items handled by this worker, by outcome: success, the error code of the "
                "result, deferred (given back while the host is busy or paused) or lost (the "
                "job was gone).",
                ["outcome"],
                registry=registry,
            ),
            item_seconds=Histogram(
                "text_extraction_job_item_seconds",
                "Time one job item took in the worker, from the claim to the stored result.",
                buckets=_ITEM_BUCKETS,
                registry=registry,
            ),
            busy=Gauge(
                "text_extraction_worker_busy_slots",
                "Slots of this worker that are processing an item.",
                registry=registry,
            ),
            job_duration=job_duration_for(registry),
        )
        _METRICS[registry] = metrics
    return metrics


def _failure(code: ErrorCode, message: str, *, retryable: bool) -> Envelope[ExtractResult]:
    return Envelope[ExtractResult](
        status="failed", error=ErrorInfo(code=code, message=message, retryable=retryable)
    )


def _outcome(envelope: Envelope[ExtractResult]) -> str:
    """Metric label: the envelope's error code, else its status (``success``)."""
    return envelope.error.code.value if envelope.error is not None else envelope.status


class Worker:
    """Processes the items of the ``extract`` queue with ``WORKER_CONCURRENCY`` slot loops.

    ``valkey`` is the client behind ``store`` and ``queue``; the caller closes it. ``clock``
    (epoch seconds), ``sleep`` and ``random`` (for the jitter) can be replaced in tests.
    """

    def __init__(
        self,
        settings: Settings,
        valkey: Redis,
        pipeline: Pipeline,
        store: JobStore,
        queue: FairQueue,
        registry: CollectorRegistry,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random: Callable[[], float] = random.random,
    ) -> None:
        self._settings = settings
        self._valkey = valkey
        self._pipeline = pipeline
        self._store = store
        self._queue = queue
        self._metrics = _metrics_for(registry)
        self._clock = clock
        self._sleep = sleep
        self._random = random
        self._lease_seconds = settings.extract_timeout_seconds * 2 + 10

    async def run(self, stop: asyncio.Event) -> None:
        """Run the slot loops until ``stop`` is set and the items in flight have finished."""
        async with asyncio.TaskGroup() as slots:
            for _ in range(self._settings.worker_concurrency):
                slots.create_task(self._slot(stop))

    async def process(self, item: QueueItem, *, exhausted: bool = False) -> None:
        """Handle one claimed item (``exhausted`` as the claim said). Valkey errors propagate."""
        started = time.perf_counter()
        self._metrics.busy.inc()
        try:
            with structlog.contextvars.bound_contextvars(job_id=item.job_id, item=item.index):
                if exhausted:
                    outcome = await self._fail_exhausted(item)
                else:
                    outcome = await self._extract(item)
                _log.info("job_item_done", attempts=item.attempts, outcome=outcome or _SUPERSEDED)
        finally:
            self._metrics.busy.dec()
            self._metrics.item_seconds.observe(time.perf_counter() - started)
        if outcome is not None:
            self._metrics.items.labels(outcome=outcome).inc()

    # --- the slot loop -----------------------------------------------------------------------

    async def _slot(self, stop: asyncio.Event) -> None:
        failures = 0  # consecutive rounds that Valkey failed
        scan_wait = _SCAN_BACKOFF_SECONDS  # next sleep after an incomplete scan
        while not stop.is_set():
            try:
                claim = await self._queue.claim(self._lease_seconds)
                if claim.item is None:
                    if claim.scan_incomplete:
                        delay, scan_wait = scan_wait, min(scan_wait * 2, _IDLE_SECONDS)
                    else:
                        delay, scan_wait = _IDLE_SECONDS, _SCAN_BACKOFF_SECONDS
                        if claim.retry_in is not None:
                            delay = min(claim.retry_in, _IDLE_SECONDS)
                    failures = 0
                    await self._pause(stop, delay * (0.5 + self._random()))
                    continue
                scan_wait = _SCAN_BACKOFF_SECONDS
                await self.process(claim.item, exhausted=claim.exhausted)
                failures = 0
            except (RedisError, OSError, ServiceError) as error:
                # A ServiceError here is the outbound gate's 503: its Valkey state is unavailable.
                failures += 1
                delay = min(2.0 ** (failures - 1), _MAX_VALKEY_BACKOFF_SECONDS)
                _log.warning(
                    "worker_valkey_unavailable", error_type=type(error).__name__, retry_in=delay
                )
                await self._pause(stop, delay)
            except Exception:
                _log.error("worker_slot_error", exc_info=True)
                await self._pause(stop, _ERROR_PAUSE_SECONDS)

    async def _pause(self, stop: asyncio.Event, seconds: float) -> None:
        """Sleep ``seconds``, or less when ``stop`` is set meanwhile."""
        if stop.is_set():
            return
        sleeping = asyncio.ensure_future(self._sleep(seconds))
        stopping = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait([sleeping, stopping], return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeping.cancel()
            stopping.cancel()

    # --- one item ----------------------------------------------------------------------------

    async def _extract(self, item: QueueItem) -> str | None:
        """The item's metric outcome; None when it is not to be counted (``_SUPERSEDED``)."""
        job = await self._store.get(item.job_id)
        url = await self._store.url(item.job_id, item.index) if job is not None else None
        if job is None or url is None:
            return await self._drop(item)
        if job.state == "queued":
            try:
                await self._store.mark_running(item.job_id)
            except JobLost:
                return await self._drop(item)
        # At the limit, a refusal of the gate is the result: the pipeline answers it.
        at_limit = item.attempts >= self._settings.job_item_max_attempts
        gate_refusals: Literal["fail", "raise"] = "fail" if at_limit else "raise"
        try:
            envelope = await self._pipeline.run(
                ExtractRequest(url=url, **job.options.model_dump()),
                Principal(name=job.owner, role=job.owner_role, rate_limit_per_minute=None),
                gate_refusals=gate_refusals,
            )
        except (OutboundBusy, CircuitOpen) as refusal:
            if await self._queue.defer(item, self._clock() + refusal.retry_after):
                return _DEFERRED
            return None  # the lease was lost: the item is queued again or another worker's
        except ServiceError as error:
            if error.http_status >= 500:
                raise
            envelope = _failure(error.code, error.message, retryable=error.retryable)
        except (RedisError, OSError):
            raise  # Valkey (a builtin TimeoutError too): the slot loop backs off
        except Exception:
            _log.error("job_item_failed", exc_info=True)
            envelope = _failure(ErrorCode.INTERNAL_ERROR, _INTERNAL_MESSAGE, retryable=False)
        return await self._finish(item, job, envelope)

    async def _fail_exhausted(self, item: QueueItem) -> str | None:
        """Record the item as lost too often, then complete it. A Valkey error propagates and
        leaves the item leased, so it is offered again after the lease instead of vanishing
        without a result."""
        _log.warning("job_item_exhausted", attempts=item.attempts)
        job = await self._store.get(item.job_id)
        if job is None:
            return await self._drop(item)
        envelope = _failure(ErrorCode.INTERNAL_ERROR, _LOST_MESSAGE, retryable=True)
        return await self._finish(item, job, envelope)

    async def _finish(
        self, item: QueueItem, job: JobRecord, envelope: Envelope[ExtractResult]
    ) -> str | None:
        """Store the item's result, then complete it. The write that completes the job observes
        its duration; a repeat write (the item had its result already) is not counted."""
        try:
            write = await self._store.write_result(item.job_id, item.index, envelope)
        except JobLost:
            return await self._drop(item)
        if write == "last":
            seconds = max(self._clock() - job.created_at.timestamp(), 0.0)
            self._metrics.job_duration.labels(kind=job.kind).observe(seconds)
            _log.info("job_completed", kind=job.kind, seconds=round(seconds, 3))
        await self._queue.complete(item)
        return None if write == "repeat" else _outcome(envelope)

    async def _drop(self, item: QueueItem) -> str:
        """Complete an item whose job is gone (expired): there is nowhere to store its result."""
        await self._queue.complete(item)
        _log.warning("job_item_dropped", reason="job_gone")
        return _LOST


# --- the process -------------------------------------------------------------------------------


async def _serve(
    worker: Worker, internal: _Server, readiness: Readiness, *, graceful_seconds: float
) -> None:
    """Serve the internal app, load the language model, then run the worker until a signal and
    shut down in the order the module describes."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in _HANDLED_SIGNALS:
        loop.add_signal_handler(sig, stop.set)
    internal_task = asyncio.create_task(internal.serve())
    stopping = asyncio.create_task(stop.wait())
    starting = asyncio.create_task(asyncio.to_thread(load_language_model))
    work: asyncio.Task[None] | None = None
    try:
        await asyncio.wait([starting, stopping, internal_task], return_when=asyncio.FIRST_COMPLETED)
        if starting.done() and not stop.is_set() and not internal_task.done():
            starting.result()  # raises a failure of the start-up
            work = asyncio.create_task(worker.run(stop))
            readiness.mark_listening()
            _log.info("worker_started")
            await asyncio.wait([work, stopping, internal_task], return_when=asyncio.FIRST_COMPLETED)
        readiness.mark_draining()
        stop.set()
        if work is not None:
            _log.info("shutdown_requested", graceful_seconds=graceful_seconds)
            done, _ = await asyncio.wait([work], timeout=graceful_seconds)
            if not done:
                _log.warning("worker_shutdown_timeout", graceful_seconds=graceful_seconds)
                work.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await work
            else:
                work.result()  # raises a failure of the loops
        internal.should_exit = True
        await internal_task
    finally:
        stopping.cancel()
        starting.cancel()  # a signal during the start-up: the thread still ends by itself
        internal.should_exit = True
        for sig in _HANDLED_SIGNALS:
            loop.remove_signal_handler(sig)


async def _run(settings: Settings) -> None:
    valkey = create_valkey(settings.valkey_url)
    fetcher = Fetcher(settings)
    pool = ConversionPool(settings.convert_concurrency)
    registry = REGISTRY
    worker = Worker(
        settings,
        valkey,
        create_pipeline(settings, valkey, fetcher, registry, pool),
        JobStore(valkey, settings),
        FairQueue(valkey, settings),
        registry,
    )
    graceful = graceful_shutdown_seconds(settings)
    readiness = Readiness()
    internal = _server(
        create_internal_app(settings, valkey, registry, readiness=readiness),
        settings.internal_host,
        settings.internal_port,
        graceful,
    )
    try:
        await _serve(worker, internal, readiness, graceful_seconds=graceful)
    finally:
        await fetcher.aclose()
        pool.shutdown()
        await valkey.aclose()
        _log.info("valkey_closed")


def main() -> None:
    asyncio.run(_run(configured_settings()))
