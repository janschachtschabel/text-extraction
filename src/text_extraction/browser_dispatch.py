"""The API's side of browser work: a sync request's page goes to the browser workers as a job.

API pods never start a browser (B1). A request that needs one (``mode: browser``, a screenshot,
browser options, or an ``auto`` result that escalates) is handed to the ``browser`` pool:

1. ``BrowserDispatcher.submit`` creates a job of kind ``extract`` or ``screenshot`` with one URL
   and the caller's secrets (B6), stores the fast result as the item's draft for an escalating
   ``auto`` request (B5), and queues item 0 on pool ``browser`` under the client
   ``<key name>#live``, so that the key's own batches do not queue it. An ``auto`` request that
   was handed over to a P1 job during its fast phase reuses that job instead: once the item is
   queued, its lease is cleared (the browser has its own time, and the lost view must not end
   it) and the browser worker writes item 0.
2. ``wait`` polls the job every 0.2 s, rising to 0.5 s. It gives up when no worker has claimed
   the item (the job is still ``queued``) after ``BROWSER_QUEUE_WAIT_SECONDS``, or when the item
   has no result ``BROWSER_TIMEOUT_SECONDS`` + 5 s after the claim
   (``BROWSER_UNBLOCK_TIMEOUT_SECONDS`` + 5 s in unblock mode).
3. A request that gives up calls ``abandon``, which deletes the job: a worker that claims the
   item later finds no job and drops it.

``Handover`` is what the route of a request with ``wait_seconds`` and its task share. Exactly one
of them may turn the request into a job (``claim_p1`` or ``claim_browser``, decided without an
``await`` in between): the route's P1 job during the fast phase, or the task's browser job. The
route then answers 202 with that job; for the browser job it detaches the task, which stops
waiting and leaves the job to the worker. A task that gives up releases the job first; detaching
and releasing exclude each other (both are synchronous), so a job answered with 202 is never
deleted, and a job being deleted is never answered with 202.

Logs carry job ids only, never a URL, header, cookie or action.
"""

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Literal

import structlog
from redis.exceptions import RedisError

from text_extraction.auth import Principal
from text_extraction.compliance import ComplianceMode
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractRequest, ExtractResult
from text_extraction.job_models import JobOptions
from text_extraction.jobs import JobLost, JobStore
from text_extraction.scheduler import FairQueue
from text_extraction.settings import Settings
from text_extraction.tracing import current_trace_id

_log = structlog.get_logger(__name__)

# The poll interval of ``wait``: it starts at the first value and rises by the step to the last.
_POLL_FIRST_SECONDS = 0.2
_POLL_STEP_SECONDS = 0.1
_POLL_MAX_SECONDS = 0.5
# Seconds on top of the render deadline that a request waits for the result after the claim.
_RESULT_MARGIN_SECONDS = 5

type BrowserJobKind = Literal["extract", "screenshot"]


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
    )


class Handover:
    """What the route of a request with ``wait_seconds`` and its task (``extract_api._flow``)
    share; see the module docstring.

    ``job_id`` is the P1 job the route created, ``browser_job`` the job the task dispatched. The
    side that claimed calls ``settle`` once its job exists, or failed to; the other side waits for
    ``settled`` before it looks at the ids.
    """

    def __init__(self) -> None:
        self.job_id: str | None = None
        self.browser_job: str | None = None
        self._claimed: Literal["p1", "browser"] | None = None
        self._settled = asyncio.Event()
        self._detached = False
        self._released = False

    @property
    def claimed(self) -> Literal["p1", "browser"] | None:
        return self._claimed

    def claim_p1(self) -> bool:
        """The route turns the request into a P1 job; False when the task dispatched first."""
        return self._claim("p1")

    def claim_browser(self) -> bool:
        """The task dispatches a browser job; False when the route is creating a P1 job."""
        return self._claim("browser")

    def _claim(self, side: Literal["p1", "browser"]) -> bool:
        if self._claimed is not None:
            return False
        self._claimed = side
        return True

    def settle(self, job_id: str | None) -> None:
        """The claiming side's job is created (``job_id``) or could not be (None)."""
        if self._claimed == "p1":
            self.job_id = job_id
        else:
            self.browser_job = job_id
        self._settled.set()

    async def settled(self) -> None:
        await self._settled.wait()

    def detach(self) -> bool:
        """The route answers 202 with the browser job: the task stops waiting for it and leaves
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


class BrowserDispatcher:
    """Puts sync browser requests on the ``browser`` pool and waits for their results; see the
    module docstring. ``queue`` is the ``browser`` pool's queue. ``clock`` (monotonic seconds)
    and ``sleep`` can be replaced in tests."""

    def __init__(
        self,
        store: JobStore,
        queue: FairQueue,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._store = store
        self._queue = queue
        self._settings = settings
        self._clock = clock
        self._sleep = sleep

    async def submit(
        self,
        kind: BrowserJobKind,
        req: ExtractRequest,
        principal: Principal,
        compliance: ComplianceMode,
        *,
        job_id: str | None = None,
        draft: Envelope[ExtractResult] | None = None,
    ) -> str:
        """Queue ``req`` on the ``browser`` pool and return its job's id. Without ``job_id`` a
        new job is created with the request's secrets; with it, that job (the P1 job of the
        request) is reused and its lease cleared. A reused job gets no secrets: only an ``auto``
        request without them has a fast phase (R8). ``draft`` is the fast result to compare the
        render with. Valkey errors propagate; a new job is deleted again first."""
        secrets = req.secrets()
        created = job_id is None
        if job_id is None:
            job_id = await self._store.create(
                kind,
                principal,
                job_options(req, compliance),
                [req.url],
                trace_id=current_trace_id(),
                secrets=secrets,
            )
        elif not secrets.empty():
            raise ValueError("a reused job cannot take secrets")
        try:
            # Draft first: the browser worker may claim the item as soon as it is queued.
            if draft is not None:
                await self._store.put_draft(job_id, 0, draft)
            await self._queue.enqueue_items(job_id, live_client(principal), [(0, req.url)])
        except BaseException:
            if created:  # also on cancellation: no job is left that nobody waits for
                await asyncio.shield(self.abandon(job_id))
            raise
        if not created:
            await self._clear_lease(job_id)
        _log.info("browser_job_queued", job_id=job_id, kind=kind, reused=not created)
        return job_id

    async def wait(
        self, job_id: str, *, unblock: bool, detached: Callable[[], bool]
    ) -> Envelope[ExtractResult] | None:
        """The result of the job's item 0, or None when no worker claimed it within
        ``BROWSER_QUEUE_WAIT_SECONDS``, it had no result within the render deadline + 5 s after
        the claim, the job is gone, or ``detached()`` turned true. Valkey errors propagate."""
        settings = self._settings
        render_seconds = (
            settings.browser_unblock_timeout_seconds
            if unblock
            else settings.browser_timeout_seconds
        )
        started = self._clock()
        claimed_at: float | None = None
        interval = _POLL_FIRST_SECONDS
        while not detached():
            record = await self._store.get(job_id)
            if record is None:
                return None
            if record.state == "completed":
                items = await self._store.items(job_id, 0, 1)
                return items[0].result if items else None
            now = self._clock()
            if record.state == "running" and claimed_at is None:
                claimed_at = now
            if claimed_at is None:
                if now - started >= settings.browser_queue_wait_seconds:
                    _log.info("browser_job_not_claimed", job_id=job_id)
                    return None
            elif now - claimed_at >= render_seconds + _RESULT_MARGIN_SECONDS:
                _log.info("browser_job_no_result", job_id=job_id)
                return None
            await self._sleep(interval)
            interval = min(interval + _POLL_STEP_SECONDS, _POLL_MAX_SECONDS)
        return None

    async def _clear_lease(self, job_id: str) -> None:
        """End a reused job's P1 lease, after its item is queued: a pod that dies before leaves
        the lease to expire into the lost view, never a leaseless job without a queued item. A
        failure keeps the lease (logged): the worker's result still completes the job, which
        may look lost meanwhile."""
        try:
            await self._store.clear_lease(job_id)
        except (RedisError, OSError, JobLost) as error:
            _log.warning("browser_job_lease_kept", job_id=job_id, error_type=type(error).__name__)

    async def abandon(self, job_id: str) -> None:
        """Delete the job; a Valkey failure leaves it to expire."""
        with contextlib.suppress(RedisError, OSError):
            await self._store.delete(job_id)
