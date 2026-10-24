"""The browser worker (entry point ``text-extraction-browser-worker``): runs the items of the
``browser`` pool in a real browser and stores their results in the job.

It is the extract worker's ``Worker`` (``worker.py``: slot loops, leases, deferrals, retries,
exhausted items, result writes, metrics, ``/ready`` and the shutdown) with another runner and
another pipeline. This is the only module outside ``browser*.py`` that imports the browser
modules, so the API and the extract worker never load Playwright or Patchright.

**The runner** (``BrowserRunner``) leases an item for ``2 * BROWSER_UNBLOCK_TIMEOUT_SECONDS
+ 10`` seconds. For each item it:

1. reads the job's caller secrets (actions, cookies, headers; B6) and the item's draft (the
   fast result of an ``auto`` item the extract worker moved here; B5). A job created with
   secrets whose secrets have expired (the item waited longer than their TTL) is not rendered:
   the item fails with ``timeout``, retryable;
2. rebuilds the request from the job's options and the secrets. A ``screenshot`` job renders in
   ``browser`` mode with ``screenshot`` and without metadata. The job's owner is the principal,
   so an admin owner's ``evaluate`` actions run, and unblock mode applies only to an admin's
   ``compliance: ignore`` job while ``BROWSER_UNBLOCK_ENABLED`` (B14, R9). An ``auto`` request
   with secrets always renders (R8), one with a draft is compared with it, one without either
   runs the fast step first;
3. runs the pipeline with an ``ArtifactSink`` bound to the job: the screenshot is stored as
   ``job:{<id>}:artifact:screenshot.png`` for ``ARTIFACT_TTL_SECONDS`` and served at
   ``/v1/jobs/<id>/artifacts/screenshot.png``. While Valkey's memory guard is high, or when the
   write fails, nothing is stored and the item fails ``internal_error``, retryable (B7);
4. deletes the secrets and the item's draft once the item has its outcome, also when the run
   failed. An item that is given back to run again (a gate refusal it is deferred for, a Valkey
   error, a cancelled shutdown) keeps them for that run; the secrets expire on their own after
   their short TTL, a draft with the job.

**The process** serves only the internal port, like the extract worker. Before ``/ready``
answers 200 it loads the language model, starts the egress proxy, then Xvfb (only for unblock
mode with a headful Chrome: Patchright reads ``DISPLAY`` when its driver starts) and the
browser pools: Playwright's Chromium, and with ``BROWSER_UNBLOCK_ENABLED`` Patchright's Chrome.
Both pools share one semaphore, so ``BROWSER_CONCURRENCY`` pages run at once in all; the worker
runs as many slot loops (``WORKER_CONCURRENCY`` does not apply). The pipeline's deadline is
``BROWSER_TIMEOUT_SECONDS`` (an unblock render has ``BROWSER_UNBLOCK_TIMEOUT_SECONDS``). After
the loops have stopped, the pools, Xvfb and the proxy stop, in that order. A browser that does
not start ends the process before it is ready.

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

import structlog
from prometheus_client import REGISTRY, CollectorRegistry
from redis.asyncio import Redis
from redis.exceptions import RedisError

from text_extraction.browser import BrowserPool, ChromiumRenderer, PlaywrightLauncher
from text_extraction.browser_options import ArtifactRef, BrowserSecrets
from text_extraction.browser_proxy import EgressProxy
from text_extraction.browser_unblock import PatchrightLauncher, UnblockRenderer, Xvfb
from text_extraction.clearance import ValkeyClearance
from text_extraction.conversion_pool import ConversionPool
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.extract_models import ExtractRequest, ExtractResult, failed_extract
from text_extraction.fetch import Fetcher
from text_extraction.internal import Readiness, create_internal_app
from text_extraction.jobs import (
    SCREENSHOT_ARTIFACT,
    JobLost,
    JobRecord,
    JobStore,
    browser_lease_seconds,
)
from text_extraction.memory_guard import MemoryGuard
from text_extraction.outbound import CircuitOpen, OutboundBusy
from text_extraction.pipeline import Pipeline, create_outbound, create_pipeline
from text_extraction.render import ArtifactUnavailable
from text_extraction.result_cache import MemoryPressure
from text_extraction.scheduler import FairQueue, QueueItem
from text_extraction.server import (
    _Server,
    _server,
    configured_settings,
    extraction_graceful_seconds,
)
from text_extraction.settings import Settings
from text_extraction.valkey import create_valkey
from text_extraction.worker import (
    Worker,
    _load_language_model,
    _serve,
    job_principal,
)

_log = structlog.get_logger(__name__)

POOL = "browser"
# What a shutdown grants an item past its render deadline: closing the page, storing the result.
_SHUTDOWN_MARGIN_SECONDS = 10
SECRETS_EXPIRED_MESSAGE = (
    "The request's browser options expired before a browser was free; send the request again."
)
# Failures after which an item is given back to run again; it still needs its secrets then.
_GIVEN_BACK = (OutboundBusy, CircuitOpen, RedisError, OSError)


def artifact_url(job_id: str, name: str) -> str:
    """Where the API serves a file of a job."""
    return f"/v1/jobs/{job_id}/artifacts/{name}"


class JobArtifacts:
    """The ``ArtifactSink`` of one job's item: the screenshot goes to the job store (B7)."""

    def __init__(self, store: JobStore, job_id: str, guard: MemoryPressure | None) -> None:
        self._store = store
        self._job_id = job_id
        self._guard = guard

    async def put_screenshot(self, png: bytes, width: int, height: int) -> ArtifactRef:
        if self._guard is not None and self._guard.high:
            raise ArtifactUnavailable("Valkey's memory is nearly full")
        try:
            expires_at = await self._store.put_artifact(self._job_id, SCREENSHOT_ARTIFACT, png)
        except (RedisError, OSError, JobLost) as error:
            _log.warning("artifact_not_stored", error_type=type(error).__name__)
            raise ArtifactUnavailable("The screenshot could not be stored") from error
        return ArtifactRef(
            name=SCREENSHOT_ARTIFACT,
            url=artifact_url(self._job_id, SCREENSHOT_ARTIFACT),
            content_type="image/png",
            bytes=len(png),
            width=width,
            height=height,
            expires_at=expires_at,
        )


class BrowserRunner:
    """The ``browser`` pool's ``ItemRunner``; see the module docstring. ``guard`` is the memory
    guard that keeps screenshots out of a nearly full Valkey."""

    def __init__(
        self,
        pipeline: Pipeline,
        store: JobStore,
        settings: Settings,
        *,
        guard: MemoryPressure | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._store = store
        self._guard = guard
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
        try:
            secrets = await self._store.secrets(job.id)
            if job.has_secrets and secrets.empty():
                # Their TTL ran out while the item waited: rendering without the caller's
                # headers, cookies or actions would answer another request than the one sent.
                finished = True
                _log.info("job_secrets_expired", job_id=job.id)
                return failed_extract(ErrorCode.TIMEOUT, SECRETS_EXPIRED_MESSAGE, retryable=True)
            draft = await self._store.draft(job.id, item.index)
            envelope = await self._pipeline.run(
                _request(job, url, secrets),
                job_principal(job),
                gate_refusals=gate_refusals,
                artifacts=JobArtifacts(self._store, job.id, self._guard),
                draft=draft,
            )
            finished = True
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
                await self._drop_secrets(job.id)
                await self._drop_draft(job.id, item.index)

    async def _drop_secrets(self, job_id: str) -> None:
        """Delete the secrets; a failure leaves them to their TTL rather than the result."""
        try:
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


def browser_graceful_seconds(settings: Settings) -> int:
    """How long the items in flight may finish after SIGTERM: the longest render deadline."""
    deadline = settings.browser_timeout_seconds
    if settings.browser_unblock_enabled:
        deadline = max(deadline, settings.browser_unblock_timeout_seconds)
    return max(
        extraction_graceful_seconds(settings), math.ceil(deadline + _SHUTDOWN_MARGIN_SECONDS)
    )


@dataclass(frozen=True)
class BrowserWorkerParts:
    """What one browser worker process runs on: the worker, its memory guard, the browsers, and
    the fetcher and conversion pool to close at the end."""

    worker: Worker
    guard: MemoryGuard
    browsers: Browsers
    fetcher: Fetcher
    conversion_pool: ConversionPool
    graceful_seconds: int

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
    )
    store = JobStore(valkey, settings)
    worker = Worker(
        settings,
        valkey,
        BrowserRunner(pipeline, store, settings, guard=guard),
        store,
        FairQueue(valkey, settings, POOL),
        registry,
        concurrency=settings.browser_concurrency,
    )
    return BrowserWorkerParts(
        worker=worker,
        guard=guard,
        browsers=browsers,
        fetcher=fetcher,
        conversion_pool=conversion_pool,
        graceful_seconds=browser_graceful_seconds(settings),
    )


async def serve(
    parts: BrowserWorkerParts,
    internal: _Server,
    readiness: Readiness,
    *,
    graceful_seconds: float,
) -> None:
    """Start the browsers, run the worker until a signal, then stop the browsers (see the
    module docstring). The caller closes the fetcher, the conversion pool and Valkey."""

    async def startup() -> None:
        await _load_language_model()
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
        await serve(parts, internal, readiness, graceful_seconds=parts.graceful_seconds)
    finally:
        await parts.fetcher.aclose()
        parts.conversion_pool.shutdown()
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
