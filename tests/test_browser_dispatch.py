"""``BrowserDispatcher`` (``browser_dispatch.py``): the API's side of a sync browser request, on
fakeredis with the real job store and the ``browser`` queue. A fake worker task claims from
the queue and writes a result where a test needs one."""

import asyncio
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from redis.exceptions import ConnectionError as RedisConnectionError

from tests.fakes import PAGE
from text_extraction.auth import Principal
from text_extraction.browser_dispatch import BrowserDispatcher, Handover
from text_extraction.browser_options import BrowserCookie, ClickAction
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractRequest, ExtractResult
from text_extraction.job_models import JobOptions
from text_extraction.jobs import JobStore
from text_extraction.scheduler import FairQueue
from text_extraction.settings import Settings

USER = Principal(name="alice", role="user", rate_limit_per_minute=None)
SETTINGS = Settings(auth_mode="none", browser_enabled=True)


def scenario(test: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., None]:
    """Run an async test body on its own event loop (the suite has no async plugin)."""

    @wraps(test)
    def run(*args: Any, **kwargs: Any) -> None:
        asyncio.run(test(*args, **kwargs))

    return run


class FakeTime:
    """A monotonic clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class Parts:
    def __init__(self, settings: Settings = SETTINGS, time: FakeTime | None = None) -> None:
        self.valkey = FakeAsyncRedis()
        self.store = JobStore(self.valkey, settings)
        self.queue = FairQueue(self.valkey, settings, "browser")
        self.time = time
        if time is None:
            self.dispatcher = BrowserDispatcher(self.store, self.queue, settings)
        else:
            self.dispatcher = BrowserDispatcher(
                self.store, self.queue, settings, clock=time.clock, sleep=time.sleep
            )

    async def job_keys(self) -> list[str]:
        return sorted(
            key.decode() if isinstance(key, bytes) else key
            for key in await self.valkey.keys("job:*")
        )


def fast_draft() -> Envelope[ExtractResult]:
    return Envelope[ExtractResult].model_validate(
        {
            "status": "partial",
            "data": {
                "url": PAGE,
                "final_url": PAGE,
                "upstream_status": 200,
                "content_type": "text/html",
                "mode_used": "fast",
                "attempts": 1,
                "language": "en",
                "word_count": 12,
                "title": "Shell",
                "format": "markdown",
                "content": "A shell page.",
                "quality": None,
                "timings": {"total_ms": 5, "compliance_ms": 1, "fetch_ms": 2, "convert_ms": 1},
                "compliance": {"mode": "enforce"},
                "metadata": None,
                "links": None,
                "fingerprint": None,
                "cache": "miss",
                "warnings": [],
            },
        }
    )


def rendered_result() -> Envelope[ExtractResult]:
    draft = fast_draft()
    assert draft.data is not None
    data = draft.data.model_copy(update={"mode_used": "browser", "word_count": 400})
    return Envelope[ExtractResult](status="success", data=data)


def never_detached() -> bool:
    return False


@scenario
async def test_submit_creates_job_with_secrets_draft_and_live_client() -> None:
    parts = Parts()
    req = ExtractRequest(
        url=PAGE,
        mode="auto",
        actions=[ClickAction(selector="#more")],
        cookies=[BrowserCookie(name="session", value="cookie-secret-1")],
        headers={"Authorization": "Bearer header-secret-1"},
        format="text",
    )
    draft = fast_draft()

    job_id = await parts.dispatcher.submit("extract", req, USER, "report", draft=draft)

    record = await parts.store.get(job_id)
    assert record is not None
    assert (record.kind, record.owner, record.state, record.progress.total) == (
        "extract",
        "alice",
        "queued",
        1,
    )
    assert record.options == JobOptions(mode="auto", format="text", compliance="report")
    assert await parts.store.secrets(job_id) == req.secrets()
    assert await parts.store.draft(job_id, 0) == draft
    claim = await parts.queue.claim(60)
    assert claim.item is not None
    assert (claim.item.job_id, claim.item.index, claim.item.client) == (job_id, 0, "alice#live")
    # Nothing went to the extract pool.
    assert (await FairQueue(parts.valkey, SETTINGS).counts()).pending == 0


@scenario
async def test_submit_of_a_screenshot_stores_its_options() -> None:
    parts = Parts()
    req = ExtractRequest(url=PAGE, mode="browser", screenshot=True, full_page=False, metadata=False)

    job_id = await parts.dispatcher.submit("screenshot", req, USER, "enforce")

    record = await parts.store.get(job_id)
    assert record is not None
    assert record.kind == "screenshot"
    assert (record.options.mode, record.options.screenshot, record.options.full_page) == (
        "browser",
        True,
        False,
    )
    assert record.options.metadata is False
    assert (await parts.store.secrets(job_id)).empty()
    assert await parts.store.draft(job_id, 0) is None


@scenario
async def test_submit_reuses_handover_job() -> None:
    parts = Parts()
    options = JobOptions(mode="auto", compliance="enforce")
    job_id = await parts.store.create("extract", USER, options, [PAGE])
    await parts.store.mark_running(job_id, lease_seconds=60)
    draft = fast_draft()

    reused = await parts.dispatcher.submit(
        "extract", ExtractRequest(url=PAGE), USER, "enforce", job_id=job_id, draft=draft
    )

    assert reused == job_id
    record = await parts.store.get(job_id)
    assert record is not None
    assert (record.state, record.lease_until) == ("running", None)  # no lost view (P1)
    assert await parts.store.draft(job_id, 0) == draft
    claim = await parts.queue.claim(60)
    assert claim.item is not None
    assert (claim.item.job_id, claim.item.index, claim.item.client) == (job_id, 0, "alice#live")
    assert len([key for key in await parts.job_keys() if key.endswith(":meta")]) == 1


@scenario
async def test_submit_refuses_secrets_for_a_reused_job() -> None:
    parts = Parts()
    job_id = await parts.store.create("extract", USER, JobOptions(compliance="enforce"), [PAGE])
    req = ExtractRequest(url=PAGE, headers={"x-token": "header-secret-1"})

    with pytest.raises(ValueError, match="secrets"):
        await parts.dispatcher.submit("extract", req, USER, "enforce", job_id=job_id)


@scenario
async def test_failed_enqueue_deletes_the_new_job(monkeypatch: pytest.MonkeyPatch) -> None:
    parts = Parts()

    async def down(*args: object, **kwargs: object) -> None:
        raise RedisConnectionError("valkey is down")

    monkeypatch.setattr(parts.queue, "enqueue_items", down)
    req = ExtractRequest(url=PAGE, mode="browser", headers={"x-token": "header-secret-1"})

    with pytest.raises(RedisConnectionError):
        await parts.dispatcher.submit("extract", req, USER, "enforce")

    assert await parts.job_keys() == []


async def fake_worker(
    parts: Parts, envelope: Envelope[ExtractResult], *, claimed: asyncio.Event | None = None
) -> None:
    """Claim the item from ``browser`` like the browser worker, then write ``envelope``."""
    while True:
        claim = await parts.queue.claim(60)
        if claim.item is not None:
            break
        await asyncio.sleep(0.01)
    await parts.store.mark_running(claim.item.job_id)
    if claimed is not None:
        claimed.set()
    await asyncio.sleep(0.05)
    await parts.store.write_result(claim.item.job_id, claim.item.index, envelope)
    await parts.queue.complete(claim.item)


@scenario
async def test_wait_returns_result() -> None:
    parts = Parts()
    job_id = await parts.dispatcher.submit("extract", ExtractRequest(url=PAGE), USER, "enforce")
    result = rendered_result()
    worker = asyncio.create_task(fake_worker(parts, result))

    got = await asyncio.wait_for(
        parts.dispatcher.wait(job_id, unblock=False, detached=never_detached), 10
    )
    await worker

    assert got == result


@scenario
async def test_wait_gives_up_when_unclaimed() -> None:
    time = FakeTime()
    parts = Parts(time=time)
    job_id = await parts.dispatcher.submit("extract", ExtractRequest(url=PAGE), USER, "enforce")

    got = await parts.dispatcher.wait(job_id, unblock=False, detached=never_detached)

    assert got is None
    waited = time.now - 100.0
    assert SETTINGS.browser_queue_wait_seconds <= waited < SETTINGS.browser_queue_wait_seconds + 1
    # The polls start at 0.2 s and rise to 0.5 s (B1).
    assert time.sleeps[0] == pytest.approx(0.2)
    assert max(time.sleeps) == pytest.approx(0.5)


@pytest.mark.parametrize("unblock", [False, True])
@scenario
async def test_wait_gives_up_after_render_timeout(unblock: bool) -> None:
    time = FakeTime()
    settings = Settings(auth_mode="none", browser_enabled=True, browser_unblock_enabled=True)
    parts = Parts(settings, time=time)
    job_id = await parts.dispatcher.submit("extract", ExtractRequest(url=PAGE), USER, "enforce")
    time.now += 9  # claimed just before the queue wait ran out
    await parts.store.mark_running(job_id)

    got = await parts.dispatcher.wait(job_id, unblock=unblock, detached=never_detached)

    assert got is None
    render = (
        settings.browser_unblock_timeout_seconds if unblock else settings.browser_timeout_seconds
    )
    waited = time.now - 109.0
    assert render + 5 <= waited < render + 6


@scenario
async def test_wait_gives_up_when_detached_or_the_job_is_gone() -> None:
    parts = Parts(time=FakeTime())
    job_id = await parts.dispatcher.submit("extract", ExtractRequest(url=PAGE), USER, "enforce")
    handover = Handover()
    handover.detach()

    assert handover.detached is True
    assert await parts.dispatcher.wait(job_id, unblock=False, detached=lambda: True) is None
    await parts.store.delete(job_id)
    assert await parts.dispatcher.wait(job_id, unblock=False, detached=never_detached) is None


@scenario
async def test_abandon_deletes_job() -> None:
    parts = Parts()
    req = ExtractRequest(url=PAGE, cookies=[BrowserCookie(name="s", value="cookie-secret-1")])
    job_id = await parts.dispatcher.submit("extract", req, USER, "enforce", draft=fast_draft())
    assert await parts.job_keys() != []

    await parts.dispatcher.abandon(job_id)

    assert await parts.job_keys() == []
    await parts.dispatcher.abandon(job_id)  # gone already: no error


@scenario
async def test_abandon_swallows_valkey_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    parts = Parts()

    async def down(*args: object, **kwargs: object) -> None:
        raise RedisConnectionError("valkey is down")

    monkeypatch.setattr(parts.store, "delete", down)

    await parts.dispatcher.abandon("f" * 32)  # left to expire


def test_handover_claims_once() -> None:
    handover = Handover()
    assert handover.claimed is None
    assert handover.claim_p1() is True
    assert handover.claim_browser() is False
    assert handover.claimed == "p1"

    other = Handover()
    assert other.claim_browser() is True
    assert other.claim_p1() is False
    assert other.claimed == "browser"
