"""The public API app (port 8080)."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import structlog
from fastapi import Depends, FastAPI
from prometheus_client import REGISTRY, CollectorRegistry
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel
from redis.asyncio import Redis

from text_extraction import __version__
from text_extraction.auth import Principal, Role, require_role
from text_extraction.conversion_pool import ConversionPool
from text_extraction.envelope import Envelope
from text_extraction.errors import install_error_handlers
from text_extraction.extract_api import router as extract_router
from text_extraction.fetch import Fetcher
from text_extraction.jobs import JobStore
from text_extraction.jobs_api import router as jobs_router
from text_extraction.metadata import load_language_model
from text_extraction.pipeline import create_pipeline
from text_extraction.queue_metrics import QueueGauges, job_metrics_for
from text_extraction.ratelimit import RateLimiter, rate_limited
from text_extraction.scheduler import FairQueue
from text_extraction.settings import Settings
from text_extraction.tracing import TraceMiddleware, current_trace_id

_log = structlog.get_logger(__name__)

SERVICE_NAME = "text-extraction"
DESCRIPTION = (
    "Turns web pages, files, audio and video into clean text or Markdown, "
    "with a fast HTTP mode and a browser mode."
)


# Seconds on top of ``EXTRACT_TIMEOUT_SECONDS`` that the shutdown waits for extractions that
# were handed over to a job.
_BACKGROUND_GRACE_SECONDS = 5.0
# What the shutdown waits for the result writes of extractions it had to cancel.
_CANCELLED_WRITE_SECONDS = 2.0


class InfoData(BaseModel):
    name: str
    version: str
    principal: str
    role: Role


async def _wait_for(background: set[asyncio.Task[Any]], seconds: float) -> None:
    """Wait until no task of ``background`` is running, or ``seconds`` have passed. Only tasks
    that are not done are waited on, so a finished member that was not removed cannot make this
    spin; the set is looked at again after each round because finishing tasks add the tasks
    that store their results."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while (remaining := deadline - loop.time()) > 0:
        running = [task for task in background if not task.done()]
        if not running:
            await asyncio.sleep(0)  # a finished task's done-callback may add a storing task
            running = [task for task in background if not task.done()]
            if not running:
                return
        await asyncio.wait(running, timeout=remaining)


async def _finish_background(app: FastAPI) -> None:
    """Let extractions that were handed over to a job (``wait_seconds``) end and store their
    results. Those still running after ``EXTRACT_TIMEOUT_SECONDS`` + the grace are cancelled; their
    jobs then get a retryable ``internal_error``, or, if that write fails, lose their lease."""
    settings: Settings = app.state.settings
    background: set[asyncio.Task[Any]] = app.state.background
    await _wait_for(background, settings.extract_timeout_seconds + _BACKGROUND_GRACE_SECONDS)
    running = [task for task in background if not task.done()]
    if running:
        _log.warning("background_extractions_cancelled", count=len(running))
        for task in running:
            task.cancel()
        await _wait_for(background, _CANCELLED_WRITE_SECONDS)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load the language model before the first request; start the queue gauges; wait for
    extractions handed over to a job; stop the gauges (their task reads Valkey, whose client the
    server closes after the lifespan); close the fetcher's connections and the conversion pool
    last."""
    await asyncio.to_thread(load_language_model)
    gauges: QueueGauges = app.state.queue_gauges
    if app.state.settings.jobs_enabled:
        gauges.start()
    try:
        yield
    finally:
        try:
            await _finish_background(app)
        finally:
            await gauges.stop()
        fetcher: Fetcher = app.state.fetcher
        pool: ConversionPool = app.state.conversion_pool
        await fetcher.aclose()
        pool.shutdown()


def create_public_app(
    settings: Settings, valkey: Redis, registry: CollectorRegistry = REGISTRY
) -> FastAPI:
    """Build the public app. Metrics are collected in ``registry`` but not exposed here.

    ``app.state.pipeline`` runs the extractions; tests may replace it (for example with a
    pipeline around a fake fetcher) before the app starts. ``app.state.job_store`` and
    ``app.state.job_queue`` back the batch and job routes; ``app.state.job_metrics`` counts the
    jobs and ``app.state.queue_gauges`` exports the queue's backlog (the lifespan starts and stops
    its task); ``app.state.background`` holds the tasks of extractions handed over to a job, which
    the shutdown waits for.
    """
    app = FastAPI(
        title="Text Extraction API",
        version=__version__,
        description=DESCRIPTION,
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.valkey = valkey
    app.state.rate_limiter = RateLimiter(valkey, settings.rate_limit_per_minute)
    app.state.fetcher = Fetcher(settings)
    app.state.conversion_pool = ConversionPool(settings.convert_concurrency)
    app.state.pipeline = create_pipeline(
        settings, valkey, app.state.fetcher, registry, app.state.conversion_pool
    )
    app.state.job_store = JobStore(valkey, settings)
    app.state.job_queue = FairQueue(valkey, settings)
    app.state.job_metrics = job_metrics_for(registry)
    app.state.queue_gauges = QueueGauges(app.state.job_queue, registry)
    app.state.background = set()
    install_error_handlers(app)
    app.include_router(extract_router)
    app.include_router(jobs_router)

    @app.get("/v1/info", dependencies=[Depends(rate_limited)])
    async def info(
        principal: Annotated[Principal, Depends(require_role("user"))],
    ) -> Envelope[InfoData]:
        """Name and version of the service, and who the caller is."""
        data = InfoData(
            name=SERVICE_NAME,
            version=__version__,
            principal=principal.name,
            role=principal.role,
        )
        return Envelope(status="success", data=data, trace_id=current_trace_id())

    Instrumentator(registry=registry).instrument(app)
    # Added last, so it is the outermost middleware and sees every request.
    app.add_middleware(TraceMiddleware)
    return app
