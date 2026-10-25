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
of them may turn the request into a job (``claim_p1`` or ``claim_browser``, decided without an
``await`` in between): the route's P1 job during the fast phase, or the task's pool job (a
browser or a Docling job; the side is named ``browser`` after the first pool that used it). The
route then answers 202 with that job; for the pool job it detaches the task, which stops waiting
and leaves the job to the worker. A task that gives up releases the job first; detaching and
releasing exclude each other (both are synchronous), so a job answered with 202 is never deleted,
and a job being deleted is never answered with 202.

**OCR (ruling F12).** ``DoclingDispatcher`` puts the documents that need OCR on pool ``docling``.
``DoclingHandoff`` is the ``ocr_handoff`` of one sync request (``Pipeline.run`` and
``Pipeline.extract_upload``):

- While Valkey's memory guard is high, or the caller has ``DOCLING_MAX_OPEN_PER_CLIENT``
  unfinished Docling jobs, nothing is stored or queued: the request answers its draft with the
  warning ``ocr_unavailable``, or without a draft ``failed``/``rate_limited``, retryable.
- Otherwise a one-item job of kind ``extract`` (a URL) or ``file`` (an upload) is created, the
  document's bytes are stored as its input, the MarkItDown result as its draft, and the item is
  queued. Without ``wait_seconds`` the request waits ``DOCLING_SYNC_WAIT_SECONDS`` in all, then
  answers the draft with ``ocr_unavailable`` (no draft: ``failed``/``timeout``, retryable) and
  deletes the job. With ``wait_seconds`` it waits until the route answers 202 with the job.
- A request whose route already handed it over to a P1 job puts the document on that job.
- A fallback answer carries no fingerprint: it was never compared, and the next request may get
  the OCR result.

``text_extraction_docling_dispatch_total{outcome}`` (``DOCLING_OUTCOMES``): ``answered`` (the
worker's result came back), ``not_claimed`` (no worker claimed the item in time), ``no_result``
(claimed, but no result in time, or the job was gone), ``handed_over`` (the request answered 202,
or put the document on its P1 job), ``backend_error`` (Valkey failed), ``memory_guard`` and
``open_limit`` (nothing was dispatched).

**The ``docling`` pool's items (the contract of the Docling worker).**

- Queue: ``docling_queue(valkey, settings)``, pool ``docling``, without the per-domain lease
  cap (nothing is fetched). Items are ``<job id>:<index>:<attempts>``; the client is
  ``<key name>#live`` for a sync request and the batch's client for a batch item moved by the
  extract worker (F13); the domain is the URL's host, ``-`` for an upload. Claims are leased for
  ``jobs.docling_lease_seconds`` (``2 * DOCLING_TIMEOUT_SECONDS + 10``).
- Job: ``JobStore.get``: kind ``extract`` (a sync URL request), ``file`` (an upload) or
  ``batch``; ``options`` are the request's (``format``, ``metadata``, ``ocr``, ``cache``,
  ``compliance``, ...); the owner is the principal (``worker.job_principal``).
  ``JobStore.url(job, index)`` is the request URL, ``""`` for an upload.
- Input: ``JobStore.get_input(job, index, DocumentInputMeta)``: the document's bytes and their
  ``DocumentInputMeta`` (kind, sniffed content type, file name of an upload, final URL, upstream
  status, validators, ``Content-Language``, charset, ``raw_sha256``, ``needs_ocr``: the probe
  said "scan", the compliance report, the fetch attempts). TTL ``DOCLING_INPUT_TTL_SECONDS``,
  never longer than the job's; None when it expired, and the item then fails ``timeout``,
  retryable. The ``Worker`` deletes the input once the item's result is written (pool
  ``docling``, and any ``file`` job).
- Draft: ``JobStore.draft(job, index)``: MarkItDown's envelope of the document, not yet cached
  nor fingerprinted, or None (images, ODF). The runner answers the better of the draft and the
  OCR result (``escalation.better``) and deletes the draft once the item has its outcome.
- Result: the runner returns the envelope and the ``Worker`` writes it as the item's result
  (only the first result of an item counts). A sync request polls the job until it is completed
  and deletes it; a job that is gone when the worker claims or writes was given up, and the item
  is dropped. The caller's open Docling jobs need no update: a completed or deleted job leaves
  the count by itself.

Logs carry job ids only, never a URL, header, cookie, action, file name or document byte.
"""

import asyncio
import contextlib
import math
import time
from collections.abc import Awaitable, Callable
from typing import Final, Literal

import structlog
from prometheus_client import CollectorRegistry
from redis.asyncio import Redis
from redis.exceptions import RedisError

from text_extraction.auth import Principal
from text_extraction.browser_options import BrowserSecrets
from text_extraction.compliance import ComplianceMode
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.errors import ServiceError
from text_extraction.extract_models import ExtractRequest, ExtractResult
from text_extraction.job_models import JobKind, JobOptions
from text_extraction.jobs import JobLost, JobStore
from text_extraction.pipeline_documents import OcrNeeded
from text_extraction.queue_metrics import dispatch_counter_for
from text_extraction.result_cache import MemoryPressure
from text_extraction.scheduler import FairQueue
from text_extraction.settings import Settings
from text_extraction.tracing import current_trace_id

_log = structlog.get_logger(__name__)

# The poll interval of ``wait_for``: it starts at the first value and rises by the step to the last.
_POLL_FIRST_SECONDS = 0.2
_POLL_STEP_SECONDS = 0.1
_POLL_MAX_SECONDS = 0.5

_BACKEND_ERRORS = (RedisError, OSError, JobLost)

# The outcomes of ``text_extraction_docling_dispatch_total``; see the module docstring.
DOCLING_OUTCOMES: Final = (
    "answered",
    "not_claimed",
    "no_result",
    "handed_over",
    "backend_error",
    "memory_guard",
    "open_limit",
)
DOCLING_POOL: Final = "docling"
# The per-domain lease cap of the ``docling`` queue: OCR fetches nothing, so no host is spared.
_DOCLING_DOMAIN_CAP: Final = 1_000_000

_OCR_TIMEOUT_MESSAGE: Final = "OCR did not finish in time. Try again later."
_OCR_BUSY_MESSAGE: Final = (
    "The service takes no new documents for OCR at the moment. Try again later."
)
_BACKEND_MESSAGE: Final = "The job backend is unavailable. Try again later."


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
    )


def docling_queue(valkey: Redis, settings: Settings) -> FairQueue:
    """The ``docling`` pool's queue, as the API pods, the extract worker and the Docling worker
    use it: its claims ignore the per-domain lease cap."""
    return FairQueue(valkey, settings, DOCLING_POOL, domain_cap=_DOCLING_DOMAIN_CAP)


class Handover:
    """What the route of a request with ``wait_seconds`` and its task (``extract_api._flow``)
    share; see the module docstring.

    ``job_id`` is the P1 job the route created, ``browser_job`` the pool job the task dispatched
    (a browser or a Docling job). The side that claimed calls ``settle`` once its job exists, or
    failed to; the other side waits for ``settled`` before it looks at the ids.
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
        """The task dispatches a pool job; False when the route is creating a P1 job."""
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
            except _BACKEND_ERRORS:
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
            if isinstance(error, _BACKEND_ERRORS):
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


# --- OCR ------------------------------------------------------------------------------------


class OpenLimitReached(Exception):  # noqa: N818 - a refusal, not an error of ours
    """The caller has ``DOCLING_MAX_OPEN_PER_CLIENT`` unfinished Docling jobs."""


def without_ocr(
    needed: OcrNeeded, without: Envelope[ExtractResult], code: ErrorCode, message: str
) -> Envelope[ExtractResult]:
    """The answer when a document that needs OCR does not get it: ``without``, the pipeline's
    answer without OCR, which is the draft with the warning ``ocr_unavailable``, without its
    fingerprint (it was never compared, and the next request may get the OCR result). A
    document without a draft fails with ``code`` and ``message``, retryable."""
    data = without.data
    if needed.draft is not None and data is not None:
        return without.model_copy(update={"data": data.model_copy(update={"fingerprint": None})})
    error = ErrorInfo(code=code, message=message, retryable=True)
    return without.model_copy(update={"status": "failed", "error": error})


class DoclingDispatcher(PoolDispatcher):
    """Puts sync requests' documents that need OCR on the ``docling`` pool and waits for their
    results; see the module docstring."""

    def __init__(
        self,
        store: JobStore,
        queue: FairQueue,
        settings: Settings,
        *,
        registry: CollectorRegistry | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(
            DOCLING_POOL,
            store,
            queue,
            settings,
            registry,
            outcomes=DOCLING_OUTCOMES,
            clock=clock,
            sleep=sleep,
        )

    async def submit(
        self,
        kind: Literal["extract", "file"],
        options: JobOptions,
        principal: Principal,
        url: str | None,
        needed: OcrNeeded,
        *,
        job_id: str | None = None,
    ) -> str:
        """Queue the document on the ``docling`` pool and return its job's id: a new job of
        ``kind``, or ``job_id`` (the request's P1 job), whose lease is cleared. The job counts
        as one of the caller's Docling jobs before its input is stored; over
        ``DOCLING_MAX_OPEN_PER_CLIENT`` this raises ``OpenLimitReached`` and a new job is deleted
        again. Valkey errors propagate."""
        settings = self._settings
        limit = settings.docling_max_open_per_client
        owner = principal.name

        async def prepare(job: str) -> None:
            if await self._store.add_docling_job(owner, job) > limit:
                await self._store.drop_docling_job(owner, job)
                raise OpenLimitReached
            await self._store.put_input(
                job, 0, needed.input.body, needed.input.meta, settings.docling_input_ttl_seconds
            )

        try:
            # Counted first, so that a caller at its limit stores nothing at all.
            if await self._open_jobs(owner) >= limit:
                raise OpenLimitReached
            return await self.submit_job(
                kind, options, principal, url, job_id=job_id, draft=needed.draft, prepare=prepare
            )
        except OpenLimitReached:
            self.count("open_limit")
            raise

    async def _open_jobs(self, owner: str) -> int:
        try:
            return await self._store.open_docling_jobs(owner)
        except (RedisError, OSError):
            self.count("backend_error")
            raise

    def limit_message(self) -> str:
        """The error of a document without a draft whose caller is at its open limit."""
        limit = self._settings.docling_max_open_per_client
        return (
            f"At most {limit} documents of a key may wait for OCR at a time. Retry when one "
            "has finished."
        )

    async def wait(
        self, job_id: str, *, extra_seconds: float = 0.0, detached: Callable[[], bool]
    ) -> Envelope[ExtractResult] | None:
        """The OCR result of the job's item 0, or None when there is none within
        ``DOCLING_SYNC_WAIT_SECONDS`` + ``extra_seconds`` in all (claimed or not), the job is
        gone, or ``detached()`` turned true. Valkey errors propagate."""
        seconds = self._settings.docling_sync_wait_seconds + extra_seconds
        return await self.wait_for(
            job_id,
            answered="answered",
            claim_within=seconds,
            total_within=seconds,
            detached=detached,
        )


class DoclingHandoff:
    """The ``ocr_handoff`` of one sync request (``Pipeline.run`` or ``extract_upload``; F12).

    ``kind`` is the job kind of the request (``extract`` for a URL, ``file`` for an upload),
    ``options`` its stored options and ``url`` its URL (None for an upload). ``wait_until`` is
    the ``time.monotonic()`` reading at which the route of a request with ``wait_seconds``
    answers 202: the request then waits that much longer, so that the route finds the job to
    answer with. ``handed_over`` is True once the job belongs to the worker (the route answered
    202 with it, or it is the request's P1 job): the caller then answers nothing."""

    def __init__(
        self,
        dispatcher: DoclingDispatcher,
        guard: MemoryPressure,
        handover: Handover,
        *,
        kind: Literal["extract", "file"],
        options: JobOptions,
        principal: Principal,
        url: str | None,
        wait_until: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._dispatcher = dispatcher
        self._guard = guard
        self._handover = handover
        self._kind = kind
        self._options = options
        self._principal = principal
        self._url = url
        self._wait_until = wait_until
        self._clock = clock
        self.handed_over = False

    async def __call__(
        self, needed: OcrNeeded, without: Envelope[ExtractResult]
    ) -> Envelope[ExtractResult]:
        dispatcher, handover = self._dispatcher, self._handover
        if self._guard.high:
            dispatcher.count("memory_guard")
            return without_ocr(needed, without, ErrorCode.RATE_LIMITED, _OCR_BUSY_MESSAGE)
        if not handover.claim_browser():  # the route is handing the request over to a P1 job
            await handover.settled()
            if handover.job_id is not None:
                return await self._on_p1_job(handover.job_id, needed, without)
            # The P1 job could not be created: the route waits for this answer.
        extra = 0.0 if self._wait_until is None else max(self._wait_until - self._clock(), 0.0)
        job_id: str | None = None
        try:
            job_id = await self._submit(needed)
        except OpenLimitReached:
            return self._at_limit(needed, without)
        except _BACKEND_ERRORS as error:  # JobLost: the new job vanished before its input
            return self._backend_failed(needed, without, error)
        finally:
            if handover.claimed == "browser":
                handover.settle(job_id)
        try:
            result = await dispatcher.wait(
                job_id, extra_seconds=extra, detached=lambda: handover.detached
            )
        except asyncio.CancelledError:
            if handover.release():
                await asyncio.shield(dispatcher.abandon(job_id))
            raise
        except (RedisError, OSError) as error:
            if not handover.release():
                self.handed_over = True  # the route answered 202 with the job
                return without
            await asyncio.shield(dispatcher.abandon(job_id))
            return self._backend_failed(needed, without, error)
        if result is None:
            if not handover.release():
                self.handed_over = True  # the route answered 202: the worker writes the result
                return without
            await asyncio.shield(dispatcher.abandon(job_id))
            return without_ocr(needed, without, ErrorCode.TIMEOUT, _OCR_TIMEOUT_MESSAGE)
        # Answered here: nobody reads the job again, unless the route answered 202 meanwhile.
        if handover.release():
            await asyncio.shield(dispatcher.answered(job_id, result))
        return result

    async def _submit(self, needed: OcrNeeded, job_id: str | None = None) -> str:
        return await self._dispatcher.submit(
            self._kind, self._options, self._principal, self._url, needed, job_id=job_id
        )

    async def _on_p1_job(
        self, job_id: str, needed: OcrNeeded, without: Envelope[ExtractResult]
    ) -> Envelope[ExtractResult]:
        """Put the document on the request's P1 job, which the route answered 202 with; the
        answer of a refusal or failure is stored in that job."""
        try:
            await self._submit(needed, job_id)
        except OpenLimitReached:
            return self._at_limit(needed, without)
        except (RedisError, OSError, JobLost) as error:
            _dispatch_failed(error)
            return without_ocr(needed, without, ErrorCode.INTERNAL_ERROR, _BACKEND_MESSAGE)
        self.handed_over = True  # the Docling worker writes item 0 of the P1 job
        return without

    def _at_limit(
        self, needed: OcrNeeded, without: Envelope[ExtractResult]
    ) -> Envelope[ExtractResult]:
        """The caller is at ``DOCLING_MAX_OPEN_PER_CLIENT``: nothing was dispatched."""
        message = self._dispatcher.limit_message()
        return without_ocr(needed, without, ErrorCode.RATE_LIMITED, message)

    @staticmethod
    def _backend_failed(
        needed: OcrNeeded, without: Envelope[ExtractResult], error: Exception
    ) -> Envelope[ExtractResult]:
        """Valkey failed: the draft with ``ocr_unavailable``; a document without a draft is our
        fault (503)."""
        _dispatch_failed(error)
        if needed.draft is None:
            raise ServiceError(ErrorCode.INTERNAL_ERROR, _BACKEND_MESSAGE, 503, retryable=True)
        return without_ocr(needed, without, ErrorCode.INTERNAL_ERROR, _BACKEND_MESSAGE)


def _dispatch_failed(error: Exception) -> None:
    # The type only: the failed command carried a document's bytes.
    _log.warning("docling_dispatch_failed", error_type=type(error).__name__)
