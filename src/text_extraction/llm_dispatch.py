"""The ``llm`` pool's dispatch: results whose post stage needs the LLM (``structured`` with strategy
``llm``, ``auto`` with fields still missing, ``semantic`` chunking, image descriptions of
``describe_images``), sent to the llm workers
(rulings S13, S14; decision D5 a), and the contract of the llm worker.

The LLM step is the last step of every result that needs it, after PII (D3 a: the LLM only ever
sees the masked content). Whoever produces such a result does not answer or store it: it stores
the result as the item's draft and queues the item on pool ``llm``. ``LlmDispatcher`` and
``LlmHandoff`` serve the sync requests of the API pods, on the generic
``pool_dispatch.PoolDispatcher``, ``pool_dispatch.Handover`` and
``pool_dispatch.LastHopHandoff``; ``LlmMove`` (``pool_dispatch.LastHopMove``) serves
``Worker._finish`` of every other pool (batch items and every job a pool worker finishes,
``nlp`` included). ``llm`` is a last hop: what the generic classes do is in the
``pool_dispatch`` module docstring; this one has what is the LLM's (which envelopes go, the
answer when the hop does not serve them). The last section is the contract of the llm worker.

**Which envelopes go.** Those whose post stage has not run and needs the LLM
(``structured.post.needs_llm``: the service has the LLM, and image descriptions are owed with
an image to send; or there is content, and strategy ``llm``, ``auto`` with fields missing after
the DOM stage and the patterns, or ``semantic`` chunking).
Every finish point runs the patterns first and routes on their outcome
(``structured.post.post_at_finish``). Any other envelope gets the post stage where it is, without
the LLM.

**Sync requests (``LlmHandoff``).** The hop of ``extract_api.post_stage`` (``extract_api._flow``,
``file_api._flow``), after the PII handoff:

- While Valkey's memory guard is high nothing is stored: the request answers its content with
  ``finish_post(..., reason="busy")``: ``partial``/``llm_failed``, retryable ("The LLM step is
  busy; retry later."), the fields asked of the LLM missing, ``semantic`` chunked ``recursive``.
- Otherwise a one-item job of kind ``extract`` (a URL) or ``file`` (an upload) is created with
  the envelope as its draft and the caller's ``X-LLM-Api-Key`` (if any) as its secret, and
  item 0 is queued on ``llm``. An image upload of ``describe_images`` (``UploadInput``) also
  stores its bytes as the item's input (``DOCLING_INPUT_TTL_SECONDS``, the input budget), for
  the llm worker to describe, when they are PNG, JPEG or WebP by their magic bytes and at most
  ``LLM_VISION_MAX_IMAGE_BYTES`` (anything else stores nothing: the image is skipped, ruling
  V-S7; the pipeline already skipped such an image upload, so it hops only for other LLM work,
  R-V3); a full budget answers as the memory guard does (``finish_post(..., reason="busy")``,
  counted ``input_budget``). The request waits ``LLM_SYNC_WAIT_SECONDS`` (plus whatever is
  left until its route hands it over), and its route answers 202 with the llm job once its
  ``wait_seconds`` pass (a request that may need the LLM and has no ``wait_seconds`` waits
  ``LLM_SYNC_WAIT_SECONDS``). An earlier pool job of the request that it gave up or answered
  with (a browser fallback, the nlp job) does not stand in the way: the hand-over is claimed
  again for the llm job (``Handover.claim_after_release``).
- A request whose route handed it over to a P1 job puts the envelope on that job: its draft, the
  item queued, the job's lease cleared; the llm worker writes item 0 (an upload's P1 job holds
  its input already).
- A wait that ends without a result while nobody took the job over, and Valkey failing, answer
  ``finish_post(..., reason="failed")``: the content without the LLM work, ``llm_failed``,
  retryable ("The LLM step did not run; retry later."), never a 5xx. The guard runs off the
  event loop (ruling R10).
- Without an llm worker running, the request answers 202 after its wait and the job waits
  (its backlog shows in the queue gauges of pool ``llm``), as with ``nlp``.

**Moves (``LlmMove``).** ``Worker._finish`` of every pool but ``llm`` calls it, after the PII
move, for a result that needs the LLM: it stores the envelope as the item's draft and queues
the item on ``llm`` (same job, index and client); the worker then completes the item without a
result (outcome ``escalated``). Draft, then queue, then completion: a crash in between reruns
the item, which moves again, and only the first result of an item counts. The pool that moves
an item keeps the caller's LLM key (the llm worker needs it); ``nlp`` moving on does not drop
the draft (the move has overwritten it). A job that expired meanwhile (``JobLost``) drops the
item. While the memory guard is high nothing is stored: the move declines and the worker writes
``finish_post(..., reason="busy")``. The browser, Docling, ASR and nlp workers' results take this
path, and so does a sync request's job of those pools: its waits are ``LLM_SYNC_WAIT_SECONDS``
longer, because the job completes only once the llm worker wrote its result.

``text_extraction_llm_dispatch_total{outcome}`` (``LLM_OUTCOMES``): ``answered`` (the worker's
result came back), ``not_claimed`` (no worker claimed the item in time), ``no_result`` (claimed,
but no result in time, or the job was gone), ``handed_over`` (the request answered 202, or put
the envelope on its P1 job), ``backend_error`` (Valkey failed), ``memory_guard`` (nothing was
dispatched; moves of the workers count here too) and ``input_budget`` (an image upload's bytes
did not fit the input budget: nothing was dispatched).

**The ``llm`` pool's items (the contract of the llm worker, ``llm_worker.py``).**

- Queue: ``llm_queue(valkey, settings)``, pool ``llm``, without the per-domain lease cap (nothing
  is fetched). Claims are leased for ``jobs.llm_lease_seconds`` (``5.5 * LLM_TIMEOUT_SECONDS +
  10``): the item's overall deadline is ``3 * LLM_TIMEOUT_SECONDS``, plus half that for an item
  that owes image descriptions (ruling R-V2).
- Job: ``JobStore.get``: its ``options.structured`` and ``options.chunking`` say what to do; its
  owner is the key the learned selectors are stored for.
- Key: ``JobStore.llm_key``, the caller's ``X-LLM-Api-Key``; without one, ``LLM_API_KEY``.
- Draft: ``JobStore.draft(job, index)``: the envelope after the DOM stage, the patterns and PII.
  None when it expired with its job: the item then answers ``internal_error``, retryable (there
  is nothing to post-process). The ``Worker`` deletes the draft once the item's result is written
  (also of an exhausted item), with any input of the item that is still there, and, but for a
  batch, the job's caller secrets. The writer of the item's result drops the caller's LLM key:
  a sync job's at once, a batch's with its last result.
- Input: ``JobStore.get_input(job, index, StoredInputMeta)``, an image upload's bytes for its
  description (a sync job's from the hop above, a P1 file job's or a Docling job's own; the
  ``Worker`` keeps an upload's input when a job with ``describe_images`` moves on to ``llm``).
  Gone: the image is skipped. The ``Worker`` deletes it with the result.
- Result: ``apply_post`` of the draft with the LLM; LLM failures are ``llm_failed`` with the
  content kept. An item that is lost too often (``JOB_ITEM_MAX_ATTEMPTS``) answers
  ``finish_post(draft, reason="failed")``, or ``internal_error``, retryable, when the draft is
  gone.

Logs carry job ids, kinds and error types only: never a text, a value, a selector or a key.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Final, Literal

from prometheus_client import CollectorRegistry
from redis.asyncio import Redis

from text_extraction.auth import Principal
from text_extraction.conversion_pool import ConversionPool
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractResult
from text_extraction.file_models import UploadInputMeta
from text_extraction.job_models import JobOptions
from text_extraction.jobs import InputBudgetExceeded, JobStore
from text_extraction.pool_dispatch import (
    Handover,
    LastHopHandoff,
    LastHopMove,
    PoolDispatcher,
    UnservedReason,
)
from text_extraction.queue_metrics import dispatch_counter_for
from text_extraction.result_cache import MemoryPressure
from text_extraction.scheduler import FairQueue
from text_extraction.settings import Settings, llm_available
from text_extraction.structured.post import finish_post, needs_llm
from text_extraction.vision.images import describable_upload

LLM_POOL: Final = "llm"
_LLM_DOMAIN_CAP: Final = 1_000_000

# The outcomes of ``text_extraction_llm_dispatch_total``; see the module docstring.
LLM_OUTCOMES: Final = (
    "answered",
    "not_claimed",
    "no_result",
    "handed_over",
    "backend_error",
    "memory_guard",
    "input_budget",
)


@dataclass(frozen=True)
class UploadInput:
    """An upload's bytes and their meta, for the sync hop of a request with
    ``describe_images`` (ruling V-S7)."""

    body: bytes
    meta: UploadInputMeta


def llm_queue(valkey: Redis, settings: Settings) -> FairQueue:
    """The ``llm`` pool's queue, as the API pods, the other workers and the llm worker use it: its
    claims ignore the per-domain lease cap."""
    return FairQueue(valkey, settings, LLM_POOL, domain_cap=_LLM_DOMAIN_CAP)


class LlmDispatcher(PoolDispatcher):
    """Puts sync requests' results that need the LLM on the ``llm`` pool and waits for the
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
        self.registry = registry  # the images' counter of an unserved envelope (None: global)
        super().__init__(
            LLM_POOL,
            store,
            queue,
            settings,
            registry,
            outcomes=LLM_OUTCOMES,
            clock=clock,
            sleep=sleep,
        )

    async def wait(
        self, job_id: str, *, extra_seconds: float = 0.0, detached: Callable[[], bool]
    ) -> Envelope[ExtractResult] | None:
        """The llm worker's result of the job's item 0, or None when there is none within
        ``LLM_SYNC_WAIT_SECONDS`` + ``extra_seconds`` in all (claimed or not), the job is gone,
        or ``detached()`` turned true. Valkey errors propagate."""
        seconds = self._settings.llm_sync_wait_seconds + extra_seconds
        return await self.wait_for(
            job_id,
            answered="answered",
            claim_within=seconds,
            total_within=seconds,
            detached=detached,
        )

    async def store_upload(self, upload: UploadInput, job_id: str) -> None:
        """Keep an image upload's bytes as item 0's input of ``job_id`` for the llm worker
        (``DOCLING_INPUT_TTL_SECONDS``). ``InputBudgetExceeded`` (counted ``input_budget``) and
        Valkey errors propagate."""
        try:
            await self._store.put_input(
                job_id, 0, upload.body, upload.meta, self._settings.docling_input_ttl_seconds
            )
        except InputBudgetExceeded:
            self.count("input_budget")
            raise


def _describable(upload: UploadInput, settings: Settings) -> bool:
    """Whether the llm worker can describe these bytes: PNG, JPEG or WebP by their magic
    bytes, at most ``LLM_VISION_MAX_IMAGE_BYTES`` (V-S7)."""
    return describable_upload(upload.body, settings.llm_vision_max_image_bytes)


class LlmHandoff(LastHopHandoff):
    """The LLM hop of one sync request (``extract_api.post_stage``; S13), the generic
    ``pool_dispatch.LastHopHandoff`` on pool ``llm``.

    ``kind`` is the job kind of the request (``extract`` for a URL, ``file`` for an upload),
    ``options`` its stored options (with ``structured``, ``chunking`` and ``describe_images``) and
    ``url`` its URL (None for an upload); ``llm_key`` is the caller's ``X-LLM-Api-Key``, stored with
    the llm job; ``upload`` an image upload's bytes, stored as a new job's input when the llm worker
    can describe them (V-S7). ``wait_until`` is the ``time.monotonic()`` reading at which the route
    answers 202; ``threads`` is the app's post pool (ruling R21). A call answers the llm worker's
    result, the envelope finished without the LLM (``finish_post`` with the reason, on ``threads``),
    or None once the job belongs to the llm worker (the route answered 202 with it, or it is the
    request's P1 job)."""

    def __init__(
        self,
        dispatcher: LlmDispatcher,
        guard: MemoryPressure,
        *,
        kind: Literal["extract", "file"],
        options: JobOptions,
        principal: Principal,
        url: str | None,
        threads: ConversionPool,
        wait_until: float | None = None,
        llm_key: str | None = None,
        upload: UploadInput | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        prepare = None
        if upload is not None and _describable(upload, dispatcher.settings):
            prepare = partial(dispatcher.store_upload, upload)
        super().__init__(
            dispatcher,
            guard,
            pool=LLM_POOL,
            kind=kind,
            options=options,
            principal=principal,
            url=url,
            wait_until=wait_until,
            llm_key=llm_key,
            prepare=prepare,
            clock=clock,
        )
        self._llm = dispatcher
        self._threads = threads
        self._unserved: UnservedReason | None = None

    async def __call__(
        self, envelope: Envelope[ExtractResult], handover: Handover
    ) -> Envelope[ExtractResult] | None:
        self._unserved = None
        try:
            result = await super().__call__(envelope, handover)
        except InputBudgetExceeded:  # the upload's bytes did not fit: nothing was dispatched
            result = self.unserved(envelope, "busy")
        if result is None or self._unserved is None:
            return result
        # The guard of an unserved envelope, on the post pool (R10, R21): chunking is CPU work.
        options = self._options
        return await self._threads.run(
            partial(
                finish_post,
                settings=self._llm.settings,
                reason=self._unserved,
                describe=options.describe_images,
                registry=self._llm.registry,
            ),
            result,
            options.structured,
            options.chunking,
        )

    def needs(self, envelope: Envelope[ExtractResult]) -> bool:
        options = self._options
        return needs_llm(
            envelope,
            options.structured,
            options.chunking,
            self._llm.settings,
            describe=options.describe_images,
        )

    def unserved(
        self, envelope: Envelope[ExtractResult], reason: UnservedReason
    ) -> Envelope[ExtractResult]:
        """The envelope as it is, its reason kept: ``__call__`` finishes it without the LLM
        (``finish_post``: ``llm_failed``, retryable, the content kept) on the post pool."""
        self._unserved = reason
        return envelope

    async def _wait(
        self, job_id: str, *, extra_seconds: float, detached: Callable[[], bool]
    ) -> Envelope[ExtractResult] | None:
        return await self._llm.wait(job_id, extra_seconds=extra_seconds, detached=detached)


class LlmMove(LastHopMove):
    """The move of a finished item to the ``llm`` pool (``Worker._finish`` of every other pool;
    S13), the generic ``pool_dispatch.LastHopMove`` with the ``llm`` queue. ``guard`` tells
    whether Valkey is nearly full; ``outcomes`` (``text_extraction_llm_dispatch_total``) counts
    the refused moves."""


def create_llm_move(
    settings: Settings,
    valkey: Redis,
    store: JobStore,
    guard: MemoryPressure | None,
    registry: CollectorRegistry,
) -> LlmMove | None:
    """The move of a worker of another pool; None while the service has no LLM."""
    if not llm_available(settings):
        return None
    return LlmMove(
        store,
        llm_queue(valkey, settings),
        guard,
        outcomes=dispatch_counter_for(registry, LLM_OUTCOMES, pool=LLM_POOL),
    )
