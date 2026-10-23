"""The internal app (port 9090): probes and metrics. Never exposed through the ingress."""

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, CollectorRegistry, generate_latest
from redis.asyncio import Redis

from text_extraction.errors import install_error_handlers
from text_extraction.settings import Settings
from text_extraction.tracing import TraceMiddleware
from text_extraction.valkey import valkey_ping


def create_internal_app(
    settings: Settings, valkey: Redis, registry: CollectorRegistry = REGISTRY
) -> FastAPI:
    """Build the internal app. It has no OpenAPI spec and no docs."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.valkey = valkey
    install_error_handlers(app)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness: the process answers. It does not look at Valkey."""
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(response: Response) -> dict[str, str]:
        """Readiness: 200 when Valkey answers, else 503."""
        if await valkey_ping(valkey):
            return {"status": "ready"}
        response.status_code = 503
        return {"status": "not_ready"}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    app.add_middleware(TraceMiddleware)
    return app
