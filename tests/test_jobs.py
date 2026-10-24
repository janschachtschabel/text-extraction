import asyncio
import re
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis

from text_extraction.auth import Principal
from text_extraction.compliance import ComplianceReport
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.extract_models import ExtractResult, Timings
from text_extraction.job_models import JobOptions, JobProgress
from text_extraction.jobs import JobLost, JobStore
from text_extraction.settings import Settings

START = 1_000_000.0
OWNER = Principal(name="alice", role="user", rate_limit_per_minute=None)
OPTIONS = JobOptions(mode="fast", format="text", metadata=False, links=True, compliance="report")
URLS = [f"https://example.org/page-{n}" for n in range(5)]


def scenario(test: Callable[[], Coroutine[Any, Any, None]]) -> Callable[[], None]:
    """Run an async test body on its own event loop (the suite has no async plugin)."""

    @wraps(test)
    def run() -> None:
        asyncio.run(test())

    return run


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        return self.now


def make_result(url: str = URLS[0], content: str | None = "Hello world") -> ExtractResult:
    return ExtractResult(
        url=url,
        final_url=url,
        upstream_status=200,
        content_type="text/html",
        mode_used="fast",
        attempts=1,
        language="en",
        word_count=2,
        title=None,
        format="text",
        content=content,
        quality=None,
        timings=Timings(total_ms=1, compliance_ms=0, fetch_ms=0, convert_ms=1),
        compliance=ComplianceReport(mode="report"),
        metadata=None,
        links=None,
        fingerprint=None,
        cache="miss",
        warnings=[],
    )


def success(url: str = URLS[0], content: str | None = "Hello world") -> Envelope[ExtractResult]:
    return Envelope[ExtractResult](status="success", data=make_result(url, content))


def failure() -> Envelope[ExtractResult]:
    return Envelope[ExtractResult](
        status="failed",
        error=ErrorInfo(code=ErrorCode.TIMEOUT, message="Timed out", retryable=True),
    )


def make_store(valkey: FakeAsyncRedis, clock: Clock | None = None, **overrides: Any) -> JobStore:
    settings = Settings(auth_mode="none", **overrides)
    return JobStore(valkey, settings, clock=clock or Clock())


async def create_job(store: JobStore, urls: list[str] | None = None, kind: Any = "batch") -> str:
    return await store.create(kind, OWNER, OPTIONS, urls if urls is not None else URLS[:3])


@scenario
async def test_create_and_get_round_trip() -> None:
    store = make_store(FakeAsyncRedis())
    job_id = await create_job(store)

    assert re.fullmatch(r"[0-9a-f]{32}", job_id)
    record = await store.get(job_id)
    assert record is not None
    assert record.id == job_id
    assert record.kind == "batch"
    assert record.owner == "alice"
    assert record.owner_role == "user"
    assert record.options == OPTIONS
    assert record.state == "queued"
    assert record.progress == JobProgress(total=3, done=0, success=0, partial=0, failed=0)
    assert record.created_at.timestamp() == START
    assert record.created_at.utcoffset() is not None
    assert record.finished_at is None
    assert record.lease_until is None
    assert await store.url(job_id, 0) == URLS[0]
    assert await store.url(job_id, 2) == URLS[2]
    assert await store.url(job_id, 3) is None
    assert await store.url(job_id, -1) is None


@scenario
async def test_create_rejects_an_empty_url_list() -> None:
    store = make_store(FakeAsyncRedis())
    with pytest.raises(ValueError, match="at least one URL"):
        await store.create("batch", OWNER, OPTIONS, [])


@scenario
async def test_store_result_counts_once() -> None:
    store = make_store(FakeAsyncRedis())
    job_id = await create_job(store)
    first = success(URLS[1], "first")
    second = success(URLS[1], "second")

    assert await store.store_result(job_id, 1, first) is True
    assert await store.store_result(job_id, 1, second) is False

    record = await store.get(job_id)
    assert record is not None
    assert record.progress == JobProgress(total=3, done=1, success=1, partial=0, failed=0)
    [item] = await store.items(job_id, 1, 1)
    assert item.result is not None
    assert item.result.data is not None
    assert item.result.data.content == "first"


@scenario
async def test_store_result_counts_each_status() -> None:
    store = make_store(FakeAsyncRedis())
    job_id = await create_job(store)
    partial = Envelope[ExtractResult](status="partial", data=make_result(URLS[1]))

    await store.store_result(job_id, 0, success())
    await store.store_result(job_id, 1, partial)
    await store.store_result(job_id, 2, failure())

    record = await store.get(job_id)
    assert record is not None
    assert record.progress == JobProgress(total=3, done=3, success=1, partial=1, failed=1)


@scenario
async def test_store_result_rejects_an_index_outside_the_job() -> None:
    store = make_store(FakeAsyncRedis())
    job_id = await create_job(store)
    for index in (-1, 3):
        with pytest.raises(ValueError, match="index"):
            await store.store_result(job_id, index, success())
    record = await store.get(job_id)
    assert record is not None
    assert record.progress.done == 0


@scenario
async def test_last_result_completes_the_job() -> None:
    valkey = FakeAsyncRedis()
    clock = Clock()
    store = make_store(valkey, clock, job_ttl_seconds=600)
    job_id = await create_job(store)
    await store.store_result(job_id, 0, success())
    await store.store_result(job_id, 2, failure())

    record = await store.get(job_id)
    assert record is not None
    assert record.state == "queued"
    assert record.finished_at is None

    keys = [key async for key in valkey.scan_iter(match=f"job:{{{job_id}}}:*")]
    assert len(keys) == 4
    for key in keys:
        await valkey.expire(key, 30)  # so the reset to the full TTL is visible

    clock.now += 5
    await store.store_result(job_id, 1, success(URLS[1]))

    record = await store.get(job_id)
    assert record is not None
    assert record.state == "completed"
    assert record.finished_at is not None
    assert record.finished_at.timestamp() == START + 5
    assert record.progress == JobProgress(total=3, done=3, success=2, partial=0, failed=1)
    for key in keys:
        assert 595 <= await valkey.ttl(key) <= 600


@scenario
async def test_creation_sets_the_ttl() -> None:
    valkey = FakeAsyncRedis()
    store = make_store(valkey, job_ttl_seconds=600)
    job_id = await create_job(store)
    assert 595 <= await valkey.ttl(f"job:{{{job_id}}}:meta") <= 600
    assert 595 <= await valkey.ttl(f"job:{{{job_id}}}:urls") <= 600

    await store.store_result(job_id, 0, success())
    for kind in ("results", "done"):
        assert 0 < await valkey.ttl(f"job:{{{job_id}}}:{kind}") <= 600


@scenario
async def test_items_pages_in_index_order() -> None:
    store = make_store(FakeAsyncRedis())
    job_id = await create_job(store, URLS)
    await store.store_result(job_id, 3, success(URLS[3]))
    await store.store_result(job_id, 0, success(URLS[0]))

    items = await store.items(job_id, 2, 2)

    assert [item.index for item in items] == [2, 3]
    assert [item.url for item in items] == [URLS[2], URLS[3]]
    assert [item.state for item in items] == ["pending", "done"]
    assert items[0].result is None
    assert items[1].result is not None
    assert items[1].result.status == "success"


@scenario
async def test_items_past_the_end_are_absent() -> None:
    store = make_store(FakeAsyncRedis())
    job_id = await create_job(store, URLS)

    assert [item.index for item in await store.items(job_id, 3, 10)] == [3, 4]
    assert await store.items(job_id, 5, 10) == []
    assert await store.items(job_id, 0, 0) == []
    assert await store.items("0" * 32, 0, 10) == []
    with pytest.raises(ValueError, match="offset"):
        await store.items(job_id, -1, 10)


@scenario
async def test_large_batches_are_stored_completely() -> None:
    store = make_store(FakeAsyncRedis())
    urls = [f"https://example.org/{n}" for n in range(2500)]
    job_id = await create_job(store, urls)

    assert await store.url(job_id, 2499) == urls[2499]
    items = await store.items(job_id, 2400, 200)
    assert [item.index for item in items] == list(range(2400, 2500))


@scenario
async def test_oversized_result_is_stored_without_content() -> None:
    store = make_store(FakeAsyncRedis(), job_item_max_bytes=10_000)
    job_id = await create_job(store)
    big = success(content="ä" * 6_000)  # 12,000 bytes as UTF-8, 6,000 characters

    assert await store.store_result(job_id, 0, big) is True

    [item] = await store.items(job_id, 0, 1)
    assert item.result is not None
    assert item.result.status == "success"
    assert item.result.data is not None
    assert item.result.data.content is None
    assert "content_not_stored_too_large" in item.result.data.warnings
    assert big.data is not None
    assert big.data.content is not None  # the caller's envelope is not changed
    assert big.data.warnings == []


@scenario
async def test_result_at_the_limit_keeps_its_content() -> None:
    store = make_store(FakeAsyncRedis(), job_item_max_bytes=10_000)
    job_id = await create_job(store)

    await store.store_result(job_id, 0, success(content="a" * 500))

    [item] = await store.items(job_id, 0, 1)
    assert item.result is not None
    assert item.result.data is not None
    assert item.result.data.content == "a" * 500
    assert item.result.data.warnings == []


@scenario
async def test_unknown_or_expired_job_is_none() -> None:
    valkey = FakeAsyncRedis()
    store = make_store(valkey)
    assert await store.get("f" * 32) is None
    assert await store.url("f" * 32, 0) is None

    job_id = await create_job(store)
    await valkey.delete(f"job:{{{job_id}}}:meta")
    assert await store.get(job_id) is None


@scenario
async def test_writing_to_an_expired_job_raises_job_lost() -> None:
    valkey = FakeAsyncRedis()
    store = make_store(valkey)
    job_id = await create_job(store)
    await valkey.delete(f"job:{{{job_id}}}:meta")

    with pytest.raises(JobLost):
        await store.store_result(job_id, 0, success())
    with pytest.raises(JobLost):
        await store.mark_running(job_id)
    assert [key async for key in valkey.scan_iter(match=f"job:{{{job_id}}}:results")] == []
    assert [key async for key in valkey.scan_iter(match=f"job:{{{job_id}}}:done")] == []


@scenario
async def test_mark_running_sets_state_and_lease() -> None:
    store = make_store(FakeAsyncRedis())
    job_id = await create_job(store)

    await store.mark_running(job_id, lease_seconds=10)

    record = await store.get(job_id)
    assert record is not None
    assert record.state == "running"
    assert record.lease_until == START + 10

    await store.mark_running(job_id)  # no lease
    record = await store.get(job_id)
    assert record is not None
    assert record.state == "running"
    assert record.lease_until is None


@scenario
async def test_mark_running_does_not_reopen_a_completed_job() -> None:
    store = make_store(FakeAsyncRedis())
    job_id = await create_job(store, URLS[:1])
    await store.store_result(job_id, 0, success())

    await store.mark_running(job_id, lease_seconds=10)

    record = await store.get(job_id)
    assert record is not None
    assert record.state == "completed"


@scenario
async def test_running_job_with_expired_lease() -> None:
    valkey = FakeAsyncRedis()
    clock = Clock()
    store = make_store(valkey, clock)
    job_id = await create_job(store)
    await store.mark_running(job_id, lease_seconds=10)

    clock.now += 11
    record = await store.get(job_id)

    assert record is not None
    assert record.state == "running"  # the API decides what an expired lease means
    assert record.lease_until is not None
    assert record.lease_until < clock()


@scenario
async def test_all_job_keys_share_one_hash_tag() -> None:
    valkey = FakeAsyncRedis()
    store = make_store(valkey)
    job_id = await create_job(store)
    await store.store_result(job_id, 0, success())

    keys = sorted([key.decode() async for key in valkey.scan_iter(match="job:*")])

    assert keys == sorted(
        f"job:{{{job_id}}}:{kind}" for kind in ("meta", "urls", "results", "done")
    )
    assert {re.search(r"\{([^}]*)\}", key).group(1) for key in keys} == {job_id}  # type: ignore[union-attr]
