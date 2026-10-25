"""Entry point: run the public and the internal app on two ports in one process.

``/ready`` (internal app) answers 200 only once the public server listens, which is after its
start-up (the language model is loaded and the document pool's children are started first).
On SIGTERM or SIGINT the shutdown runs in this order:

1. ``/ready`` turns 503 at once, so Kubernetes takes the pod out of the Service, while both
   servers keep serving for ``SHUTDOWN_DRAIN_SECONDS`` (a second signal ends this wait early);
2. the public server stops accepting connections and gives requests in flight
   ``graceful_shutdown_seconds`` to finish;
3. the public app's lifespan ends: it waits for extractions that became a job
   (``wait_seconds``), at most ``EXTRACT_TIMEOUT_SECONDS`` + 7 s (5 s to end, then cancelled; 2 s
   for the writes of the cancelled ones). Such an extraction ends within
   ``EXTRACT_TIMEOUT_SECONDS`` of its start and runs while step 2 does, so normally this adds
   nothing. If the pod is killed before, the job's lease expires and the job reports it lost;
4. the internal server stops, so ``/health`` and ``/metrics`` answer until the end.

``shutdown_seconds`` is the drain plus the graceful phase (steps 1 and 2); the API pods'
``terminationGracePeriodSeconds`` must stay above it plus a 10 s margin. Step 3 is not part of it.
While ``BROWSER_ENABLED`` the graceful phase also covers a sync request waiting for the browser
(``graceful_shutdown_seconds``, B13): 75 s by default, 105 s with unblock mode. While
``DOCLING_ENABLED`` it also covers a sync request waiting for OCR: the fast phase,
``DOCLING_SYNC_WAIT_SECONDS`` and 5 s (50 s by default; F12). The Helm chart must compute the
grace period with the same formula and refuse a set value that is too short; ``_helpers.tpl``
does so for the browser terms (45, 90 or 120 s), and the Docling term has yet to be added there.
"""

import asyncio
import contextlib
import math
import signal
import socket
import sys
from collections.abc import Iterator

import structlog
import uvicorn
from fastapi import FastAPI

from text_extraction.app import create_public_app
from text_extraction.internal import Readiness, create_internal_app
from text_extraction.logging import configure_logging
from text_extraction.settings import ConfigError, Settings, load_settings
from text_extraction.valkey import create_valkey

EXIT_CONFIG_ERROR = 2
# Requests in flight get at least this long after the drain, and always the extract timeout plus
# a margin (`graceful_shutdown_seconds`). The API pods' terminationGracePeriodSeconds must stay
# above the drain plus this (`shutdown_seconds`); the Helm chart mirrors the formula.
MIN_GRACEFUL_SHUTDOWN_SECONDS = 30
_SHUTDOWN_MARGIN_SECONDS = 5
# On top of the wait for a browser worker and the render: the result margin of the wait (5 s)
# and the answer.
_BROWSER_MARGIN_SECONDS = 10
# On top of the fast phase and the wait for the Docling worker: the answer.
_DOCLING_MARGIN_SECONDS = 5

_HANDLED_SIGNALS = (signal.SIGTERM, signal.SIGINT)

_log = structlog.get_logger(__name__)


class _Server(uvicorn.Server):
    """A uvicorn server that leaves signal handling to `main`.

    Two servers in one loop would each replace the other's handlers; `_serve` installs one
    handler that stops both. With ``readiness``, the server marks it listening once it has
    started.
    """

    def __init__(self, config: uvicorn.Config, readiness: Readiness | None = None) -> None:
        super().__init__(config)
        self._readiness = readiness

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        if self.started and self._readiness is not None:
            self._readiness.mark_listening()


def extraction_graceful_seconds(settings: Settings) -> int:
    """How long an extraction in flight may finish after SIGTERM: one that has just started can
    still end within its own deadline. The workers use this bound; they never wait for another
    pool."""
    return max(
        MIN_GRACEFUL_SHUTDOWN_SECONDS,
        math.ceil(settings.extract_timeout_seconds + _SHUTDOWN_MARGIN_SECONDS),
    )


def graceful_shutdown_seconds(settings: Settings) -> int:
    """How long requests in flight to the API may finish after SIGTERM: an extraction that has
    just started can still end within its own deadline. While ``BROWSER_ENABLED``, a sync request
    may also wait ``BROWSER_QUEUE_WAIT_SECONDS`` for a browser worker and then the render, after
    the fast phase of an escalating ``auto`` request (bounded by ``EXTRACT_TIMEOUT_SECONDS``;
    B13). While ``DOCLING_ENABLED``, a sync request with a document that needs OCR waits
    ``DOCLING_SYNC_WAIT_SECONDS`` for the Docling worker after its fast phase (F12)."""
    seconds = extraction_graceful_seconds(settings)
    if settings.browser_enabled:
        render = settings.browser_timeout_seconds
        if settings.browser_unblock_enabled:
            render = max(render, settings.browser_unblock_timeout_seconds)
        browser = (
            settings.extract_timeout_seconds
            + settings.browser_queue_wait_seconds
            + render
            + _BROWSER_MARGIN_SECONDS
        )
        seconds = max(seconds, math.ceil(browser))
    if settings.docling_enabled:
        docling = (
            settings.extract_timeout_seconds
            + settings.docling_sync_wait_seconds
            + _DOCLING_MARGIN_SECONDS
        )
        seconds = max(seconds, math.ceil(docling))
    return seconds


def shutdown_seconds(settings: Settings) -> float:
    """The longest time from SIGTERM to the end of the public server: drain, then graceful."""
    return settings.shutdown_drain_seconds + graceful_shutdown_seconds(settings)


def _server(
    app: FastAPI,
    host: str,
    port: int,
    graceful_seconds: int = MIN_GRACEFUL_SHUTDOWN_SECONDS,
    *,
    readiness: Readiness | None = None,
) -> _Server:
    # `log_config=None` keeps uvicorn from installing its own plain-text handlers;
    # `TraceMiddleware` writes the access line.
    # `server_header=False`: no `server: uvicorn` response header (ops guide: no default headers).
    # `timeout_graceful_shutdown`: requests in flight get this long after SIGTERM.
    return _Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_config=None,
            access_log=False,
            server_header=False,
            timeout_graceful_shutdown=graceful_seconds,
        ),
        readiness,
    )


async def _serve(
    public: _Server, internal: _Server, readiness: Readiness, *, drain_seconds: float
) -> None:
    """Run both servers until a signal, then shut down in the order the module describes."""
    loop = asyncio.get_running_loop()
    asked = asyncio.Event()  # first signal: begin the shutdown
    hurry = asyncio.Event()  # second signal: end the drain early

    def on_signal() -> None:
        if asked.is_set():
            hurry.set()
        asked.set()

    for sig in _HANDLED_SIGNALS:
        loop.add_signal_handler(sig, on_signal)
    public_task = asyncio.create_task(public.serve())
    internal_task = asyncio.create_task(internal.serve())
    waiting = asyncio.create_task(asked.wait())
    try:
        await asyncio.wait(
            [public_task, internal_task, waiting], return_when=asyncio.FIRST_COMPLETED
        )
        readiness.mark_draining()
        if asked.is_set():
            _log.info("shutdown_requested", drain_seconds=drain_seconds)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(drain_seconds):
                    await hurry.wait()
        public.should_exit = True
        await public_task  # raises a failure of the server itself
        internal.should_exit = True
        await internal_task
    finally:
        waiting.cancel()
        for sig in _HANDLED_SIGNALS:
            loop.remove_signal_handler(sig)


async def _run(settings: Settings) -> None:
    valkey = create_valkey(settings.valkey_url)
    graceful = graceful_shutdown_seconds(settings)
    readiness = Readiness()
    public = _server(
        create_public_app(settings, valkey, start_documents=True),
        settings.public_host,
        settings.public_port,
        graceful,
        readiness=readiness,
    )
    internal = _server(
        create_internal_app(settings, valkey, readiness=readiness),
        settings.internal_host,
        settings.internal_port,
        graceful,
    )
    try:
        await _serve(public, internal, readiness, drain_seconds=settings.shutdown_drain_seconds)
    finally:
        await valkey.aclose()
        _log.info("valkey_closed")


def configured_settings() -> Settings:
    """The settings from the environment, with logging set up from them. Invalid settings are
    logged and end the process with ``EXIT_CONFIG_ERROR`` (2)."""
    try:
        settings = load_settings()
    except ConfigError as error:
        # Logging is not configured yet. The message holds no secrets.
        configure_logging("INFO", "json")
        structlog.get_logger(__name__).error("invalid_configuration", message=str(error))
        sys.exit(EXIT_CONFIG_ERROR)
    configure_logging(settings.log_level, settings.log_format)
    return settings


def main() -> None:
    asyncio.run(_run(configured_settings()))
