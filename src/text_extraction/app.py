"""The public API app (port 8080)."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

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
from text_extraction.metadata import load_language_model
from text_extraction.pipeline import create_pipeline
from text_extraction.ratelimit import RateLimiter, rate_limited
from text_extraction.settings import Settings
from text_extraction.tracing import TraceMiddleware, current_trace_id

SERVICE_NAME = "text-extraction"
DESCRIPTION = (
    "Turns web pages, files, audio and video into clean text or Markdown, "
    "with a fast HTTP mode and a browser mode."
)


class InfoData(BaseModel):
    name: str
    version: str
    principal: str
    role: Role


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load the language model before the first request; close the fetcher's connections and
    the conversion pool last."""
    await asyncio.to_thread(load_language_model)
    try:
        yield
    finally:
        fetcher: Fetcher = app.state.fetcher
        pool: ConversionPool = app.state.conversion_pool
        await fetcher.aclose()
        pool.shutdown()


def create_public_app(
    settings: Settings, valkey: Redis, registry: CollectorRegistry = REGISTRY
) -> FastAPI:
    """Build the public app. Metrics are collected in ``registry`` but not exposed here.

    ``app.state.pipeline`` runs the extractions; tests may replace it (for example with a
    pipeline around a fake fetcher) before the app starts.
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
    install_error_handlers(app)
    app.include_router(extract_router)

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
