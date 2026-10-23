"""Entry point: run the public and the internal app on two ports in one process."""

import asyncio
import contextlib
import math
import signal
import sys
from collections.abc import Iterator

import structlog
import uvicorn
from fastapi import FastAPI

from text_extraction.app import create_public_app
from text_extraction.internal import create_internal_app
from text_extraction.logging import configure_logging
from text_extraction.settings import ConfigError, Settings, load_settings
from text_extraction.valkey import create_valkey

EXIT_CONFIG_ERROR = 2
# Requests in flight get at least this long after SIGTERM, and always the extract timeout plus
# a margin (`graceful_shutdown_seconds`). The Helm chart's terminationGracePeriodSeconds (40)
# must stay above it: raise both together when EXTRACT_TIMEOUT_SECONDS goes above 30.
MIN_GRACEFUL_SHUTDOWN_SECONDS = 30
_SHUTDOWN_MARGIN_SECONDS = 5

_HANDLED_SIGNALS = (signal.SIGTERM, signal.SIGINT)

_log = structlog.get_logger(__name__)


class _Server(uvicorn.Server):
    """A uvicorn server that leaves signal handling to `main`.

    Two servers in one loop would each replace the other's handlers; `_serve` installs one
    handler that stops both.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def graceful_shutdown_seconds(settings: Settings) -> int:
    """How long requests in flight may finish after SIGTERM: an extraction that has just started
    can still end within its own deadline."""
    return max(
        MIN_GRACEFUL_SHUTDOWN_SECONDS,
        math.ceil(settings.extract_timeout_seconds + _SHUTDOWN_MARGIN_SECONDS),
    )


def _server(
    app: FastAPI,
    host: str,
    port: int,
    graceful_seconds: int = MIN_GRACEFUL_SHUTDOWN_SECONDS,
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
        )
    )


async def _serve(servers: list[_Server]) -> None:
    loop = asyncio.get_running_loop()

    def stop() -> None:
        _log.info("shutdown_requested")
        for server in servers:
            server.should_exit = True

    for sig in _HANDLED_SIGNALS:
        loop.add_signal_handler(sig, stop)
    try:
        await asyncio.gather(*(server.serve() for server in servers))
    finally:
        for sig in _HANDLED_SIGNALS:
            loop.remove_signal_handler(sig)


async def _run(settings: Settings) -> None:
    valkey = create_valkey(settings.valkey_url)
    graceful = graceful_shutdown_seconds(settings)
    public = _server(
        create_public_app(settings, valkey), settings.public_host, settings.public_port, graceful
    )
    internal = _server(
        create_internal_app(settings, valkey),
        settings.internal_host,
        settings.internal_port,
        graceful,
    )
    try:
        await _serve([public, internal])
    finally:
        await valkey.aclose()
        _log.info("valkey_closed")


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as error:
        # Logging is not configured yet. The message holds no secrets.
        configure_logging("INFO", "json")
        structlog.get_logger(__name__).error("invalid_configuration", message=str(error))
        sys.exit(EXIT_CONFIG_ERROR)
    configure_logging(settings.log_level, settings.log_format)
    asyncio.run(_run(settings))
