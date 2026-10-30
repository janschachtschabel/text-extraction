"""The public API app (port 8080)."""

import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import Annotated, Any

import structlog
from fastapi import Depends, FastAPI
from prometheus_client import REGISTRY, CollectorRegistry
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel
from redis.asyncio import Redis

from text_extraction import __version__
from text_extraction.asr_dispatch import AsrDispatcher, asr_queue
from text_extraction.auth import Principal, Role, require_role
from text_extraction.body_limit import BodyLimitMiddleware
from text_extraction.browser_dispatch import BrowserDispatcher
from text_extraction.conversion_pool import ConversionPool
from text_extraction.docling_dispatch import DoclingDispatcher, docling_queue
from text_extraction.documents.pool import DocumentPool
from text_extraction.documents.routing import ocr_available
from text_extraction.envelope import Envelope
from text_extraction.errors import install_error_handlers
from text_extraction.extract_api import router as extract_router
from text_extraction.fetch import Fetcher
from text_extraction.file_api import PATH as UPLOAD_PATH
from text_extraction.file_api import router as file_router
from text_extraction.file_api import text_body_limit, upload_body_limit
from text_extraction.jobs import JobStore
from text_extraction.jobs_api import batch_body_limit
from text_extraction.jobs_api import router as jobs_router
from text_extraction.llm_dispatch import LlmDispatcher, llm_queue
from text_extraction.media.pool import MediaFetcher, create_media_fetcher
from text_extraction.memory_guard import MemoryGuard
from text_extraction.metadata import load_language_model
from text_extraction.pii_dispatch import PiiDispatcher, nlp_queue, pii_available
from text_extraction.pipeline import create_outbound, create_pipeline
from text_extraction.pipeline_documents import create_document_pool
from text_extraction.pipeline_media import asr_available
from text_extraction.pool_dispatch import GracefulShutdown
from text_extraction.queue_metrics import QueueGauges, job_metrics_for
from text_extraction.ratelimit import RateLimiter, rate_limited
from text_extraction.scheduler import FairQueue
from text_extraction.screenshot_api import router as screenshot_router
from text_extraction.settings import Settings, llm_available
from text_extraction.structured.selector_pool import SelectorRunner, create_selector_runner
from text_extraction.structured_api import CHUNK_PATH
from text_extraction.structured_api import router as structured_router
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
    """Load the language model before the first request, and start the document pool's and
    the selector pool's children and the media fetcher (its children and its egress proxy, with
    ``MEDIA_ENABLED``) meanwhile when asked to (``start_documents``); start the memory guard and
    the queue gauges; wait for extractions handed over to a job; stop the guard and the gauges
    (their tasks read Valkey, whose client the server closes after the lifespan); close the
    fetcher's connections, the conversion pool, the document pool, the selector children, the
    media fetcher and, last, the post pool."""
    documents: DocumentPool = app.state.document_pool
    selectors: SelectorRunner = app.state.selector_runner
    media: MediaFetcher | None = app.state.media_fetcher
    starts: list[Awaitable[None]] = []
    if app.state.start_documents:
        starts.append(documents.start())
        starts.append(selectors.start())
        if media is not None:
            starts.append(media.start())
    await asyncio.gather(asyncio.to_thread(load_language_model), *starts)
    guard: MemoryGuard = app.state.memory_guard
    gauges: QueueGauges = app.state.queue_gauges
    guard.start()
    if app.state.settings.jobs_enabled:
        gauges.start()
    try:
        yield
    finally:
        try:
            await _finish_background(app)
        finally:
            await gauges.stop()
            await guard.stop()
        fetcher: Fetcher = app.state.fetcher
        pool: ConversionPool = app.state.conversion_pool
        await fetcher.aclose()
        pool.shutdown()
        await documents.shutdown()
        await selectors.shutdown()
        if media is not None:
            await media.stop()
        post_pool: ConversionPool = app.state.post_pool
        post_pool.shutdown()


def create_public_app(
    settings: Settings,
    valkey: Redis,
    registry: CollectorRegistry = REGISTRY,
    *,
    start_documents: bool = False,
) -> FastAPI:
    """Build the public app. Metrics are collected in ``registry`` but not exposed here.

    ``app.state.document_pool`` holds the MarkItDown children of the pipeline,
    ``app.state.selector_runner`` its selector children (ruling R19), ``app.state.post_pool``
    the threads of the post stage (``POST_CONCURRENCY``, R21), and
    ``app.state.media_fetcher`` the media children with their egress proxy (None without
    ``MEDIA_ENABLED``). With ``start_documents`` (the server's choice) the lifespan starts them,
    so they are ready before the server listens and ``/ready`` answers 200; otherwise (tests)
    the first document or video starts them. The lifespan shuts them down either way.

    ``app.state.pipeline`` runs the extractions; tests may replace it (for example with a
    pipeline around a fake fetcher) before the app starts. ``app.state.job_store`` and
    ``app.state.job_queue`` back the batch and job routes; with ``BROWSER_ENABLED``,
    ``app.state.browser_queue`` is the ``browser`` pool's queue and
    ``app.state.browser_dispatcher`` sends sync requests there (both None otherwise); while the
    service has OCR (``DOCLING_ENABLED`` and ``JOBS_ENABLED``), ``app.state.docling_queue`` and
    ``app.state.docling_dispatcher`` do the same for documents that need OCR; while it has ASR
    (``ASR_ENABLED`` and ``JOBS_ENABLED``), ``app.state.asr_queue`` and
    ``app.state.asr_dispatcher`` for media that needs a transcription; while it has PII
    (``PII_ENABLED`` and ``JOBS_ENABLED``), ``app.state.pii_queue`` and
    ``app.state.pii_dispatcher`` for results that owe the PII step; while it has the LLM
    (``LLM_ENABLED`` and ``JOBS_ENABLED``), ``app.state.llm_queue`` and
    ``app.state.llm_dispatcher`` for results whose post stage needs the LLM;
    ``app.state.job_metrics`` counts the jobs and ``app.state.queue_gauges`` exports the backlog
    of each queue; ``app.state.memory_guard`` tells whether Valkey is nearly full (the lifespan
    starts and stops the tasks of both);
    ``app.state.background`` holds the tasks of extractions handed over to a job, which
    the shutdown waits for; ``app.state.graceful_shutdown`` is begun by the server when its
    graceful phase starts, and ends the routes' waits for a last-hop pool (``nlp``, ``llm``) with
    a 202 (``pool_dispatch``, issue #19).
    """
    app = FastAPI(
        title="Text Extraction API",
        version=__version__,
        description=DESCRIPTION,
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.valkey = valkey
    app.state.registry = registry  # the post stage's image counter (issue #20) counts here too
    app.state.rate_limiter = RateLimiter(valkey, settings.rate_limit_per_minute)
    app.state.memory_guard = MemoryGuard(valkey, settings.valkey_memory_guard_ratio, registry)
    gate, app.state.fetcher = create_outbound(settings, valkey)
    app.state.conversion_pool = ConversionPool(settings.convert_concurrency)
    app.state.document_pool = create_document_pool(settings)
    app.state.selector_runner = create_selector_runner(settings)
    app.state.post_pool = ConversionPool(settings.post_concurrency, name="post")
    app.state.media_fetcher = (
        create_media_fetcher(settings, registry) if settings.media_enabled else None
    )
    app.state.start_documents = start_documents
    app.state.pipeline = create_pipeline(
        settings,
        valkey,
        app.state.fetcher,
        registry,
        app.state.conversion_pool,
        gate=gate,
        memory_guard=app.state.memory_guard,
        document_pool=app.state.document_pool,
        media_fetcher=app.state.media_fetcher,
        selector_runner=app.state.selector_runner,
    )
    app.state.job_store = JobStore(valkey, settings)
    app.state.job_queue = FairQueue(valkey, settings)
    queues = [app.state.job_queue]
    app.state.browser_queue = None
    app.state.browser_dispatcher = None
    if settings.browser_enabled:
        app.state.browser_queue = FairQueue(valkey, settings, "browser")
        app.state.browser_dispatcher = BrowserDispatcher(
            app.state.job_store, app.state.browser_queue, settings, registry=registry
        )
        queues.append(app.state.browser_queue)
    app.state.docling_queue = None
    app.state.docling_dispatcher = None
    if ocr_available(settings):
        app.state.docling_queue = docling_queue(valkey, settings)
        app.state.docling_dispatcher = DoclingDispatcher(
            app.state.job_store, app.state.docling_queue, settings, registry=registry
        )
        queues.append(app.state.docling_queue)
    app.state.asr_queue = None
    app.state.asr_dispatcher = None
    if asr_available(settings):
        app.state.asr_queue = asr_queue(valkey, settings)
        app.state.asr_dispatcher = AsrDispatcher(
            app.state.job_store, app.state.asr_queue, settings, registry=registry
        )
        queues.append(app.state.asr_queue)
    app.state.pii_queue = None
    app.state.pii_dispatcher = None
    if pii_available(settings):
        app.state.pii_queue = nlp_queue(valkey, settings)
        app.state.pii_dispatcher = PiiDispatcher(
            app.state.job_store, app.state.pii_queue, settings, registry=registry
        )
        queues.append(app.state.pii_queue)
    app.state.llm_queue = None
    app.state.llm_dispatcher = None
    if llm_available(settings):
        app.state.llm_queue = llm_queue(valkey, settings)
        app.state.llm_dispatcher = LlmDispatcher(
            app.state.job_store, app.state.llm_queue, settings, registry=registry
        )
        queues.append(app.state.llm_queue)
    app.state.job_metrics = job_metrics_for(registry)
    app.state.queue_gauges = QueueGauges(queues, registry)
    app.state.background = set()
    app.state.graceful_shutdown = GracefulShutdown()
    install_error_handlers(app)
    app.include_router(extract_router)
    app.include_router(file_router)
    app.include_router(screenshot_router)
    app.include_router(structured_router)
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

    # Inside the metrics middleware, so refused bodies are counted like any other answer.
    app.add_middleware(
        BodyLimitMiddleware,
        default_limit=settings.request_max_body_bytes,
        path_limits={
            "/v1/batch": batch_body_limit(settings),
            UPLOAD_PATH: upload_body_limit(settings),
            CHUNK_PATH: text_body_limit(settings),  # a text is chunked as an upload
        },
    )
    Instrumentator(registry=registry).instrument(app)
    # Added last, so it is the outermost middleware and sees every request.
    app.add_middleware(TraceMiddleware)
    return app
