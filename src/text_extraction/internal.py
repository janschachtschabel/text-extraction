"""The internal app (port 9090): probes and metrics. Never exposed through the ingress."""

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, CollectorRegistry, generate_latest
from redis.asyncio import Redis

from text_extraction.errors import install_error_handlers
from text_extraction.settings import Settings
from text_extraction.tracing import TraceMiddleware
from text_extraction.valkey import valkey_ping


class Readiness:
    """Whether the pod may get traffic, apart from Valkey: the public server listens and the
    shutdown has not begun. The server sets both; ``/ready`` reads them."""

    def __init__(self) -> None:
        self._listening = False
        self._draining = False

    def mark_listening(self) -> None:
        """The public server has started and accepts connections."""
        self._listening = True

    def mark_draining(self) -> None:
        """The shutdown has begun: leave the load balancer, keep serving for now."""
        self._draining = True

    @property
    def ready(self) -> bool:
        return self._listening and not self._draining


def create_internal_app(
    settings: Settings,
    valkey: Redis,
    registry: CollectorRegistry = REGISTRY,
    *,
    readiness: Readiness,
) -> FastAPI:
    """Build the internal app. It has no OpenAPI spec and no docs."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.valkey = valkey
    app.state.readiness = readiness
    install_error_handlers(app)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness: the process answers. It does not look at Valkey."""
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(response: Response) -> dict[str, str]:
        """Readiness: 200 when the public server listens, no shutdown has begun and Valkey
        answers; else 503."""
        if readiness.ready and await valkey_ping(valkey):
            return {"status": "ready"}
        response.status_code = 503
        return {"status": "not_ready"}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    app.add_middleware(TraceMiddleware)
    return app
