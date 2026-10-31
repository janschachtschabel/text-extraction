"""Maximum extraction (ruling P12-S1) end to end: a site that refuses our user agent.

The local site (pytest-httpserver on 127.0.0.1) answers ``/article`` with a plain 403 to a
``User-Agent`` that contains ``text-extraction`` (the fast fetch's) and with the article to
Chrome's (the unblock renderer's). The browser worker runs as in production
(``browser_worker``: ``Worker`` with ``BrowserRunner``, the real pipeline and fetcher, the egress
proxy, Playwright's and Patchright's pools on one semaphore) on fakeredis; the egress proxy
admits the site's port only. No request leaves 127.0.0.1.

An admin's ``compliance: ignore`` job with ``BROWSER_UNBLOCK_ENABLED`` gets past the 403 in the
browser; respect mode keeps the 403; a user may not ask for maximum extraction at all.
"""

import asyncio
import hashlib
import re
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from tests.browser.conftest import SiteProxy
from tests.browser.site import ARTICLE_TEXT, article
from text_extraction.app import create_public_app
from text_extraction.auth import Principal
from text_extraction.browser import BrowserPool, ChromiumRenderer, PlaywrightLauncher
from text_extraction.browser_unblock import PatchrightLauncher, UnblockRenderer
from text_extraction.browser_worker import POOL, BrowserRunner, Browsers
from text_extraction.clearance import ValkeyClearance
from text_extraction.conversion_pool import ConversionPool
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.extract_models import ExtractResult
from text_extraction.job_models import JobOptions
from text_extraction.jobs import JobStore
from text_extraction.pipeline import create_outbound, create_pipeline
from text_extraction.scheduler import FairQueue
from text_extraction.settings import ApiKey, Settings
from text_extraction.worker import Worker

pytestmark = pytest.mark.browser

ADMIN = Principal(name="root", role="admin", rate_limit_per_minute=None)
USER = Principal(name="alice", role="user", rate_limit_per_minute=None)
USER_KEY = "user-key-0123456789-maximum"
ARTICLE_PATH = "/article"
FIRST_SENTENCE = ARTICLE_TEXT.split(".")[0]
DONE_WITHIN_SECONDS = 60


@dataclass(frozen=True)
class Seen:
    """A request the site got, and what it answered."""

    path: str
    user_agent: str
    status: int


class RefusingSite:
    """``/article`` refuses our user agent with a plain 403 and serves Chrome; every other path
    (robots.txt, the TDM files) is a 404."""

    def __init__(self, httpserver: HTTPServer) -> None:
        self.seen: list[Seen] = []
        self.url = httpserver.url_for(ARTICLE_PATH)
        self.port = httpserver.port
        httpserver.expect_request(re.compile(".*")).respond_with_handler(self._handle)

    def _handle(self, request: Request) -> Response:
        agent = request.headers.get("User-Agent", "")
        if request.path != ARTICLE_PATH:
            answer = Response("Not found", status=404, content_type="text/plain")
        elif "text-extraction" in agent or "Chrome/" not in agent:
            answer = Response("Forbidden", status=403, content_type="text/plain")
        else:
            answer = Response(article(), content_type="text/html; charset=utf-8")
        self.seen.append(Seen(request.path, agent, answer.status_code))
        return answer

    def article_answers(self) -> list[tuple[bool, int]]:
        """Each request for the article: whether it came from our fetcher, and its status."""
        return [
            ("text-extraction" in seen.user_agent, seen.status)
            for seen in self.seen
            if seen.path == ARTICLE_PATH
        ]


@pytest.fixture
def refusing_site(httpserver: HTTPServer) -> RefusingSite:
    return RefusingSite(httpserver)


@pytest.fixture
def worker_settings(chromium: Path | None) -> Settings:
    """Browser and unblock mode on (Patchright's Chromium, headless), the site on 127.0.0.1."""
    return Settings(
        auth_mode="none",
        outbound_min_delay_ms=0,
        browser_enabled=True,
        browser_unblock_enabled=True,
        browser_unblock_channel="chromium",
        browser_unblock_headless=True,
        browser_executable_path=chromium,
        fetch_allow_private_networks=True,  # R9, as in conftest.py
        browser_idle_wait_seconds=1,
    )


@dataclass
class BrowserWorker:
    """A running browser worker (``Worker.run``) and the job store and queue it serves."""

    store: JobStore
    queue: FairQueue

    async def run_job(
        self, owner: Principal, options: JobOptions, url: str
    ) -> Envelope[ExtractResult]:
        """Create a one-URL ``extract`` job of ``owner``, queue it in the browser pool and wait
        for its item's result."""
        job_id = await self.store.create("extract", owner, options, [url])
        await self.queue.enqueue(job_id, f"{owner.name}#live", [url])
        deadline = time.monotonic() + DONE_WITHIN_SECONDS
        while True:
            [item] = await self.store.items(job_id, 0, 1)
            if item.result is not None:
                assert item.run is not None
                assert item.run.pool == POOL
                return item.result
            assert time.monotonic() < deadline, item.state
            await asyncio.sleep(0.1)


@asynccontextmanager
async def browser_worker(
    settings: Settings, site: RefusingSite, patchright: Path | None, profiles: Path
) -> AsyncIterator[BrowserWorker]:
    """The parts of ``browser_worker.build_browser_worker``, with the test launchers (the
    Chromium the tests found) and an egress proxy that admits the site's port only."""
    valkey = FakeAsyncRedis()
    registry = CollectorRegistry()
    proxy = SiteProxy(settings, {site.port}, registry)
    slots = asyncio.Semaphore(settings.browser_concurrency)
    standard = BrowserPool(
        settings, proxy, registry, launcher=PlaywrightLauncher(settings), slots=slots
    )
    unblock = BrowserPool(
        settings,
        proxy,
        registry,
        launcher=PatchrightLauncher(settings, executable_path=patchright, profile_root=profiles),
        slots=slots,
    )
    browsers = Browsers(
        proxy,
        None,
        (standard, unblock),
        ChromiumRenderer(standard, settings),
        UnblockRenderer(unblock, settings),
    )
    gate, fetcher = create_outbound(settings, valkey)
    conversion_pool = ConversionPool(settings.convert_concurrency)
    post_pool = ConversionPool(settings.post_concurrency, name="post")
    pipeline = create_pipeline(
        settings,
        valkey,
        fetcher,
        registry,
        conversion_pool,
        gate=gate,
        renderer=browsers.renderer,
        unblock_renderer=browsers.unblock_renderer,
        clearance=ValkeyClearance(valkey, settings),
        timeout_seconds=settings.browser_timeout_seconds,
    )
    store = JobStore(valkey, settings)
    queue = FairQueue(valkey, settings, POOL)
    worker = Worker(
        settings,
        valkey,
        BrowserRunner(pipeline, store, settings),
        store,
        queue,
        registry,
        concurrency=settings.browser_concurrency,
        post_pool=post_pool,
    )
    stop = asyncio.Event()
    await browsers.start()
    loop = asyncio.create_task(worker.run(stop))
    try:
        yield BrowserWorker(store, queue)
    finally:
        stop.set()
        await asyncio.wait_for(loop, DONE_WITHIN_SECONDS)
        await browsers.stop()
        await fetcher.aclose()
        conversion_pool.shutdown()
        post_pool.shutdown()
        await valkey.aclose()


@pytest.fixture
def run_job(
    worker_settings: Settings,
    refusing_site: RefusingSite,
    patchright_chromium: Path | None,
    tmp_path: Path,
) -> Any:
    """``run_job(owner, options)``: the result of a one-URL job for the article, run by the
    browser worker."""

    def run(owner: Principal, options: JobOptions) -> Envelope[ExtractResult]:
        async def main() -> Envelope[ExtractResult]:
            async with browser_worker(
                worker_settings, refusing_site, patchright_chromium, tmp_path
            ) as worker:
                return await worker.run_job(owner, options, refusing_site.url)

        return asyncio.run(main())

    return run


def test_admin_maximum_extraction_gets_past_a_plain_403(
    run_job: Any, refusing_site: RefusingSite
) -> None:
    envelope = run_job(ADMIN, JobOptions(mode="auto", compliance="ignore"))

    assert envelope.status == "success", envelope.error
    assert envelope.data is not None
    assert envelope.data.mode_used == "browser"
    assert envelope.data.unblocked is True
    assert envelope.data.upstream_status == 200
    assert envelope.data.content is not None
    assert FIRST_SENTENCE in envelope.data.content
    # The fast fetch got the 403, then Chrome got the article.
    assert refusing_site.article_answers() == [(True, 403), (False, 200)]


def test_respect_mode_keeps_the_403(run_job: Any, refusing_site: RefusingSite) -> None:
    envelope = run_job(ADMIN, JobOptions(mode="auto", compliance="enforce"))

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code is ErrorCode.TARGET_HTTP_ERROR
    assert envelope.data is not None
    assert envelope.data.mode_used == "fast"
    assert envelope.data.upstream_status == 403
    assert not envelope.data.unblocked
    assert refusing_site.article_answers() == [(True, 403)]  # no browser asked


@pytest.fixture
def user_api(refusing_site: RefusingSite) -> Iterator[TestClient]:
    """The public app with a user key and unblock mode on."""
    settings = Settings(
        auth_mode="api_key",
        api_keys=[
            ApiKey(
                name=USER.name, role="user", sha256=hashlib.sha256(USER_KEY.encode()).hexdigest()
            )
        ],
        outbound_min_delay_ms=0,
        browser_enabled=True,
        browser_unblock_enabled=True,
        fetch_allow_private_networks=True,
    )
    app = create_public_app(settings, FakeAsyncRedis(), CollectorRegistry())
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_user_maximum_is_forbidden(
    user_api: TestClient, run_job: Any, refusing_site: RefusingSite
) -> None:
    # REST refuses a user's ``compliance: ignore`` before anything is fetched.
    answer = user_api.post(
        "/v1/extract",
        json={"url": refusing_site.url, "compliance": "ignore"},
        headers={"Authorization": f"Bearer {USER_KEY}"},
    )
    assert answer.status_code == 403, answer.text
    assert answer.json()["error"]["code"] == "forbidden"
    assert refusing_site.seen == []

    # A user's job that asks for it anyway (written into Valkey, not through REST) fails
    # ``forbidden`` in the browser worker, and the site sees no request.
    envelope = run_job(USER, JobOptions(mode="auto", compliance="ignore"))
    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code is ErrorCode.FORBIDDEN
    assert refusing_site.seen == []
