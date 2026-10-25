"""The ``asr`` pool's dispatch: media that needs speech recognition, handed to the ASR workers.

``AsrDispatcher`` and ``AsrHandoff`` serve the sync requests of the API pods, on the generic
``pool_dispatch.PoolDispatcher`` and ``pool_dispatch.Handover``; ``AsrMove`` serves the extract
worker's batch items (as F13). The last section is the contract of the ASR worker.

**Sync requests (ruling M9).** A transcription is always a job: nothing waits for it.
``AsrHandoff`` is the ``asr_handoff`` of one sync request (``Pipeline.run`` with
``transcribe: true``, and ``Pipeline.extract_upload`` for an audio or video upload, M14):

- While Valkey's memory guard is high, the caller has ``ASR_MAX_OPEN_PER_CLIENT`` unfinished ASR
  jobs, or the input would exceed ``DOCLING_INPUT_BUDGET_BYTES`` (the budget of all inputs,
  ``jobs.py``), nothing is stored or queued: the request answers its draft (a platform video's
  metadata) with the warning ``asr_unavailable``, or without a draft (a media file, an upload)
  ``failed``/``rate_limited``, retryable.
- Otherwise a one-item job of kind ``extract`` (a URL) or ``file`` (an upload) is created, the
  ``MediaInputMeta`` is stored as its input (with an upload's bytes; an empty body for a URL,
  whose media the ASR worker downloads itself), the metadata result as its draft, the caller's
  ``X-ASR-Api-Key`` as the job's secret (``JobStore.put_asr_key``), and the item is queued. The
  task leaves the job to the route (``Handover.adopt``) and answers None: the route answers 202
  with the job at once, whatever ``wait_seconds`` says.
- A request whose route already handed it over to a P1 job puts the media on that job: the
  ``MediaInputMeta`` replaces an upload's own input (``UploadInputMeta``), the item is queued and
  the job's lease is cleared. A refusal or failure is answered as above, and the request's task
  stores that answer in the P1 job.
- Valkey failing answers the draft with ``asr_unavailable``; without a draft it is our fault (503).
- A fallback answer carries no fingerprint: it was never compared, and the next request may get
  the transcription.

``text_extraction_asr_dispatch_total{outcome}`` (``ASR_OUTCOMES``): ``handed_over`` (the job was
queued: answered 202, or put on the request's P1 job), ``backend_error`` (Valkey failed),
``memory_guard``, ``open_limit`` and ``input_budget`` (nothing was dispatched).

**The caller's key** (M11). ``X-ASR-Api-Key`` (``asr_key_of``: 1 to 1024 visible ASCII
characters, else 400 without repeating it) is stored only with a job that waits for a
transcription. No job view, log line or result carries it; it leaves with the job, its TTL, or
the item's outcome (the ``Worker`` of pool ``asr``).

**Batch items (as F13).** ``AsrMove`` is the ``asr_handoff`` of one item of the extract worker
(``worker.ExtractRunner``): it stores the ``MediaInputMeta`` as the item's input (with the bytes
of an uploaded file's item), the metadata result as its draft, then queues the item on ``asr``
with the same job, index and client; the extract worker completes it without a result. Input,
then draft, then the queue: a crash in between leaves the item leased in ``extract``, and the
rerun moves it once more (the input and the draft are replaced). While the memory guard is high,
or the input budget is full, nothing is stored: the item keeps its draft with
``asr_unavailable`` (without a draft it fails ``rate_limited``, retryable), counted as
``memory_guard`` or ``input_budget``. A batch carries no caller key.

**The ``asr`` pool's items (the contract of the ASR worker, M10).**

- Queue: ``asr_queue(valkey, settings)``, pool ``asr``, without the per-domain lease cap: a
  download takes the host's turn of the outbound gate itself, and a long transcription must not
  hold the host's other items back. Items are ``<job id>:<index>:<attempts>``; the client is
  ``<key name>#live`` for a sync request and the batch's client for a moved batch item; the
  domain is the URL's host, ``-`` for an upload. Claims are leased for ``asr_lease_seconds``
  (``2 * ASR_TIMEOUT_SECONDS + 10``).
- Job: ``JobStore.get``: kind ``extract`` (a sync URL request), ``file`` (an upload) or
  ``batch``; ``options`` are the request's (``format``, ``metadata``, ``transcribe``,
  ``languages``, ``cache``, ``compliance``, ...); the owner is the principal
  (``worker.job_principal``). A reused P1 job is ``running`` without a lease when its item is
  queued. ``JobStore.url(job, index)`` is the request URL, ``""`` for an upload.
- Input: ``JobStore.get_input(job, index, MediaInputMeta)``: ``source`` (``platform``,
  ``file`` or ``upload``), ``platform``, ``url`` (the canonical URL of a platform video, to be
  downloaded with ``extractor`` as yt-dlp's only allowed extractor; the final URL of a media
  file), ``kind`` (a file's or an upload's ``MediaKind``), ``file_name`` (uploads, for the
  result only), ``duration`` (seconds, when the platform said), ``languages``, ``compliance``
  (the checks the URL passed) and ``attempts``. The body is the upload's bytes, empty for a URL.
  TTL ``ASR_INPUT_TTL_SECONDS``, never longer than the job's; None when it expired: the item then
  answers its draft with ``asr_unavailable`` (``draft_without_asr``: neither fingerprinted nor
  cached), or without a draft fails ``timeout``, retryable.
- Draft: ``JobStore.draft(job, index)``: the platform video's metadata result
  (``transcript_source: metadata_only``), not yet cached nor fingerprinted, or None (a media
  file, an upload).
- Key: ``JobStore.asr_key(job)``: the caller's key of the external endpoint, which wins over
  ``ASR_EXTERNAL_API_KEY``; None without one.
- Result: the runner returns the envelope and the ``Worker`` writes it as the item's result;
  then it deletes the item's input and draft, and the job's key unless the job is a batch. An
  item lost too often (``JOB_ITEM_MAX_ATTEMPTS``) answers ``draft_without_asr`` the same way
  (``internal_error``, retryable, without a draft). A job that is gone when the worker claims or
  writes was given up, and the item is dropped. The caller's open ASR jobs need no update: a
  completed or deleted job leaves the count by itself.

Logs carry job ids only, never a URL, a video id, a file name, a key or media bytes.
"""

import re
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
from text_extraction.jobs import InputBudgetExceeded, JobRecord, JobStore, asr_lease_seconds
from text_extraction.pipeline_media import ASR_UNAVAILABLE_WARNING, AsrNeeded
from text_extraction.pool_dispatch import BACKEND_ERRORS, Handover, PoolDispatcher
from text_extraction.result_cache import MemoryPressure
from text_extraction.scheduler import FairQueue, QueueItem
from text_extraction.settings import Settings

__all__ = [
    "ASR_BUSY_MESSAGE",
    "ASR_KEY_HEADER",
    "ASR_OUTCOMES",
    "ASR_POOL",
    "AsrDispatcher",
    "AsrHandoff",
    "AsrMove",
    "AsrOpenLimitReached",
    "asr_fallback",
    "asr_key_of",
    "asr_lease_seconds",
    "asr_queue",
    "draft_without_asr",
]

_log = structlog.get_logger(__name__)

# The outcomes of ``text_extraction_asr_dispatch_total``; see the module docstring.
ASR_OUTCOMES: Final = (
    "handed_over",
    "backend_error",
    "memory_guard",
    "open_limit",
    "input_budget",
)
ASR_POOL: Final = "asr"
# The per-domain lease cap of the ``asr`` queue: the worker's downloads take the gate's turns.
_ASR_DOMAIN_CAP: Final = 1_000_000

# The request header with the caller's key of the external transcription endpoint (M11).
ASR_KEY_HEADER: Final = "X-ASR-Api-Key"
_ASR_KEY: Final = re.compile(r"[\x21-\x7e]{1,1024}", re.ASCII)
_ASR_KEY_MESSAGE: Final = f"The {ASR_KEY_HEADER} header must be 1 to 1024 visible ASCII characters."

# The error of media without a draft while nothing can be queued for transcription.
ASR_BUSY_MESSAGE: Final = (
    "The service takes no new media for transcription at the moment. Try again later."
)
_BACKEND_MESSAGE: Final = "The job backend is unavailable. Try again later."


def asr_queue(valkey: Redis, settings: Settings) -> FairQueue:
    """The ``asr`` pool's queue, as the API pods, the extract worker and the ASR worker use it:
    its claims ignore the per-domain lease cap."""
    return FairQueue(valkey, settings, ASR_POOL, domain_cap=_ASR_DOMAIN_CAP)


def asr_key_of(value: str | None) -> str | None:
    """The caller's key from ``X-ASR-Api-Key``, None without the header; 400 for a value that is
    not 1 to 1024 visible ASCII characters (the answer never repeats it)."""
    if value is None:
        return None
    if _ASR_KEY.fullmatch(value) is None:
        raise ServiceError(ErrorCode.INVALID_REQUEST, _ASR_KEY_MESSAGE, 400)
    return value


class AsrOpenLimitReached(Exception):  # noqa: N818 - a refusal, not an error of ours
    """The caller has ``ASR_MAX_OPEN_PER_CLIENT`` unfinished ASR jobs."""


def asr_fallback(
    needed: AsrNeeded, without: Envelope[ExtractResult], code: ErrorCode, message: str
) -> Envelope[ExtractResult]:
    """The answer when media that needs a transcription does not get one: ``without``, the
    pipeline's answer without it (``pipeline_media.without_asr``: the draft with the warning
    ``asr_unavailable`` and without its fingerprint). Media without a draft fails with ``code``
    and ``message``, retryable."""
    if needed.draft is not None and without.data is not None:
        return without
    error = ErrorInfo(code=code, message=message, retryable=True)
    return without.model_copy(update={"status": "failed", "error": error})


def draft_without_asr(draft: Envelope[ExtractResult] | None) -> Envelope[ExtractResult] | None:
    """A queued item's draft as its answer when its transcription cannot run at all (its input
    expired, or the item was lost too often): with the warning ``asr_unavailable`` and without
    its fingerprint, so it is neither compared nor cached. None without a draft (a media file,
    an upload)."""
    if draft is None or draft.data is None:
        return None
    warnings = list(dict.fromkeys([*draft.data.warnings, ASR_UNAVAILABLE_WARNING]))
    data = draft.data.model_copy(update={"warnings": warnings, "fingerprint": None})
    return draft.model_copy(update={"data": data})


class AsrDispatcher(PoolDispatcher):
    """Puts sync requests' media that needs a transcription on the ``asr`` pool; see the module
    docstring. It never waits for a result."""

    def __init__(
        self,
        store: JobStore,
        queue: FairQueue,
        settings: Settings,
        *,
        registry: CollectorRegistry | None = None,
    ) -> None:
        super().__init__(ASR_POOL, store, queue, settings, registry, outcomes=ASR_OUTCOMES)

    async def submit(
        self,
        kind: Literal["extract", "file"],
        options: JobOptions,
        principal: Principal,
        url: str | None,
        needed: AsrNeeded,
        body: bytes = b"",
        *,
        key: str | None = None,
        job_id: str | None = None,
    ) -> str:
        """Queue the media on the ``asr`` pool and return its job's id: a new job of ``kind``,
        or ``job_id`` (the request's P1 job), whose lease is cleared. ``body`` is an upload's
        bytes, ``key`` the caller's ``X-ASR-Api-Key``. The job counts as one of the caller's ASR
        jobs before its input is stored; over ``ASR_MAX_OPEN_PER_CLIENT`` this raises
        ``AsrOpenLimitReached``, over ``DOCLING_INPUT_BUDGET_BYTES`` ``InputBudgetExceeded``,
        and a new job is deleted again. Valkey errors propagate."""
        settings = self._settings
        limit = settings.asr_max_open_per_client
        owner = principal.name

        async def prepare(job: str) -> None:
            if await self._store.add_asr_job(owner, job) > limit:
                await self._store.drop_asr_job(owner, job)
                raise AsrOpenLimitReached
            try:
                await self._store.put_input(
                    job, 0, body, needed.input, settings.asr_input_ttl_seconds
                )
            except InputBudgetExceeded:
                await self._store.drop_asr_job(owner, job)
                raise
            if key is not None:
                await self._store.put_asr_key(job, key)

        try:
            # Counted first, so that a caller at its limit stores nothing at all.
            if await self._open_jobs(owner) >= limit:
                raise AsrOpenLimitReached
            return await self.submit_job(
                kind, options, principal, url, job_id=job_id, draft=needed.draft, prepare=prepare
            )
        except AsrOpenLimitReached:
            self.count("open_limit")
            raise
        except InputBudgetExceeded:
            self.count("input_budget")
            raise

    async def _open_jobs(self, owner: str) -> int:
        try:
            return await self._store.open_asr_jobs(owner)
        except (RedisError, OSError):
            self.count("backend_error")
            raise

    def limit_message(self) -> str:
        """The error of media without a draft whose caller is at its open limit."""
        limit = self._settings.asr_max_open_per_client
        return (
            f"At most {limit} transcriptions of a key may wait at a time. Retry when one has "
            "finished."
        )


class AsrHandoff:
    """The ``asr_handoff`` of one sync request (``Pipeline.run`` or ``extract_upload``; M9).

    ``kind`` is the job kind of the request (``extract`` for a URL, ``file`` for an upload),
    ``options`` its stored options, ``url`` its URL (None for an upload), ``body`` an upload's
    bytes and ``key`` the caller's ``X-ASR-Api-Key``. ``handed_over`` is True once the job
    belongs to the worker (the route answers 202 with it, or it is the request's P1 job): the
    caller then answers None."""

    def __init__(
        self,
        dispatcher: AsrDispatcher,
        guard: MemoryPressure,
        handover: Handover,
        *,
        kind: Literal["extract", "file"],
        options: JobOptions,
        principal: Principal,
        url: str | None,
        body: bytes = b"",
        key: str | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._guard = guard
        self._handover = handover
        self._kind = kind
        self._options = options
        self._principal = principal
        self._url = url
        self._body = body
        self._key = key
        self.handed_over = False

    async def __call__(
        self, needed: AsrNeeded, without: Envelope[ExtractResult]
    ) -> Envelope[ExtractResult]:
        dispatcher, handover = self._dispatcher, self._handover
        if self._guard.high:
            dispatcher.count("memory_guard")
            return asr_fallback(needed, without, ErrorCode.RATE_LIMITED, ASR_BUSY_MESSAGE)
        claimed = handover.claim_pool(ASR_POOL)
        if not claimed:  # the route is handing it over to a P1 job
            await handover.settled()
            if handover.job_id is not None:
                return await self._on_p1_job(handover.job_id, needed, without)
            # The P1 job could not be created and the route waits for this answer: it answers
            # 202 with the job dispatched now (``adopt``).
        job_id: str | None = None
        try:
            job_id = await self._submit(needed)
            dispatcher.count("handed_over")
            handover.adopt(ASR_POOL, job_id)
        except AsrOpenLimitReached:
            return self._at_limit(needed, without)
        except InputBudgetExceeded:
            return asr_fallback(needed, without, ErrorCode.RATE_LIMITED, ASR_BUSY_MESSAGE)
        except BACKEND_ERRORS as error:  # JobLost: the new job vanished before its input
            return self._backend_failed(needed, without, error)
        finally:
            if claimed:
                handover.settle(job_id)
        self.handed_over = True  # the ASR worker writes the job's result
        return without

    async def _submit(self, needed: AsrNeeded, job_id: str | None = None) -> str:
        return await self._dispatcher.submit(
            self._kind,
            self._options,
            self._principal,
            self._url,
            needed,
            self._body,
            key=self._key,
            job_id=job_id,
        )

    async def _on_p1_job(
        self, job_id: str, needed: AsrNeeded, without: Envelope[ExtractResult]
    ) -> Envelope[ExtractResult]:
        """Put the media on the request's P1 job, which the route answered 202 with; the answer
        of a refusal or failure is stored in that job."""
        try:
            await self._submit(needed, job_id)
        except AsrOpenLimitReached:
            return self._at_limit(needed, without)
        except InputBudgetExceeded:
            return asr_fallback(needed, without, ErrorCode.RATE_LIMITED, ASR_BUSY_MESSAGE)
        except BACKEND_ERRORS as error:
            _dispatch_failed(error)
            return asr_fallback(needed, without, ErrorCode.INTERNAL_ERROR, _BACKEND_MESSAGE)
        self.handed_over = True  # the ASR worker writes item 0 of the P1 job
        return without

    def _at_limit(
        self, needed: AsrNeeded, without: Envelope[ExtractResult]
    ) -> Envelope[ExtractResult]:
        """The caller is at ``ASR_MAX_OPEN_PER_CLIENT``: nothing was dispatched."""
        message = self._dispatcher.limit_message()
        return asr_fallback(needed, without, ErrorCode.RATE_LIMITED, message)

    @staticmethod
    def _backend_failed(
        needed: AsrNeeded, without: Envelope[ExtractResult], error: Exception
    ) -> Envelope[ExtractResult]:
        """Valkey failed: the draft with ``asr_unavailable``; media without a draft is our fault
        (503)."""
        _dispatch_failed(error)
        if needed.draft is None:
            raise ServiceError(ErrorCode.INTERNAL_ERROR, _BACKEND_MESSAGE, 503, retryable=True)
        return asr_fallback(needed, without, ErrorCode.INTERNAL_ERROR, _BACKEND_MESSAGE)


class AsrMove:
    """The ASR handoff of one item (as F13): moves its media to the ``asr`` pool, or, while
    ``guard`` is high or the input budget is full, keeps its draft with ``asr_unavailable``.
    ``body`` is the bytes of an uploaded file's item (empty for a URL). ``moved`` is True once
    the item is queued there; its runner then answers None. ``outcomes``
    (``text_extraction_asr_dispatch_total``) counts those refusals."""

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
        body: bytes = b"",
        outcomes: Counter | None = None,
    ) -> None:
        self._store = store
        self._queue = queue
        self._guard = guard
        self._settings = settings
        self._job = job
        self._item = item
        self._url = url
        self._body = body
        self._outcomes = outcomes
        self.moved = False

    async def __call__(
        self, needed: AsrNeeded, without: Envelope[ExtractResult]
    ) -> Envelope[ExtractResult]:
        if self._guard is not None and self._guard.high:
            self._count("memory_guard")
            return asr_fallback(needed, without, ErrorCode.RATE_LIMITED, ASR_BUSY_MESSAGE)
        job_id, index = self._job.id, self._item.index
        # Input, then draft, then the queue: the ASR worker may claim the item at once.
        try:
            await self._store.put_input(
                job_id, index, self._body, needed.input, self._settings.asr_input_ttl_seconds
            )
        except InputBudgetExceeded:
            self._count("input_budget")
            return asr_fallback(needed, without, ErrorCode.RATE_LIMITED, ASR_BUSY_MESSAGE)
        if needed.draft is not None:
            await self._store.put_draft(job_id, index, needed.draft)
        await self._queue.enqueue_items(job_id, self._item.client, [(index, self._url)])
        self.moved = True
        return without  # not stored: the item is completed without a result

    def _count(self, outcome: str) -> None:
        if self._outcomes is not None:
            self._outcomes.labels(outcome=outcome).inc()


def _dispatch_failed(error: Exception) -> None:
    # The type only: the failed command may have carried an upload's bytes or the caller's key.
    _log.warning("asr_dispatch_failed", error_type=type(error).__name__)
