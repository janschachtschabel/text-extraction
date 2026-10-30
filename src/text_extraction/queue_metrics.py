"""Metrics of the job system that the API pods export.

- ``text_extraction_queue_pending_items{pool}``, ``..._leased_items{pool}`` and
  ``..._clients{pool}``: the backlog of the fair queue, for autoscaling (KEDA). The API pods
  export them, not the workers, because workers may scale to zero and the backlog must stay
  visible. Every API pod reads the same queue, so a query takes ``max()`` over pods (``sum()``
  would count the backlog once per pod).
- ``text_extraction_jobs_created_total{kind}``: jobs accepted by this pod (``batch``,
  ``extract``, ``screenshot``, ``file`` or ``site``). A sync request's browser, Docling or ASR
  job counts once it is handed over to the caller (202), like a P1 job.
- ``text_extraction_job_duration_seconds{kind}``: observed by whichever process writes the last
  result of a job. The definition lives here so that the worker and the API cannot drift apart;
  each process has its own registry.
- ``text_extraction_browser_dispatch_total{outcome}``: how the sync requests of this pod that
  went to the browser workers ended (``browser_dispatch.DISPATCH_OUTCOMES``).
- ``text_extraction_docling_dispatch_total{outcome}``: how the dispatch of this pod's sync
  requests with a document that needs OCR ended (``docling_dispatch.DOCLING_OUTCOMES``). The queue
  gauges have pool ``docling`` while the service has OCR.
- ``text_extraction_asr_dispatch_total{outcome}``: how the dispatch of this pod's media that
  needs a transcription ended (``asr_dispatch.ASR_OUTCOMES``). The queue gauges have pool ``asr``
  while the service has ASR.
- ``text_extraction_nlp_dispatch_total{outcome}``: how the dispatch of this pod's results that
  need PII handling ended (``pii_dispatch.PII_OUTCOMES``). The queue gauges have pool ``nlp``
  while the service has PII handling (``PII_ENABLED``).
- ``text_extraction_llm_dispatch_total{outcome}``: how the dispatch of this pod's results whose
  post stage needs the LLM ended (``llm_dispatch.LLM_OUTCOMES``). The queue gauges have pool
  ``llm`` while the service has the LLM (``LLM_ENABLED``).

A background task reads ``FairQueue.counts()`` (three size reads in Valkey) every ``interval``
seconds, not once per scrape, since collectors are synchronous; the collector only reports the
last values. When Valkey fails the last values stay and the outage is logged once.
"""

import asyncio
import contextlib
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
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
            "Jobs accepted by this pod, by kind: batch (POST /v1/batch), extract (a slow "
            "POST /v1/extract handed over to a job), screenshot (a slow POST /v1/screenshot) or "
            "file (a slow POST /v1/extract/file) or site (POST /v1/site-structure).",
            ["kind"],
            registry=registry,
        )
        for kind in ("batch", "extract", "screenshot", "file", "site"):
            created.labels(kind=kind)  # export 0 from the start, so rate() sees the first job
        metrics = JobMetrics(created=created, duration=job_duration_for(registry))
        _JOB_METRICS[registry] = metrics
    return metrics


# One counter per registry and pool, as for the job metrics.
_DISPATCH: WeakKeyDictionary[CollectorRegistry, dict[str, Counter]] = WeakKeyDictionary()
_DISPATCH_HELP: Final = {
    "browser": "Sync requests this pod sent to the browser workers, by how they ended: rendered, "
    "not_claimed, no_result, handed_over or backend_error.",
    "docling": "Documents of this pod that needed OCR, by how their dispatch to the Docling "
    "workers ended: answered, not_claimed, no_result, handed_over, backend_error (sync "
    "requests), memory_guard, open_limit or input_budget (nothing dispatched; batch items count "
    "memory_guard and input_budget).",
    "asr": "Media of this pod that needed a transcription, by how its dispatch to the ASR "
    "workers ended: handed_over, backend_error, memory_guard, open_limit or input_budget "
    "(nothing dispatched; batch items count memory_guard and input_budget).",
    "nlp": "Results of this pod that needed PII handling, by how their dispatch to the nlp "
    "workers ended: answered, not_claimed, no_result, handed_over, backend_error (sync "
    "requests) or memory_guard (nothing dispatched).",
    "llm": "Results of this pod whose post stage needed the LLM, by how their dispatch to the "
    "llm workers ended: answered, not_claimed, no_result, handed_over, backend_error (sync "
    "requests) or memory_guard (nothing dispatched).",
}


def dispatch_counter_for(
    registry: CollectorRegistry, outcomes: Iterable[str], *, pool: str = "browser"
) -> Counter:
    """``text_extraction_<pool>_dispatch_total{outcome}`` in ``registry``, every one of
    ``outcomes`` exported as 0 from the start."""
    counters = _DISPATCH.setdefault(registry, {})
    counter = counters.get(pool)
    if counter is None:
        counter = Counter(
            f"text_extraction_{pool}_dispatch_total",
            _DISPATCH_HELP.get(pool, f"Sync requests this pod sent to the {pool} workers."),
            ["outcome"],
            registry=registry,
        )
        counters[pool] = counter
    for outcome in outcomes:
        counter.labels(outcome=outcome)
    return counter


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


class _PoolGauges:
    """The last counts of one pool's queue and whether its reads are failing."""

    def __init__(self, queue: _CountedQueue) -> None:
        self.queue = queue
        self.counts: QueueCounts | None = None
        self.last_success: float | None = None
        self.down = False

    async def refresh(self, clock: Callable[[], float]) -> None:
        try:
            counts = await self.queue.counts()
        except Exception as error:  # a failing read must never end the loop
            if not self.down:
                self.down = True
                _log.warning(
                    "queue_metrics_unavailable",
                    pool=self.queue.pool,
                    error_type=type(error).__name__,
                    error=str(error),
                )
            return
        self.counts = counts
        self.last_success = clock()
        if self.down:
            self.down = False
            _log.info("queue_metrics_recovered", pool=self.queue.pool)


class QueueGauges(Collector):
    """The queue gauges of one or more pools, fed by a background task.

    Until the first read of a pool succeeds nothing is exported for it, so that a scaler never
    mistakes "not known yet" for an empty queue.
    """

    def __init__(
        self,
        queues: _CountedQueue | Sequence[_CountedQueue],
        registry: CollectorRegistry,
        *,
        interval: float = REFRESH_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        listed = tuple(queues) if isinstance(queues, Sequence) else (queues,)
        self._pools = [_PoolGauges(queue) for queue in listed]
        self._interval = interval
        self._clock = clock
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
        known = [pool for pool in self._pools if pool.counts is not None]
        if not known:
            return
        values = {
            pool.queue.pool: (counts.pending, counts.leased, counts.clients)
            for pool in known
            if (counts := pool.counts) is not None
        }
        for index, (name, doc) in enumerate(_GAUGES):
            family = GaugeMetricFamily(name, doc, labels=["pool"])
            for name_of_pool, counted in values.items():
                family.add_metric([name_of_pool], counted[index])
            yield family
        family = GaugeMetricFamily(*_LAST_SUCCESS, labels=["pool"])
        for pool in known:
            if pool.last_success is not None:
                family.add_metric([pool.queue.pool], pool.last_success)
        yield family

    async def refresh(self) -> None:
        """Read each queue once. A failure (Valkey is down, mostly) keeps the pool's last values
        and is logged on the first failure of an outage; the first success after it is logged
        too."""
        for pool in self._pools:
            await pool.refresh(self._clock)

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
