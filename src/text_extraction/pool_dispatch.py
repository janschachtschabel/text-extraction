"""Sync requests' work on another worker pool: a one-item job, queued, waited for, and deleted.

API pods hand two kinds of work to worker pools and wait for it: renders (pool ``browser``,
``browser_dispatch.py``) and OCR (pool ``docling``). ``PoolDispatcher`` is what both share:

1. ``submit_job`` creates a job with one item (or reuses the request's P1 job), stores the item's
   draft, and queues item 0 on the pool under the client ``<key name>#live``, so that the key's
   own batches do not queue it. A reused job's lease is cleared once its item is queued: the pool
   has its own time, and the lost view must not end it. A job created here is deleted again when
   anything fails before the item is queued, also on cancellation.
2. ``wait_for`` polls the job every 0.2 s, rising to 0.5 s, until the job is completed, gone, or a
   limit passed: no claim (the job is still ``queued``) within ``claim_within`` seconds, no result
   ``result_within`` seconds after the claim, or ``total_within`` seconds in all.
3. ``abandon`` deletes the job of a request that gives up: a worker that claims the item later
   finds no job and drops it. ``answered`` deletes the job of a request that answered with its
   result; one whose result has a screenshot is kept as long as the screenshot.

Each pool counts how its dispatches ended in a counter of its own
(``text_extraction_<pool>_dispatch_total{outcome}``, ``queue_metrics.dispatch_counter_for``).

``Handover`` is what the route of a request with ``wait_seconds`` and its task share. Exactly one
of them may turn the request into a job (``claim_p1`` or ``claim_pool``, decided without an
``await`` in between): the route's P1 job during the fast phase, or the task's pool job (a
browser or a Docling job; ``pool`` names it). The route then answers 202 with that job; for the
pool job it detaches the task, which stops waiting and leaves the job to the worker. A task that
gives up releases the job first; detaching and releasing exclude each other (both are
synchronous), so a job answered with 202 is never deleted, and a job being deleted is never
answered with 202.

The ``docling`` pool's policy (OCR) and the contract of its worker are in ``docling_dispatch.py``.

Logs carry job ids only, never a URL, header, cookie, action or document byte.
"""

import asyncio
import contextlib
import math
import time
from collections.abc import Awaitable, Callable
from typing import Literal

import structlog
from prometheus_client import CollectorRegistry
from redis.exceptions import RedisError

from text_extraction.auth import Principal
from text_extraction.browser_options import BrowserSecrets
from text_extraction.compliance import ComplianceMode
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractRequest, ExtractResult
from text_extraction.job_models import JobKind, JobOptions
from text_extraction.jobs import JobLost, JobStore
from text_extraction.queue_metrics import dispatch_counter_for
from text_extraction.scheduler import FairQueue
from text_extraction.settings import Settings
from text_extraction.tracing import current_trace_id

_log = structlog.get_logger(__name__)

# The poll interval of ``wait_for``: it starts at the first value and rises by the step to the last.
_POLL_FIRST_SECONDS = 0.2
_POLL_STEP_SECONDS = 0.1
_POLL_MAX_SECONDS = 0.5

# What a dispatch fails with when Valkey fails (``JobLost``: the job vanished meanwhile).
BACKEND_ERRORS = (RedisError, OSError, JobLost)


def live_client(principal: Principal) -> str:
    """The queue client of a key's sync requests: apart from its batches, so they never wait
    behind them."""
    return f"{principal.name}#live"


def job_options(req: ExtractRequest, compliance: ComplianceMode) -> JobOptions:
    """The stored options of a job for ``req``, with the compliance mode resolved for its
    caller."""
    return JobOptions(
        mode=req.mode,
        format=req.format,
        metadata=req.metadata,
        links=req.links,
        compliance=compliance,
        cache=req.cache,
        screenshot=req.screenshot,
        full_page=req.full_page,
        ocr=req.ocr,
        transcribe=req.transcribe,
        languages=req.languages,
    )


class Handover:
    """What the route of a request with ``wait_seconds`` and its task (``extract_api._flow``)
    share; see the module docstring.

    ``job_id`` is the P1 job the route created, ``pool_job`` the job the task dispatched to
    ``pool`` (``browser`` or ``docling``). The side that claimed calls ``settle`` once its job
    exists, or failed to; the other side waits for ``settled`` before it looks at the ids.
    """

    def __init__(self) -> None:
        self.job_id: str | None = None
        self.pool_job: str | None = None
        self.pool: str | None = None
        self._claimed: Literal["p1", "pool"] | None = None
        self._settled = asyncio.Event()
        self._detached = False
        self._released = False

    @property
    def claimed(self) -> Literal["p1", "pool"] | None:
        return self._claimed

    def claim_p1(self) -> bool:
        """The route turns the request into a P1 job; False when the task dispatched first."""
        return self._claim("p1")

    def claim_pool(self, pool: str) -> bool:
        """The task dispatches a job to ``pool``; False when the route is creating a P1 job."""
        if not self._claim("pool"):
            return False
        self.pool = pool
        return True

    def _claim(self, side: Literal["p1", "pool"]) -> bool:
        if self._claimed is not None:
            return False
        self._claimed = side
        return True

    def settle(self, job_id: str | None) -> None:
        """The claiming side's job is created (``job_id``) or could not be (None)."""
        if self._claimed == "p1":
            self.job_id = job_id
        else:
            self.pool_job = job_id
        self._settled.set()

    async def settled(self) -> None:
        await self._settled.wait()

    def detach(self) -> bool:
        """The route answers 202 with the pool job: the task stops waiting for it and leaves
        the job to the worker. False when the task has released the job already (it is deleting
        it and answers on its own); then the route waits for that answer."""
        if self._released:
            return False
        self._detached = True
        return True

    def release(self) -> bool:
        """The task ends without leaving its job to the worker (it is about to delete it). False
        when the route has detached already: the job is the caller's now and must stay."""
        if self._detached:
            return False
        self._released = True
        return True

    @property
    def detached(self) -> bool:
        return self._detached


class PoolDispatcher:
    """Puts sync requests' items on the queue of one pool and waits for their results; see the
    module docstring. ``registry`` gets the pool's dispatch counter with ``outcomes`` (a registry
    of its own when None). ``clock`` (monotonic seconds) and ``sleep`` can be replaced in
    tests."""

    def __init__(
        self,
        pool: str,
        store: JobStore,
        queue: FairQueue,
        settings: Settings,
        registry: CollectorRegistry | None,
        *,
        outcomes: tuple[str, ...],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.pool = pool
        self._store = store
        self._queue = queue
        self._settings = settings
        self._clock = clock
        self._sleep = sleep
        self._outcomes = dispatch_counter_for(
            registry if registry is not None else CollectorRegistry(), outcomes, pool=pool
        )

    def count(self, outcome: str) -> None:
        self._outcomes.labels(outcome=outcome).inc()

    async def submit_job(
        self,
        kind: JobKind,
        options: JobOptions,
        principal: Principal,
        url: str | None,
        *,
        job_id: str | None = None,
        draft: Envelope[ExtractResult] | None = None,
        secrets: BrowserSecrets | None = None,
        prepare: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """Queue item 0 of a new job (or of ``job_id``, the request's P1 job) on the pool and
        return the job's id. ``prepare`` runs before the draft is stored (a Docling job's input);
        ``draft`` is the result the worker compares its own with. Valkey errors propagate, and so
        does whatever ``prepare`` raises; a new job is deleted again first."""
        created = job_id is None
        if job_id is None:
            try:
                job_id = await self._store.create(
                    kind,
                    principal,
                    options,
                    [url],
                    trace_id=current_trace_id(),
                    secrets=secrets,
                )
            except BACKEND_ERRORS:
                self.count("backend_error")
                raise
        elif secrets is not None and not secrets.empty():
            raise ValueError("a reused job cannot take secrets")
        try:
            if prepare is not None:
                await prepare(job_id)
            # Draft first: a worker may claim the item as soon as it is queued.
            if draft is not None:
                await self._store.put_draft(job_id, 0, draft)
            await self._queue.enqueue_items(job_id, live_client(principal), [(0, url)])
        except BaseException as error:
            if isinstance(error, BACKEND_ERRORS):
                self.count("backend_error")
            if created:  # also on cancellation: no job is left that nobody waits for
                await asyncio.shield(self.abandon(job_id))
            raise
        if not created:
            await self._clear_lease(job_id)
            self.count("handed_over")  # the P1 job, answered with 202 already
        _log.info(f"{self.pool}_job_queued", job_id=job_id, kind=kind, reused=not created)
        return job_id

    async def wait_for(
        self,
        job_id: str,
        *,
        answered: str,
        claim_within: float,
        result_within: float = math.inf,
        total_within: float = math.inf,
        detached: Callable[[], bool],
    ) -> Envelope[ExtractResult] | None:
        """The result of the job's item 0, or None when no worker claimed it within
        ``claim_within`` seconds, it had no result ``result_within`` seconds after the claim or
        ``total_within`` seconds in all, the job is gone, or ``detached()`` turned true. Valkey
        errors propagate. Each call counts its outcome once; a result counts as ``answered``."""
        try:
            outcome, result = await self._wait(
                job_id,
                claim_within=claim_within,
                result_within=result_within,
                total_within=total_within,
                detached=detached,
            )
        except (RedisError, OSError):
            self.count("backend_error")
            raise
        self.count(answered if outcome == "answered" else outcome)
        return result

    async def _wait(
        self,
        job_id: str,
        *,
        claim_within: float,
        result_within: float,
        total_within: float,
        detached: Callable[[], bool],
    ) -> tuple[str, Envelope[ExtractResult] | None]:
        started = self._clock()
        claimed_at: float | None = None
        interval = _POLL_FIRST_SECONDS
        while not detached():
            record = await self._store.get(job_id)
            if record is None:
                return "no_result", None
            if record.state == "completed":
                items = await self._store.items(job_id, 0, 1)
                result = items[0].result if items else None
                return ("answered" if result is not None else "no_result"), result
            now = self._clock()
            if record.state == "running" and claimed_at is None:
                claimed_at = now
            if claimed_at is None:
                if now - started >= min(claim_within, total_within):
                    _log.info(f"{self.pool}_job_not_claimed", job_id=job_id)
                    return "not_claimed", None
            elif now - claimed_at >= result_within or now - started >= total_within:
                _log.info(f"{self.pool}_job_no_result", job_id=job_id)
                return "no_result", None
            await self._sleep(interval)
            interval = min(interval + _POLL_STEP_SECONDS, _POLL_MAX_SECONDS)
        return "handed_over", None

    async def _clear_lease(self, job_id: str) -> None:
        """End a reused job's P1 lease, after its item is queued: a pod that dies before leaves
        the lease to expire into the lost view, never a leaseless job without a queued item. A
        failure keeps the lease (logged): the worker's result still completes the job, which
        may look lost meanwhile."""
        try:
            await self._store.clear_lease(job_id)
        except (RedisError, OSError, JobLost) as error:
            _log.warning(
                f"{self.pool}_job_lease_kept", job_id=job_id, error_type=type(error).__name__
            )

    async def answered(self, job_id: str, result: Envelope[ExtractResult]) -> None:
        """The request answered with ``result``: delete the job, or keep one with a screenshot
        only as long as its file. A Valkey failure leaves it to expire (logged)."""
        try:
            if result.data is not None and result.data.screenshot is not None:
                await self._store.shorten(job_id, self._settings.artifact_ttl_seconds)
            else:
                await self._store.delete(job_id)
        except (RedisError, OSError) as error:
            _log.warning(f"{self.pool}_job_kept", job_id=job_id, error_type=type(error).__name__)

    async def abandon(self, job_id: str) -> None:
        """Delete the job; a Valkey failure leaves it to expire."""
        with contextlib.suppress(RedisError, OSError):
            await self._store.delete(job_id)
