"""The browser worker (``browser_worker.py``): the ``BrowserRunner`` in the shared ``Worker`` on
pool ``browser``, with fakeredis (Lua), a fake renderer and a fake fetcher, and the process
start-up with the browser pools faked."""

import asyncio
import io
import os
import signal
from collections.abc import Callable, Coroutine, Iterator
from functools import wraps
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis
from PIL import Image
from prometheus_client import CollectorRegistry
from redis.exceptions import ConnectionError as RedisConnectionError
from structlog.testing import capture_logs

from tests.fakes import (
    ARTICLE,
    PAGE,
    FakeClearance,
    FakeFetcher,
    FakeRenderer,
    RefusingGate,
    fake_png,
    fetched,
    rendered,
)
from tests.test_pipeline_render import SHELL, SHELL_WITH_WORDS
from text_extraction import browser_worker
from text_extraction.asr_dispatch import asr_queue
from text_extraction.auth import Principal
from text_extraction.browser import BrowserPool
from text_extraction.browser_options import (
    BrowserCookie,
    BrowserSecrets,
    ClickAction,
    EvaluateAction,
    ScreenshotOptions,
    Viewport,
)
from text_extraction.browser_proxy import EgressProxy
from text_extraction.browser_unblock import Xvfb
from text_extraction.browser_worker import (
    BrowserRunner,
    BrowserWorkerParts,
    build_browser_worker,
    serve,
)
from text_extraction.compliance import ComplianceChecker
from text_extraction.documents.pool import DocumentPool
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.extract_models import ExtractRequest, ExtractResult
from text_extraction.fetch import Fetcher
from text_extraction.fingerprint import FingerprintStore
from text_extraction.internal import Readiness, create_internal_app
from text_extraction.job_models import JobItem, JobKind, JobOptions
from text_extraction.jobs import JobStore, browser_lease_seconds
from text_extraction.media.pool import MediaFetcher
from text_extraction.outbound import DomainGate, OutboundBusy
from text_extraction.pipeline import Pipeline
from text_extraction.pipeline_media import MediaInputMeta, asr_available
from text_extraction.render import ClearanceStore, Renderer, RenderOptions
from text_extraction.result_cache import ResultCache, cache_key
from text_extraction.scheduler import Claim, FairQueue
from text_extraction.server import _server
from text_extraction.settings import Settings
from text_extraction.worker import Worker

START = 1_000_000.0
USER = Principal(name="alice", role="user", rate_limit_per_minute=None)
ADMIN = Principal(name="root", role="admin", rate_limit_per_minute=None)
PNG = fake_png(1920, 3000)
LOST_MESSAGE = "The item was lost by a worker too often."
SECRETS = BrowserSecrets(
    actions=[ClickAction(type="click", selector="#more")],
    cookies=[BrowserCookie(name="session", value="cookie-secret-1")],
    headers={"authorization": "Bearer header-secret-1"},
)


def scenario(test: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., None]:
    """Run an async test body on its own event loop (the suite has no async plugin)."""

    @wraps(test)
    def run(*args: Any, **kwargs: Any) -> None:
        asyncio.run(test(*args, **kwargs))

    return run


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        return self.now


async def no_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "auth_mode": "none",
        "outbound_min_delay_ms": 0,
        "browser_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


class HighGuard:
    high = True


class Harness:
    """A browser worker on fakeredis: real job store, queue and pipeline, fake renderers."""

    def __init__(
        self,
        renderer: FakeRenderer | None = None,
        *,
        unblock_renderer: FakeRenderer | None = None,
        answers: dict[str, Any] | None = None,
        gate: object | None = None,
        guard: object | None = None,
        store: JobStore | None = None,
        media: object | None = None,
        **overrides: Any,
    ) -> None:
        self.valkey = FakeAsyncRedis()
        self.clock = Clock()
        self.settings = make_settings(**overrides)
        self.registry = CollectorRegistry()
        self.fetcher = FakeFetcher(answers or {})
        self.renderer = renderer or FakeRenderer(rendered())
        self.unblock_renderer = unblock_renderer or FakeRenderer(rendered())
        fetcher = cast(Fetcher, self.fetcher)
        self.pipeline = Pipeline(
            fetcher,
            cast(DomainGate, gate) if gate is not None else DomainGate(self.valkey, self.settings),
            ComplianceChecker(fetcher, self.valkey, self.settings),
            ResultCache(self.valkey, self.settings),
            FingerprintStore(self.valkey, self.settings),
            self.settings,
            self.registry,
            renderer=cast(Renderer, self.renderer),
            unblock_renderer=cast(Renderer, self.unblock_renderer),
            clearance=cast(ClearanceStore, FakeClearance()),
            timeout_seconds=self.settings.browser_timeout_seconds,
            media_fetcher=cast(MediaFetcher | None, media),
        )
        self.store = store or JobStore(self.valkey, self.settings, clock=self.clock)
        self.queue = FairQueue(self.valkey, self.settings, "browser", clock=self.clock)
        self.asr_queue = asr_queue(self.valkey, self.settings)
        self.runner = BrowserRunner(
            self.pipeline,
            self.store,
            self.settings,
            guard=cast(Any, guard),
            asr_queue=self.asr_queue if asr_available(self.settings) else None,
            registry=self.registry,
        )
        self.worker = Worker(
            self.settings,
            self.valkey,
            self.runner,
            self.store,
            self.queue,
            self.registry,
            concurrency=self.settings.browser_concurrency,
            clock=self.clock,
            sleep=no_sleep,
            random=lambda: 0.5,
        )

    async def submit(
        self,
        options: JobOptions,
        *,
        kind: JobKind = "extract",
        owner: Principal = USER,
        secrets: BrowserSecrets | None = None,
        urls: list[str] | None = None,
    ) -> str:
        urls = urls or [PAGE]
        job_id = await self.store.create(kind, owner, options, urls, secrets=secrets)
        await self.queue.enqueue(job_id, f"{owner.name}#live", urls)
        return job_id

    async def claim(self) -> Claim:
        claim: Claim = await self.queue.claim(self.runner.lease_seconds)
        assert claim.item is not None
        return claim

    async def process_next(self) -> None:
        claim = await self.claim()
        await self.worker.process(claim.item, exhausted=claim.exhausted)  # type: ignore[arg-type]

    async def item(self, job_id: str) -> JobItem:
        [item] = await self.store.items(job_id, 0, 1)
        return item

    async def result(self, job_id: str) -> Envelope[ExtractResult]:
        item = await self.item(job_id)
        assert item.result is not None
        return item.result

    def outcome(self, outcome: str) -> float:
        value = self.registry.get_sample_value(
            "text_extraction_job_items_total", {"outcome": outcome}
        )
        return value or 0.0


def error_of(envelope: Envelope[ExtractResult]) -> tuple[ErrorCode, bool]:
    assert envelope.status == "failed"
    assert envelope.error is not None
    return envelope.error.code, envelope.error.retryable


BROWSER = JobOptions(mode="browser", compliance="enforce")
AUTO = JobOptions(mode="auto", compliance="enforce")


# --- screenshots and artifacts (B7) ------------------------------------------------------------


@scenario
async def test_screenshot_job_stores_artifact_and_result() -> None:
    harness = Harness(FakeRenderer(rendered(screenshot=PNG, screenshot_size=(1920, 3000))))
    options = JobOptions(mode="browser", screenshot=True, full_page=False, compliance="enforce")
    job_id = await harness.submit(options, kind="screenshot")

    await harness.process_next()

    assert await harness.store.artifact(job_id, "screenshot.png") == PNG
    result = await harness.result(job_id)
    assert result.status == "success"
    assert result.data is not None
    assert result.data.mode_used == "browser"
    assert result.data.metadata is None  # a screenshot job reads no metadata
    ref = result.data.screenshot
    assert ref is not None
    assert ref.url == f"/v1/jobs/{job_id}/artifacts/screenshot.png"
    assert (ref.name, ref.content_type) == ("screenshot.png", "image/png")
    assert (ref.bytes, ref.width, ref.height) == (len(PNG), 1920, 3000)
    assert ref.expires_at.timestamp() == START + harness.settings.artifact_ttl_seconds
    [call] = harness.renderer.calls
    assert (call.options.screenshot, call.options.full_page) == (True, False)
    assert harness.outcome("success") == 1


@scenario
async def test_screenshot_options_decide_the_stored_file() -> None:
    buffer = io.BytesIO()
    Image.effect_noise((1200, 630), 64).convert("RGB").save(buffer, format="PNG")
    harness = Harness(
        FakeRenderer(rendered(screenshot=buffer.getvalue(), screenshot_size=(1200, 630)))
    )
    options = JobOptions(
        mode="browser",
        screenshot=True,
        compliance="enforce",
        screenshot_options=ScreenshotOptions(
            viewport=Viewport(width=1200, height=630), format="webp", width=600
        ),
    )
    job_id = await harness.submit(options, kind="screenshot")

    await harness.process_next()

    stored = await harness.store.artifact(job_id, "screenshot.webp")
    assert stored is not None
    assert stored.startswith(b"RIFF")
    assert await harness.store.artifact(job_id, "screenshot.png") is None
    data = (await harness.result(job_id)).data
    assert data is not None
    ref = data.screenshot
    assert ref is not None
    assert ref.url == f"/v1/jobs/{job_id}/artifacts/screenshot.webp"
    assert (ref.name, ref.content_type) == ("screenshot.webp", "image/webp")
    assert (ref.bytes, ref.width, ref.height) == (len(stored), 600, 315)


@scenario
async def test_screenshot_kind_renders_whatever_the_stored_options_say() -> None:
    harness = Harness(FakeRenderer(rendered(screenshot=PNG, screenshot_size=(1920, 1080))))
    job_id = await harness.submit(AUTO, kind="screenshot")  # options without screenshot

    await harness.process_next()

    result = await harness.result(job_id)
    assert result.status == "success"
    assert result.data is not None
    assert result.data.screenshot is not None
    assert harness.fetcher.page_calls() == []  # no fast step: mode browser
    [call] = harness.renderer.calls
    assert call.options.screenshot


@scenario
async def test_memory_guard_high_fails_screenshot_retryable() -> None:
    harness = Harness(
        FakeRenderer(rendered(screenshot=PNG, screenshot_size=(1920, 1080))), guard=HighGuard()
    )
    options = JobOptions(mode="browser", screenshot=True, compliance="enforce")
    job_id = await harness.submit(options, kind="screenshot")

    await harness.process_next()

    assert error_of(await harness.result(job_id)) == (ErrorCode.INTERNAL_ERROR, True)
    assert await harness.store.artifact(job_id, "screenshot.png") is None


class FailingArtifactStore(JobStore):
    async def put_artifact(self, job_id: str, name: str, data: bytes) -> Any:
        raise RedisConnectionError("Valkey is down")


@scenario
async def test_artifact_write_error_fails_screenshot_retryable() -> None:
    valkey_settings = make_settings()
    harness = Harness(
        FakeRenderer(rendered(screenshot=PNG, screenshot_size=(1920, 1080))),
        store=FailingArtifactStore(FakeAsyncRedis(), valkey_settings),
    )
    options = JobOptions(mode="browser", screenshot=True, compliance="enforce")
    job_id = await harness.submit(options, kind="screenshot")

    await harness.process_next()

    assert error_of(await harness.result(job_id)) == (ErrorCode.INTERNAL_ERROR, True)


# --- drafts (B5) --------------------------------------------------------------------------------


async def fast_draft(body: str, url: str = PAGE) -> Envelope[ExtractResult]:
    """The fast result of an ``auto`` request for ``body``, as the extract worker stores it."""
    fast = Harness(answers={url: fetched(url, body)}, browser_enabled=False)
    return await fast.pipeline.run(ExtractRequest(url=url, mode="fast"), USER)


@pytest.mark.parametrize(
    ("drafted", "rendered_body", "kept"),
    [(SHELL_WITH_WORDS, ARTICLE, "browser"), (ARTICLE, SHELL, "fast")],
)
@scenario
async def test_draft_is_compared_with_render(drafted: str, rendered_body: str, kept: str) -> None:
    harness = Harness(FakeRenderer(rendered(rendered_body)))
    job_id = await harness.submit(AUTO)
    draft = await fast_draft(drafted)
    await harness.store.put_draft(job_id, 0, draft)

    await harness.process_next()

    result = await harness.result(job_id)
    assert result.data is not None
    assert result.data.mode_used == kept
    assert harness.fetcher.page_calls() == []  # the draft stands in for the fast step
    assert len(harness.renderer.calls) == 1


@scenario
async def test_auto_item_without_a_draft_runs_the_fast_step_first() -> None:
    harness = Harness(FakeRenderer(rendered(ARTICLE)), answers={PAGE: fetched(body=SHELL)})
    job_id = await harness.submit(AUTO)

    await harness.process_next()

    result = await harness.result(job_id)
    assert result.data is not None
    assert result.data.mode_used == "browser"
    assert len(harness.fetcher.page_calls()) == 1
    assert len(harness.renderer.calls) == 1


# --- secrets (B6) -------------------------------------------------------------------------------


@scenario
async def test_secrets_are_used_and_dropped() -> None:
    harness = Harness(FakeRenderer(rendered(ARTICLE)))
    job_id = await harness.submit(AUTO, secrets=SECRETS)

    with capture_logs() as logs:
        await harness.process_next()

    [call] = harness.renderer.calls
    assert call.options.headers == {"authorization": "Bearer header-secret-1"}
    assert [(cookie.name, cookie.value) for cookie in call.options.cookies] == [
        ("session", "cookie-secret-1")
    ]
    assert list(call.options.actions) == SECRETS.actions
    # R8: an ``auto`` item with secrets renders at once, without a fast fetch.
    assert harness.fetcher.page_calls() == []
    assert (await harness.result(job_id)).status == "success"
    assert (await harness.store.secrets(job_id)).empty()
    for secret in ("cookie-secret-1", "header-secret-1", "#more"):
        assert secret not in repr(logs)


@scenario
async def test_secrets_are_dropped_when_the_run_raises() -> None:
    harness = Harness(FakeRenderer(RuntimeError("the renderer broke")))
    job_id = await harness.submit(BROWSER, secrets=SECRETS)

    await harness.process_next()

    assert error_of(await harness.result(job_id)) == (ErrorCode.INTERNAL_ERROR, False)
    assert (await harness.store.secrets(job_id)).empty()


@scenario
async def test_expired_secrets_fail_the_item_without_a_render() -> None:
    # A job whose request carried browser options never renders without them.
    harness = Harness(FakeRenderer(rendered(ARTICLE)))
    job_id = await harness.submit(BROWSER, secrets=SECRETS)
    await harness.store.drop_secrets(job_id)  # their TTL ran out while the item was queued

    with capture_logs() as logs:
        await harness.process_next()

    assert [log["job_id"] for log in logs if log["event"] == "job_secrets_expired"] == [job_id]

    assert harness.renderer.calls == []
    assert harness.fetcher.calls == []
    result = await harness.result(job_id)
    assert error_of(result) == (ErrorCode.TIMEOUT, True)
    assert result.error is not None
    assert "expired" in result.error.message


@scenario
async def test_job_without_secrets_renders_as_before() -> None:
    harness = Harness(FakeRenderer(rendered(ARTICLE)))
    job_id = await harness.submit(BROWSER)

    await harness.process_next()

    assert len(harness.renderer.calls) == 1
    assert (await harness.result(job_id)).status == "success"


@scenario
async def test_deferred_item_keeps_its_secrets_for_its_next_run() -> None:
    harness = Harness(gate=RefusingGate(OutboundBusy(3.0)))
    job_id = await harness.submit(BROWSER, secrets=SECRETS)

    await harness.process_next()

    assert harness.outcome("deferred") == 1
    assert (await harness.item(job_id)).state == "pending"
    assert await harness.store.secrets(job_id) == SECRETS


@pytest.mark.parametrize(("owner", "allowed"), [(ADMIN, True), (USER, False)])
@scenario
async def test_admin_owner_allows_evaluate_user_does_not(owner: Principal, allowed: bool) -> None:
    harness = Harness(FakeRenderer(rendered(ARTICLE)))
    secrets = BrowserSecrets(actions=[EvaluateAction(type="evaluate", script="1 + 1")])
    await harness.submit(BROWSER, owner=owner, secrets=secrets)

    await harness.process_next()

    [call] = harness.renderer.calls
    assert call.options.allow_evaluate is allowed


# --- unblock mode (B14, R9; Review Focus 5) ---------------------------------------------------


@pytest.mark.parametrize(
    ("owner", "compliance", "unblock_enabled", "unblocked"),
    [
        (ADMIN, "ignore", True, True),
        (ADMIN, "ignore", False, False),
        (ADMIN, "enforce", True, False),
        (ADMIN, "report", True, False),
        # R9: a user's job never unblocks, even where the operator's default is ``ignore``.
        (USER, "ignore", True, False),
    ],
)
@scenario
async def test_unblock_only_for_admin_ignore_jobs(
    owner: Principal, compliance: str, unblock_enabled: bool, unblocked: bool
) -> None:
    harness = Harness(browser_unblock_enabled=unblock_enabled, compliance_default=compliance)
    options = JobOptions(mode="browser", compliance=compliance)  # type: ignore[arg-type]
    job_id = await harness.submit(options, owner=owner)

    await harness.process_next()

    assert len(harness.unblock_renderer.calls) == (1 if unblocked else 0)
    assert len(harness.renderer.calls) == (0 if unblocked else 1)
    result = await harness.result(job_id)
    assert result.data is not None
    assert result.data.unblocked is unblocked


@pytest.mark.parametrize(
    ("owner", "compliance", "requested", "unblocked"),
    [
        (ADMIN, "ignore", True, True),
        # Package 22: an admin's ``unblock: false`` renders in the standard browser.
        (ADMIN, "ignore", False, False),
        # Review Focus 5: a user's or a ``report`` job's ``unblock`` changes nothing.
        (ADMIN, "report", False, False),
        (USER, "ignore", True, False),
        (USER, "report", False, False),
    ],
)
@scenario
async def test_unblock_false_renders_with_the_standard_browser(
    owner: Principal, compliance: str, requested: bool, unblocked: bool
) -> None:
    harness = Harness(browser_unblock_enabled=True, compliance_default=compliance)
    options = JobOptions(mode="browser", compliance=compliance, unblock=requested)  # type: ignore[arg-type]
    job_id = await harness.submit(options, owner=owner)

    await harness.process_next()

    assert len(harness.unblock_renderer.calls) == (1 if unblocked else 0)
    assert len(harness.renderer.calls) == (0 if unblocked else 1)
    result = await harness.result(job_id)
    assert result.data is not None
    assert result.data.unblocked is unblocked


@pytest.mark.parametrize(
    ("owner", "compliance", "requested", "keyed"),
    [
        (ADMIN, "ignore", True, False),  # the default keeps the key of the release before
        (ADMIN, "ignore", False, True),  # the standard browser's result is kept apart
        (ADMIN, "report", False, False),  # changed nothing, so not part of the key
        (USER, "report", False, False),
    ],
)
@scenario
async def test_unblock_joins_the_cache_key_only_where_it_changed_the_renderer(
    owner: Principal, compliance: str, requested: bool, keyed: bool
) -> None:
    harness = Harness(
        FakeRenderer(rendered(ARTICLE)),
        unblock_renderer=FakeRenderer(rendered(ARTICLE)),
        browser_unblock_enabled=True,
        compliance_default=compliance,
    )
    options = JobOptions(mode="browser", compliance=compliance, unblock=requested)  # type: ignore[arg-type]
    await harness.submit(options, owner=owner)

    await harness.process_next()

    base: dict[str, Any] = {
        "mode": "browser",
        "format": "markdown",
        "metadata": True,
        "links": False,
        "compliance": compliance,
    }
    expected = cache_key(PAGE, {**base, "unblock": False} if keyed else base)
    assert sorted(await harness.valkey.keys("res:*")) == [expected.encode()]


@scenario
async def test_switching_unblock_off_and_on_reports_no_change_of_the_page() -> None:
    """Minor 3 of the review: the fingerprint of a page rendered with ``unblock: false`` is
    compared with the earlier one of the same switch only, as its cache key is kept apart."""
    harness = Harness(
        FakeRenderer(rendered(SHELL_WITH_WORDS)),
        unblock_renderer=FakeRenderer(rendered(ARTICLE)),
        browser_unblock_enabled=True,
        compliance_default="ignore",
    )
    changes = []
    for unblock in (True, False, True, False):
        options = JobOptions(mode="browser", compliance="ignore", unblock=unblock, cache=False)
        job_id = await harness.submit(options, owner=ADMIN)
        await harness.process_next()
        data = (await harness.result(job_id)).data
        assert data is not None
        assert data.fingerprint is not None
        changes.append(data.fingerprint.changed)

    assert changes == [None, None, False, False]


# --- the unblock browser's own turns (package 22) -----------------------------------------------


IGNORE_BROWSER = JobOptions(mode="browser", compliance="ignore", cache=False)


class GatedRenderer(FakeRenderer):
    """Renders after ``release`` is set; notes how many renders run at once, and how much of its
    deadline each render had left when it began."""

    def __init__(self) -> None:
        super().__init__(rendered(ARTICLE))
        self.release = asyncio.Event()
        self.now = 0
        self.most = 0
        self.left: list[float] = []

    async def render(self, url: str, check: Any, options: RenderOptions, *, deadline: float) -> Any:
        self.left.append(deadline - asyncio.get_running_loop().time())
        self.now += 1
        self.most = max(self.most, self.now)
        try:
            await self.release.wait()
            return await super().render(url, check, options, deadline=deadline)
        finally:
            self.now -= 1


class CountingPipeline:
    """The real pipeline, counting the runs that began (the host's turn is taken inside)."""

    def __init__(self, real: Pipeline) -> None:
        self.real = real
        self.started = 0

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        self.started += 1
        return await self.real.run(*args, **kwargs)


def turn_harness(**overrides: Any) -> tuple[Harness, GatedRenderer, GatedRenderer]:
    standard, unblock = GatedRenderer(), GatedRenderer()
    values: dict[str, Any] = {
        "browser_unblock_enabled": True,
        "browser_concurrency": 4,
        "browser_unblock_concurrency": 2,
        "compliance_default": "ignore",
        **overrides,
    }
    harness = Harness(standard, unblock_renderer=unblock, **values)
    harness.runner._unblock_turns = asyncio.Semaphore(
        browser_worker.unblock_concurrency(harness.settings)
    )
    return harness, standard, unblock


async def claim_all(harness: Harness, count: int) -> list[Any]:
    return [(await harness.claim()).item for _ in range(count)]


@scenario
async def test_a_burst_of_unblock_items_waits_outside_the_render_deadline() -> None:
    """Four unblock items, two turns: two render at once, the other two wait for a turn before
    their pipeline (and deadline, and the host's turn) starts, and none ends in ``timeout``."""
    harness, _, unblock = turn_harness(browser_queue_wait_seconds=30)
    counting = CountingPipeline(harness.pipeline)
    harness.runner._pipeline = cast(Pipeline, counting)
    # Four hosts: the queue's per-domain cap would keep a fifth claim of one host back anyway.
    jobs = [
        await harness.submit(IGNORE_BROWSER, owner=ADMIN, urls=[f"https://h{n}.example.org/"])
        for n in range(4)
    ]
    items = await claim_all(harness, 4)

    running = [asyncio.create_task(harness.worker.process(item)) for item in items]
    await wait_for(lambda: unblock.now == 2)
    await asyncio.sleep(0.2)
    assert counting.started == 2  # the waiting two hold no host turn and no deadline
    unblock.release.set()
    async with asyncio.timeout(20):
        await asyncio.gather(*running)

    assert unblock.most == 2
    assert counting.started == 4
    budget = harness.settings.browser_unblock_timeout_seconds
    # Each render had the same budget, the two that waited (over 0.2 s) included: the
    # deadline started after the turn.
    assert min(unblock.left) > budget - 3
    assert max(unblock.left) - min(unblock.left) < 0.15
    for job_id in jobs:
        result = await harness.result(job_id)
        assert result.status == "success"
        assert result.data is not None
        assert result.data.unblocked is True


@scenario
async def test_standard_items_never_wait_for_the_unblock_turns() -> None:
    harness, standard, unblock = turn_harness()
    standard.release.set()
    turns = harness.runner._unblock_turns
    assert turns is not None
    await turns.acquire()
    await turns.acquire()  # every unblock turn is taken
    plain = await harness.submit(
        JobOptions(mode="browser", compliance="enforce", cache=False), owner=ADMIN
    )
    off = await harness.submit(
        JobOptions(mode="browser", compliance="ignore", unblock=False, cache=False), owner=ADMIN
    )
    user = await harness.submit(
        JobOptions(mode="browser", compliance="enforce", cache=False), owner=USER
    )

    async with asyncio.timeout(5):
        for _ in range(3):
            await harness.process_next()

    assert len(standard.calls) == 3
    assert unblock.calls == []
    for job_id in (plain, off, user):
        assert (await harness.result(job_id)).status == "success"


@scenario
async def test_no_turn_defers_the_item_without_asking_the_host() -> None:
    harness, _, unblock = turn_harness(browser_queue_wait_seconds=0.05)
    turns = harness.runner._unblock_turns
    assert turns is not None
    await turns.acquire()
    await turns.acquire()
    job_id = await harness.submit(IGNORE_BROWSER, owner=ADMIN, secrets=SECRETS)

    await harness.process_next()

    assert harness.outcome("deferred") == 1
    assert (await harness.item(job_id)).state == "pending"
    assert await harness.store.secrets(job_id) == SECRETS  # kept for its next run
    assert unblock.calls == []
    assert harness.fetcher.calls == []
    assert await harness.valkey.keys("out:*") == []  # no host turn taken, no failure counted
    turns.release()
    turns.release()
    assert turns._value == 2  # the deferred item holds none


@scenario
async def test_no_turn_at_the_last_attempt_answers_without_timeout() -> None:
    """At ``JOB_ITEM_MAX_ATTEMPTS`` the item is not deferred again: a draft answers with
    ``browser_unavailable``, an item without one ``rate_limited``; never ``timeout``, and the
    host's failures are not counted."""
    harness, _, unblock = turn_harness(
        browser_queue_wait_seconds=0.05,
        job_item_max_attempts=1,
        answers={PAGE: fetched(body=ARTICLE)},
    )
    turns = harness.runner._unblock_turns
    assert turns is not None
    await turns.acquire()
    await turns.acquire()
    bare = await harness.submit(IGNORE_BROWSER, owner=ADMIN)
    await harness.process_next()  # deferred once: now at the limit
    harness.clock.now += 2  # past the deferral's short pause
    await harness.process_next()

    code, retryable = error_of(await harness.result(bare))
    assert (code, retryable) == (ErrorCode.RATE_LIMITED, True)
    assert await harness.valkey.keys("out:*") == []  # the host was never asked

    auto = await harness.submit(JobOptions(mode="auto", compliance="ignore"), owner=ADMIN)
    draft = await harness.pipeline.run(
        ExtractRequest(url=PAGE, mode="fast", compliance="ignore"), ADMIN
    )
    await harness.store.put_draft(auto, 0, draft)
    gate_keys = sorted(await harness.valkey.keys("out:*"))  # the draft's own fetch
    await harness.process_next()
    harness.clock.now += 2
    await harness.process_next()
    assert sorted(await harness.valkey.keys("out:*")) == gate_keys

    result = await harness.result(auto)
    assert result.data is not None
    assert result.data.mode_used == "fast"
    assert "browser_unavailable" in result.data.warnings
    assert result.data.fingerprint is None
    assert unblock.calls == []
    for kind in (b"fail", b"circuit", b"cooldown"):
        assert [key for key in gate_keys if kind in key] == []


@scenario
async def test_the_turn_is_given_back_when_the_run_fails_or_is_cancelled() -> None:
    harness, _, unblock = turn_harness(browser_unblock_concurrency=1)
    turns = harness.runner._unblock_turns
    assert turns is not None
    unblock.results = [RuntimeError("render failed")]
    unblock.release.set()
    await harness.submit(IGNORE_BROWSER, owner=ADMIN)
    await harness.process_next()  # the worker stores the failure
    assert not turns.locked()

    unblock.release.clear()
    unblock.results = [rendered(ARTICLE)]
    await harness.submit(IGNORE_BROWSER, owner=ADMIN)
    [item] = await claim_all(harness, 1)
    task = asyncio.create_task(harness.worker.process(item))
    await wait_for(lambda: unblock.now == 1)
    assert turns.locked()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not turns.locked()


def test_unblock_turns_are_capped_at_browser_concurrency() -> None:
    assert (
        browser_worker.unblock_concurrency(
            make_settings(browser_concurrency=2, browser_unblock_concurrency=5)
        )
        == 2
    )
    assert browser_worker.unblock_concurrency(make_settings(browser_concurrency=4)) == 2
    # The wait for a turn fits the lease beside the render deadline.
    tight = make_settings(
        browser_queue_wait_seconds=60, browser_timeout_seconds=5, browser_unblock_timeout_seconds=10
    )
    assert browser_worker.unblock_turn_seconds(tight) == 10
    assert browser_worker.unblock_turn_seconds(make_settings()) == 10


@scenario
async def test_a_batch_item_waits_for_a_turn_beyond_the_queue_wait() -> None:
    """Final review of package 22, finding 5: a batch item has no caller waiting, so it waits
    for a turn up to its lease's headroom instead of ``BROWSER_QUEUE_WAIT_SECONDS``; a long
    admin batch then does not spend its attempts on deferrals while renders of 20 to 60 s run.
    A sync item keeps the short wait."""
    harness, _, unblock = turn_harness(browser_queue_wait_seconds=0.05)
    unblock.release.set()
    turns = harness.runner._unblock_turns
    assert turns is not None
    await turns.acquire()
    await turns.acquire()
    batch = await harness.submit(IGNORE_BROWSER, kind="batch", owner=ADMIN)

    async def free_a_turn() -> None:
        await asyncio.sleep(0.3)  # six times the sync wait
        turns.release()

    freeing = asyncio.create_task(free_a_turn())
    await harness.process_next()
    await freeing

    assert harness.outcome("deferred") == 0
    assert (await harness.result(batch)).status == "success"
    assert len(unblock.calls) == 1

    sync = await harness.submit(IGNORE_BROWSER, owner=ADMIN)
    await turns.acquire()  # both taken again
    await harness.process_next()
    assert harness.outcome("deferred") == 1
    assert (await harness.item(sync)).state == "pending"


def test_the_turn_wait_of_a_batch_item_is_the_leases_headroom() -> None:
    settings = make_settings(browser_unblock_enabled=True)
    headroom = browser_lease_seconds(settings) - settings.browser_unblock_timeout_seconds - 10
    assert headroom == 60  # the defaults: a 130 s lease, a 60 s unblock render
    assert browser_worker.unblock_turn_seconds(settings, batch=True) == headroom
    assert browser_worker.unblock_turn_seconds(settings) == 10


def test_the_graceful_shutdown_covers_a_sync_items_turn_wait() -> None:
    """Final review of package 22, T7 B: an unblock item a sync caller waits for may wait for
    its turn first, then render; the shutdown gives it both. A batch item still waiting at
    the end is cancelled and runs again after its lease (no result is lost)."""
    on = make_settings(browser_unblock_enabled=True)
    off = make_settings()
    assert browser_worker.browser_graceful_seconds(on) == 60 + 10 + 10
    assert browser_worker.browser_graceful_seconds(off) == 40  # 30 s render + 10, no turns


# --- the shared worker behaviour on pool ``browser`` ------------------------------------------


@scenario
async def test_lost_lease_and_exhausted_items_behave_as_extract_pool() -> None:
    harness = Harness(job_item_max_attempts=1)
    assert harness.runner.lease_seconds == browser_lease_seconds(harness.settings)

    # A lost lease: the item runs again, and only the first result counts.
    job_id = await harness.submit(BROWSER)
    stale = (await harness.claim()).item
    harness.clock.now += harness.runner.lease_seconds + 1
    current = (await harness.claim()).item
    assert stale is not None
    assert current is not None
    assert current.attempts == 1
    await harness.worker.process(current)
    await harness.worker.process(stale)
    record = await harness.store.get(job_id)
    assert record is not None
    assert (record.state, record.progress.done, record.progress.success) == ("completed", 1, 1)
    assert harness.outcome("success") == 1

    # Exhausted: lost more often than allowed, the item fails retryable without a render.
    renders = len(harness.renderer.calls)
    job_id = await harness.submit(BROWSER)
    await harness.claim()
    harness.clock.now += harness.runner.lease_seconds + 1
    await harness.claim()
    harness.clock.now += harness.runner.lease_seconds + 1
    claim = await harness.claim()
    assert claim.exhausted
    await harness.worker.process(claim.item, exhausted=True)  # type: ignore[arg-type]
    result = await harness.result(job_id)
    assert error_of(result) == (ErrorCode.INTERNAL_ERROR, True)
    assert result.error is not None
    assert result.error.message == LOST_MESSAGE
    assert len(harness.renderer.calls) == renders
    counts = await harness.queue.counts()
    assert (counts.pending, counts.leased) == (0, 0)


@scenario
async def test_an_item_moved_twice_renders_once() -> None:
    """Two extract holders of one item (a lost lease) both queued it here: once the first copy
    has its result, the second is completed without another render."""
    harness = Harness(FakeRenderer(rendered(ARTICLE)))
    uncached = JobOptions(mode="auto", compliance="enforce", cache=False)  # no cache hit either
    job_id = await harness.store.create("batch", USER, uncached, [PAGE])
    await harness.store.put_draft(job_id, 0, await fast_draft(SHELL))
    await harness.queue.enqueue_items(job_id, USER.name, [(0, PAGE)])
    await harness.queue.enqueue_items(job_id, USER.name, [(0, PAGE)])

    await harness.process_next()
    await harness.process_next()

    assert len(harness.renderer.calls) == 1
    counts = await harness.queue.counts()
    assert (counts.pending, counts.leased) == (0, 0)
    record = await harness.store.get(job_id)
    assert record is not None
    assert (record.state, record.progress.done, record.progress.success) == ("completed", 1, 1)
    assert harness.outcome("success") == 1


@scenario
async def test_exhausted_item_drops_its_secrets() -> None:
    harness = Harness(job_item_max_attempts=1)
    job_id = await harness.submit(BROWSER, secrets=SECRETS)
    for _ in range(2):
        await harness.claim()
        harness.clock.now += harness.runner.lease_seconds + 1
    claim = await harness.claim()
    assert claim.exhausted

    await harness.worker.process(claim.item, exhausted=True)  # type: ignore[arg-type]

    assert error_of(await harness.result(job_id)) == (ErrorCode.INTERNAL_ERROR, True)
    assert (await harness.store.secrets(job_id)).empty()
    assert harness.renderer.calls == []


@scenario
async def test_a_moved_item_finishes_its_job_in_the_browser_pool() -> None:
    """The extract worker moved item 1 (draft stored, queued with its index): the browser
    worker writes it, and the job completes."""
    urls = [PAGE, f"{PAGE}&n=1"]
    harness = Harness(FakeRenderer(rendered(ARTICLE, url=urls[1])))
    job_id = await harness.store.create("batch", USER, AUTO, urls)
    await harness.store.write_result(job_id, 0, await fast_draft(ARTICLE))
    await harness.store.put_draft(job_id, 1, await fast_draft(SHELL, urls[1]))
    await harness.queue.enqueue_items(job_id, USER.name, [(1, urls[1])])

    await harness.process_next()

    record = await harness.store.get(job_id)
    assert record is not None
    assert record.state == "completed"
    items = await harness.store.items(job_id, 0, 2)
    assert items[1].result is not None
    assert items[1].result.data is not None
    assert items[1].result.data.mode_used == "browser"
    assert await harness.store.draft(job_id, 1) is None  # done with once the item has its result


@scenario
async def test_a_deferred_item_keeps_its_draft_for_its_next_run() -> None:
    harness = Harness(gate=RefusingGate(OutboundBusy(3.0)))
    job_id = await harness.submit(AUTO)
    draft = await fast_draft(SHELL_WITH_WORDS)
    await harness.store.put_draft(job_id, 0, draft)

    await harness.process_next()

    assert harness.outcome("deferred") == 1
    assert await harness.store.draft(job_id, 0) == draft


# --- the one video of a thin page (package 22, M22) ----------------------------------------------


@scenario
async def test_a_rendered_thin_pages_video_moves_to_the_asr_pool() -> None:
    """A ``transcribe`` item whose rendered page is thin with one video moves to the ``asr``
    pool, with the page's answer as its draft (kept, not dropped with the browser's draft);
    the browser pool completes it without a result."""
    from tests.test_pipeline_media import MP4, ONE_VIDEO, VIDEO, FakeMediaFetcher, probe, thin_page

    harness = Harness(
        FakeRenderer(rendered(thin_page(ONE_VIDEO))),
        answers={VIDEO: fetched(VIDEO, MP4, content_type="video/mp4")},
        media=FakeMediaFetcher(probe()),
        asr_enabled=True,
    )
    job_id = await harness.submit(JobOptions(mode="browser", compliance="enforce", transcribe=True))

    await harness.process_next()

    assert (await harness.item(job_id)).result is None
    assert (await harness.asr_queue.counts()).pending == 1
    stored = await harness.store.get_input(job_id, 0, MediaInputMeta)
    assert stored is not None
    _body, meta = stored
    assert meta.page is True
    assert meta.url == VIDEO
    draft = await harness.store.draft(job_id, 0)
    assert draft is not None
    assert draft.data is not None
    assert draft.data.mode_used == "browser"
    assert draft.data.title == "Film lesson"


@scenario
async def test_without_transcribe_the_rendered_page_is_the_result() -> None:
    from tests.test_pipeline_media import MP4, ONE_VIDEO, VIDEO, FakeMediaFetcher, probe, thin_page

    harness = Harness(
        FakeRenderer(rendered(thin_page(ONE_VIDEO))),
        answers={VIDEO: fetched(VIDEO, MP4, content_type="video/mp4")},
        media=FakeMediaFetcher(probe()),
        asr_enabled=True,
    )
    job_id = await harness.submit(BROWSER)

    await harness.process_next()

    result = await harness.result(job_id)
    assert result.data is not None
    assert result.data.media is not None
    assert result.data.media.transcript_source is None
    assert (await harness.asr_queue.counts()).pending == 0


def thin_video_harness(**overrides: Any) -> Harness:
    from tests.test_pipeline_media import MP4, ONE_VIDEO, VIDEO, FakeMediaFetcher, probe, thin_page

    overrides.setdefault("asr_enabled", True)
    return Harness(
        FakeRenderer(rendered(thin_page(ONE_VIDEO))),
        answers={VIDEO: fetched(VIDEO, MP4, content_type="video/mp4")},
        media=FakeMediaFetcher(probe()),
        **overrides,
    )


TRANSCRIBE = JobOptions(mode="browser", compliance="enforce", transcribe=True)


@scenario
@pytest.mark.parametrize("kind", ["extract", "batch"])
async def test_a_moved_item_keeps_the_jobs_asr_key(kind: JobKind) -> None:
    """The ASR worker needs the caller's ``X-ASR-Api-Key`` (a batch's serves its other items
    too): a move drops the browser's secrets only."""
    harness = thin_video_harness()
    job_id = await harness.submit(TRANSCRIBE, kind=kind)
    await harness.store.put_asr_key(job_id, "asr-key-1", whole_job=kind == "batch")

    await harness.process_next()

    assert (await harness.asr_queue.counts()).pending == 1
    assert await harness.store.asr_key(job_id) == "asr-key-1"


@scenario
async def test_a_batch_item_rendered_without_a_move_keeps_the_batchs_asr_key() -> None:
    """Its other items may still need it; it goes with the batch's last result."""
    harness = Harness(FakeRenderer(rendered(ARTICLE)))
    job_id = await harness.submit(BROWSER, kind="batch", urls=[PAGE, f"{PAGE}?part=2"])
    await harness.store.put_asr_key(job_id, "asr-key-1", whole_job=True)

    await harness.process_next()

    assert (await harness.result(job_id)).status == "success"
    assert await harness.store.asr_key(job_id) == "asr-key-1"


@scenario
async def test_a_sync_jobs_move_counts_as_one_of_the_callers_asr_jobs() -> None:
    harness = thin_video_harness()
    job_id = await harness.submit(TRANSCRIBE)

    await harness.process_next()

    assert (await harness.asr_queue.counts()).pending == 1
    assert await harness.store.open_asr_jobs(USER.name) == 1
    assert job_id


@scenario
async def test_at_the_open_limit_the_page_answers_without_its_transcript() -> None:
    """``ASR_MAX_OPEN_PER_CLIENT`` bounds the moves of a sync request's render as it bounds
    ``AsrDispatcher``: at the limit nothing moves, and the page answers with ``asr_unavailable``
    and without its fingerprint."""
    harness = thin_video_harness(asr_max_open_per_client=1)
    waiting = await harness.store.create("extract", USER, TRANSCRIBE, [PAGE])
    await harness.store.add_asr_job(USER.name, waiting)
    job_id = await harness.submit(TRANSCRIBE)

    await harness.process_next()

    assert (await harness.asr_queue.counts()).pending == 0
    result = await harness.result(job_id)
    assert result.status != "failed"
    assert result.data is not None
    assert result.data.title == "Film lesson"
    assert "asr_unavailable" in result.data.warnings
    assert result.data.fingerprint is None
    assert await harness.store.open_asr_jobs(USER.name) == 1
    assert (
        harness.registry.get_sample_value(
            "text_extraction_asr_dispatch_total", {"outcome": "open_limit"}
        )
        == 1
    )


@scenario
async def test_a_batchs_moves_are_not_counted_against_the_open_limit() -> None:
    harness = thin_video_harness(asr_max_open_per_client=1)
    waiting = await harness.store.create("extract", USER, TRANSCRIBE, [PAGE])
    await harness.store.add_asr_job(USER.name, waiting)
    await harness.submit(TRANSCRIBE, kind="batch")

    await harness.process_next()

    assert (await harness.asr_queue.counts()).pending == 1


@scenario
async def test_a_youtube_url_in_the_browser_pool_is_not_moved() -> None:
    """R7: the browser pool moves only the one media of a page (``meta.page``); a platform
    video's own URL (read by an ``auto`` item's fast step here) keeps today's answer, its
    metadata with ``asr_unavailable``."""
    from tests.test_pipeline_media import YT, FakeMediaFetcher, probe

    harness = Harness(media=FakeMediaFetcher(probe(track=None)), asr_enabled=True)
    options = JobOptions(mode="auto", compliance="enforce", transcribe=True)
    job_id = await harness.submit(options, urls=[YT])

    await harness.process_next()

    assert (await harness.asr_queue.counts()).pending == 0
    result = await harness.result(job_id)
    assert result.data is not None
    assert result.data.title == "Photosynthesis for kids"
    assert "asr_unavailable" in result.data.warnings
    assert await harness.store.get_input(job_id, 0, MediaInputMeta) is None


@scenario
async def test_a_media_file_url_in_the_browser_pool_is_not_moved() -> None:
    """R7 (final review of package 22): a direct media file URL (read by an ``auto`` item's
    fast step here) is no page's one media: it is not moved to the ``asr`` pool and answers
    ``TRANSCRIPTION_NOT_HANDED``."""
    from tests.test_pipeline_media import MP4, VIDEO, FakeMediaFetcher, probe
    from text_extraction.pipeline_media import TRANSCRIPTION_NOT_HANDED_MESSAGE

    harness = Harness(
        answers={VIDEO: fetched(VIDEO, MP4, content_type="video/mp4")},
        media=FakeMediaFetcher(probe()),
        asr_enabled=True,
    )
    options = JobOptions(mode="auto", compliance="enforce", transcribe=True)
    job_id = await harness.submit(options, urls=[VIDEO])

    await harness.process_next()

    assert (await harness.asr_queue.counts()).pending == 0
    assert await harness.store.get_input(job_id, 0, MediaInputMeta) is None
    result = await harness.result(job_id)
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code.value == "no_transcript_available"
    assert result.error.message == TRANSCRIPTION_NOT_HANDED_MESSAGE


# --- the process ------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_sigterm() -> Iterator[None]:
    previous = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, previous)


class FakeBrowsers:
    """Stands in for the pools' and Xvfb's start and stop, noting the order of the calls."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[str] = []
        self.release = asyncio.Event()
        self.release.set()
        self.fail = False
        browsers = self

        async def pool_start(pool: BrowserPool) -> None:
            browsers.events.append(f"start {pool.driver}")
            await browsers.release.wait()
            if browsers.fail:
                raise RuntimeError("no browser")

        async def pool_stop(pool: BrowserPool) -> None:
            browsers.events.append(f"stop {pool.driver}")

        async def xvfb_start(_xvfb: Xvfb, display: str = ":99") -> None:
            browsers.events.append("start xvfb")

        async def xvfb_stop(_xvfb: Xvfb) -> None:
            browsers.events.append("stop xvfb")

        original_proxy_start = EgressProxy.start
        original_proxy_stop = EgressProxy.stop

        async def proxy_start(proxy: EgressProxy) -> None:
            browsers.events.append("start proxy")
            await original_proxy_start(proxy)

        async def proxy_stop(proxy: EgressProxy) -> None:
            browsers.events.append("stop proxy")
            await original_proxy_stop(proxy)

        monkeypatch.setattr(BrowserPool, "start", pool_start)
        monkeypatch.setattr(BrowserPool, "stop", pool_stop)
        monkeypatch.setattr(Xvfb, "start", xvfb_start)
        monkeypatch.setattr(Xvfb, "stop", xvfb_stop)
        monkeypatch.setattr(EgressProxy, "start", proxy_start)
        monkeypatch.setattr(EgressProxy, "stop", proxy_stop)


def build(**overrides: Any) -> tuple[BrowserWorkerParts, Readiness, Any]:
    """The parts of a browser worker process, its readiness and its internal server."""
    settings, valkey = make_settings(**overrides), FakeAsyncRedis()
    parts = build_browser_worker(settings, valkey, CollectorRegistry())
    readiness = Readiness()
    app = create_internal_app(settings, valkey, CollectorRegistry(), readiness=readiness)
    return parts, readiness, _server(app, "127.0.0.1", 0)


async def wait_for(condition: Callable[[], bool], *, within: float = 20) -> None:
    async with asyncio.timeout(within):
        while not condition():  # noqa: ASYNC110 - polls state that has no event of its own
            await asyncio.sleep(0.01)


@scenario
async def test_ready_only_after_browser_started(monkeypatch: pytest.MonkeyPatch) -> None:
    browsers = FakeBrowsers(monkeypatch)
    browsers.release.clear()
    parts, readiness, internal = build(worker_concurrency=1)

    serving = asyncio.create_task(serve(parts, internal, readiness, graceful_seconds=1))
    await wait_for(lambda: "start playwright" in browsers.events)
    await asyncio.sleep(0.2)
    assert not readiness.ready  # the browser is still starting
    browsers.release.set()
    await wait_for(lambda: readiness.ready)
    assert parts.guard.running

    os.kill(os.getpid(), signal.SIGTERM)
    async with asyncio.timeout(20):
        await serving

    assert browsers.events == ["start proxy", "start playwright", "stop playwright", "stop proxy"]
    assert not parts.guard.running
    parts.conversion_pool.shutdown()


@scenario
async def test_document_pool_starts_before_ready_and_stops_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browsers = FakeBrowsers(monkeypatch)

    async def start(pool: DocumentPool) -> None:
        # The selector children (ruling R19) are a DocumentPool of their own.
        browsers.events.append("start documents" if pool is parts.document_pool else "start other")

    monkeypatch.setattr(DocumentPool, "start", start)
    # Without media: the media fetcher's children are a DocumentPool too (see the next test).
    parts, readiness, internal = build(worker_concurrency=1, media_enabled=False)

    serving = asyncio.create_task(
        serve(parts, internal, readiness, graceful_seconds=1, start_documents=True)
    )
    await wait_for(lambda: readiness.ready)
    assert "start documents" in browsers.events

    os.kill(os.getpid(), signal.SIGTERM)
    async with asyncio.timeout(20):
        await serving
    assert browsers.events.count("start documents") == 1
    assert browsers.events.count("start other") == 1  # the selector children
    await parts.document_pool.shutdown()
    await parts.selectors.shutdown()
    parts.conversion_pool.shutdown()
    parts.post_pool.shutdown()


@scenario
async def test_media_fetcher_starts_before_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    browsers = FakeBrowsers(monkeypatch)

    async def start(media: MediaFetcher) -> None:
        browsers.events.append("start media")

    async def documents(pool: DocumentPool) -> None:
        pass

    monkeypatch.setattr(MediaFetcher, "start", start)
    monkeypatch.setattr(DocumentPool, "start", documents)
    parts, readiness, internal = build(worker_concurrency=1)
    assert parts.media is not None

    serving = asyncio.create_task(
        serve(parts, internal, readiness, graceful_seconds=1, start_documents=True)
    )
    await wait_for(lambda: readiness.ready)
    assert browsers.events.index("start media") < browsers.events.index("start playwright")

    os.kill(os.getpid(), signal.SIGTERM)
    async with asyncio.timeout(20):
        await serving
    assert browsers.events.count("start media") == 1
    await parts.media.stop()
    await parts.document_pool.shutdown()
    parts.conversion_pool.shutdown()


@scenario
async def test_failed_browser_start_never_gets_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    browsers = FakeBrowsers(monkeypatch)
    browsers.fail = True
    parts, readiness, internal = build()

    with pytest.raises(RuntimeError, match="no browser"):
        async with asyncio.timeout(20):
            await serve(parts, internal, readiness, graceful_seconds=1)

    assert not readiness.ready
    assert browsers.events[-2:] == ["stop playwright", "stop proxy"]
    parts.conversion_pool.shutdown()


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, ["start proxy", "start playwright"]),
        (
            {"browser_unblock_enabled": True},
            ["start proxy", "start xvfb", "start playwright", "start patchright"],
        ),
        (
            {"browser_unblock_enabled": True, "browser_unblock_headless": True},
            ["start proxy", "start playwright", "start patchright"],
        ),
    ],
)
@scenario
async def test_xvfb_starts_before_the_pools_only_for_headful_unblock(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], expected: list[str]
) -> None:
    browsers = FakeBrowsers(monkeypatch)
    parts, _, _ = build(**overrides)

    await parts.start()
    started = list(browsers.events)
    await parts.stop()

    assert started == expected
    stopped = browsers.events[len(started) :]
    assert stopped == [
        event.replace("start", "stop") for event in reversed(expected)
    ]  # the reverse order
    parts.conversion_pool.shutdown()


def test_browser_worker_wiring() -> None:
    settings = make_settings(
        browser_concurrency=3, worker_concurrency=8, browser_unblock_enabled=True
    )
    parts = build_browser_worker(settings, FakeAsyncRedis(), CollectorRegistry())
    try:
        worker = parts.worker
        assert worker._concurrency == 3  # BROWSER_CONCURRENCY, not WORKER_CONCURRENCY
        assert worker._queue.pool == "browser"
        runner = worker._runner
        assert isinstance(runner, BrowserRunner)
        pipeline = runner._pipeline
        assert pipeline._renderer is not None
        assert pipeline._unblock_renderer is not None
        assert pipeline._clearance is not None
        assert pipeline._timeout_seconds == settings.browser_timeout_seconds
        assert pipeline._cache._guard is parts.guard
        # The two drivers share one semaphore: BROWSER_CONCURRENCY pages in all; unblock items
        # take a turn of their own first (BROWSER_UNBLOCK_CONCURRENCY, package 22).
        standard, unblock = parts.browsers.pools
        assert standard._slots is unblock._slots
        assert runner._unblock_turns is not None
        assert runner._unblock_turns._value == settings.browser_unblock_concurrency
        assert parts.browsers.xvfb is not None
        assert parts.graceful_seconds >= settings.browser_unblock_timeout_seconds + 5
        assert runner._asr_queue is None  # without ASR, nothing moves to the asr pool
    finally:
        parts.conversion_pool.shutdown()

    transcribing = build_browser_worker(
        make_settings(asr_enabled=True), FakeAsyncRedis(), CollectorRegistry()
    )
    try:
        runner = transcribing.worker._runner
        assert isinstance(runner, BrowserRunner)
        assert runner._asr_queue is not None
        assert runner._asr_queue.pool == "asr"
    finally:
        transcribing.conversion_pool.shutdown()

    plain = build_browser_worker(make_settings(), FakeAsyncRedis(), CollectorRegistry())
    try:
        assert len(plain.browsers.pools) == 1
        assert plain.worker._runner._asr_queue is None  # type: ignore[attr-defined]
        assert plain.browsers.xvfb is None
        runner = plain.worker._runner
        assert isinstance(runner, BrowserRunner)
        assert runner._pipeline._unblock_renderer is None
        assert runner._pipeline._clearance is None
        assert runner._unblock_turns is None
    finally:
        plain.conversion_pool.shutdown()


@pytest.mark.parametrize(("fail", "code"), [(False, 0), (True, 1)])
def test_check_launches_each_driver_once(
    monkeypatch: pytest.MonkeyPatch, fail: bool, code: int
) -> None:
    browsers = FakeBrowsers(monkeypatch)
    browsers.fail = fail
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("BROWSER_UNBLOCK_ENABLED", "true")
    monkeypatch.setenv("BROWSER_UNBLOCK_HEADLESS", "true")

    with pytest.raises(SystemExit) as exit_info:
        browser_worker.main(["--check"])

    assert exit_info.value.code == code
    started = [event for event in browsers.events if event.startswith("start")]
    assert started == (
        ["start proxy", "start playwright"]
        if fail
        else ["start proxy", "start playwright", "start patchright"]
    )
    assert browsers.events[-1] == "stop proxy"


def test_main_exits_2_on_invalid_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("BROWSER_CONCURRENCY", "0")

    with pytest.raises(SystemExit) as exit_info:
        browser_worker.main([])

    assert exit_info.value.code == 2
