"""The public API app (port 8080)."""

import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Annotated, Any

import structlog
from fastapi import Depends, FastAPI
from prometheus_client import REGISTRY, CollectorRegistry
from prometheus_fastapi_instrumentator import Instrumentator
from redis.asyncio import Redis
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from text_extraction import __version__
from text_extraction.asr_dispatch import AsrDispatcher, asr_queue
from text_extraction.auth import Principal, require_role
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
from text_extraction.info import InfoData, service_info
from text_extraction.jobs import JobStore
from text_extraction.jobs_api import batch_body_limit
from text_extraction.jobs_api import router as jobs_router
from text_extraction.llm_dispatch import LlmDispatcher, llm_queue
from text_extraction.mcp_server.gate import McpGate
from text_extraction.mcp_server.server import PATH as MCP_PATH
from text_extraction.mcp_server.server import McpMount, build_mcp, mcp_body_limit
from text_extraction.media.pool import MediaFetcher, create_media_fetcher
from text_extraction.memory_guard import MemoryGuard
from text_extraction.metadata import load_language_model
from text_extraction.openapi import install_openapi
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
from text_extraction.site.store import SiteStore
from text_extraction.site_api import router as site_router
from text_extraction.structured.selector_pool import SelectorRunner, create_selector_runner
from text_extraction.structured_api import CHUNK_PATH
from text_extraction.structured_api import router as structured_router
from text_extraction.tracing import TraceMiddleware
from text_extraction.vision.fetch import ImageFetcher

_log = structlog.get_logger(__name__)

DESCRIPTION = (
    "Turns web pages, files, audio and video into clean text or Markdown, "
    "with a fast HTTP mode and a browser mode."
)


# Seconds on top of ``EXTRACT_TIMEOUT_SECONDS`` that the shutdown waits for extractions that
# were handed over to a job.
_BACKGROUND_GRACE_SECONDS = 5.0
# What the shutdown waits for the result writes of extractions it had to cancel.
_CANCELLED_WRITE_SECONDS = 2.0


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
    media fetcher, the UI's image fetcher and, last, the post pool. With ``MCP_ENABLED`` it runs
    the MCP app's lifespan (its session manager) while the app serves."""
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
    mcp: McpMount | None = app.state.mcp
    try:
        # The MCP session manager stops first, so no tool call starts while the rest shuts down.
        async with AsyncExitStack() as stack:
            if mcp is not None:
                await stack.enter_async_context(mcp.lifespan())
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
        ui_images: ImageFetcher | None = app.state.ui_images
        if ui_images is not None:
            await ui_images.aclose()
        post_pool: ConversionPool = app.state.post_pool
        post_pool.shutdown()


async def _not_found(request: Request) -> Response:
    raise StarletteHTTPException(404)


def _add_mcp(app: FastAPI, settings: Settings, registry: CollectorRegistry) -> McpMount:
    """Serve the MCP endpoint at exactly ``/mcp`` behind its gate (rulings P10-S2, P10-S3), left
    out of the OpenAPI spec. ``/mcp/`` is 404 rather than a redirect. ``GET /mcp`` reaches the
    stateless MCP app, which answers 405 (it has no stream)."""
    mount = build_mcp(app.state, settings, registry)
    gate = McpGate(mount.app, settings)
    app.router.routes.append(
        Route(MCP_PATH, gate, methods=["GET", "POST", "DELETE"], include_in_schema=False)
    )
    app.router.add_route(
        f"{MCP_PATH}/", _not_found, methods=["GET", "POST", "DELETE"], include_in_schema=False
    )
    return mount


def _add_ui(app: FastAPI, settings: Settings) -> dict[str, int]:
    """Serve the UI at ``/ui`` (rulings P10-S12 to P10-S16): its routes (left out of the OpenAPI
    spec), its static files at ``/ui/static``, the plain-text 403 of the ``/ui/run`` guard and
    the previews' ``ImageFetcher`` in ``app.state.ui_images`` (with its counter).
    Returns the body limits of its ``/ui/run`` routes. The UI's modules are imported only here
    and in `_add_ui_headers`, so ``jinja2`` is not imported while the UI is off."""
    from fastapi.staticfiles import StaticFiles  # only with UI_ENABLED

    from text_extraction.ui.previews import preview_counter
    from text_extraction.ui.routes import STATIC_DIR, create_ui_router
    from text_extraction.ui.security import (
        STATIC_PREFIX,
        UiRefused,
        run_body_limits,
        ui_refused_response,
    )
    from text_extraction.vision.fetch import create_image_fetcher

    # The previews' fetcher (P10-S16): the vision step's checks, a fetcher of its own; the
    # lifespan closes it.
    app.state.ui_images = create_image_fetcher(settings, app.state.valkey)
    app.state.ui_image_previews = preview_counter(app.state.registry)
    app.include_router(create_ui_router(settings))
    app.mount(STATIC_PREFIX, StaticFiles(directory=STATIC_DIR), name="ui-static")
    app.add_exception_handler(UiRefused, ui_refused_response)
    return run_body_limits(settings)


def _add_ui_headers(app: FastAPI) -> None:
    """The security headers of every ``/ui`` answer (P10-S12) and the guard of ``/ui/run/*``
    (P10-S13); added after the body limit, so it wraps it: a 413 under ``/ui`` carries the
    headers, and a refused ``/ui/run`` request is answered before its body is read."""
    from text_extraction.ui.security import UiHeadersMiddleware  # only with UI_ENABLED

    app.add_middleware(UiHeadersMiddleware)


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
    ``app.state.job_queue`` back the batch and job routes, with ``app.state.site_store`` (the
    crawl keys of a ``site`` job) also ``POST /v1/site-structure``; with ``BROWSER_ENABLED``,
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
    app.state.site_store = SiteStore(valkey, app.state.job_store, settings)
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
    install_openapi(app, settings)
    app.include_router(extract_router)
    app.include_router(file_router)
    app.include_router(screenshot_router)
    app.include_router(structured_router)
    app.include_router(jobs_router)
    app.include_router(site_router)

    @app.get("/v1/info", dependencies=[Depends(rate_limited)])
    async def info(
        principal: Annotated[Principal, Depends(require_role("user"))],
    ) -> Envelope[InfoData]:
        """Name and version of the service, and who the caller is."""
        return service_info(principal)

    path_limits = {
        "/v1/batch": batch_body_limit(settings),
        UPLOAD_PATH: upload_body_limit(settings),
        CHUNK_PATH: text_body_limit(settings),  # a text is chunked as an upload
    }
    app.state.mcp = None
    app.state.ui_images = None
    if settings.mcp_enabled:
        app.state.mcp = _add_mcp(app, settings, registry)
        path_limits[MCP_PATH] = mcp_body_limit(settings)
    if settings.ui_enabled:
        path_limits.update(_add_ui(app, settings))

    # Inside the metrics middleware, so refused bodies are counted like any other answer.
    app.add_middleware(
        BodyLimitMiddleware,
        default_limit=settings.request_max_body_bytes,
        path_limits=path_limits,
    )
    if settings.ui_enabled:
        _add_ui_headers(app)
    Instrumentator(registry=registry).instrument(app)
    # Added last, so it is the outermost middleware and sees every request.
    app.add_middleware(TraceMiddleware)
    return app
