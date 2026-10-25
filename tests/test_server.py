"""Readiness and the shutdown sequence: /ready follows the public listener, SIGTERM drains."""

import asyncio
import contextlib
import os
import signal
import socket
import time
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from text_extraction.internal import Readiness, create_internal_app
from text_extraction.server import (
    _serve,
    _server,
    extraction_graceful_seconds,
    graceful_shutdown_seconds,
    shutdown_seconds,
)
from text_extraction.settings import Settings

SETTINGS = Settings(auth_mode="none")


def ready_status(readiness: Readiness, valkey: FakeAsyncRedis | None = None) -> int:
    app = create_internal_app(
        SETTINGS, valkey or FakeAsyncRedis(), CollectorRegistry(), readiness=readiness
    )
    with TestClient(app) as client:
        return client.get("/ready").status_code


def test_ready_is_503_until_the_public_server_listens() -> None:
    assert ready_status(Readiness()) == 503


def test_ready_is_200_while_the_public_server_listens_and_valkey_answers() -> None:
    readiness = Readiness()
    readiness.mark_listening()

    assert ready_status(readiness) == 200
    assert ready_status(readiness, FakeAsyncRedis(connected=False)) == 503


def test_ready_is_503_once_shutdown_begins() -> None:
    readiness = Readiness()
    readiness.mark_listening()
    readiness.mark_draining()

    assert ready_status(readiness) == 503


def test_shutdown_fits_the_helm_grace_period() -> None:
    # The Helm chart computes the API grace period with the same formula (_helpers.tpl,
    # deploy/helm/test.sh): drain + graceful + 10 = 45 without the browser.
    assert shutdown_seconds(SETTINGS) == 35
    assert shutdown_seconds(Settings(auth_mode="none", shutdown_drain_seconds=0)) == 30
    assert shutdown_seconds(Settings(auth_mode="none", extract_timeout_seconds=60)) == 70


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def slow_public_app() -> FastAPI:
    """Stands in for the public app: its start-up (the language model) takes a while."""

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await asyncio.sleep(1.0)
        yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"status": "pong"}

    return app


async def status_of(client: httpx.AsyncClient, url: str) -> int | None:
    try:
        return (await client.get(url, timeout=2)).status_code
    except httpx.TransportError:
        return None  # not listening


def test_ready_follows_the_public_listener_and_drains_on_sigterm() -> None:
    public_port, internal_port = free_port(), free_port()
    readiness = Readiness()
    public = _server(slow_public_app(), "127.0.0.1", public_port, readiness=readiness)
    internal = _server(
        create_internal_app(SETTINGS, FakeAsyncRedis(), CollectorRegistry(), readiness=readiness),
        "127.0.0.1",
        internal_port,
    )
    ready_url = f"http://127.0.0.1:{internal_port}/ready"
    ping_url = f"http://127.0.0.1:{public_port}/ping"

    async def scenario() -> dict[str, object]:
        seen: dict[str, object] = {}
        # A drain far longer than any probe below: nothing races against its end. The
        # second signal cuts it short.
        serving = asyncio.create_task(_serve(public, internal, readiness, drain_seconds=60.0))
        async with httpx.AsyncClient(trust_env=False) as client:
            # /ready is asked first: once it says ready, the public listener must already
            # answer. Asking /ping first would let the server finish starting in between.
            early: list[int | None] = []
            deadline = time.monotonic() + 20
            while (ready := await status_of(client, ready_url)) != 200:
                assert time.monotonic() < deadline, "the service never became ready"
                early.append(ready)
                await asyncio.sleep(0.05)
            seen["early"] = early
            seen["ping_once_ready"] = await status_of(client, ping_url)

            os.kill(os.getpid(), signal.SIGTERM)
            deadline = time.monotonic() + 20
            while (draining := await status_of(client, ready_url)) == 200:
                assert time.monotonic() < deadline, "/ready did not follow SIGTERM"
                await asyncio.sleep(0.05)
            seen["draining_ready"] = draining
            seen["draining_ping"] = await status_of(client, ping_url)

            os.kill(os.getpid(), signal.SIGTERM)  # a second signal ends the drain early
            async with asyncio.timeout(30):
                await serving
            seen["after_ping"] = await status_of(client, ping_url)
        return seen

    seen = asyncio.run(scenario())

    early = seen["early"]
    assert isinstance(early, list)
    assert 503 in early  # the internal listener was up, the public one still starting
    assert seen["ping_once_ready"] == 200
    assert seen["draining_ready"] == 503  # out of the load balancer first ...
    assert seen["draining_ping"] == 200  # ... while requests are still served
    assert seen["after_ping"] is None


@pytest.fixture(autouse=True)
def _restore_sigterm() -> Iterator[None]:
    previous = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, previous)


def test_graceful_seconds_cover_browser_wait() -> None:
    # B13: a sync request may wait BROWSER_QUEUE_WAIT_SECONDS for a worker, then the render.
    plain = Settings(auth_mode="none")
    browser = Settings(auth_mode="none", browser_enabled=True)
    unblock = Settings(auth_mode="none", browser_enabled=True, browser_unblock_enabled=True)
    unblock_only = Settings(auth_mode="none", browser_unblock_enabled=True)

    assert graceful_shutdown_seconds(plain) == 30
    assert graceful_shutdown_seconds(unblock_only) == 30
    # An escalating auto request runs its fast phase (EXTRACT_TIMEOUT_SECONDS, 25) first.
    assert graceful_shutdown_seconds(browser) == 25 + 10 + 30 + 10
    assert graceful_shutdown_seconds(unblock) == 25 + 10 + 60 + 10
    slow = Settings(auth_mode="none", browser_enabled=True, extract_timeout_seconds=90)
    assert graceful_shutdown_seconds(slow) == 90 + 10 + 30 + 10
    # Workers never wait for another pool: the extract worker keeps the extraction bound.
    assert extraction_graceful_seconds(unblock) == 30


def test_graceful_seconds_cover_docling_wait() -> None:
    # F12: a sync request that needs OCR runs its fast phase, then waits
    # DOCLING_SYNC_WAIT_SECONDS for the Docling worker, then answers (5 s).
    docling = Settings(auth_mode="none", docling_enabled=True)
    assert graceful_shutdown_seconds(docling) == 25 + 20 + 5
    slow = Settings(auth_mode="none", docling_enabled=True, extract_timeout_seconds=60)
    assert graceful_shutdown_seconds(slow) == 60 + 20 + 5
    # The longer of the browser's and Docling's bound.
    both = Settings(auth_mode="none", docling_enabled=True, browser_enabled=True)
    assert graceful_shutdown_seconds(both) == 25 + 10 + 30 + 10
    short = Settings(auth_mode="none", docling_enabled=True, docling_sync_wait_seconds=0)
    assert graceful_shutdown_seconds(short) == 30
    assert extraction_graceful_seconds(docling) == 30
