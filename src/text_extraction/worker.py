"""The extract worker (entry point ``text-extraction-worker``): runs batch items from the fair
queue through the fast pipeline and stores their results in the job.

**Worker and runner.** ``Worker`` is the part every worker pool shares: the slot loops, the
claims and leases, deferrals, retries, exhausted items, result writes and metrics. What one item
does is its pool's ``ItemRunner``: here the ``ExtractRunner`` (pool ``extract``, the fast
pipeline); the browser worker (``browser_worker.py``, pool ``browser``) runs the same ``Worker``
with its ``BrowserRunner``. A runner gives the lease its items need and turns one item into its
envelope, or into None when it moved the item to another pool.

A worker pod runs ``WORKER_CONCURRENCY`` slot loops. Each loop:

1. claims the next item, leased for the runner's lease (``EXTRACT_TIMEOUT_SECONDS * 2 + 10``
   seconds here). An item that lost too many leases (``exhausted``) fails with
   ``internal_error``, retryable (an item of pool ``docling`` answers its draft with
   ``ocr_unavailable`` instead, when it has one; of pool ``asr`` with ``asr_unavailable``; of
   pool ``nlp`` with ``pii_failed``, retryable, never its unprocessed draft; of pool ``llm``
   with its draft without the LLM work, ``llm_failed``, retryable, when it has one), and
   is completed once that is recorded or its job is gone. A Valkey error leaves it leased: it
   comes back after the lease, so its batch still gets a result for it;
2. with nothing to claim, sleeps ``min(retry_in or 1, 1)`` seconds, or backs off from 0.05 s to
   1 s while claims keep answering that their scan budget ran out; every idle sleep has ±50 %
   jitter, so the loops of many pods do not claim in step;
3. records the claim with the job (``JobStore.record_claim``: the pool, the claim number, which
   is ``attempts + 1``, and the time; D8), so the job shows where the item runs and how often it
   was claimed; a Valkey error there is logged (``job_item_claim_not_recorded``) and the item
   runs anyway. A copy of an item that has its result, and an item whose job is gone, record
   nothing; an exhausted item records its last claim before it fails. Then it hands the item
   to the runner, which rebuilds the request from the job (its options and its owner as the
   principal) and runs the pipeline. When the outbound gate refuses the host (busy or paused),
   the item is deferred to the gate's retry time, which frees the slot for other domains. An
   item at ``JOB_ITEM_MAX_ATTEMPTS`` is not deferred again: it runs once more with the refusal
   answered as the pipeline's ``rate_limited`` or ``connection_error`` envelope, which is
   stored;
4. stores the result (only the first result of an item counts), then completes the item. An item
   whose job expired is completed and dropped. A worker whose lease was taken back (it hung past
   it) still stores its result, but its repeat write or deferral is not counted in the metrics.
   An item the runner moved, or whose result moved to the ``nlp`` or the ``llm`` pool (PII and
   the LLM, below), is completed without a result (outcome ``escalated``). A claimed
   item that has its result already (a copy left by a lost lease or a double move) is completed
   without running again. An exhausted item's caller secrets are deleted with its failure.

**Auto escalation (B5).** With ``BROWSER_ENABLED``, an ``auto`` item whose fast result needs the
browser (``escalation.needs_browser``; under unblock mode, for an admin's ``compliance: ignore``
job, bot walls and refusals too, B16), or whose fast result is a YouTube video's details whose
subtitles could not be read (``escalation.transcript_panel_applies``, P13-S1: the browser worker
tries the video's transcript view), moves to the ``browser`` pool: its fast envelope is
stored as the item's draft, the item is queued on ``browser`` with the same job, index and
client, and it is completed here without a result. The browser worker renders it and keeps the
better of both. Draft first, then the browser queue, then the completion: a crash in between
leaves the item leased here, so it runs again and moves again, and only the first result of an
item counts.

**OCR (F13).** While the service has OCR (``DOCLING_ENABLED`` and ``JOBS_ENABLED``), an item
whose document needs it moves to the ``docling`` pool the same way: its bytes are stored as the
item's input (``DOCLING_INPUT_TTL_SECONDS``), MarkItDown's result as its draft, the item is
queued on ``docling`` with the same job, index and client, and it is completed here without a
result. Input, then draft, then the queue, then the completion: a crash in between leaves the
item leased here, and the rerun moves it once more. While the memory guard is high nothing is
stored: the item keeps its draft with the warning ``ocr_unavailable`` (an image without one fails
``rate_limited``, retryable). The ``docling_dispatch`` module docstring has what the Docling worker
reads. A worker of the ``docling`` pool deletes an item's input and draft once its result is
written (an exhausted item's too).

**Transcriptions (M9).** While the service has ASR (``ASR_ENABLED`` and ``JOBS_ENABLED``), an
item whose media needs a transcription (``transcribe: true``: a platform video without
subtitles, a media file; an audio or video upload) moves to the ``asr`` pool the same way
(``asr_dispatch.AsrMove``): its ``MediaInputMeta`` is stored as the item's input
(``ASR_INPUT_TTL_SECONDS``, with an upload's bytes), the video's metadata as its draft, the item
is queued on ``asr`` with the same job, index and client, and it is completed here without a
result. While the memory guard is high or the input budget is full nothing is stored: the item
keeps its draft with the warning ``asr_unavailable`` (media without one fails ``rate_limited``,
retryable). The ``asr_dispatch`` module docstring has what the ASR worker reads. A worker of the
``asr`` pool deletes an item's input and draft once its result is written, and the job's caller
key unless the job is a batch; an exhausted item of that pool answers its draft with
``asr_unavailable`` when it has one.

**PII (N11, N12).** While the service has PII (``PII_ENABLED`` and ``JOBS_ENABLED``), a result
of a job with ``pii`` that owes the step (not processed yet, with text that N5 processes) is not
written by ``_finish`` of any pool but ``nlp``: it moves to the ``nlp`` pool
(``pii_dispatch.PiiMove``): the result is stored as the item's draft, the item is queued on
``nlp`` with the same job, index and client, and it is completed here without a result (outcome
``escalated``). Draft, then the queue, then the completion: a crash in between leaves the item
leased here, and the rerun moves it once more; only the first result of an item counts. The
browser, Docling and ASR workers take the same path, so a render, an OCR result or a transcript
of such a job is processed before its job completes. While the memory guard is high nothing is
stored: the item fails ``pii_failed``, retryable, its text withheld. A result without any such
text is written as it is, with ``pii`` null. Every result passes the guard
``pii.fields.fail_closed`` before it is written: one that still owes the step (a worker without
the move) is ``pii_failed`` without its text. The ``pii_dispatch`` module docstring has what the
nlp worker reads; a worker of pool ``nlp`` deletes an item's draft and input, and a sync
request's caller secrets, once its result is written.

**The post stage (S5, S13).** A result of a job with ``structured``, ``chunking`` or
``describe_images`` that ``_finish`` writes gets the post stage first
(``structured.post.post_at_finish``: ``run_post_patterns``, then ``finish_post``, on the worker's
post pool, ``POST_CONCURRENCY`` threads: ruling R21), on its final content: after the PII step,
before the guard ``fail_closed``. A result that moves to the ``nlp`` pool gets it there, on the
masked content; one that still owes the PII step and did not move gets none (it is withheld).

**The LLM (S13, S14).** While the service has the LLM (``LLM_ENABLED`` and ``JOBS_ENABLED``), a
result whose post stage needs it (``structured.post.needs_llm``, after the patterns) is not written
by ``_finish`` of any pool but ``llm``: it moves to the ``llm`` pool (``llm_dispatch.LlmMove``) as
results move to ``nlp``, after the PII move (so ``nlp`` moves its masked results on), and is
completed without a result (outcome ``escalated``). The pool that moves it keeps the caller's LLM
key; ``nlp`` keeps the draft, which the move overwrote. While the memory guard is high nothing is
stored: the result is written without the LLM work (``partial``/``llm_failed``, retryable, "The LLM
step is busy; retry later.", its content kept); so is a result of a worker without the move. The
image descriptions of ``describe_images`` are LLM work too (issue #20): a result with images to
describe moves, one without gets its empty report here; without the move its images count
``failed``, ``llm_failed``. An image upload (PNG, JPEG, WebP) that moves on with its description
owed keeps the item's input (its bytes, which the llm worker describes and deletes with the
result); any other upload's input goes with the move, as without the option. The
``llm_dispatch`` module docstring has what the llm worker reads. Whichever pool writes an item's
result drops the caller's LLM key of the job (R11): a sync request's job at once, a batch with its
last result.

**Site jobs (package 9).** Every item of a ``site`` job (a page, robots.txt, a sitemap) goes
to the ``SiteRunner`` (``site.runner``), which appends what the item found as new items of the
same job and queues them before the item's own result is written; its items never move to
another pool. An exhausted item of a ``site`` job first queues again the items it appended
(``SiteRunner.requeue_children``), then fails as above, so the job still completes.

**Uploads.** An item of a ``file`` job has no URL (``JobStore.url`` is ``""``): its bytes are
the item's input, which the runner converts with ``Pipeline.extract_upload``. An input that
expired fails ``timeout``, retryable. Once such an item's result is written, the worker deletes
its input; so does a worker of pool ``docling`` for each of its items.

While Valkey's memory is at ``VALKEY_MEMORY_GUARD_RATIO`` of its maximum (``memory_guard.py``,
read every 5 s from the start to the end of the process), the result cache skips its writes; the
results still go to the job. Over the same time the process samples its event loop's lag every
0.5 s (``loop_lag.py``, ``text_extraction_event_loop_lag_seconds``).

A Valkey error, an ``OSError`` (a Valkey call timing out) or the gate's 503 backs the loop off
for 1, 2, 4 … 30 s; the lease of the item in hand then expires and the item is claimed again.
No error ends a loop.

The process serves only the internal port (``/health``, ``/ready``, ``/metrics``). Its start-up
loads the language model and starts the document pool's children (``DocumentPool``, which
converts documents fetched by URL) and, with ``MEDIA_ENABLED``, the media fetcher's children and
egress proxy (``media.pool``, subtitles of platform videos); ``/ready`` answers 200 once the
loops run after it, while Valkey answers. On SIGTERM or SIGINT it turns 503, the
loops stop claiming, the items in flight get ``extraction_graceful_seconds`` to finish (any still
running then are cancelled; their leases expire and other workers take them over), and the
internal server stops last. A worker built with ``give_back_on_cancel`` (the ASR worker, whose
leases last hours) gives a cancelled item back to its queue at once instead (``FairQueue.defer``
to now: the front of its domain, ``attempts + 1``, so the item is failed as lost once it was
given back ``JOB_ITEM_MAX_ATTEMPTS`` times); when Valkey does not answer within
``_GIVE_BACK_SECONDS`` the lease expires as without it. A claim already sent when the signal
arrives is still processed within that time, since its item is leased to this worker already.
"""

import asyncio
import contextlib
import random
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal, Protocol
from weakref import WeakKeyDictionary

import structlog
from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from text_extraction.asr_dispatch import (
    ASR_OUTCOMES,
    ASR_POOL,
    AsrMove,
    asr_queue,
    draft_without_asr,
)
from text_extraction.auth import Principal
from text_extraction.conversion_pool import ConversionPool
from text_extraction.docling_dispatch import (
    DOCLING_OUTCOMES,
    DOCLING_POOL,
    DoclingMove,
    docling_queue,
    draft_without_ocr,
)
from text_extraction.documents.pool import DocumentPool
from text_extraction.documents.routing import ocr_available
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.escalation import needs_browser, transcript_panel_applies, unblock_for
from text_extraction.extract_models import ExtractRequest, ExtractResult, failed_extract
from text_extraction.fetch import Fetcher
from text_extraction.file_models import UploadInputMeta, form_of
from text_extraction.internal import Readiness, create_internal_app
from text_extraction.jobs import JobLost, JobRecord, JobStore, job_principal
from text_extraction.llm_dispatch import LLM_POOL, LlmMove, create_llm_move
from text_extraction.loop_lag import LoopLag
from text_extraction.media.pool import MediaFetcher, create_media_fetcher
from text_extraction.memory_guard import MemoryGuard
from text_extraction.metadata import load_language_model
from text_extraction.outbound import CircuitOpen, OutboundBusy
from text_extraction.pii.fields import FAIL_CLOSED_MESSAGE, fail_closed, needs_pii, withheld
from text_extraction.pii_dispatch import (
    PII_BUSY_MESSAGE,
    PII_POOL,
    PiiMove,
    create_pii_move,
)
from text_extraction.pipeline import Pipeline, create_outbound, create_pipeline
from text_extraction.pipeline_documents import create_document_pool
from text_extraction.pipeline_media import asr_available
from text_extraction.queue_metrics import dispatch_counter_for, job_duration_for
from text_extraction.result_cache import MemoryPressure
from text_extraction.scheduler import FairQueue, QueueItem
from text_extraction.server import (
    _HANDLED_SIGNALS,
    _Server,
    _server,
    configured_settings,
    extraction_graceful_seconds,
)
from text_extraction.settings import Settings
from text_extraction.site.runner import SiteRunner
from text_extraction.site.store import SiteStore
from text_extraction.structured.post import LlmHop, finish_post, post_at_finish
from text_extraction.structured.selector_pool import SelectorRunner, create_selector_runner
from text_extraction.valkey import create_valkey
from text_extraction.vision.describe import needs_vision

_log = structlog.get_logger(__name__)

# Idle sleep without an item: the most a loop waits before it claims again.
_IDLE_SECONDS = 1.0
# First sleep after a claim that ran out of its scan budget; it doubles up to the idle sleep.
_SCAN_BACKOFF_SECONDS = 0.05
# Backoff while Valkey fails: 1, 2, 4 … seconds, at most this.
_MAX_VALKEY_BACKOFF_SECONDS = 30.0
# Pause after an unexpected error in a loop, so a bug cannot spin it.
_ERROR_PAUSE_SECONDS = 1.0
# How long a shutdown waits for a cancelled start-up to unwind.
_START_UNWIND_SECONDS = 5.0
# How long a cancelled item's give-back may take before its lease is left to expire.
_GIVE_BACK_SECONDS = 5.0

_LOST_MESSAGE = "The item was lost by a worker too often."
_INPUT_EXPIRED_MESSAGE = "The uploaded file expired before it was extracted. Upload it again."
_INTERNAL_MESSAGE = "Internal server error."
_NO_SITE_MESSAGE = "This worker cannot run site structure items."

# Outcomes of an item besides its envelope's: given back to the queue, its job is gone, or it
# moved to another pool (the browser's, Docling's, ASR's, nlp's or llm's).
_DEFERRED = "deferred"
_LOST = "lost"
_ESCALATED = "escalated"
# Logged, not counted: the item had its result already, or another worker holds it now.
_SUPERSEDED = "superseded"

_ITEM_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 20, 25, 30, 45, 60)


@dataclass(frozen=True)
class _Metrics:
    items: Counter
    item_seconds: Histogram
    busy: Gauge
    job_duration: Histogram


# One set of metrics per registry, so several workers (tests) can share one.
_METRICS: WeakKeyDictionary[CollectorRegistry, _Metrics] = WeakKeyDictionary()


def _metrics_for(registry: CollectorRegistry) -> _Metrics:
    metrics = _METRICS.get(registry)
    if metrics is None:
        metrics = _Metrics(
            items=Counter(
                "text_extraction_job_items_total",
                "Job items handled by this worker, by outcome: success, the error code of the "
                "result, deferred (given back while the host is busy or paused), lost (the "
                "job was gone) or escalated (moved to the browser, the docling, the asr, the nlp "
                "or the llm pool).",
                ["outcome"],
                registry=registry,
            ),
            item_seconds=Histogram(
                "text_extraction_job_item_seconds",
                "Time one job item took in the worker, from the claim to the stored result.",
                buckets=_ITEM_BUCKETS,
                registry=registry,
            ),
            busy=Gauge(
                "text_extraction_worker_busy_slots",
                "Slots of this worker that are processing an item.",
                registry=registry,
            ),
            job_duration=job_duration_for(registry),
        )
        _METRICS[registry] = metrics
    return metrics


def _outcome(envelope: Envelope[ExtractResult]) -> str:
    """Metric label: the envelope's error code, else its status (``success``)."""
    return envelope.error.code.value if envelope.error is not None else envelope.status


class ItemRunner(Protocol):
    """What a worker pool does with one claimed item (see the module docstring)."""

    # How long a claim of this pool holds its item: longer than one run can take.
    lease_seconds: float

    async def run(
        self,
        job: JobRecord,
        item: QueueItem,
        url: str,
        *,
        gate_refusals: Literal["fail", "raise"],
    ) -> Envelope[ExtractResult] | None:
        """The item's result, or None when the item was moved to another pool: it is then
        completed without a result. ``url`` is ``""`` for an upload's item. A gate refusal
        (``OutboundBusy``, ``CircuitOpen``) with ``gate_refusals="raise"``, a Valkey error and
        ``JobLost`` propagate; the worker defers, backs off or drops the item."""
        ...


class ExtractRunner:
    """The ``extract`` pool's runner: the fast pipeline, the move of an ``auto`` item that needs
    the browser to the ``browser`` pool (B5), the move of a document that needs OCR to the
    ``docling`` pool (F13), and the move of media that needs a transcription to the ``asr`` pool
    (M9). ``browser_queue`` is the browser pool's queue, None while ``BROWSER_ENABLED`` is false;
    ``docling_queue`` the Docling pool's, None while the service has no OCR; ``asr_queue`` the
    ASR pool's, None while the service has no ASR; then nothing moves there. ``guard`` tells
    whether Valkey is nearly full; ``registry`` gets ``text_extraction_docling_dispatch_total``
    and ``text_extraction_asr_dispatch_total`` for the refused moves. ``site`` runs the items of
    ``site`` jobs (package 9), which never move to another pool; without it they fail
    ``internal_error``."""

    def __init__(
        self,
        pipeline: Pipeline,
        store: JobStore,
        settings: Settings,
        browser_queue: FairQueue | None,
        *,
        docling_queue: FairQueue | None = None,
        asr_queue: FairQueue | None = None,
        guard: MemoryPressure | None = None,
        registry: CollectorRegistry | None = None,
        site: SiteRunner | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._store = store
        self._settings = settings
        self._browser_queue = browser_queue
        self.site = site
        self._docling_queue = docling_queue
        self._asr_queue = asr_queue
        self._guard = guard
        # The refused moves count in text_extraction_docling_dispatch_total, as sync requests'.
        self._docling_outcomes = (
            dispatch_counter_for(registry, DOCLING_OUTCOMES, pool=DOCLING_POOL)
            if registry is not None and docling_queue is not None
            else None
        )
        self._asr_outcomes = (
            dispatch_counter_for(registry, ASR_OUTCOMES, pool=ASR_POOL)
            if registry is not None and asr_queue is not None
            else None
        )
        self.lease_seconds = settings.extract_timeout_seconds * 2 + 10

    async def run(
        self,
        job: JobRecord,
        item: QueueItem,
        url: str,
        *,
        gate_refusals: Literal["fail", "raise"],
    ) -> Envelope[ExtractResult] | None:
        if job.kind == "site":
            # Its options are no extraction's (P9-S1): no request is built from them here.
            if self.site is None:
                return failed_extract(ErrorCode.INTERNAL_ERROR, _NO_SITE_MESSAGE, retryable=False)
            return await self.site.run(job, item, url, gate_refusals=gate_refusals)
        principal = job_principal(job)
        move = self._docling_move(job, item, url or None)
        if job.kind == "file":
            return await self._upload(job, item, principal, move)
        request = ExtractRequest(url=url, **job.options.model_dump())
        transcription = self._asr_move(job, item, url) if request.transcribe else None
        handoffs: dict[str, Any] = {}
        if move is not None:
            handoffs["ocr_handoff"] = move
        if transcription is not None:
            handoffs["asr_handoff"] = transcription
        # Without a handoff (no OCR, no ASR), the call of package 3.
        envelope = await self._pipeline.run(
            request, principal, gate_refusals=gate_refusals, **handoffs
        )
        if (move is not None and move.moved) or (transcription is not None and transcription.moved):
            return None
        if self._browser_queue is None or job.options.mode != "auto":
            return envelope
        unblock = unblock_for(principal, job.options.compliance, self._settings)
        if not (
            needs_browser(envelope, unblock=unblock)
            or transcript_panel_applies(envelope, self._settings, transcribe=request.transcribe)
        ):
            return envelope
        # Draft first: the browser worker may claim the item as soon as it is queued.
        await self._store.put_draft(job.id, item.index, envelope)
        await self._browser_queue.enqueue_items(job.id, item.client, [(item.index, url)])
        return None

    def _docling_move(self, job: JobRecord, item: QueueItem, url: str | None) -> DoclingMove | None:
        if self._docling_queue is None:
            return None
        return DoclingMove(
            self._store,
            self._docling_queue,
            self._guard,
            self._settings,
            job,
            item,
            url,
            outcomes=self._docling_outcomes,
        )

    def _asr_move(
        self, job: JobRecord, item: QueueItem, url: str | None, body: bytes = b""
    ) -> AsrMove | None:
        if self._asr_queue is None:
            return None
        return AsrMove(
            self._store,
            self._asr_queue,
            self._guard,
            self._settings,
            job,
            item,
            url,
            body=body,
            outcomes=self._asr_outcomes,
        )

    async def _upload(
        self, job: JobRecord, item: QueueItem, principal: Principal, move: DoclingMove | None
    ) -> Envelope[ExtractResult] | None:
        """An uploaded file's item, converted from its input."""
        # A rerun after a move to ``docling`` or ``asr`` reads that pool's meta: it parses as
        # ``UploadInputMeta`` only through the fields they share (file name, content type).
        stored = await self._store.get_input(job.id, item.index, UploadInputMeta)
        if stored is None:
            return failed_extract(ErrorCode.TIMEOUT, _INPUT_EXPIRED_MESSAGE, retryable=True)
        body, meta = stored
        transcription = self._asr_move(job, item, None, body)
        envelope = await self._pipeline.extract_upload(
            body,
            meta.file_name,
            form_of(job.options),
            principal,
            content_type=meta.content_type,
            ocr_handoff=move,
            asr_handoff=transcription,
            short_ok=meta.short_ok,
        )
        if (move is not None and move.moved) or (transcription is not None and transcription.moved):
            return None
        return envelope


class Worker:
    """Processes the items of one pool's queue with ``concurrency`` slot loops (default
    ``WORKER_CONCURRENCY``), each item through ``runner``.

    ``valkey`` is the client behind ``store`` and ``queue``; the caller closes it. ``clock``
    (epoch seconds), ``sleep`` and ``random`` (for the jitter) can be replaced in tests.
    ``give_back_on_cancel``: an item cancelled while it runs (the shutdown's end) goes back to
    the queue at once, instead of when its lease expires. ``pii_move`` moves a result that owes
    the PII step to the ``nlp`` pool (every pool but ``nlp``; None while the service has no
    PII); ``llm_move`` one that needs the LLM to the ``llm`` pool (every pool but ``llm``; None
    while the service has no LLM). ``post_pool`` runs the post stage's CPU work (ruling R21:
    the process's pool of ``POST_CONCURRENCY`` threads, which its owner shuts down); without one
    the worker makes its own.
    """

    def __init__(
        self,
        settings: Settings,
        valkey: Redis,
        runner: ItemRunner,
        store: JobStore,
        queue: FairQueue,
        registry: CollectorRegistry,
        *,
        concurrency: int | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random: Callable[[], float] = random.random,
        give_back_on_cancel: bool = False,
        pii_move: PiiMove | None = None,
        llm_move: LlmMove | None = None,
        post_pool: ConversionPool | None = None,
    ) -> None:
        self._settings = settings
        self._valkey = valkey
        self._runner = runner
        self._store = store
        self._queue = queue
        self._registry = registry
        self._metrics = _metrics_for(registry)
        self._clock = clock
        self._sleep = sleep
        self._random = random
        self._concurrency = concurrency if concurrency is not None else settings.worker_concurrency
        self._lease_seconds = runner.lease_seconds
        self._give_back_on_cancel = give_back_on_cancel
        self._pii_move = pii_move
        self._llm_move = llm_move
        self._post_pool = (
            post_pool
            if post_pool is not None
            else ConversionPool(settings.post_concurrency, name="post")
        )

    async def run(self, stop: asyncio.Event) -> None:
        """Run the slot loops until ``stop`` is set and the items in flight have finished."""
        async with asyncio.TaskGroup() as slots:
            for _ in range(self._concurrency):
                slots.create_task(self._slot(stop))

    async def process(self, item: QueueItem, *, exhausted: bool = False) -> None:
        """Handle one claimed item (``exhausted`` as the claim said). Valkey errors propagate.

        Its log lines carry the job id, the item's index and the trace id of the request that
        created the job (when it had one); so does the envelope the pipeline produces."""
        started = time.perf_counter()
        self._metrics.busy.inc()
        try:
            with structlog.contextvars.bound_contextvars(job_id=item.job_id, item=item.index):
                job = await self._store.get(item.job_id)
                trace = {"trace_id": job.trace_id} if job is not None and job.trace_id else {}
                with structlog.contextvars.bound_contextvars(**trace):
                    if exhausted:
                        outcome = await self._fail_exhausted(item, job)
                    else:
                        outcome = await self._extract(item, job)
                    _log.info(
                        "job_item_done", attempts=item.attempts, outcome=outcome or _SUPERSEDED
                    )
        finally:
            self._metrics.busy.dec()
            self._metrics.item_seconds.observe(time.perf_counter() - started)
        if outcome is not None:
            self._metrics.items.labels(outcome=outcome).inc()

    # --- the slot loop -----------------------------------------------------------------------

    async def _slot(self, stop: asyncio.Event) -> None:
        failures = 0  # consecutive rounds that Valkey failed
        scan_wait = _SCAN_BACKOFF_SECONDS  # next sleep after an incomplete scan
        while not stop.is_set():
            try:
                claim = await self._queue.claim(self._lease_seconds)
                if claim.item is None:
                    if claim.scan_incomplete:
                        delay, scan_wait = scan_wait, min(scan_wait * 2, _IDLE_SECONDS)
                    else:
                        delay, scan_wait = _IDLE_SECONDS, _SCAN_BACKOFF_SECONDS
                        if claim.retry_in is not None:
                            delay = min(claim.retry_in, _IDLE_SECONDS)
                    failures = 0
                    await self._pause(stop, delay * (0.5 + self._random()))
                    continue
                scan_wait = _SCAN_BACKOFF_SECONDS
                await self.process(claim.item, exhausted=claim.exhausted)
                failures = 0
            except (RedisError, OSError, ServiceError) as error:
                # A ServiceError here is the outbound gate's 503: its Valkey state is unavailable.
                failures += 1
                delay = min(2.0 ** (failures - 1), _MAX_VALKEY_BACKOFF_SECONDS)
                _log.warning(
                    "worker_valkey_unavailable", error_type=type(error).__name__, retry_in=delay
                )
                await self._pause(stop, delay)
            except Exception:
                _log.error("worker_slot_error", exc_info=True)
                await self._pause(stop, _ERROR_PAUSE_SECONDS)

    async def _pause(self, stop: asyncio.Event, seconds: float) -> None:
        """Sleep ``seconds``, or less when ``stop`` is set meanwhile."""
        if stop.is_set():
            return
        sleeping = asyncio.ensure_future(self._sleep(seconds))
        stopping = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait([sleeping, stopping], return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeping.cancel()
            stopping.cancel()

    # --- one item ----------------------------------------------------------------------------

    async def _extract(self, item: QueueItem, job: JobRecord | None) -> str | None:
        """The item's metric outcome; None when it is not to be counted (``_SUPERSEDED``).
        ``job`` is the item's job, None when it is gone."""
        url = await self._store.url(item.job_id, item.index) if job is not None else None
        if job is None or url is None:
            return await self._drop(item)
        if await self._store.has_result(item.job_id, item.index):
            # A copy of an item that has finished (it was rerun after a lost lease, or moved to
            # this pool twice): its result stands, and the target is not asked again.
            await self._drop_input(job, item)
            await self._queue.complete(item)
            _log.debug("job_item_skipped", reason="has_result")
            return None
        if job.state == "queued":
            try:
                await self._store.mark_running(item.job_id)
            except JobLost:
                return await self._drop(item)
        await self._record_claim(item)
        # At the limit, a refusal of the gate is the result: the pipeline answers it.
        at_limit = item.attempts >= self._settings.job_item_max_attempts
        gate_refusals: Literal["fail", "raise"] = "fail" if at_limit else "raise"
        try:
            envelope = await self._runner.run(job, item, url, gate_refusals=gate_refusals)
        except asyncio.CancelledError:
            if self._give_back_on_cancel:
                await self._give_back(item)
            raise
        except JobLost:
            return await self._drop(item)
        except (OutboundBusy, CircuitOpen) as refusal:
            if await self._queue.defer(item, self._clock() + refusal.retry_after):
                return _DEFERRED
            return None  # the lease was lost: the item is queued again or another worker's
        except ServiceError as error:
            if error.http_status >= 500:
                raise
            envelope = failed_extract(error.code, error.message, retryable=error.retryable)
        except (RedisError, OSError):
            raise  # Valkey (a builtin TimeoutError too): the slot loop backs off
        except Exception:
            _log.error("job_item_failed", exc_info=True)
            envelope = failed_extract(ErrorCode.INTERNAL_ERROR, _INTERNAL_MESSAGE, retryable=False)
        if envelope is None:
            return await self._moved(item)
        return await self._finish(item, job, envelope)

    async def _record_claim(self, item: QueueItem) -> None:
        """Record the claim with the job (D8, ruling P10-S11: its pool, claim number and time),
        before the item runs: one Valkey round trip. A Valkey error is logged and ignored, so
        the item still runs."""
        try:
            await self._store.record_claim(
                item.job_id, item.index, self._queue.pool, item.attempts + 1
            )
        except (RedisError, OSError) as error:  # a builtin TimeoutError too
            _log.warning("job_item_claim_not_recorded", error_type=type(error).__name__)

    async def _give_back(self, item: QueueItem) -> None:
        """Give a cancelled item back to its queue, claimable now. Shielded from the
        cancellation in progress; a Valkey error or a timeout leaves the lease to expire."""
        try:
            async with asyncio.timeout(_GIVE_BACK_SECONDS):
                given_back = await asyncio.shield(self._queue.defer(item, self._clock()))
        except (RedisError, OSError) as error:  # a builtin TimeoutError too
            _log.warning("job_item_give_back_failed", error_type=type(error).__name__)
            return
        if given_back:
            _log.info("job_item_given_back", attempts=item.attempts + 1)

    async def _moved(self, item: QueueItem) -> str | None:
        """Complete an item the runner queued on another pool, without a result. A worker whose
        lease was taken back does not count it: the current holder moves it as well."""
        return _ESCALATED if await self._queue.complete(item) else None

    async def _fail_exhausted(self, item: QueueItem, job: JobRecord | None) -> str | None:
        """Record the item as lost too often, then complete it; an item of pool ``docling`` or
        ``asr`` answers its draft instead, when it has one (``draft_without_ocr``,
        ``draft_without_asr``), one of pool ``nlp`` ``pii_failed``, never its draft, one of pool
        ``llm`` its draft without the LLM work (``finish_post``, ``llm_failed``), when it has
        one. A Valkey
        error propagates and leaves the item leased, so it is offered again after the lease
        instead of vanishing without a result."""
        _log.warning("job_item_exhausted", attempts=item.attempts)
        if job is None:
            return await self._drop(item)
        await self._record_claim(item)
        site = self._runner.site if isinstance(self._runner, ExtractRunner) else None
        if job.kind == "site" and site is not None:
            # A crash after its extend and before its queueing: its children still run (P9-S12).
            await site.requeue_children(job, item)
        envelope = failed_extract(ErrorCode.INTERNAL_ERROR, _LOST_MESSAGE, retryable=True)
        if self._queue.pool == DOCLING_POOL:
            # A document whose OCR never finished keeps its text layer, unstored.
            draft = await self._store.draft(item.job_id, item.index)
            envelope = draft_without_ocr(draft) or envelope
        elif self._queue.pool == ASR_POOL:
            # A video whose transcription never finished keeps its metadata, unstored.
            draft = await self._store.draft(item.job_id, item.index)
            envelope = draft_without_asr(draft) or envelope
        elif self._queue.pool == PII_POOL:
            # The draft is the result before its personal data was handled: never answered.
            envelope = failed_extract(ErrorCode.PII_FAILED, FAIL_CLOSED_MESSAGE, retryable=True)
        elif self._queue.pool == LLM_POOL:
            # The draft is the final content: it is answered without the LLM work.
            try:
                draft = await self._store.draft(item.job_id, item.index)
            except ValidationError as error:
                # The type only: pydantic's message quotes the stored text.
                _log.error("llm_draft_invalid", error=type(error).__name__)
                draft = None
            if draft is not None:
                structured, chunking = job.options.structured, job.options.chunking
                envelope = await self._post_pool.run(
                    partial(
                        finish_post,
                        settings=self._settings,
                        reason="failed",
                        describe=job.options.describe_images,
                        registry=self._registry,
                    ),
                    draft,
                    structured,
                    chunking,
                )
        outcome = await self._finish(item, job, envelope)
        # The item never runs again: its caller secrets (a browser job's, B6; an ASR key, M11)
        # are done with. A batch's ASR key serves its other items (a batch has no browser
        # secrets); it goes with the batch's last result (``_finish``).
        if job.kind != "batch":
            await self._store.drop_secrets(item.job_id)
        return outcome

    async def _finish(
        self, item: QueueItem, job: JobRecord, envelope: Envelope[ExtractResult]
    ) -> str | None:
        """Store the item's result, then complete it. The write that completes the job observes
        its duration; a repeat write (the item had its result already) is not counted.

        A result that owes the PII step (the job's ``pii``, ruling N11) moves to the ``nlp``
        pool instead and is completed here without one. Whatever is written passes the guard
        (``pii.fields.fail_closed``, N12): a result that owes the step and did not move (the
        memory guard, a worker without the move) is ``pii_failed`` with its text withheld. A job
        that expired before a move (``JobLost``) drops the item, as before a write. Before the
        guard, a result that is not done gets the post stage of the job's ``structured`` and
        ``chunking`` (S13, ``structured.post.post_at_finish``): one that needs the LLM moves to
        the ``llm`` pool (every pool but ``llm``) and is completed here without a result; one
        whose move declines (the memory guard) or that has no move is answered without the LLM
        work. The writer of the result drops the caller's LLM key (R11): a sync job's at once
        (``_drop_input``), a batch's with its last result."""
        pii = job.options.pii
        if (
            pii is not None
            and self._queue.pool != PII_POOL
            and self._pii_move is not None
            and needs_pii(envelope, pii)
        ):
            try:
                moved = await self._pii_move(job, item, envelope)
            except JobLost:
                return await self._drop(item)
            if moved:
                return await self._moved_on(job, item, envelope)
            envelope = withheld(envelope, pii, message=PII_BUSY_MESSAGE)
        try:
            finished = await post_at_finish(
                envelope,
                job.options.structured,
                job.options.chunking,
                pii,
                settings=self._settings,
                threads=self._post_pool,
                hop=self._llm_hop(job, item),
                describe=job.options.describe_images,
                registry=self._registry,
            )
        except JobLost:
            return await self._drop(item)
        if finished is None:
            return await self._moved_on(job, item, envelope)
        envelope = fail_closed(finished, pii)
        try:
            write = await self._store.write_result(item.job_id, item.index, envelope)
        except JobLost:
            return await self._drop(item)
        if write == "last":
            seconds = max(self._clock() - job.created_at.timestamp(), 0.0)
            self._metrics.job_duration.labels(kind=job.kind).observe(seconds)
            _log.info("job_completed", kind=job.kind, seconds=round(seconds, 3))
            if job.kind == "batch":
                # Its caller's keys (M11, R11) served all its items; whichever pool wrote last.
                await self._store.drop_asr_key(item.job_id)
                await self._store.drop_llm_key(item.job_id)
        await self._drop_input(job, item)
        await self._queue.complete(item)
        return None if write == "repeat" else _outcome(envelope)

    def _llm_hop(self, job: JobRecord, item: QueueItem) -> LlmHop | None:
        """The move of the item's result to the ``llm`` pool, for ``post_at_finish``; None on
        pool ``llm`` and without the move."""
        move = self._llm_move
        if move is None or self._queue.pool == LLM_POOL:
            return None

        async def hop(
            envelope: Envelope[ExtractResult],
        ) -> tuple[Envelope[ExtractResult] | None, Literal["busy"] | None]:
            if await move(job, item, envelope):
                return None, None
            return envelope, "busy"  # the memory guard: nothing was stored

        return hop

    async def _moved_on(
        self, job: JobRecord, item: QueueItem, envelope: Envelope[ExtractResult]
    ) -> str | None:
        """Complete an item that moved to a last hop, ``nlp`` or ``llm`` (``_moved``). Once that
        completion holds, the item never runs on this pool again: what this pool kept for a
        rerun goes at once (ruling R13), as ``_drop_input`` would delete it with a result, but
        for the draft, which is the last hop's now (``nlp`` moving on to ``llm`` overwrote its
        own), and the caller's LLM key, which the llm worker needs. Otherwise a last hop that is
        down would keep the inputs of ``docling`` and ``asr`` filling their budget and
        Valkey. An image upload that moved with its description owed keeps its input too (ruling
        V-S7; ``envelope`` is the result that moved, ``vision.describe.needs_vision``, which is
        false for an upload the pipeline skipped as one the model cannot take, R-V3): its bytes
        are what the llm worker describes, and it deletes them with the result (``_drop_input``;
        such a job has no ``pii``, so no nlp hop in between). Any other upload's input goes, also
        with ``describe_images`` (a PDF that moved for ``structured``: nothing to describe)."""
        outcome = await self._moved(item)
        if outcome is None:
            return None  # the lease was lost: the current holder may need them
        pool = self._queue.pool
        pooled = pool in (DOCLING_POOL, ASR_POOL)
        described = (
            job.kind == "file"
            and envelope.data is not None
            and needs_vision(envelope.data, job.options.describe_images)
        )
        if (job.kind == "file" or pooled or pool == PII_POOL) and not described:
            await self._store.delete_input(item.job_id, item.index)
        if pool in (ASR_POOL, PII_POOL) and job.kind != "batch":
            await self._store.drop_secrets(item.job_id)
        return outcome

    async def _drop_input(self, job: JobRecord, item: QueueItem) -> None:
        """Delete the item's bytes once it has its result: an upload's, or those of an item
        that was queued for OCR (pool ``docling``) or a transcription (pool ``asr``), with its
        draft; on pool ``asr`` also the caller's key of a sync request's job (a batch's serves
        its other items and goes with its last result, ``_finish``). Pools ``nlp`` and ``llm``
        are last hops: they delete the draft, any input of the item that is still there (the
        pool before deletes it once its move is complete, ``_moved_on``) and, but for a batch,
        the job's caller secrets. Every pool deletes the caller's LLM key of a sync request's
        job (R11: whoever writes the result; a batch's goes with its last result, ``_finish``).
        Only after the result is written: an item that runs again (a failed write, a lost lease)
        needs them."""
        pooled = self._queue.pool in (DOCLING_POOL, ASR_POOL)
        last_hop = self._queue.pool in (PII_POOL, LLM_POOL)
        if pooled or last_hop:
            await self._store.drop_draft(item.job_id, item.index)
        if job.kind == "file" or pooled or last_hop:
            await self._store.delete_input(item.job_id, item.index)
        if (self._queue.pool == ASR_POOL or last_hop) and job.kind != "batch":
            await self._store.drop_secrets(item.job_id)
        if job.kind != "batch":
            await self._store.drop_llm_key(item.job_id)

    async def _drop(self, item: QueueItem) -> str:
        """Complete an item whose job is gone (expired or deleted): there is nowhere to store its
        result. An item of pool ``docling`` or ``asr`` deletes its input first: the job's
        deletion knows only item 0's (``JobStore.delete``), and a batch item's bytes would
        otherwise outlive the job until their TTL, holding their share of the input budget. A
        Valkey error leaves the item leased, to be dropped again."""
        if self._queue.pool in (DOCLING_POOL, ASR_POOL):
            await self._store.delete_input(item.job_id, item.index)
        await self._queue.complete(item)
        _log.warning("job_item_dropped", reason="job_gone")
        return _LOST


# --- the process -------------------------------------------------------------------------------


async def _serve(
    worker: Worker,
    internal: _Server,
    readiness: Readiness,
    *,
    graceful_seconds: float,
    guard: MemoryGuard | None = None,
    startup: Callable[[], Coroutine[Any, Any, None]] | None = None,
    loop_lag: LoopLag | None = None,
) -> None:
    """Serve the internal app, run the start-up (by default: load the language model), then run
    the worker until a signal and shut down in the order the module describes. ``/ready``
    answers 200 only once the start-up has finished; a failed start-up raises. ``guard``'s task
    runs from the start to the end of this call, so it is stopped before the caller closes the
    Valkey client; so does ``loop_lag``'s sampling."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in _HANDLED_SIGNALS:
        loop.add_signal_handler(sig, stop.set)
    if guard is not None:
        guard.start()
    if loop_lag is not None:
        loop_lag.start()
    internal_task = asyncio.create_task(internal.serve())
    stopping = asyncio.create_task(stop.wait())
    starting = asyncio.create_task(startup() if startup is not None else _load_language_model())
    work: asyncio.Task[None] | None = None
    try:
        await asyncio.wait([starting, stopping, internal_task], return_when=asyncio.FIRST_COMPLETED)
        if starting.done() and not stop.is_set() and not internal_task.done():
            starting.result()  # raises a failure of the start-up
            work = asyncio.create_task(worker.run(stop))
            readiness.mark_listening()
            _log.info("worker_started")
            await asyncio.wait([work, stopping, internal_task], return_when=asyncio.FIRST_COMPLETED)
        readiness.mark_draining()
        stop.set()
        if work is not None:
            _log.info("shutdown_requested", graceful_seconds=graceful_seconds)
            done, _ = await asyncio.wait([work], timeout=graceful_seconds)
            if not done:
                _log.warning("worker_shutdown_timeout", graceful_seconds=graceful_seconds)
                work.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await work
            else:
                work.result()  # raises a failure of the loops
        internal.should_exit = True
        await internal_task
    finally:
        stopping.cancel()
        starting.cancel()  # a signal during the start-up: the model's thread still ends by itself
        internal.should_exit = True
        for sig in _HANDLED_SIGNALS:
            loop.remove_signal_handler(sig)
        try:
            # A start-up coroutine (the browser worker's) unwinds before its caller cleans up;
            # one stuck in a driver call does not hold the shutdown for long.
            await asyncio.wait([starting], timeout=_START_UNWIND_SECONDS)
            if not starting.done():
                _log.warning("worker_start_not_unwound", waited_seconds=_START_UNWIND_SECONDS)
        finally:
            if loop_lag is not None:
                await loop_lag.stop()
            if guard is not None:
                await guard.stop()


async def _load_language_model() -> None:
    await asyncio.to_thread(load_language_model)


@dataclass(frozen=True)
class WorkerParts:
    """What one worker process runs on: the worker, its memory guard, its event-loop lag
    sampling (``text_extraction_event_loop_lag_seconds``), and the fetcher, conversion pool,
    document pool, selector children, post pool, sitemap pool (one thread for the site crawl's
    sitemap parses, so they never hold the pages' conversion threads) and media fetcher (None
    without ``MEDIA_ENABLED``) to close at the end."""

    worker: Worker
    guard: MemoryGuard
    loop_lag: LoopLag
    fetcher: Fetcher
    pool: ConversionPool
    document_pool: DocumentPool
    selectors: SelectorRunner
    post_pool: ConversionPool
    sitemap_pool: ConversionPool
    media: MediaFetcher | None = None


async def start_up(parts: WorkerParts) -> None:
    """The start-up of a worker process, before ``/ready``: the language model, and meanwhile
    the document pool's and the selector pool's children and the media fetcher."""
    media = [parts.media.start()] if parts.media is not None else []
    await asyncio.gather(
        _load_language_model(), parts.document_pool.start(), parts.selectors.start(), *media
    )


def build_worker(settings: Settings, valkey: Redis, registry: CollectorRegistry) -> WorkerParts:
    """The worker of a worker process. Its result cache skips writes while the memory guard
    reports Valkey as nearly full (the job store's result writes are not guarded, so accepted
    jobs complete); the guard's gauges are exported from ``registry``."""
    guard = MemoryGuard(valkey, settings.valkey_memory_guard_ratio, registry)
    gate, fetcher = create_outbound(settings, valkey)
    pool = ConversionPool(settings.convert_concurrency)
    document_pool = create_document_pool(settings)
    selectors = create_selector_runner(settings)
    post_pool = ConversionPool(settings.post_concurrency, name="post")
    # One sitemap parse at a time per pod (seconds of CPU each): the pages keep their threads.
    sitemap_pool = ConversionPool(1, name="sitemap")
    media = create_media_fetcher(settings, registry) if settings.media_enabled else None
    pipeline = create_pipeline(
        settings,
        valkey,
        fetcher,
        registry,
        pool,
        gate=gate,
        memory_guard=guard,
        document_pool=document_pool,
        media_fetcher=media,
        selector_runner=selectors,
    )
    store = JobStore(valkey, settings)
    queue = FairQueue(valkey, settings)
    browser_queue = FairQueue(valkey, settings, "browser") if settings.browser_enabled else None
    ocr_queue = docling_queue(valkey, settings) if ocr_available(settings) else None
    transcription_queue = asr_queue(valkey, settings) if asr_available(settings) else None
    worker = Worker(
        settings,
        valkey,
        ExtractRunner(
            pipeline,
            store,
            settings,
            browser_queue,
            docling_queue=ocr_queue,
            asr_queue=transcription_queue,
            guard=guard,
            registry=registry,
            site=SiteRunner(
                pipeline,
                store,
                SiteStore(valkey, store, settings),
                queue,
                settings,
                sitemap_pool,
                guard=guard,
                registry=registry,
            ),
        ),
        store,
        queue,
        registry,
        pii_move=create_pii_move(settings, valkey, store, guard, registry),
        llm_move=create_llm_move(settings, valkey, store, guard, registry),
        post_pool=post_pool,
    )
    return WorkerParts(
        worker=worker,
        guard=guard,
        loop_lag=LoopLag(registry),
        fetcher=fetcher,
        pool=pool,
        document_pool=document_pool,
        selectors=selectors,
        post_pool=post_pool,
        sitemap_pool=sitemap_pool,
        media=media,
    )


async def _run(settings: Settings) -> None:
    valkey = create_valkey(settings.valkey_url)
    registry = REGISTRY
    parts = build_worker(settings, valkey, registry)
    graceful = extraction_graceful_seconds(settings)
    readiness = Readiness()
    internal = _server(
        create_internal_app(settings, valkey, registry, readiness=readiness),
        settings.internal_host,
        settings.internal_port,
        graceful,
    )
    try:
        await _serve(
            parts.worker,
            internal,
            readiness,
            graceful_seconds=graceful,
            guard=parts.guard,
            startup=lambda: start_up(parts),
            loop_lag=parts.loop_lag,
        )
    finally:
        await parts.fetcher.aclose()
        parts.pool.shutdown()
        await parts.document_pool.shutdown()
        await parts.selectors.shutdown()
        if parts.media is not None:
            await parts.media.stop()
        parts.post_pool.shutdown()
        parts.sitemap_pool.shutdown()
        await valkey.aclose()
        _log.info("valkey_closed")


def main() -> None:
    asyncio.run(_run(configured_settings()))
