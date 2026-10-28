"""The ``nlp`` pool's dispatch: results whose personal data a request or job asked to handle, sent
to the nlp workers (rulings N11, N12, N13), and the contract of the nlp worker.

PII is the last step of every result that asked for it (``pii`` in the request or the job's
options). Whoever produces the result does not answer or store it: it stores the result as the
item's draft and queues the item on pool ``nlp``. ``PiiDispatcher`` and ``PiiHandoff`` serve the
sync requests of the API pods, on the generic ``pool_dispatch.PoolDispatcher`` and
``pool_dispatch.Handover``; ``PiiMove`` serves ``Worker._finish`` of every other pool (batch items
and every job a pool worker finishes). The last section is the contract of the nlp worker.

**Which envelopes go.** One that is not processed yet (``data.pii`` is None) and has something
N5 processes (``pii.fields.needs_pii``). An envelope without data (a failure of ours) goes as it
is; one with data but nothing to process (a failure without content and metadata) answers as it
is with a report of zero entities (``without_findings``), without the hop.

**Sync requests (``PiiHandoff``).** The ``pii_handoff`` of ``extract_api._flow`` and
``file_api._flow``, called once the flow has its envelope (fast, from a browser or Docling job,
or an OCR or browser fallback):

- While Valkey's memory guard is high nothing is stored: the request answers ``failed``/
  ``pii_failed``, retryable ("PII handling is busy; retry later."), the content withheld.
- Otherwise a one-item job of kind ``extract`` (a URL) or ``file`` (an upload) is created with
  the envelope as its draft, and item 0 is queued on ``nlp``. The request waits
  ``PII_SYNC_WAIT_SECONDS`` (plus whatever is left until its route hands it over), and its route
  answers 202 with the nlp job once its ``wait_seconds`` pass (D4 a: a request with ``pii`` and
  without ``wait_seconds`` waits ``PII_SYNC_WAIT_SECONDS``). An earlier pool job of the request
  that it gave up (a browser or OCR fallback) does not stand in the way: the hand-over is claimed
  again for the nlp job (``Handover.claim_after_release``).
- A request whose route handed it over to a P1 job puts the envelope on that job: its draft, the
  item queued, the job's lease cleared; the nlp worker writes item 0.
- A wait that ends without a result while nobody took the job over (the route could not create
  or detach a job), and Valkey failing, answer ``pii_failed``, retryable, content withheld: never
  the unprocessed envelope.
- A draft over ``JOB_ITEM_MAX_BYTES`` is stored without its content, as every job item's result
  is (``content_not_stored_too_large``): the processed answer then has no content either.

**Moves (``PiiMove``).** ``Worker._finish`` of every pool except ``nlp`` calls it for an envelope
that owes the step: it stores the envelope as the item's draft and queues the item on ``nlp``
(same job, index and client); the worker then completes the item without a result (outcome
``escalated``). Draft, then queue, then completion: a crash in between reruns the item, which
moves again, and only the first result of an item counts. While the memory guard is high nothing
is stored: the move declines and the worker writes ``pii_failed``, retryable, withheld. The
browser worker's renders, the Docling worker's OCR results and the ASR worker's transcripts take
this path, and so does a sync request's job of those pools: its waits are ``PII_SYNC_WAIT_SECONDS``
longer, because the job completes only once the nlp worker wrote its result.

``text_extraction_nlp_dispatch_total{outcome}`` (``PII_OUTCOMES``): ``answered`` (the worker's
result came back), ``not_claimed`` (no worker claimed the item in time), ``no_result`` (claimed,
but no result in time, or the job was gone), ``handed_over`` (the request answered 202, or put
the envelope on its P1 job), ``backend_error`` (Valkey failed) and ``memory_guard`` (nothing was
dispatched; moves of the workers count here too).

**The ``nlp`` pool's items (the contract of the nlp worker).**

- Queue: ``nlp_queue(valkey, settings)``, pool ``nlp``, without the per-domain lease cap (nothing
  is fetched). Items are ``<job id>:<index>:<attempts>``. Claims are leased for
  ``jobs.pii_lease_seconds`` (``2 * PII_TIMEOUT_SECONDS + 10``).
- Job: ``JobStore.get``: its ``options.pii`` says what to do.
- Draft: ``JobStore.draft(job, index)``: the unprocessed envelope of the item. None when it
  expired with its job: the item then answers ``pii_failed``, retryable. The ``Worker`` deletes
  the draft once the item's result is written (also of an exhausted item), with whatever the
  pools before left for the item: its input bytes and, but for a batch, the job's caller
  secrets. There are no input bytes of its own.
- Result: the processed envelope, or ``pii_failed`` / ``too_large`` with the content withheld.
  An item that is lost too often (``JOB_ITEM_MAX_ATTEMPTS``) answers ``pii_failed``, retryable,
  never its draft.

Logs carry job ids, kinds and error types only: never a text, a URL or a value of the result.
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
from text_extraction.extract_models import ExtractResult
from text_extraction.job_models import JobOptions
from text_extraction.jobs import JobRecord, JobStore
from text_extraction.pii.fields import (
    FAIL_CLOSED_MESSAGE,
    TOO_LARGE_MESSAGE,
    fail_closed,
    needs_pii,
)
from text_extraction.pii.replace import hash_key
from text_extraction.pii.types import PiiLanguage, PiiLanguageOption, PiiOptions, PiiReport
from text_extraction.pool_dispatch import BACKEND_ERRORS, Handover, PoolDispatcher
from text_extraction.queue_metrics import dispatch_counter_for
from text_extraction.result_cache import MemoryPressure
from text_extraction.scheduler import FairQueue, QueueItem
from text_extraction.settings import Settings

_log = structlog.get_logger(__name__)

PII_POOL: Final = "nlp"
_PII_DOMAIN_CAP: Final = 1_000_000

# The outcomes of ``text_extraction_nlp_dispatch_total``; see the module docstring.
PII_OUTCOMES: Final = (
    "answered",
    "not_claimed",
    "no_result",
    "handed_over",
    "backend_error",
    "memory_guard",
)
PII_BUSY_MESSAGE: Final = "PII handling is busy; retry later."


def pii_available(settings: Settings) -> bool:
    """Whether the service handles PII: the nlp workers are on, and so are jobs (the hop is a
    job)."""
    return settings.pii_enabled and settings.jobs_enabled


def nlp_queue(valkey: Redis, settings: Settings) -> FairQueue:
    """The ``nlp`` pool's queue, as the API pods, the other workers and the nlp worker use it:
    its claims ignore the per-domain lease cap."""
    return FairQueue(valkey, settings, PII_POOL, domain_cap=_PII_DOMAIN_CAP)


def choose_language(
    option: PiiLanguageOption, language: str | None, fallback: PiiLanguage
) -> tuple[PiiLanguage, bool]:
    """The model for a result (N8) and whether it is the fallback for a language that has no
    model of its own (the caller then adds ``pii_language_unsupported``)."""
    if option != "auto":
        return option, False
    if language == "de":
        return "de", False
    if language == "en":
        return "en", False
    return fallback, True


def withheld(
    envelope: Envelope[ExtractResult],
    options: PiiOptions | None,
    message: str = FAIL_CLOSED_MESSAGE,
) -> Envelope[ExtractResult]:
    """``pii.fields.fail_closed`` with ``message``: an envelope that owes the PII step as
    ``failed``/``pii_failed``, retryable, its text withheld. Any other envelope as it is."""
    if not needs_pii(envelope, options):
        return envelope
    held = fail_closed(envelope, options)
    error = ErrorInfo(code=ErrorCode.PII_FAILED, message=message, retryable=True)
    return held.model_copy(update={"error": error})


def without_findings(
    envelope: Envelope[ExtractResult], options: PiiOptions | None, settings: Settings
) -> Envelope[ExtractResult]:
    """An envelope with data that asked for PII, is not processed and has nothing N5 processes
    (a failure without content and metadata): as it is, with a report of zero entities. Any
    other envelope as it is, also one whose text the hop withheld (``_withheld_by_pii``): it
    has nothing left to process, but it was not processed either."""
    data = envelope.data
    if options is None or data is None or data.pii is not None or needs_pii(envelope, options):
        return envelope
    if _withheld_by_pii(envelope):
        return envelope
    language, _fallback = choose_language(
        options.language, data.language, settings.pii_fallback_language
    )
    report = PiiReport(
        mode=options.mode,
        language=language,
        entities={},
        items=[] if options.mode == "annotate" else None,
        hash_scope=hash_key(settings)[1] if options.mode == "hash" else None,
    )
    return envelope.model_copy(update={"data": data.model_copy(update={"pii": report})})


def _withheld_by_pii(envelope: Envelope[ExtractResult]) -> bool:
    """Whether the PII step withheld the envelope's text: ``pii_failed``, or the nlp worker's
    ``too_large`` (a target's own ``too_large`` has another message)."""
    error = envelope.error
    if error is None:
        return False
    if error.code == ErrorCode.PII_FAILED:
        return True
    return error.code == ErrorCode.TOO_LARGE and error.message == TOO_LARGE_MESSAGE


class PiiDispatcher(PoolDispatcher):
    """Puts sync requests' results that owe the PII step on the ``nlp`` pool and waits for the
    processed results; see the module docstring."""

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
            PII_POOL,
            store,
            queue,
            settings,
            registry,
            outcomes=PII_OUTCOMES,
            clock=clock,
            sleep=sleep,
        )

    async def submit(
        self,
        envelope: Envelope[ExtractResult],
        kind: Literal["extract", "file"],
        options: JobOptions,
        principal: Principal,
        url: str | None,
        job_id: str | None = None,
    ) -> str:
        """Queue item 0 on the ``nlp`` pool with ``envelope`` as its draft and return its job's
        id: a new job of ``kind``, or ``job_id`` (the request's P1 job), whose lease is cleared.
        Valkey errors propagate; a new job is deleted again first."""
        return await self.submit_job(kind, options, principal, url, job_id=job_id, draft=envelope)

    async def wait(
        self, job_id: str, *, extra_seconds: float = 0.0, detached: Callable[[], bool]
    ) -> Envelope[ExtractResult] | None:
        """The processed result of the job's item 0, or None when there is none within
        ``PII_SYNC_WAIT_SECONDS`` + ``extra_seconds`` in all (claimed or not), the job is gone,
        or ``detached()`` turned true. Valkey errors propagate."""
        seconds = self._settings.pii_sync_wait_seconds + extra_seconds
        return await self.wait_for(
            job_id,
            answered="answered",
            claim_within=seconds,
            total_within=seconds,
            detached=detached,
        )


class PiiHandoff:
    """The ``pii_handoff`` of one sync request (``extract_api._flow``, ``file_api._flow``; N11).

    ``kind`` is the job kind of the request (``extract`` for a URL, ``file`` for an upload),
    ``options`` its stored options (with ``pii``) and ``url`` its URL (None for an upload).
    ``wait_until`` is the ``time.monotonic()`` reading at which the route answers 202: the
    request waits that much longer, so that the route finds the job to answer with. A call
    answers the processed envelope, a withheld one, or None once the job belongs to the nlp
    worker (the route answered 202 with it, or it is the request's P1 job)."""

    def __init__(
        self,
        dispatcher: PiiDispatcher,
        guard: MemoryPressure,
        settings: Settings,
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
        self._settings = settings
        self._kind = kind
        self._options = options
        self._principal = principal
        self._url = url
        self._wait_until = wait_until
        self._clock = clock

    async def __call__(
        self, envelope: Envelope[ExtractResult], handover: Handover
    ) -> Envelope[ExtractResult] | None:
        pii = self._options.pii
        if pii is None:
            return envelope
        if not needs_pii(envelope, pii):
            return without_findings(envelope, pii, self._settings)
        dispatcher = self._dispatcher
        if self._guard.high:
            dispatcher.count("memory_guard")
            return withheld(envelope, pii, PII_BUSY_MESSAGE)
        claimed = handover.claim_after_release(PII_POOL)
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
            _dispatch_failed(error)
            return withheld(envelope, pii)
        finally:
            if claimed:
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
                return None  # the route answered 202 with the job: the worker writes its result
            await asyncio.shield(dispatcher.abandon(job_id))
            _dispatch_failed(error)
            return withheld(envelope, pii)
        if result is None:
            if not handover.release():
                return None  # the route answered 202 with the job: the worker writes its result
            await asyncio.shield(dispatcher.abandon(job_id))
            return withheld(envelope, pii)
        # Answered here: nobody reads the job again, unless the route answered 202 meanwhile.
        if handover.release():
            await asyncio.shield(dispatcher.answered(job_id, result))
        return result

    async def _submit(self, envelope: Envelope[ExtractResult], job_id: str | None = None) -> str:
        return await self._dispatcher.submit(
            envelope, self._kind, self._options, self._principal, self._url, job_id=job_id
        )

    async def _on_p1_job(
        self, job_id: str, envelope: Envelope[ExtractResult]
    ) -> Envelope[ExtractResult] | None:
        """Put the envelope on the request's P1 job, which the route answered 202 with; the
        withheld answer of a failure is stored in that job."""
        try:
            await self._submit(envelope, job_id)
        except BACKEND_ERRORS as error:
            _dispatch_failed(error)
            return withheld(envelope, self._options.pii)
        return None  # the nlp worker writes item 0 of the P1 job


class PiiMove:
    """The move of a finished item to the ``nlp`` pool (``Worker._finish`` of every other pool;
    N11). ``guard`` tells whether Valkey is nearly full; ``outcomes``
    (``text_extraction_nlp_dispatch_total``) counts the refused moves."""

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
        """Store ``envelope`` as the item's draft and queue the item on ``nlp`` with the same job,
        index and client; True once it is queued. False while the memory guard is high: nothing
        was stored. Valkey errors and ``JobLost`` propagate."""
        if self._guard is not None and self._guard.high:
            if self._outcomes is not None:
                self._outcomes.labels(outcome="memory_guard").inc()
            return False
        url = await self._store.url(job.id, item.index)
        # Draft first: the nlp worker may claim the item as soon as it is queued.
        await self._store.put_draft(job.id, item.index, envelope)
        await self._queue.enqueue_items(job.id, item.client, [(item.index, url or None)])
        return True


def create_pii_move(
    settings: Settings,
    valkey: Redis,
    store: JobStore,
    guard: MemoryPressure | None,
    registry: CollectorRegistry,
) -> PiiMove | None:
    """The move of a worker of another pool; None while the service has no PII."""
    if not pii_available(settings):
        return None
    return PiiMove(
        store,
        nlp_queue(valkey, settings),
        guard,
        outcomes=dispatch_counter_for(registry, PII_OUTCOMES, pool=PII_POOL),
    )


def _dispatch_failed(error: Exception) -> None:
    # The type only: the failed command carried the result's text.
    _log.warning("nlp_dispatch_failed", error_type=type(error).__name__)
