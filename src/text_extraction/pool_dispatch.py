"""Sync requests' work on another worker pool: a one-item job, queued, waited for, and deleted.

API pods hand three kinds of work to worker pools: renders (pool ``browser``,
``browser_dispatch.py``) and OCR (pool ``docling``), which they wait for, and transcriptions
(pool ``asr``, ``asr_dispatch.py``), which they never wait for. ``PoolDispatcher`` is what all
share:

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
answered with 202. A task that dispatched a job it does not wait for (a transcription) ``adopt``s
it to the route and answers None: the route answers 202 with that job, also when it had claimed
the hand-over for a P1 job that it could not create. A task that released its pool job (a
fallback) and then dispatches another one (its result goes on to the ``nlp`` pool) claims the
hand-over again (``claim_after_release``): the route may then answer 202 with the new job.

**The last hop.** A pool whose worker writes the item's final result (``nlp``, PII; ``llm``, the
LLM work of ``structured`` and ``chunking``) takes envelopes that are done otherwise: the result
of a sync request (``LastHopHandoff``) or of an item another pool finished (``LastHopMove``). Both
are generic in the pool, ``needs(envelope)`` (whether the envelope owes the hop) and
``unserved(envelope, reason)`` (the answer when the hop could not serve it: ``busy`` while the
memory guard is high, ``failed`` when Valkey failed or nobody answered in time); the pool's
module subclasses them (``pii_dispatch.PiiHandoff``, ``pii_dispatch.PiiMove``) and documents its
policy and its worker's contract.

- ``LastHopHandoff`` (sync): while the memory guard is high nothing is stored and the request
  answers ``unserved(..., "busy")``. Otherwise a one-item job of the request's kind is created
  with the envelope as its draft, item 0 is queued on the pool, and the request waits for the
  result (the pool's ``_wait``, plus whatever is left until its route hands it over). An earlier
  pool job of the request that it gave up does not stand in the way: the hand-over is claimed
  again for the new job (``Handover.claim_after_release``), and the route may answer 202 with
  it. A request whose route handed it over to a P1 job puts the envelope on that job (its draft,
  the item queued, the job's lease cleared): the pool's worker writes item 0. A wait that ends
  without a result while nobody took the job over, and Valkey failing, answer
  ``unserved(..., "failed")``, never the envelope that owes the hop. The handoff answers None
  once the job belongs to the pool's worker (the route answered 202 with it, or it is the P1
  job).
- ``LastHopMove`` (``Worker._finish`` of the pools before): stores the envelope as the item's
  draft and queues the item on the pool with the same job, index and client; the worker then
  completes the item without a result. Draft first: the pool's worker may claim the item as soon
  as it is queued. While the memory guard is high nothing is stored and the move declines.

**The graceful shutdown** (issue #19, M11.4): a sync request may wait for a last hop longer than
the API pod's grace period (``wait_seconds`` up to 60 s). ``server._serve`` calls
``GracefulShutdown.begin`` when the public server stops accepting connections; from then on a
route whose task waits for a last-hop job (``Handover.in_last_hop``) answers at once as it does
when its wait runs out: 202 with that job, which the pool's worker completes. Waits for a
browser or Docling job are not cut short, also when its item has moved on to ``nlp`` or ``llm``
(``LastHopMove``: the handover still names the earlier pool). They end within the graceful phase
only while it is at least the request's wait: 75 s or more with browser workers, 50 s with
Docling workers only (``server.graceful_shutdown_seconds``), against a ``wait_seconds`` of up to
60 s.

The ``docling`` pool's policy (OCR) and the contract of its worker are in ``docling_dispatch.py``,
the ``asr`` pool's in ``asr_dispatch.py``, the ``nlp`` pool's in ``pii_dispatch.py``.

Logs carry job ids only, never a URL, header, cookie, action or document byte.
"""

import asyncio
import contextlib
import math
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Literal

import structlog
from prometheus_client import CollectorRegistry, Counter
from redis.exceptions import RedisError

from text_extraction.auth import Principal
from text_extraction.browser_options import BrowserSecrets
from text_extraction.compliance import ComplianceMode
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractRequest, ExtractResult, with_own_fault_data
from text_extraction.job_models import JobKind, JobOptions
from text_extraction.jobs import JobLost, JobRecord, JobStore
from text_extraction.queue_metrics import dispatch_counter_for
from text_extraction.result_cache import MemoryPressure
from text_extraction.scheduler import FairQueue, QueueItem
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
        pii=req.pii,
        structured=req.structured,
        chunking=req.chunking,
        describe_images=req.describe_images,
    )


class GracefulShutdown:
    """Whether the API pod's graceful shutdown has begun (the module docstring). ``begin`` and
    ``wait`` run on the server's event loop; each ``wait`` makes its own future on the running
    loop, so one instance serves apps that tests run on several loops."""

    def __init__(self) -> None:
        self._begun = False
        self._waiters: set[asyncio.Future[None]] = set()

    @property
    def begun(self) -> bool:
        return self._begun

    def begin(self) -> None:
        self._begun = True
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def wait(self) -> None:
        """Return once the graceful shutdown has begun."""
        if self._begun:
            return
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.add(waiter)
        try:
            await waiter
        finally:
            self._waiters.discard(waiter)


class Handover:
    """What the route of a request with ``wait_seconds`` and its task (``extract_api._flow``)
    share; see the module docstring.

    ``job_id`` is the P1 job the route created, ``pool_job`` the job the task dispatched to
    ``pool`` (``browser``, ``docling``, ``asr``, or a last hop: ``nlp``, ``llm``). The side that
    claimed calls ``settle`` once its job exists, or failed to; the other side waits for
    ``settled`` before it looks at the ids. ``in_last_hop`` returns while the task waits for a
    last-hop job that it has not released.
    """

    def __init__(self) -> None:
        self.job_id: str | None = None
        self.pool_job: str | None = None
        self.pool: str | None = None
        self._claimed: Literal["p1", "pool"] | None = None
        self._settled = asyncio.Event()
        self._last_hop = asyncio.Event()
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

    def claim_after_release(self, pool: str) -> bool:
        """The task dispatches a job to ``pool`` after its earlier pool job, if any, was released
        (it gave that job up and answered without it: a browser or OCR fallback that now goes to
        the ``nlp`` pool). The hand-over then starts over for the new job: the route waits for
        it to be settled and may answer 202 with it. False when the route is creating or has a
        P1 job, or answered 202 with the earlier job; with nothing claimed yet, ``claim_pool``."""
        if self._claimed is None:
            return self.claim_pool(pool)
        if self._claimed != "pool" or not self._released or self._detached:
            return False
        self.pool = pool
        self.pool_job = None
        self._released = False
        self._settled = asyncio.Event()
        return True

    def _claim(self, side: Literal["p1", "pool"]) -> bool:
        if self._claimed is not None:
            return False
        self._claimed = side
        return True

    def settle(self, job_id: str | None, *, last_hop: bool = False) -> None:
        """The claiming side's job is created (``job_id``) or could not be (None). ``last_hop``:
        the task's pool job is a last hop's (``LastHopHandoff``), which it now waits for."""
        if self._claimed == "p1":
            self.job_id = job_id
        else:
            self.pool_job = job_id
            if last_hop and job_id is not None:
                self._last_hop.set()
        self._settled.set()

    async def settled(self) -> None:
        await self._settled.wait()

    async def in_last_hop(self) -> None:
        """Return once the task waits for a last-hop job (at once while it does)."""
        await self._last_hop.wait()

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
        self._last_hop.clear()
        return True

    @property
    def detached(self) -> bool:
        return self._detached

    def adopt(self, pool: str, job_id: str) -> None:
        """The task dispatched ``job_id`` to ``pool`` and does not wait for it (a transcription):
        the job is the route's to answer 202 with, once the task answers None, whatever side
        claimed the hand-over (the route's P1 job could not be created). Called after the job
        exists, and never by a task that may still release its job."""
        self.pool = pool
        self.pool_job = job_id
        self._detached = True


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

    @property
    def settings(self) -> Settings:
        return self._settings

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
        llm_key: str | None = None,
    ) -> str:
        """Queue item 0 of a new job (or of ``job_id``, the request's P1 job) on the pool and
        return the job's id. ``prepare`` runs before the draft is stored (a Docling job's input);
        ``draft`` is the result the worker compares its own with. ``llm_key`` is the caller's
        ``X-LLM-Api-Key`` of a request that may need the LLM: a new job keeps it for the llm
        worker (the request's P1 job has it already). Valkey errors propagate, and so does
        whatever ``prepare`` raises; a new job is deleted again first."""
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
            if created and llm_key is not None:
                await self._store.put_llm_key(job_id, llm_key)
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
                if result is not None:  # an own fault has the envelope's fields too (C14)
                    result = with_own_fault_data(
                        result,
                        items[0].url,
                        format=record.options.format,
                        compliance=record.options.compliance,
                    )
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


# --- the last hop -------------------------------------------------------------------------------

type UnservedReason = Literal["busy", "failed"]


class LastHopHandoff(ABC):
    """The hand-over of one sync request's envelope to a last-hop pool (``pool``; the module
    docstring). ``kind`` is the job kind of the request (``extract`` for a URL, ``file`` for an
    upload), ``options`` its stored options and ``url`` its URL (None for an upload); ``llm_key`` is
    the caller's ``X-LLM-Api-Key`` of a request that may need the LLM, stored with a new job;
    ``prepare`` runs with a new job's id before its draft is stored (``PoolDispatcher.submit_job``:
    an upload's input for the llm worker; whatever it raises propagates, the job deleted).
    ``wait_until`` is the ``time.monotonic()`` reading at which the route answers 202: the request
    waits that much longer, so that the route finds the job to answer with. A call answers the
    envelope the pool's worker produced, ``unserved(...)``, or None once the job belongs to the
    pool's worker (the route answered 202 with it, or it is the request's P1 job). An envelope that
    does not ``need`` the hop comes back as it is."""

    def __init__(
        self,
        dispatcher: PoolDispatcher,
        guard: MemoryPressure,
        *,
        pool: str,
        kind: Literal["extract", "file"],
        options: JobOptions,
        principal: Principal,
        url: str | None,
        wait_until: float | None = None,
        llm_key: str | None = None,
        prepare: Callable[[str], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._dispatcher = dispatcher
        self._guard = guard
        self._pool = pool
        self._prepare = prepare
        self._kind = kind
        self._options = options
        self._principal = principal
        self._url = url
        self._wait_until = wait_until
        self._llm_key = llm_key
        self._clock = clock

    @abstractmethod
    def needs(self, envelope: Envelope[ExtractResult]) -> bool:
        """Whether the envelope owes this hop."""

    @abstractmethod
    def unserved(
        self, envelope: Envelope[ExtractResult], reason: UnservedReason
    ) -> Envelope[ExtractResult]:
        """The answer for an envelope that owes the hop and did not get it: ``busy`` while the
        memory guard is high, ``failed`` otherwise."""

    @abstractmethod
    async def _wait(
        self, job_id: str, *, extra_seconds: float, detached: Callable[[], bool]
    ) -> Envelope[ExtractResult] | None:
        """The pool's result of the job's item 0, or None (the pool's dispatcher's wait, longer
        by ``extra_seconds``). Valkey errors propagate."""

    async def __call__(
        self, envelope: Envelope[ExtractResult], handover: Handover
    ) -> Envelope[ExtractResult] | None:
        if not self.needs(envelope):
            return envelope
        dispatcher = self._dispatcher
        if self._guard.high:
            dispatcher.count("memory_guard")
            return self.unserved(envelope, "busy")
        claimed = handover.claim_after_release(self._pool)
        if not claimed:  # the route is handing it over to a P1 job
            await handover.settled()
            if handover.claimed == "p1" and handover.job_id is not None:
                return await self._on_p1_job(handover.job_id, envelope)
            # The P1 job could not be created: the route waits for this answer.
        extra = 0.0 if self._wait_until is None else max(self._wait_until - self._clock(), 0.0)
        job_id: str | None = None
        try:
            job_id = await self._submit(envelope)
        except BACKEND_ERRORS as error:  # JobLost: the new job vanished before its draft
            self._dispatch_failed(error)
            return self.unserved(envelope, "failed")
        finally:
            if claimed:
                handover.settle(job_id, last_hop=True)
        try:
            result = await self._wait(
                job_id, extra_seconds=extra, detached=lambda: handover.detached
            )
        except asyncio.CancelledError:
            if handover.release():
                await asyncio.shield(dispatcher.abandon(job_id))
            raise
        except (RedisError, OSError) as error:
            if not handover.release():
                return None  # the route answered 202 with the job: the worker writes its result
            await asyncio.shield(dispatcher.abandon(job_id))
            self._dispatch_failed(error)
            return self.unserved(envelope, "failed")
        if result is None:
            if not handover.release():
                return None  # the route answered 202 with the job: the worker writes its result
            await asyncio.shield(dispatcher.abandon(job_id))
            return self.unserved(envelope, "failed")
        # Answered here: nobody reads the job again, unless the route answered 202 meanwhile.
        if handover.release():
            await asyncio.shield(dispatcher.answered(job_id, result))
        return result

    async def _submit(self, envelope: Envelope[ExtractResult], job_id: str | None = None) -> str:
        """Queue item 0 on the pool with ``envelope`` as its draft: of a new job (with the
        caller's LLM key, if any, and after ``prepare``) or of ``job_id`` (the request's P1 job,
        whose lease is cleared; it holds what ``prepare`` would store already: an upload's
        input). Valkey errors propagate; a new job is deleted again first."""
        return await self._dispatcher.submit_job(
            self._kind,
            self._options,
            self._principal,
            self._url,
            job_id=job_id,
            draft=envelope,
            prepare=self._prepare if job_id is None else None,
            llm_key=self._llm_key,
        )

    async def _on_p1_job(
        self, job_id: str, envelope: Envelope[ExtractResult]
    ) -> Envelope[ExtractResult] | None:
        """Put the envelope on the request's P1 job, which the route answered 202 with; the
        unserved answer of a failure is stored in that job."""
        try:
            await self._submit(envelope, job_id)
        except BACKEND_ERRORS as error:
            self._dispatch_failed(error)
            return self.unserved(envelope, "failed")
        return None  # the pool's worker writes item 0 of the P1 job

    def _dispatch_failed(self, error: Exception) -> None:
        # The type only: the failed command carried the result's text.
        _log.warning(f"{self._pool}_dispatch_failed", error_type=type(error).__name__)


class LastHopMove:
    """The move of a finished item to a last-hop pool (``Worker._finish`` of the pools before;
    the module docstring): ``queue`` is that pool's. ``guard`` tells whether Valkey is nearly
    full; ``outcomes`` (the pool's ``text_extraction_<pool>_dispatch_total``) counts the refused
    moves."""

    def __init__(
        self,
        store: JobStore,
        queue: FairQueue,
        guard: MemoryPressure | None,
        *,
        outcomes: Counter | None = None,
    ) -> None:
        self._store = store
        self._queue = queue
        self._guard = guard
        self._outcomes = outcomes

    async def __call__(
        self, job: JobRecord, item: QueueItem, envelope: Envelope[ExtractResult]
    ) -> bool:
        """Store ``envelope`` as the item's draft and queue the item on the pool with the same
        job, index and client; True once it is queued. False while the memory guard is high:
        nothing was stored. Valkey errors and ``JobLost`` propagate."""
        if self._guard is not None and self._guard.high:
            if self._outcomes is not None:
                self._outcomes.labels(outcome="memory_guard").inc()
            return False
        url = await self._store.url(job.id, item.index)
        # Draft first: the pool's worker may claim the item as soon as it is queued.
        await self._store.put_draft(job.id, item.index, envelope)
        await self._queue.enqueue_items(job.id, item.client, [(item.index, url or None)])
        return True
