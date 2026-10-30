"""The ``nlp`` pool's dispatch: results whose personal data a request or job asked to handle, sent
to the nlp workers (rulings N11, N12, N13), and the contract of the nlp worker.

PII is the last step of every result that asked for it (``pii`` in the request or the job's
options). Whoever produces the result does not answer or store it: it stores the result as the
item's draft and queues the item on pool ``nlp``. ``PiiDispatcher`` and ``PiiHandoff`` serve the
sync requests of the API pods, on the generic ``pool_dispatch.PoolDispatcher``,
``pool_dispatch.Handover`` and ``pool_dispatch.LastHopHandoff``; ``PiiMove``
(``pool_dispatch.LastHopMove``) serves ``Worker._finish`` of every other pool (batch items and
every job a pool worker finishes). ``nlp`` is a last hop: what the generic classes do is in the
``pool_dispatch`` module docstring; this one has what is PII's (which envelopes go, the answer
when the hop does not serve them: withheld). The last section is the contract of the nlp
worker.

**Which envelopes go.** Without a DOM stage's ``structured`` (R12 b: it read the raw page,
which this step does not mask; a request with ``pii`` has no DOM stage anyway). One that is not
processed yet (``data.pii`` is None) and has something
N5 processes (``pii.fields.needs_pii``). Any other envelope answers as it is, without the hop:
one without data (a failure of ours), and one with nothing to process (a failure without content
and metadata), whose ``pii`` stays null because nothing was processed (issue #15).

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
moves again, and only the first result of an item counts. Once the completion holds, the item
never runs on its pool again: the worker deletes its input bytes and, on pool ``asr`` but for a
batch, the caller's key at once (R13); the draft is nlp's. A job that expired meanwhile
(``JobLost``) drops the item. While the memory guard is high nothing
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
  pools before may have left for the item (its input bytes, again, and, but for a batch, the
  job's caller secrets). There are no input bytes of its own.
- Result: the processed envelope, or ``pii_failed`` / ``too_large`` with the content withheld.
  An item that is lost too often (``JOB_ITEM_MAX_ATTEMPTS``) answers ``pii_failed``, retryable,
  never its draft.

Logs carry job ids, kinds and error types only: never a text, a URL or a value of the result.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Final, Literal

from prometheus_client import CollectorRegistry
from redis.asyncio import Redis

from text_extraction.auth import Principal
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractResult
from text_extraction.job_models import JobOptions
from text_extraction.jobs import JobRecord, JobStore
from text_extraction.pii.fields import needs_pii, withheld
from text_extraction.pii.types import PiiLanguage, PiiLanguageOption
from text_extraction.pool_dispatch import (
    Handover,
    LastHopHandoff,
    LastHopMove,
    PoolDispatcher,
    UnservedReason,
)
from text_extraction.queue_metrics import dispatch_counter_for
from text_extraction.result_cache import MemoryPressure
from text_extraction.scheduler import FairQueue, QueueItem
from text_extraction.settings import Settings
from text_extraction.structured.checks import wants_llm
from text_extraction.structured.post import without_dom_stage

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
        llm_key: str | None = None,
    ) -> str:
        """Queue item 0 on the ``nlp`` pool with ``envelope`` as its draft and return its job's
        id: a new job of ``kind`` (with the caller's ``llm_key``, if any: the result moves on to
        the llm worker), or ``job_id`` (the request's P1 job), whose lease is cleared. Valkey
        errors propagate; a new job is deleted again first."""
        return await self.submit_job(
            kind, options, principal, url, job_id=job_id, draft=envelope, llm_key=llm_key
        )

    async def wait(
        self,
        job_id: str,
        *,
        extra_seconds: float = 0.0,
        detached: Callable[[], bool],
        llm: bool = False,
    ) -> Envelope[ExtractResult] | None:
        """The processed result of the job's item 0, or None when there is none within
        ``PII_SYNC_WAIT_SECONDS`` + ``extra_seconds`` in all (claimed or not; with ``llm``
        ``LLM_SYNC_WAIT_SECONDS`` more, since the result moves on to the ``llm`` pool in the same
        job), the job is gone, or ``detached()`` turned true. Valkey errors propagate."""
        seconds = self._settings.pii_sync_wait_seconds + extra_seconds
        if llm:
            seconds += self._settings.llm_sync_wait_seconds
        return await self.wait_for(
            job_id,
            answered="answered",
            claim_within=seconds,
            total_within=seconds,
            detached=detached,
        )


class PiiHandoff(LastHopHandoff):
    """The ``pii_handoff`` of one sync request (``extract_api._flow``, ``file_api._flow``; N11),
    the generic ``pool_dispatch.LastHopHandoff`` on pool ``nlp``.

    ``kind`` is the job kind of the request (``extract`` for a URL, ``file`` for an upload),
    ``options`` its stored options (with ``pii``) and ``url`` its URL (None for an upload);
    ``llm_key`` is the caller's ``X-LLM-Api-Key`` of a request that may need the LLM, stored with
    the nlp job. ``wait_until`` is the ``time.monotonic()`` reading at which the route answers
    202: the request waits that much longer, so that the route finds the job to answer with. A call
    answers the processed envelope, a withheld one, or None once the job belongs to the nlp
    worker (the route answered 202 with it, or it is the request's P1 job)."""

    def __init__(
        self,
        dispatcher: PiiDispatcher,
        guard: MemoryPressure,
        *,
        kind: Literal["extract", "file"],
        options: JobOptions,
        principal: Principal,
        url: str | None,
        wait_until: float | None = None,
        llm_key: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(
            dispatcher,
            guard,
            pool=PII_POOL,
            kind=kind,
            options=options,
            principal=principal,
            url=url,
            wait_until=wait_until,
            llm_key=llm_key,
            clock=clock,
        )
        self._nlp = dispatcher
        # A result that moves on to the llm worker completes later (ruling S13).
        self._llm = wants_llm(
            options.structured,
            options.chunking,
            dispatcher.settings,
            describe=options.describe_images,
        )

    async def __call__(
        self, envelope: Envelope[ExtractResult], handover: Handover
    ) -> Envelope[ExtractResult] | None:
        # Defence in depth (R12 b): no DOM-stage value of the raw page passes the PII step.
        return await super().__call__(without_dom_stage(envelope), handover)

    def needs(self, envelope: Envelope[ExtractResult]) -> bool:
        pii = self._options.pii
        return pii is not None and needs_pii(envelope, pii)

    def unserved(
        self, envelope: Envelope[ExtractResult], reason: UnservedReason
    ) -> Envelope[ExtractResult]:
        """Withheld: ``pii_failed``, retryable, the text removed."""
        if reason == "busy":
            return withheld(envelope, self._options.pii, message=PII_BUSY_MESSAGE)
        return withheld(envelope, self._options.pii)

    async def _wait(
        self, job_id: str, *, extra_seconds: float, detached: Callable[[], bool]
    ) -> Envelope[ExtractResult] | None:
        return await self._nlp.wait(
            job_id, extra_seconds=extra_seconds, detached=detached, llm=self._llm
        )


class PiiMove(LastHopMove):
    """The move of a finished item to the ``nlp`` pool (``Worker._finish`` of every other pool;
    N11), the generic ``pool_dispatch.LastHopMove`` with the ``nlp`` queue. ``guard`` tells
    whether Valkey is nearly full; ``outcomes`` (``text_extraction_nlp_dispatch_total``) counts
    the refused moves. The draft has no DOM-stage ``structured`` (R12 b)."""

    async def __call__(
        self, job: JobRecord, item: QueueItem, envelope: Envelope[ExtractResult]
    ) -> bool:
        return await super().__call__(job, item, without_dom_stage(envelope))


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
