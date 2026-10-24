"""Metrics of the job system that the API pods export.

- ``text_extraction_queue_pending_items{pool}``, ``..._leased_items{pool}`` and
  ``..._clients{pool}``: the backlog of the fair queue, for autoscaling (KEDA). The API pods
  export them, not the workers, because workers may scale to zero and the backlog must stay
  visible. Every API pod reads the same queue, so a query takes ``max()`` over pods (``sum()``
  would count the backlog once per pod).
- ``text_extraction_jobs_created_total{kind}``: jobs accepted by this pod (``batch`` or
  ``extract``).
- ``text_extraction_job_duration_seconds{kind}``: observed by whichever process writes the last
  result of a job. The definition lives here so that the worker and the API cannot drift apart;
  each process has its own registry.

``FairQueue.counts()`` walks the clients' lists, so a background task reads it every
``interval`` seconds, not once per scrape; the collector only reports the last values (collectors
are synchronous). When Valkey fails the last values stay and the outage is logged once.
"""

import asyncio
import contextlib
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Final, Protocol
from weakref import WeakKeyDictionary

import structlog
from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from text_extraction.scheduler import QueueCounts

_log = structlog.get_logger(__name__)

# Seconds between two reads of the queue.
REFRESH_SECONDS: Final = 5.0

JOB_DURATION_BUCKETS: Final = (
    1,
    5,
    15,
    30,
    60,
    120,
    300,
    600,
    1800,
    3600,
    7200,
    14400,
    43200,
    86400,
)

# One histogram per registry, so the worker and the API share the definition and tests can share a
# registry.
_JOB_DURATION: WeakKeyDictionary[CollectorRegistry, Histogram] = WeakKeyDictionary()


def job_duration_for(registry: CollectorRegistry) -> Histogram:
    """``text_extraction_job_duration_seconds{kind}`` in ``registry``."""
    histogram = _JOB_DURATION.get(registry)
    if histogram is None:
        histogram = Histogram(
            "text_extraction_job_duration_seconds",
            "Time from the creation of a job to its last result, by job kind.",
            ["kind"],
            buckets=JOB_DURATION_BUCKETS,
            registry=registry,
        )
        _JOB_DURATION[registry] = histogram
    return histogram


@dataclass(frozen=True)
class JobMetrics:
    """What the routes of an API pod record about jobs."""

    created: Counter
    duration: Histogram


_JOB_METRICS: WeakKeyDictionary[CollectorRegistry, JobMetrics] = WeakKeyDictionary()


def job_metrics_for(registry: CollectorRegistry) -> JobMetrics:
    metrics = _JOB_METRICS.get(registry)
    if metrics is None:
        created = Counter(
            "text_extraction_jobs_created_total",
            "Jobs accepted by this pod, by kind: batch (POST /v1/batch) or extract (a slow "
            "POST /v1/extract handed over to a job).",
            ["kind"],
            registry=registry,
        )
        for kind in ("batch", "extract"):
            created.labels(kind=kind)  # export 0 from the start, so rate() sees the first job
        metrics = JobMetrics(created=created, duration=job_duration_for(registry))
        _JOB_METRICS[registry] = metrics
    return metrics


class _CountedQueue(Protocol):
    @property
    def pool(self) -> str: ...

    async def counts(self) -> QueueCounts: ...


_GAUGES: Final = (
    ("text_extraction_queue_pending_items", "Items waiting in the queue of the pool."),
    ("text_extraction_queue_leased_items", "Items of the pool that a worker has leased."),
    ("text_extraction_queue_clients", "Clients with items waiting in the queue of the pool."),
)
_LAST_SUCCESS: Final = (
    "text_extraction_queue_metrics_last_success_timestamp_seconds",
    "Unix time of the last successful read of the queue of the pool. It stays unchanged while "
    "Valkey fails, so time() minus it tells how stale the queue gauges are.",
)


class QueueGauges(Collector):
    """The queue gauges of one pool, fed by a background task.

    Until the first read succeeds nothing is exported, so that a scaler never mistakes "not
    known yet" for an empty queue.
    """

    def __init__(
        self,
        queue: _CountedQueue,
        registry: CollectorRegistry,
        *,
        interval: float = REFRESH_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._queue = queue
        self._interval = interval
        self._clock = clock
        self._last_success: float | None = None
        self._counts: QueueCounts | None = None
        self._down = False
        self._task: asyncio.Task[None] | None = None
        registry.register(self)

    @property
    def running(self) -> bool:
        """True while the refresh task is running (started and not stopped)."""
        return self._task is not None and not self._task.done()

    def describe(self) -> Iterable[GaugeMetricFamily]:
        return [
            GaugeMetricFamily(name, doc, labels=["pool"]) for name, doc in (*_GAUGES, _LAST_SUCCESS)
        ]

    def collect(self) -> Iterator[GaugeMetricFamily]:
        counts = self._counts
        if counts is None:
            return
        pool = self._queue.pool
        values = (counts.pending, counts.leased, counts.clients)
        for (name, doc), value in zip(_GAUGES, values, strict=True):
            family = GaugeMetricFamily(name, doc, labels=["pool"])
            family.add_metric([pool], value)
            yield family
        if self._last_success is not None:
            family = GaugeMetricFamily(*_LAST_SUCCESS, labels=["pool"])
            family.add_metric([pool], self._last_success)
            yield family

    async def refresh(self) -> None:
        """Read the queue once. A failure (Valkey is down, mostly) keeps the last values and is
        logged on the first failure of an outage; the first success after it is logged too."""
        try:
            counts = await self._queue.counts()
        except Exception as error:  # a failing read must never end the loop
            if not self._down:
                self._down = True
                _log.warning(
                    "queue_metrics_unavailable",
                    pool=self._queue.pool,
                    error_type=type(error).__name__,
                    error=str(error),
                )
            return
        self._counts = counts
        self._last_success = self._clock()
        if self._down:
            self._down = False
            _log.info("queue_metrics_recovered", pool=self._queue.pool)

    async def _run(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(self._interval)

    def start(self) -> None:
        """Start the refresh task in the running event loop; a second call does nothing."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="queue-gauges")

    async def stop(self) -> None:
        """Cancel the refresh task and wait until it has ended. Call it before the Valkey client
        is closed."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
