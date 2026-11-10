"""The browser worker (entry point ``text-extraction-browser-worker``): runs the items of the
``browser`` pool in a real browser and stores their results in the job.

It is the extract worker's ``Worker`` (``worker.py``: slot loops, leases, deferrals, retries,
exhausted items, result writes, metrics, ``/ready`` and the shutdown) with another runner and
another pipeline; a render of a job with ``pii`` moves on to the ``nlp`` pool there
(``pii_dispatch.PiiMove``). This is the only module outside ``browser*.py`` that imports the browser
modules, so the API and the extract worker never load Playwright or Patchright.

**The runner** (``BrowserRunner``) leases an item for ``2 * max(BROWSER_TIMEOUT_SECONDS,
BROWSER_UNBLOCK_TIMEOUT_SECONDS) + 10`` seconds. For each item it:

1. reads the job's caller secrets (actions, cookies, headers; B6) and the item's draft (the
   fast result of an ``auto`` item the extract worker moved here; B5). A job created with
   secrets whose secrets have expired (the item waited longer than their TTL) is not rendered:
   the item fails with ``timeout``, retryable;
2. rebuilds the request from the job's options and the secrets. A ``screenshot`` job renders in
   ``browser`` mode with ``screenshot`` and without metadata. The job's owner is the principal,
   so an admin owner's ``evaluate`` actions run, and unblock mode applies only to an admin's
   ``compliance: ignore`` job while ``BROWSER_UNBLOCK_ENABLED`` (B14, R9), unless the job's
   ``unblock: false`` switched it off (package 22). An ``auto`` request
   with secrets always renders (R8), one with a draft is compared with it (a YouTube video's
   details whose subtitles could not be read try its transcript view instead, P13-S2), one
   without either runs the fast step first;
2b. takes a turn of the unblock browser before the pipeline runs, when the item will use it
   (unblock mode applies, not ``mode: fast``, not a transcript-view draft; package 22):
   ``BROWSER_UNBLOCK_CONCURRENCY`` turns per process (at most ``BROWSER_CONCURRENCY``), each a
   headful Chrome. The wait comes before the render deadline starts and before the host's turn,
   and lasts at most ``BROWSER_QUEUE_WAIT_SECONDS`` for an item a caller waits for; a batch
   item waits up to what its lease leaves beside the render deadline (60 s by default:
   ``unblock_turn_seconds``). When no turn comes, the item is deferred
   (``UnblockBusy``, a short ``OutboundBusy``: the worker gives it back with its secrets and its
   draft, nothing is charged to the host); at ``JOB_ITEM_MAX_ATTEMPTS`` it answers instead, as an
   expired queue wait does: its draft with ``browser_unavailable``, without one
   ``rate_limited``, retryable. The turn is given back when the pipeline ends, however it ends;
3. runs the pipeline with an ``ArtifactSink`` bound to the job: the screenshot is stored as
   ``job:{<id>}:artifact:<name>`` (``screenshot.png``, ``screenshot.jpg`` or
   ``screenshot.webp``) for ``ARTIFACT_TTL_SECONDS`` and served at
   ``/v1/jobs/<id>/artifacts/<name>``. While Valkey's memory guard is high, or when the
   write fails, nothing is stored and the item fails ``internal_error``, retryable (B7).
   With ``transcribe: true`` and the service's ASR, the one media of a rendered thin page
   (M22) moves the item to the ``asr`` pool (``asr_dispatch.AsrMove``: the media's input and
   the page's answer as the item's draft); the item is then completed here without a result.
   Only a page's one media moves (R7): an ``auto`` item's request URL that is itself media
   answers as before, and a sync request's job counts against ``ASR_MAX_OPEN_PER_CLIENT``;
4. deletes the secrets and the item's draft once the item has its outcome, also when the run
   failed (but not the draft of an item that moved to the ``asr`` pool). The caller's ASR key
   stays for an item that moved and for a batch, whose other items need it (``JobStore.
   drop_browser_secrets``). An item that is given
   back to run again (a gate refusal it is deferred for, a Valkey error, a cancelled shutdown)
   keeps them for that run; the secrets expire on their own after their short TTL, a draft
   with the job.

**The process** serves only the internal port, like the extract worker. Before ``/ready``
answers 200 it loads the language model (and meanwhile starts the document pool's children,
for documents an ``auto`` item's fast step fetches, and with ``MEDIA_ENABLED`` the media
fetcher's children and egress proxy, for platform videos), starts the egress proxy, then Xvfb (only
for unblock mode with a headful Chrome: Patchright reads ``DISPLAY`` when its driver starts)
and the browser pools: Playwright's Chromium, and with ``BROWSER_UNBLOCK_ENABLED``
Patchright's Chrome. Both pools share one semaphore, so ``BROWSER_CONCURRENCY`` pages run at
once in all; the worker runs as many slot loops (``WORKER_CONCURRENCY`` does not apply). Unblock
items also take a turn of their own first (step 2b), so standard items keep the slots an
unblock item waits beside. The
pipeline's deadline is ``BROWSER_TIMEOUT_SECONDS`` (an unblock render has
``BROWSER_UNBLOCK_TIMEOUT_SECONDS``). After the loops have stopped, the pools, Xvfb and the
proxy stop, in that order; the document pool and the media fetcher stop with the process. A
browser that does not start ends the process before it is ready.

``--check`` launches each enabled driver once, stops it and exits 0 (1 when one fails): the
image build uses it to prove that the browsers in the image start.

Logs carry ids, hosts and codes only, never a URL, header, cookie, action or script.
"""

import argparse
import asyncio
import math
import sys
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

import structlog
from prometheus_client import REGISTRY, CollectorRegistry
from redis.asyncio import Redis
from redis.exceptions import RedisError

from text_extraction.asr_dispatch import ASR_OUTCOMES, ASR_POOL, AsrMove, asr_queue
from text_extraction.browser import BrowserPool, ChromiumRenderer, PlaywrightLauncher
from text_extraction.browser_options import ArtifactRef, BrowserSecrets
from text_extraction.browser_proxy import EgressProxy
from text_extraction.browser_unblock import PatchrightLauncher, UnblockRenderer, Xvfb
from text_extraction.clearance import ValkeyClearance
from text_extraction.conversion_pool import ConversionPool
from text_extraction.documents.pool import DocumentPool
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.escalation import (
    BROWSER_UNAVAILABLE_WARNING,
    needs_transcript_panel,
    unblock_for,
)
from text_extraction.extract_models import ExtractRequest, ExtractResult, failed_extract
from text_extraction.fetch import Fetcher
from text_extraction.internal import Readiness, create_internal_app
from text_extraction.jobs import (
    JobLost,
    JobRecord,
    JobStore,
    browser_lease_seconds,
    job_principal,
)
from text_extraction.llm_dispatch import create_llm_move
from text_extraction.media.pool import MediaFetcher, create_media_fetcher
from text_extraction.memory_guard import MemoryGuard
from text_extraction.outbound import CircuitOpen, OutboundBusy
from text_extraction.pii_dispatch import create_pii_move
from text_extraction.pipeline import Pipeline, create_outbound, create_pipeline
from text_extraction.pipeline_documents import create_document_pool
from text_extraction.pipeline_media import asr_available
from text_extraction.queue_metrics import dispatch_counter_for
from text_extraction.render import ArtifactUnavailable
from text_extraction.result_cache import MemoryPressure
from text_extraction.scheduler import FairQueue, QueueItem
from text_extraction.screenshot_image import EncodedScreenshot
from text_extraction.server import (
    _Server,
    _server,
    configured_settings,
    extraction_graceful_seconds,
)
from text_extraction.settings import Settings
from text_extraction.structured.selector_pool import SelectorRunner, create_selector_runner
from text_extraction.valkey import create_valkey
from text_extraction.worker import Worker, _load_language_model, _serve

_log = structlog.get_logger(__name__)

POOL = "browser"
# What a shutdown grants an item past its render deadline: closing the page, storing the result.
_SHUTDOWN_MARGIN_SECONDS = 10
SECRETS_EXPIRED_MESSAGE = (
    "The request's browser options expired before a browser was free; send the request again."
)
# Failures after which an item is given back to run again; it still needs its secrets then.
_GIVEN_BACK = (OutboundBusy, CircuitOpen, RedisError, OSError)
# An item that got no turn of the unblock browser is claimable again after this many seconds.
UNBLOCK_RETRY_SECONDS = 1.0
UNBLOCK_BUSY_MESSAGE = "The unblock browser was busy; send the request again."


class UnblockBusy(OutboundBusy):
    """No turn of the unblock browser came within ``BROWSER_QUEUE_WAIT_SECONDS``: the worker
    defers the item as for a busy host, but nothing was asked of the host (package 22)."""


def unblock_concurrency(settings: Settings) -> int:
    """Unblock items at once: ``BROWSER_UNBLOCK_CONCURRENCY``, at most ``BROWSER_CONCURRENCY``."""
    return min(settings.browser_unblock_concurrency, settings.browser_concurrency)


def unblock_turn_seconds(settings: Settings, *, batch: bool = False) -> float:
    """How long an item waits for a turn of the unblock browser: what its lease leaves beside
    the render deadline and the shutdown margin (60 s by default), and for an item a caller
    waits for (not a ``batch`` item) at most ``BROWSER_QUEUE_WAIT_SECONDS``, since the API's
    wait for it is bounded by that. A batch item waits the longer time: each deferral costs
    it an attempt, and renders of 20 to 60 s would exhaust ``JOB_ITEM_MAX_ATTEMPTS`` of a long
    admin batch of unblock items at the short wait (final review of package 22)."""
    headroom = (
        browser_lease_seconds(settings)
        - settings.browser_unblock_timeout_seconds
        - _SHUTDOWN_MARGIN_SECONDS
    )
    wait = headroom if batch else min(settings.browser_queue_wait_seconds, headroom)
    return max(0.0, wait)


def unblock_unavailable(url: str, draft: Envelope[ExtractResult] | None) -> Envelope[ExtractResult]:
    """The answer of an item at its last attempt that got no turn of the unblock browser: its
    draft with ``browser_unavailable`` (and without the fingerprint, never compared), as an
    expired queue wait answers; without one ``rate_limited``, retryable, never ``timeout``,
    which would count against the host."""
    _log.info("unblock_turn_unavailable", host=urlsplit(url).hostname)
    if draft is not None and draft.data is not None:
        data = draft.data.model_copy(
            update={
                "fingerprint": None,
                "warnings": [*draft.data.warnings, BROWSER_UNAVAILABLE_WARNING],
            }
        )
        return draft.model_copy(update={"data": data})
    return failed_extract(ErrorCode.RATE_LIMITED, UNBLOCK_BUSY_MESSAGE, retryable=True)


def artifact_url(job_id: str, name: str) -> str:
    """Where the API serves a file of a job."""
    return f"/v1/jobs/{job_id}/artifacts/{name}"


class JobArtifacts:
    """The ``ArtifactSink`` of one job's item: the screenshot goes to the job store (B7)."""

    def __init__(self, store: JobStore, job_id: str, guard: MemoryPressure | None) -> None:
        self._store = store
        self._job_id = job_id
        self._guard = guard

    async def put_screenshot(self, image: EncodedScreenshot) -> ArtifactRef:
        if self._guard is not None and self._guard.high:
            raise ArtifactUnavailable("Valkey's memory is nearly full")
        try:
            expires_at = await self._store.put_artifact(self._job_id, image.name, image.data)
        except (RedisError, OSError, JobLost) as error:
            _log.warning("artifact_not_stored", error_type=type(error).__name__)
            raise ArtifactUnavailable("The screenshot could not be stored") from error
        return ArtifactRef(
            name=image.name,
            url=artifact_url(self._job_id, image.name),
            content_type=image.content_type,
            bytes=len(image.data),
            width=image.width,
            height=image.height,
            expires_at=expires_at,
        )


class BrowserRunner:
    """The ``browser`` pool's ``ItemRunner``; see the module docstring. ``guard`` is the memory
    guard that keeps screenshots out of a nearly full Valkey (and media out of the ``asr``
    pool). ``asr_queue`` is the ``asr`` pool's queue, None while the service has no ASR: the one
    media of a rendered thin page moves there (M22); ``registry`` gets
    ``text_extraction_asr_dispatch_total`` for the refused moves."""

    def __init__(
        self,
        pipeline: Pipeline,
        store: JobStore,
        settings: Settings,
        *,
        guard: MemoryPressure | None = None,
        asr_queue: FairQueue | None = None,
        registry: CollectorRegistry | None = None,
        unblock_turns: asyncio.Semaphore | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._store = store
        self._settings = settings
        self._guard = guard
        # The unblock browser's turns (package 22); None without unblock mode.
        self._unblock_turns = unblock_turns
        self._asr_queue = asr_queue
        self._asr_outcomes = (
            dispatch_counter_for(registry, ASR_OUTCOMES, pool=ASR_POOL)
            if registry is not None and asr_queue is not None
            else None
        )
        self.lease_seconds = browser_lease_seconds(settings)

    async def run(
        self,
        job: JobRecord,
        item: QueueItem,
        url: str,
        *,
        gate_refusals: Literal["fail", "raise"],
    ) -> Envelope[ExtractResult] | None:
        finished = False  # the item gets its outcome now: its secrets are done with
        moved = False  # to the ``asr`` pool, which reads the draft it stored
        try:
            secrets = await self._store.secrets(job.id)
            if job.has_secrets and secrets.empty():
                # Their TTL ran out while the item waited: rendering without the caller's
                # headers, cookies or actions would answer another request than the one sent.
                finished = True
                _log.info("job_secrets_expired", job_id=job.id)
                return failed_extract(ErrorCode.TIMEOUT, SECRETS_EXPIRED_MESSAGE, retryable=True)
            draft = await self._store.draft(job.id, item.index)
            request = _request(job, url, secrets)
            turns = self._unblock_turns if self._unblocks(job, request, draft) else None
            if turns is not None and not await self._turn(turns, batch=job.kind == "batch"):
                if gate_refusals == "raise":
                    raise UnblockBusy(UNBLOCK_RETRY_SECONDS)  # given back, secrets kept
                finished = True
                return unblock_unavailable(url, draft)
            try:  # from here the turn is given back however the run ends
                transcription = self._asr_move(job, item, url) if request.transcribe else None
                envelope = await self._pipeline.run(
                    request,
                    job_principal(job),
                    gate_refusals=gate_refusals,
                    artifacts=JobArtifacts(self._store, job.id, self._guard),
                    draft=draft,
                    asr_handoff=transcription,
                    asr_pages_only=True,  # a request URL's own media is not moved (R7)
                )
            finally:
                if turns is not None:
                    turns.release()
            finished = True
            if transcription is not None and transcription.moved:
                moved = True
                return None  # the ASR worker writes the item's result
            return envelope
        except _GIVEN_BACK:
            raise
        except ServiceError as error:
            finished = error.http_status < 500  # a 503 of the gate's Valkey: given back
            raise
        except Exception:
            finished = True  # the worker stores it as the item's failure
            raise
        finally:
            if finished:
                # The ASR key stays for an item that moved (the ASR worker reads it) and for a
                # batch (its other items need it; it goes with the batch's last result).
                await self._drop_secrets(job.id, keep_asr_key=moved or job.kind == "batch")
                if not moved:
                    await self._drop_draft(job.id, item.index)

    def _unblocks(
        self, job: JobRecord, request: ExtractRequest, draft: Envelope[ExtractResult] | None
    ) -> bool:
        """Whether the item will use the unblock browser, as the pipeline decides it: unblock
        mode applies to the job's owner, compliance and ``unblock``, the request may render
        (not ``fast``), and it is no transcript-view draft (always the standard browser)."""
        if self._unblock_turns is None or request.mode == "fast":
            return False
        if request.mode == "auto" and draft is not None and needs_transcript_panel(draft):
            return False
        return unblock_for(
            job_principal(job), job.options.compliance, self._settings, requested=request.unblock
        )

    async def _turn(self, turns: asyncio.Semaphore, *, batch: bool) -> bool:
        """Take a turn of the unblock browser within `unblock_turn_seconds`; False when none
        came. A cancelled or timed-out wait holds no turn (``Semaphore.acquire``)."""
        try:
            async with asyncio.timeout(unblock_turn_seconds(self._settings, batch=batch)):
                await turns.acquire()
        except TimeoutError:
            return False
        return True

    def _asr_move(self, job: JobRecord, item: QueueItem, url: str) -> AsrMove | None:
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
            outcomes=self._asr_outcomes,
            open_limit=job.kind != "batch",  # a sync request's render, as AsrDispatcher counts
        )

    async def _drop_secrets(self, job_id: str, *, keep_asr_key: bool) -> None:
        """Delete the secrets (with ``keep_asr_key`` the browser's only); a failure leaves them
        to their TTL rather than the result."""
        try:
            if keep_asr_key:
                await self._store.drop_browser_secrets(job_id)
            else:
                await self._store.drop_secrets(job_id)
        except (RedisError, OSError) as error:
            _log.warning("job_secrets_not_dropped", error_type=type(error).__name__)

    async def _drop_draft(self, job_id: str, index: int) -> None:
        """Delete the item's draft; a failure leaves it to the job's TTL rather than the
        result."""
        try:
            await self._store.drop_draft(job_id, index)
        except (RedisError, OSError) as error:
            _log.warning("job_draft_not_dropped", error_type=type(error).__name__)


def _request(job: JobRecord, url: str, secrets: BrowserSecrets) -> ExtractRequest:
    fields: dict[str, Any] = job.options.model_dump()
    if job.kind == "screenshot":
        fields.update(mode="browser", screenshot=True, metadata=False)
    return ExtractRequest(
        url=url,
        **fields,
        actions=secrets.actions,
        cookies=secrets.cookies,
        headers=secrets.headers,
    )


# --- the browsers -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Browsers:
    """The egress proxy, the virtual display (None when not needed), the pools (standard first,
    then unblock) and their renderers. ``start`` and ``stop`` keep the order of the module
    docstring; ``stop`` stops what it can, also after a failed or cut ``start``."""

    proxy: EgressProxy
    xvfb: Xvfb | None
    pools: tuple[BrowserPool, ...]
    renderer: ChromiumRenderer
    unblock_renderer: UnblockRenderer | None

    async def start(self) -> None:
        await self.proxy.start()
        if self.xvfb is not None:
            await self.xvfb.start()  # before Patchright's driver starts: it reads DISPLAY then
        for pool in self.pools:
            await pool.start()
        _log.info("browsers_started", drivers=[pool.driver for pool in self.pools])

    async def stop(self) -> None:
        for pool in reversed(self.pools):
            await _quietly(pool.stop(), "browser_pool")
        if self.xvfb is not None:
            await _quietly(self.xvfb.stop(), "xvfb")
        await _quietly(self.proxy.stop(), "egress_proxy")


async def _quietly(stopping: Awaitable[None], part: str) -> None:
    try:
        await stopping
    except Exception as error:
        _log.warning("browser_stop_failed", part=part, error_type=type(error).__name__)


def create_browsers(settings: Settings, registry: CollectorRegistry) -> Browsers:
    """The browser parts for ``settings``, not started yet."""
    proxy = EgressProxy(settings, registry=registry)
    slots = asyncio.Semaphore(settings.browser_concurrency)  # shared by both drivers
    standard = BrowserPool(
        settings, proxy, registry, launcher=PlaywrightLauncher(settings), slots=slots
    )
    if not settings.browser_unblock_enabled:
        return Browsers(proxy, None, (standard,), ChromiumRenderer(standard, settings), None)
    unblock = BrowserPool(
        settings, proxy, registry, launcher=PatchrightLauncher(settings), slots=slots
    )
    return Browsers(
        proxy,
        None if settings.browser_unblock_headless else Xvfb(),
        (standard, unblock),
        ChromiumRenderer(standard, settings),
        UnblockRenderer(unblock, settings),
    )


# --- the process ------------------------------------------------------------------------------


def browser_deadline_seconds(settings: Settings) -> float:
    """The longest deadline of this worker's pipeline: ``BROWSER_TIMEOUT_SECONDS``, or the
    unblock timeout where unblock mode is on. A media probe of an ``auto`` item runs under it,
    so the media fetcher's tunnels must live that long."""
    deadline = settings.browser_timeout_seconds
    if settings.browser_unblock_enabled:
        deadline = max(deadline, settings.browser_unblock_timeout_seconds)
    return deadline


def browser_graceful_seconds(settings: Settings) -> int:
    """How long the items in flight may finish after SIGTERM: the longest render deadline, and
    with unblock mode the turn wait of an item a caller waits for before it
    (``unblock_turn_seconds``). A batch item still waiting for its turn when the time is up is
    cancelled and runs again once its lease has expired."""
    deadline = browser_deadline_seconds(settings)
    if settings.browser_unblock_enabled:
        deadline += unblock_turn_seconds(settings)
    return max(
        extraction_graceful_seconds(settings), math.ceil(deadline + _SHUTDOWN_MARGIN_SECONDS)
    )


@dataclass(frozen=True)
class BrowserWorkerParts:
    """What one browser worker process runs on: the worker, its memory guard, the browsers, and
    the fetcher, conversion pool, document pool, selector children, media fetcher (None without
    ``MEDIA_ENABLED``) and post pool to close at the end."""

    worker: Worker
    guard: MemoryGuard
    browsers: Browsers
    fetcher: Fetcher
    conversion_pool: ConversionPool
    document_pool: DocumentPool
    graceful_seconds: int
    selectors: SelectorRunner
    post_pool: ConversionPool
    media: MediaFetcher | None = None

    async def start(self) -> None:
        await self.browsers.start()

    async def stop(self) -> None:
        await self.browsers.stop()


def build_browser_worker(
    settings: Settings, valkey: Redis, registry: CollectorRegistry
) -> BrowserWorkerParts:
    """The browser worker of a process, nothing started yet. As in the extract worker, its
    result cache skips writes while the memory guard reports Valkey as nearly full; so does the
    screenshot sink, which fails the item instead."""
    guard = MemoryGuard(valkey, settings.valkey_memory_guard_ratio, registry)
    gate, fetcher = create_outbound(settings, valkey)
    conversion_pool = ConversionPool(settings.convert_concurrency)
    document_pool = create_document_pool(settings)
    selectors = create_selector_runner(settings)
    post_pool = ConversionPool(settings.post_concurrency, name="post")
    media = (
        create_media_fetcher(settings, registry, call_seconds=browser_deadline_seconds(settings))
        if settings.media_enabled
        else None
    )
    browsers = create_browsers(settings, registry)
    unblock = browsers.unblock_renderer is not None
    pipeline = create_pipeline(
        settings,
        valkey,
        fetcher,
        registry,
        conversion_pool,
        gate=gate,
        memory_guard=guard,
        renderer=browsers.renderer,
        unblock_renderer=browsers.unblock_renderer,
        clearance=ValkeyClearance(valkey, settings) if unblock else None,
        timeout_seconds=settings.browser_timeout_seconds,
        document_pool=document_pool,
        media_fetcher=media,
        selector_runner=selectors,
    )
    store = JobStore(valkey, settings)
    worker = Worker(
        settings,
        valkey,
        BrowserRunner(
            pipeline,
            store,
            settings,
            guard=guard,
            asr_queue=asr_queue(valkey, settings) if asr_available(settings) else None,
            registry=registry,
            unblock_turns=asyncio.Semaphore(unblock_concurrency(settings)) if unblock else None,
        ),
        store,
        FairQueue(valkey, settings, POOL),
        registry,
        concurrency=settings.browser_concurrency,
        pii_move=create_pii_move(settings, valkey, store, guard, registry),
        llm_move=create_llm_move(settings, valkey, store, guard, registry),
        post_pool=post_pool,
    )
    return BrowserWorkerParts(
        worker=worker,
        guard=guard,
        browsers=browsers,
        fetcher=fetcher,
        conversion_pool=conversion_pool,
        document_pool=document_pool,
        graceful_seconds=browser_graceful_seconds(settings),
        selectors=selectors,
        post_pool=post_pool,
        media=media,
    )


async def serve(
    parts: BrowserWorkerParts,
    internal: _Server,
    readiness: Readiness,
    *,
    graceful_seconds: float,
    start_documents: bool = False,
) -> None:
    """Start the browsers, run the worker until a signal, then stop the browsers (see the
    module docstring). With ``start_documents`` (the process's choice) the document pool's and
    the selector pool's children and the media fetcher start with the language model; otherwise
    the first document, selector rule or video starts them. The caller closes the fetcher, the
    conversion pool, the document pool, the selector children, the media fetcher, the post pool
    and Valkey."""

    async def startup() -> None:
        starts: list[Awaitable[None]] = []
        if start_documents:
            starts.append(parts.document_pool.start())
            starts.append(parts.selectors.start())
            if parts.media is not None:
                starts.append(parts.media.start())
        await asyncio.gather(_load_language_model(), *starts)
        await parts.start()

    try:
        await _serve(
            parts.worker,
            internal,
            readiness,
            graceful_seconds=graceful_seconds,
            guard=parts.guard,
            startup=startup,
        )
    finally:
        await parts.stop()


async def _run(settings: Settings) -> None:
    valkey = create_valkey(settings.valkey_url)
    registry = REGISTRY
    parts = build_browser_worker(settings, valkey, registry)
    readiness = Readiness()
    internal = _server(
        create_internal_app(settings, valkey, registry, readiness=readiness),
        settings.internal_host,
        settings.internal_port,
        parts.graceful_seconds,
    )
    try:
        await serve(
            parts,
            internal,
            readiness,
            graceful_seconds=parts.graceful_seconds,
            start_documents=True,
        )
    finally:
        await parts.fetcher.aclose()
        parts.conversion_pool.shutdown()
        await parts.document_pool.shutdown()
        await parts.selectors.shutdown()
        if parts.media is not None:
            await parts.media.stop()
        parts.post_pool.shutdown()
        await valkey.aclose()
        _log.info("valkey_closed")


async def _check(settings: Settings) -> int:
    """Launch each enabled driver once and stop it again: 0 when all started, else 1."""
    browsers = create_browsers(settings, CollectorRegistry())
    try:
        await browsers.start()
    except Exception as error:
        _log.error("browser_check_failed", error_type=type(error).__name__, exc_info=True)
        return 1
    finally:
        await browsers.stop()
    _log.info("browser_check_passed")
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="text-extraction-browser-worker",
        description="Run the browser worker (pool browser) of text-extraction.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Launch each enabled browser driver once, then exit (0: all started).",
    )
    args = parser.parse_args(argv)
    settings = configured_settings()
    if args.check:
        sys.exit(asyncio.run(_check(settings)))
    asyncio.run(_run(settings))
