"""The ``docling`` pool's dispatch: documents that need OCR, handed to the Docling workers.

``DoclingDispatcher`` and ``DoclingHandoff`` serve the sync requests of the API pods, on the
generic ``pool_dispatch.PoolDispatcher`` and ``pool_dispatch.Handover``; ``DoclingMove`` serves
the extract worker's batch items (F13). The last section is the contract of the Docling worker.

**Sync requests (ruling F12).** ``DoclingDispatcher`` puts the documents that need OCR on pool
``docling``. ``DoclingHandoff`` is the ``ocr_handoff`` of one sync request (``Pipeline.run`` and
``Pipeline.extract_upload``):

- While Valkey's memory guard is high, the caller has ``DOCLING_MAX_OPEN_PER_CLIENT``
  unfinished Docling jobs, or the input would exceed ``DOCLING_INPUT_BUDGET_BYTES``
  (``JobStore.put_input`` refuses with ``InputBudgetExceeded``; the budget is that of all jobs'
  inputs, see ``jobs.py``), nothing is stored or queued: the request answers its draft with the
  warning ``ocr_unavailable``, or without a draft ``failed``/``rate_limited``, retryable.
- Otherwise a one-item job of kind ``extract`` (a URL) or ``file`` (an upload) is created, the
  document's bytes are stored as its input, the MarkItDown result as its draft, and the item is
  queued. Without ``wait_seconds`` the request waits ``DOCLING_SYNC_WAIT_SECONDS`` in all, then
  answers the draft with ``ocr_unavailable`` (no draft: ``failed``/``timeout``, retryable) and
  deletes the job. With ``wait_seconds`` it waits until the route answers 202 with the job.
- A request whose route already handed it over to a P1 job puts the document on that job: the
  document's input replaces the upload's own (``UploadInputMeta``), the item is queued and the
  job's lease is cleared. A refusal or failure is answered as above, and the request's task
  stores that answer in the P1 job.
- A fallback answer carries no fingerprint: it was never compared, and the next request may get
  the OCR result.

``text_extraction_docling_dispatch_total{outcome}`` (``DOCLING_OUTCOMES``): ``answered`` (the
worker's result came back), ``not_claimed`` (no worker claimed the item in time), ``no_result``
(claimed, but no result in time, or the job was gone), ``handed_over`` (the request answered 202,
or put the document on its P1 job), ``backend_error`` (Valkey failed), ``memory_guard``,
``open_limit`` and ``input_budget`` (nothing was dispatched).

**Batch items (F13).** ``DoclingMove`` is the ``ocr_handoff`` of one item of the extract worker
(``worker.ExtractRunner``): it stores the document as the item's input and MarkItDown's result as
its draft, then queues the item on ``docling`` with the same job, index and client; the extract
worker completes it without a result. While the memory guard is high, or the input budget is
full, nothing is stored: the item keeps its draft with ``ocr_unavailable`` (without a draft it
fails ``rate_limited``, retryable), counted as ``memory_guard`` or ``input_budget``.

**The ``docling`` pool's items (the contract of the Docling worker).**

- Queue: ``docling_queue(valkey, settings)``, pool ``docling``, without the per-domain lease
  cap (nothing is fetched). Items are ``<job id>:<index>:<attempts>``; the client is
  ``<key name>#live`` for a sync request and the batch's client for a batch item moved by the
  extract worker (F13); the domain is the URL's host, ``-`` for an upload. Claims are leased for
  ``jobs.docling_lease_seconds`` (``2 * DOCLING_TIMEOUT_SECONDS + 10``).
- Job: ``JobStore.get``: kind ``extract`` (a sync URL request), ``file`` (an upload) or
  ``batch``; ``options`` are the request's (``format``, ``metadata``, ``ocr``, ``cache``,
  ``compliance``, ...); the owner is the principal (``worker.job_principal``). A reused P1 job
  is ``running`` without a lease when its item is queued.
  ``JobStore.url(job, index)`` is the request URL, ``""`` for an upload.
- Input: ``JobStore.get_input(job, index, DocumentInputMeta)``: the document's bytes and their
  ``DocumentInputMeta`` (kind, sniffed content type, file name of an upload, final URL, upstream
  status, validators, ``Content-Language``, charset, ``raw_sha256``, ``needs_ocr``: the probe
  said "scan", the compliance report, the fetch attempts). TTL ``DOCLING_INPUT_TTL_SECONDS``,
  never longer than the job's; None when it expired: the item then answers its draft with
  ``ocr_unavailable`` (``draft_without_ocr``: neither fingerprinted nor cached), or without a
  draft fails ``timeout``, retryable. The ``Worker`` deletes the input once the item's result is
  written (pool ``docling``, and any ``file`` job). An item lost too often
  (``JOB_ITEM_MAX_ATTEMPTS``) answers its draft the same way.
- Draft: ``JobStore.draft(job, index)``: MarkItDown's envelope of the document, not yet cached
  nor fingerprinted, or None (images, ODF). The runner answers the OCR result unless it failed
  (under ``ocr: always`` or ``needs_ocr``; otherwise the better of the two, ``escalation.
  better``), and the draft is deleted once the item has its outcome.
- Result: the runner returns the envelope and the ``Worker`` writes it as the item's result
  (only the first result of an item counts). A sync request polls the job until it is completed
  and deletes it; a job that is gone when the worker claims or writes was given up, and the item
  is dropped. The caller's open Docling jobs need no update: a completed or deleted job leaves
  the count by itself.

Logs carry job ids only, never a URL, header, cookie, action, file name or document byte.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Final, Literal

import structlog
from prometheus_client import CollectorRegistry, Counter
from redis.asyncio import Redis
from redis.exceptions import RedisError

from text_extraction.auth import Principal
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.errors import ServiceError
from text_extraction.extract_models import ExtractResult
from text_extraction.job_models import JobOptions
from text_extraction.jobs import InputBudgetExceeded, JobRecord, JobStore
from text_extraction.pipeline_documents import OCR_UNAVAILABLE_WARNING, OcrNeeded
from text_extraction.pool_dispatch import BACKEND_ERRORS, Handover, PoolDispatcher
from text_extraction.result_cache import MemoryPressure
from text_extraction.scheduler import FairQueue, QueueItem
from text_extraction.settings import Settings

_log = structlog.get_logger(__name__)

# The outcomes of ``text_extraction_docling_dispatch_total``; see the module docstring.
DOCLING_OUTCOMES: Final = (
    "answered",
    "not_claimed",
    "no_result",
    "handed_over",
    "backend_error",
    "memory_guard",
    "open_limit",
    "input_budget",
)
DOCLING_POOL: Final = "docling"
# The per-domain lease cap of the ``docling`` queue: OCR fetches nothing, so no host is spared.
_DOCLING_DOMAIN_CAP: Final = 1_000_000

# The error of a document without a draft while Valkey's memory guard is high.
OCR_BUSY_MESSAGE: Final = (
    "The service takes no new documents for OCR at the moment. Try again later."
)
_OCR_TIMEOUT_MESSAGE: Final = "OCR did not finish in time. Try again later."
_BACKEND_MESSAGE: Final = "The job backend is unavailable. Try again later."


def docling_queue(valkey: Redis, settings: Settings) -> FairQueue:
    """The ``docling`` pool's queue, as the API pods, the extract worker and the Docling worker
    use it: its claims ignore the per-domain lease cap."""
    return FairQueue(valkey, settings, DOCLING_POOL, domain_cap=_DOCLING_DOMAIN_CAP)


class OpenLimitReached(Exception):  # noqa: N818 - a refusal, not an error of ours
    """The caller has ``DOCLING_MAX_OPEN_PER_CLIENT`` unfinished Docling jobs."""


def ocr_fallback(
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


def draft_without_ocr(draft: Envelope[ExtractResult] | None) -> Envelope[ExtractResult] | None:
    """A queued item's draft as its answer when its OCR cannot run at all (its input expired, or
    the item was lost too often): with the warning ``ocr_unavailable`` and without its
    fingerprint, so it is neither compared nor cached, as for a retryable OCR failure. None
    without a draft (an image, an OpenDocument file)."""
    if draft is None or draft.data is None:
        return None
    warnings = list(dict.fromkeys([*draft.data.warnings, OCR_UNAVAILABLE_WARNING]))
    data = draft.data.model_copy(update={"warnings": warnings, "fingerprint": None})
    return draft.model_copy(update={"data": data})


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
        llm_key: str | None = None,
    ) -> str:
        """Queue the document on the ``docling`` pool and return its job's id: a new job of
        ``kind`` (with the caller's ``llm_key``, if any), or ``job_id`` (the request's P1 job),
        whose lease is cleared. The job counts
        as one of the caller's Docling jobs before its input is stored; over
        ``DOCLING_MAX_OPEN_PER_CLIENT`` this raises ``OpenLimitReached``, over
        ``DOCLING_INPUT_BUDGET_BYTES`` ``InputBudgetExceeded``, and a new job is deleted again.
        Valkey errors propagate."""
        settings = self._settings
        limit = settings.docling_max_open_per_client
        owner = principal.name

        async def prepare(job: str) -> None:
            if await self._store.add_docling_job(owner, job) > limit:
                await self._store.drop_docling_job(owner, job)
                raise OpenLimitReached
            try:
                await self._store.put_input(
                    job, 0, needed.input.body, needed.input.meta, settings.docling_input_ttl_seconds
                )
            except InputBudgetExceeded:
                await self._store.drop_docling_job(owner, job)
                raise

        try:
            # Counted first, so that a caller at its limit stores nothing at all.
            if await self._open_jobs(owner) >= limit:
                raise OpenLimitReached
            return await self.submit_job(
                kind,
                options,
                principal,
                url,
                job_id=job_id,
                draft=needed.draft,
                prepare=prepare,
                llm_key=llm_key,
            )
        except OpenLimitReached:
            self.count("open_limit")
            raise
        except InputBudgetExceeded:
            self.count("input_budget")
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
        self,
        job_id: str,
        *,
        extra_seconds: float = 0.0,
        detached: Callable[[], bool],
        pii: bool = False,
        llm: bool = False,
    ) -> Envelope[ExtractResult] | None:
        """The OCR result of the job's item 0, or None when there is none within
        ``DOCLING_SYNC_WAIT_SECONDS`` + ``extra_seconds`` in all (claimed or not; with ``pii``
        ``PII_SYNC_WAIT_SECONDS`` more in all, since the OCR result moves on to the ``nlp`` pool
        in the same job, and with ``llm`` ``LLM_SYNC_WAIT_SECONDS`` more for the llm pool), the
        job is gone, or ``detached()`` turned true. Valkey errors propagate."""
        seconds = self._settings.docling_sync_wait_seconds + extra_seconds
        pii_seconds = self._settings.pii_sync_wait_seconds if pii else 0.0
        llm_seconds = self._settings.llm_sync_wait_seconds if llm else 0.0
        return await self.wait_for(
            job_id,
            answered="answered",
            claim_within=seconds,
            total_within=seconds + pii_seconds + llm_seconds,
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
        llm_key: str | None = None,
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
        self._llm_key = llm_key
        self._clock = clock
        self.handed_over = False

    async def __call__(
        self, needed: OcrNeeded, without: Envelope[ExtractResult]
    ) -> Envelope[ExtractResult]:
        dispatcher, handover = self._dispatcher, self._handover
        if self._guard.high:
            dispatcher.count("memory_guard")
            return ocr_fallback(needed, without, ErrorCode.RATE_LIMITED, OCR_BUSY_MESSAGE)
        if not handover.claim_pool(DOCLING_POOL):  # the route is handing it over to a P1 job
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
        except InputBudgetExceeded:
            return ocr_fallback(needed, without, ErrorCode.RATE_LIMITED, OCR_BUSY_MESSAGE)
        except BACKEND_ERRORS as error:  # JobLost: the new job vanished before its input
            return self._backend_failed(needed, without, error)
        finally:
            if handover.claimed == "pool":
                handover.settle(job_id)
        try:
            result = await dispatcher.wait(
                job_id,
                extra_seconds=extra,
                detached=lambda: handover.detached,
                pii=self._options.pii is not None,
                llm=dispatcher.wants_llm(self._options),
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
            return ocr_fallback(needed, without, ErrorCode.TIMEOUT, _OCR_TIMEOUT_MESSAGE)
        # Answered here: nobody reads the job again, unless the route answered 202 meanwhile.
        if handover.release():
            await asyncio.shield(dispatcher.answered(job_id, result))
        return result

    async def _submit(self, needed: OcrNeeded, job_id: str | None = None) -> str:
        return await self._dispatcher.submit(
            self._kind,
            self._options,
            self._principal,
            self._url,
            needed,
            job_id=job_id,
            llm_key=self._llm_key,
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
        except InputBudgetExceeded:
            return ocr_fallback(needed, without, ErrorCode.RATE_LIMITED, OCR_BUSY_MESSAGE)
        except BACKEND_ERRORS as error:
            _dispatch_failed(error)
            return ocr_fallback(needed, without, ErrorCode.INTERNAL_ERROR, _BACKEND_MESSAGE)
        self.handed_over = True  # the Docling worker writes item 0 of the P1 job
        return without

    def _at_limit(
        self, needed: OcrNeeded, without: Envelope[ExtractResult]
    ) -> Envelope[ExtractResult]:
        """The caller is at ``DOCLING_MAX_OPEN_PER_CLIENT``: nothing was dispatched."""
        message = self._dispatcher.limit_message()
        return ocr_fallback(needed, without, ErrorCode.RATE_LIMITED, message)

    @staticmethod
    def _backend_failed(
        needed: OcrNeeded, without: Envelope[ExtractResult], error: Exception
    ) -> Envelope[ExtractResult]:
        """Valkey failed: the draft with ``ocr_unavailable``; a document without a draft is our
        fault (503)."""
        _dispatch_failed(error)
        if needed.draft is None:
            raise ServiceError(ErrorCode.INTERNAL_ERROR, _BACKEND_MESSAGE, 503, retryable=True)
        return ocr_fallback(needed, without, ErrorCode.INTERNAL_ERROR, _BACKEND_MESSAGE)


class DoclingMove:
    """The OCR handoff of one item (F13): moves its document to the ``docling`` pool, or, while
    ``guard`` is high or the input budget is full, keeps its draft with ``ocr_unavailable``.
    ``moved`` is True once the item is queued there; its runner then answers None. ``outcomes``
    (``text_extraction_docling_dispatch_total``) counts those refusals."""

    def __init__(
        self,
        store: JobStore,
        queue: FairQueue,
        guard: MemoryPressure | None,
        settings: Settings,
        job: JobRecord,
        item: QueueItem,
        url: str | None,
        *,
        outcomes: Counter | None = None,
    ) -> None:
        self._store = store
        self._queue = queue
        self._guard = guard
        self._settings = settings
        self._job = job
        self._item = item
        self._url = url
        self._outcomes = outcomes
        self.moved = False

    async def __call__(
        self, needed: OcrNeeded, without: Envelope[ExtractResult]
    ) -> Envelope[ExtractResult]:
        if self._guard is not None and self._guard.high:
            self._count("memory_guard")
            return ocr_fallback(needed, without, ErrorCode.RATE_LIMITED, OCR_BUSY_MESSAGE)
        job_id, index = self._job.id, self._item.index
        document = needed.input
        # Input, then draft, then the queue: the Docling worker may claim the item at once.
        try:
            await self._store.put_input(
                job_id,
                index,
                document.body,
                document.meta,
                self._settings.docling_input_ttl_seconds,
            )
        except InputBudgetExceeded:
            self._count("input_budget")
            return ocr_fallback(needed, without, ErrorCode.RATE_LIMITED, OCR_BUSY_MESSAGE)
        if needed.draft is not None:
            await self._store.put_draft(job_id, index, needed.draft)
        await self._queue.enqueue_items(job_id, self._item.client, [(index, self._url)])
        self.moved = True
        return without  # not stored: the item is completed without a result

    def _count(self, outcome: str) -> None:
        if self._outcomes is not None:
            self._outcomes.labels(outcome=outcome).inc()


def _dispatch_failed(error: Exception) -> None:
    # The type only: the failed command carried a document's bytes.
    _log.warning("docling_dispatch_failed", error_type=type(error).__name__)
