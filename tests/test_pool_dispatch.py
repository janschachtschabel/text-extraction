"""``PoolDispatcher`` and ``DoclingDispatcher`` (``pool_dispatch.py``) on fakeredis with the real
job store and queues. The browser pool's behaviour is covered by ``test_browser_dispatch.py``."""

import asyncio
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from prometheus_client import CollectorRegistry

from text_extraction.auth import Principal
from text_extraction.browser_dispatch import BrowserDispatcher
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractResult
from text_extraction.job_models import JobOptions
from text_extraction.jobs import JobStore
from text_extraction.pipeline_documents import DocumentInput, DocumentInputMeta, OcrNeeded
from text_extraction.pool_dispatch import (
    DoclingDispatcher,
    OpenLimitReached,
    PoolDispatcher,
    docling_queue,
)
from text_extraction.scheduler import FairQueue
from text_extraction.settings import Settings

ALICE = Principal(name="alice", role="user", rate_limit_per_minute=None)
SETTINGS = Settings(auth_mode="none", docling_enabled=True)
OPTIONS = JobOptions(compliance="enforce")
URL = "https://docs.example.org/scan.pdf"


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

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


def never() -> bool:
    return False


def needed(draft: Envelope[ExtractResult] | None = None) -> OcrNeeded:
    meta = DocumentInputMeta(kind="pdf", content_type="application/pdf", raw_sha256="0" * 64)
    return OcrNeeded(draft=draft, input=DocumentInput(b"%PDF-1.7\n", meta))


class Parts:
    def __init__(self, settings: Settings = SETTINGS) -> None:
        self.valkey = FakeAsyncRedis()
        self.time = FakeTime()
        self.store = JobStore(self.valkey, settings)
        self.queue = docling_queue(self.valkey, settings)
        self.registry = CollectorRegistry()
        self.docling = DoclingDispatcher(
            self.store,
            self.queue,
            settings,
            registry=self.registry,
            clock=self.time.clock,
            sleep=self.time.sleep,
        )

    def count(self, outcome: str) -> float:
        value = self.registry.get_sample_value(
            "text_extraction_docling_dispatch_total", {"outcome": outcome}
        )
        assert value is not None
        return value


@scenario
async def test_a_new_job_is_deleted_when_prepare_fails() -> None:
    parts = Parts()
    dispatcher = PoolDispatcher(
        "docling", parts.store, parts.queue, SETTINGS, CollectorRegistry(), outcomes=()
    )

    async def refuse(job_id: str) -> None:
        raise OpenLimitReached

    with pytest.raises(OpenLimitReached):
        await dispatcher.submit_job("extract", OPTIONS, ALICE, URL, prepare=refuse)

    assert await parts.valkey.keys("job:*") == []
    assert (await parts.queue.counts()).pending == 0


@scenario
async def test_wait_ends_after_total_within_also_once_claimed() -> None:
    parts = Parts()
    job_id = await parts.docling.submit("extract", OPTIONS, ALICE, URL, needed())
    await parts.store.mark_running(job_id, lease_seconds=60)  # claimed, but never answered

    result = await parts.docling.wait(job_id, detached=never)

    assert result is None
    assert 20 <= parts.time.now - 100 <= 21
    assert parts.count("no_result") == 1


@scenario
async def test_wait_with_extra_seconds_waits_longer() -> None:
    parts = Parts()
    job_id = await parts.docling.submit("extract", OPTIONS, ALICE, URL, needed())

    assert await parts.docling.wait(job_id, extra_seconds=30, detached=never) is None

    assert 50 <= parts.time.now - 100 <= 51
    assert parts.count("not_claimed") == 1


@scenario
async def test_submit_stores_input_and_counts_the_open_job() -> None:
    parts = Parts()
    draft = Envelope[ExtractResult](status="partial")

    job_id = await parts.docling.submit("file", OPTIONS, ALICE, None, needed(draft))

    record = await parts.store.get(job_id)
    assert record is not None
    assert (record.kind, record.state) == ("file", "queued")
    assert await parts.store.url(job_id, 0) == ""
    stored = await parts.store.get_input(job_id, 0, DocumentInputMeta)
    assert stored is not None
    assert stored[0] == b"%PDF-1.7\n"
    assert await parts.store.draft(job_id, 0) == draft
    assert await parts.store.open_docling_jobs("alice") == 1
    claim = await parts.queue.claim(60)
    assert claim.item is not None
    assert (claim.item.job_id, claim.item.client, claim.item.domain) == (job_id, "alice#live", "-")


@scenario
async def test_open_limit_refuses_before_any_input_is_stored() -> None:
    parts = Parts(Settings(auth_mode="none", docling_enabled=True, docling_max_open_per_client=1))
    await parts.docling.submit("extract", OPTIONS, ALICE, URL, needed())

    with pytest.raises(OpenLimitReached):
        await parts.docling.submit("extract", OPTIONS, ALICE, URL, needed())

    assert len(await parts.valkey.keys("job:*:meta")) == 1
    assert len(await parts.valkey.keys("job:*:input:*")) == 1
    assert parts.count("open_limit") == 1


@scenario
async def test_a_reused_job_over_the_limit_is_kept() -> None:
    parts = Parts(Settings(auth_mode="none", docling_enabled=True, docling_max_open_per_client=1))
    await parts.docling.submit("extract", OPTIONS, ALICE, URL, needed())
    p1 = await parts.store.create("extract", ALICE, OPTIONS, [URL])
    await parts.store.mark_running(p1, lease_seconds=60)

    with pytest.raises(OpenLimitReached):
        await parts.docling.submit("extract", OPTIONS, ALICE, URL, needed(), job_id=p1)

    record = await parts.store.get(p1)
    assert record is not None
    assert record.lease_until is not None  # still the P1 job's: it stores the fallback
    assert await parts.store.open_docling_jobs("alice") == 1


@scenario
async def test_a_reused_job_is_queued_and_its_lease_cleared() -> None:
    parts = Parts()
    p1 = await parts.store.create("file", ALICE, OPTIONS, [None])
    await parts.store.mark_running(p1, lease_seconds=60)

    assert await parts.docling.submit("file", OPTIONS, ALICE, None, needed(), job_id=p1) == p1

    record = await parts.store.get(p1)
    assert record is not None
    assert (record.state, record.lease_until) == ("running", None)
    assert (await parts.queue.counts()).pending == 1
    assert parts.count("handed_over") == 1


@scenario
async def test_dispatch_counters_of_two_pools_share_a_registry() -> None:
    parts = Parts()
    BrowserDispatcher(
        parts.store, FairQueue(parts.valkey, SETTINGS, "browser"), SETTINGS, registry=parts.registry
    )

    assert (
        parts.registry.get_sample_value(
            "text_extraction_browser_dispatch_total", {"outcome": "rendered"}
        )
        == 0
    )
    assert parts.count("answered") == 0
