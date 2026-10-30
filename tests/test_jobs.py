import asyncio
import json
import re
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from pydantic import BaseModel

from text_extraction.auth import Principal
from text_extraction.browser_options import BrowserCookie, BrowserSecrets, ClickAction
from text_extraction.chunking.types import Chunk
from text_extraction.compliance import ComplianceReport
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.extract_models import ExtractResult, Timings
from text_extraction.job_models import JobOptions, JobProgress
from text_extraction.jobs import (
    InputBudgetExceeded,
    JobLost,
    JobStore,
    _options_json,
    browser_lease_seconds,
)
from text_extraction.settings import Settings
from text_extraction.site.types import SiteOptions
from text_extraction.structured.types import StructuredResult, Table
from text_extraction.vision.types import DescribeImagesOptions

START = 1_000_000.0
OWNER = Principal(name="alice", role="user", rate_limit_per_minute=None)
OPTIONS = JobOptions(mode="fast", format="text", metadata=False, links=True, compliance="report")
URLS = [f"https://example.org/page-{n}" for n in range(5)]
BROWSER_OPTIONS = JobOptions(mode="browser", compliance="enforce")
SECRETS = BrowserSecrets(
    actions=[ClickAction(type="click", selector="#more")],
    cookies=[BrowserCookie(name="session", value="cookie-secret-1")],
    headers={"authorization": "Bearer header-secret-1"},
)
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


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
async def test_write_result_counts_once() -> None:
    store = make_store(FakeAsyncRedis())
    job_id = await create_job(store)
    first = success(URLS[1], "first")
    second = success(URLS[1], "second")

    assert await store.write_result(job_id, 1, first) == "first"
    assert await store.write_result(job_id, 1, second) == "repeat"

    record = await store.get(job_id)
    assert record is not None
    assert record.progress == JobProgress(total=3, done=1, success=1, partial=0, failed=0)
    [item] = await store.items(job_id, 1, 1)
    assert item.result is not None
    assert item.result.data is not None
    assert item.result.data.content == "first"


@scenario
async def test_write_result_counts_each_status() -> None:
    store = make_store(FakeAsyncRedis())
    job_id = await create_job(store)
    partial = Envelope[ExtractResult](status="partial", data=make_result(URLS[1]))

    await store.write_result(job_id, 0, success())
    await store.write_result(job_id, 1, partial)
    await store.write_result(job_id, 2, failure())

    record = await store.get(job_id)
    assert record is not None
    assert record.progress == JobProgress(total=3, done=3, success=1, partial=1, failed=1)


@scenario
async def test_write_result_tells_which_write_completed_the_job() -> None:
    store = make_store(FakeAsyncRedis())
    job_id = await create_job(store, URLS[:2])

    assert await store.write_result(job_id, 1, success(URLS[1])) == "first"
    assert await store.write_result(job_id, 1, success(URLS[1])) == "repeat"
    assert await store.write_result(job_id, 0, success()) == "last"
    assert await store.write_result(job_id, 0, success()) == "repeat"


@scenario
async def test_write_result_rejects_an_index_outside_the_job() -> None:
    store = make_store(FakeAsyncRedis())
    job_id = await create_job(store)
    for index in (-1, 3):
        with pytest.raises(ValueError, match="index"):
            await store.write_result(job_id, index, success())
    record = await store.get(job_id)
    assert record is not None
    assert record.progress.done == 0


@scenario
async def test_last_result_completes_the_job() -> None:
    valkey = FakeAsyncRedis()
    clock = Clock()
    store = make_store(valkey, clock, job_ttl_seconds=600)
    job_id = await create_job(store)
    await store.write_result(job_id, 0, success())
    await store.write_result(job_id, 2, failure())

    record = await store.get(job_id)
    assert record is not None
    assert record.state == "queued"
    assert record.finished_at is None

    keys = [key async for key in valkey.scan_iter(match=f"job:{{{job_id}}}:*")]
    assert len(keys) == 4
    for key in keys:
        await valkey.expire(key, 30)  # so the reset to the full TTL is visible

    clock.now += 5
    await store.write_result(job_id, 1, success(URLS[1]))

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

    await store.write_result(job_id, 0, success())
    for kind in ("results", "done"):
        assert 0 < await valkey.ttl(f"job:{{{job_id}}}:{kind}") <= 600


@scenario
async def test_items_pages_in_index_order() -> None:
    store = make_store(FakeAsyncRedis())
    job_id = await create_job(store, URLS)
    await store.write_result(job_id, 3, success(URLS[3]))
    await store.write_result(job_id, 0, success(URLS[0]))

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

    assert await store.write_result(job_id, 0, big) == "first"

    [item] = await store.items(job_id, 0, 1)
    assert item.result is not None
    assert item.result.status == "success"
    assert item.result.data is not None
    assert item.result.data.content is None
    assert "content_not_stored_too_large" in item.result.data.warnings
    assert big.data is not None
    assert big.data.content is not None  # the caller's envelope is not changed
    assert big.data.warnings == []


def chunked(content: str, *, chunks: int, tables: int = 0) -> Envelope[ExtractResult]:
    """A success whose chunks repeat its content ``chunks`` times, with ``tables`` tables of
    the same size (as overlapping chunks and big tables do)."""
    envelope = success(content=content)
    assert envelope.data is not None
    data = envelope.data.model_copy(
        update={
            "chunks": [
                Chunk(index=index, text=content, start=0, end=len(content), headings=[])
                for index in range(chunks)
            ],
            "structured": StructuredResult(
                values={"title": "T"},
                sources={"title": "metadata"},
                stage="done",
                tables=[
                    Table(index=index, caption=None, headers=[], rows=[[content]])
                    for index in range(tables)
                ],
            ),
        }
    )
    return envelope.model_copy(update={"data": data})


@scenario
async def test_oversized_result_drops_chunks_and_tables_after_the_content() -> None:
    """I1 (ruling R20): JOB_ITEM_MAX_BYTES bounds what is stored. Chunks repeat the content
    (with overlap many times over), so a result still over the limit without its content loses
    its chunks and its tables too, each with a warning of its own."""
    valkey = FakeAsyncRedis()
    store = make_store(valkey, job_item_max_bytes=10_000)
    job_id = await create_job(store)
    big = chunked("a" * 3_000, chunks=4, tables=2)

    await store.write_result(job_id, 0, big)

    [item] = await store.items(job_id, 0, 1)
    assert item.result is not None
    data = item.result.data
    assert data is not None
    assert data.content is None
    assert data.chunks is None
    assert data.structured is not None
    assert data.structured.tables is None
    assert data.structured.values == {"title": "T"}  # the values stay
    assert data.warnings == [
        "content_not_stored_too_large",
        "chunks_not_stored_too_large",
        "tables_not_stored_too_large",
    ]
    raw = await valkey.hget(f"job:{{{job_id}}}:results", "0")
    assert raw is not None
    assert len(raw) <= 10_000


@scenario
async def test_oversized_result_keeps_its_chunks_when_the_content_was_enough() -> None:
    store = make_store(FakeAsyncRedis(), job_item_max_bytes=10_000)
    job_id = await create_job(store)

    await store.write_result(job_id, 0, chunked("a" * 6_000, chunks=1))

    [item] = await store.items(job_id, 0, 1)
    assert item.result is not None
    data = item.result.data
    assert data is not None
    assert data.content is None
    assert data.chunks is not None
    assert len(data.chunks) == 1
    assert data.warnings == ["content_not_stored_too_large"]


@scenario
async def test_result_at_the_limit_keeps_its_content() -> None:
    store = make_store(FakeAsyncRedis(), job_item_max_bytes=10_000)
    job_id = await create_job(store)

    await store.write_result(job_id, 0, success(content="a" * 500))

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
        await store.write_result(job_id, 0, success())
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
    await store.write_result(job_id, 0, success())

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
    job_id = await store.create("extract", OWNER, BROWSER_OPTIONS, URLS[:1], secrets=SECRETS)
    await store.put_draft(job_id, 0, success())
    await store.put_artifact(job_id, "screenshot.png", PNG)
    await store.write_result(job_id, 0, success())

    keys = sorted([key.decode() async for key in valkey.scan_iter(match="job:*")])

    assert keys == sorted(
        f"job:{{{job_id}}}:{kind}"
        for kind in (
            "meta",
            "urls",
            "results",
            "done",
            "secrets",
            "drafts",
            "artifact:screenshot.png",
        )
    )
    assert {re.search(r"\{([^}]*)\}", key).group(1) for key in keys} == {job_id}  # type: ignore[union-attr]


@scenario
async def test_create_keeps_the_creators_trace_id() -> None:
    store = make_store(FakeAsyncRedis())
    traced = await store.create("batch", OWNER, OPTIONS, URLS[:1], trace_id="trace-42")
    plain = await create_job(store)

    traced_record = await store.get(traced)
    plain_record = await store.get(plain)

    assert traced_record is not None
    assert traced_record.trace_id == "trace-42"
    assert plain_record is not None
    assert plain_record.trace_id is None


# --- browser jobs: secrets, drafts and artifacts -------------------------------------------------


@scenario
async def test_secrets_round_trip_and_ttl() -> None:
    valkey = FakeAsyncRedis()
    store = make_store(
        valkey, browser_timeout_seconds=30, browser_queue_wait_seconds=10, job_ttl_seconds=86400
    )
    job_id = await store.create("extract", OWNER, BROWSER_OPTIONS, URLS[:1], secrets=SECRETS)

    assert await store.secrets(job_id) == SECRETS
    ttl = await valkey.ttl(f"job:{{{job_id}}}:secrets")
    # B6: the queue wait, then the browser lease (2 * the unblock deadline 60 + 10), plus 60.
    assert 10 + 130 + 60 - 5 <= ttl <= 10 + 130 + 60

    await store.drop_secrets(job_id)

    assert await store.secrets(job_id) == BrowserSecrets()
    assert await valkey.exists(f"job:{{{job_id}}}:secrets") == 0
    assert await store.secrets("f" * 32) == BrowserSecrets()  # an unknown job has none


@pytest.mark.parametrize(("browser", "unblock"), [(30, 60), (30, 180), (120, 60), (120, 10)])
@scenario
async def test_secrets_outlive_a_crashed_render_until_its_rerun(browser: int, unblock: int) -> None:
    # M-2: a worker that dies at the claim gives the item back once the lease ends; the rerun
    # must still find the secrets, also for an unblock render and for a long standard one.
    valkey = FakeAsyncRedis()
    settings = Settings(
        auth_mode="none",
        browser_timeout_seconds=browser,
        browser_unblock_timeout_seconds=unblock,
        browser_queue_wait_seconds=10,
    )
    store = JobStore(valkey, settings, clock=Clock())
    job_id = await store.create("extract", OWNER, BROWSER_OPTIONS, URLS[:1], secrets=SECRETS)

    ttl = await valkey.ttl(f"job:{{{job_id}}}:secrets")

    lease = browser_lease_seconds(settings)
    assert lease == 2 * max(browser, unblock) + 10
    assert ttl >= 10 + lease + 30  # claimed at the end of the queue wait, then a crash
    assert ttl >= 10 + 2 * max(browser, unblock) + 30  # two whole renders


@pytest.mark.parametrize(
    ("browser", "unblock", "lease"), [(30, 60, 130), (120, 60, 250), (120, 10, 250), (5, 180, 370)]
)
def test_the_browser_lease_covers_two_of_the_longest_render(
    browser: int, unblock: int, lease: int
) -> None:
    # A standard render may be the longer one (BROWSER_TIMEOUT_SECONDS above the unblock
    # deadline): a shorter lease would offer the item again while it is still rendering.
    settings = Settings(
        auth_mode="none",
        browser_timeout_seconds=browser,
        browser_unblock_timeout_seconds=unblock,
    )

    assert browser_lease_seconds(settings) == lease
    assert browser_lease_seconds(settings) > 2 * browser


@scenario
async def test_empty_secrets_store_no_key() -> None:
    valkey = FakeAsyncRedis()
    store = make_store(valkey)
    job_id = await store.create(
        "extract", OWNER, BROWSER_OPTIONS, URLS[:1], secrets=BrowserSecrets()
    )

    assert await valkey.exists(f"job:{{{job_id}}}:secrets") == 0
    assert (await store.secrets(job_id)).empty()


@scenario
async def test_secrets_not_in_job_view() -> None:
    store = make_store(FakeAsyncRedis())
    job_id = await store.create("extract", OWNER, BROWSER_OPTIONS, URLS[:1], secrets=SECRETS)
    await store.write_result(job_id, 0, success())

    record = await store.get(job_id)
    items = await store.items(job_id, 0, 10)

    shown = repr(record) + "".join(item.model_dump_json() for item in items)
    for secret in ("cookie-secret-1", "header-secret-1", "#more", "authorization"):
        assert secret not in shown
    assert "cookie-secret-1" not in repr(await store.secrets(job_id))  # not even in a repr


@scenario
async def test_draft_round_trip() -> None:
    valkey = FakeAsyncRedis()
    clock = Clock()
    store = make_store(valkey, clock, job_ttl_seconds=600)
    job_id = await create_job(store)
    clock.now += 100
    draft = success(URLS[1], "A draft of the page")

    await store.put_draft(job_id, 1, draft)

    assert await store.draft(job_id, 1) == draft
    assert await store.draft(job_id, 0) is None
    assert await store.draft("f" * 32, 1) is None
    # The drafts live as long as the job: they take its remaining TTL.
    assert 0 < await valkey.ttl(f"job:{{{job_id}}}:drafts") <= 600


@scenario
async def test_draft_of_an_expired_job_raises_job_lost() -> None:
    store = make_store(FakeAsyncRedis())
    with pytest.raises(JobLost):
        await store.put_draft("f" * 32, 0, success())


@scenario
async def test_artifact_round_trip_and_ttl() -> None:
    valkey = FakeAsyncRedis()
    store = make_store(valkey, artifact_ttl_seconds=3600)
    job_id = await create_job(store, kind="screenshot")

    expires_at = await store.put_artifact(job_id, "screenshot.png", PNG)

    assert await store.artifact(job_id, "screenshot.png") == PNG
    assert await store.artifact(job_id, "other.png") is None
    assert await store.artifact("f" * 32, "screenshot.png") is None
    assert 3595 <= await valkey.ttl(f"job:{{{job_id}}}:artifact:screenshot.png") <= 3600
    assert expires_at.timestamp() == START + 3600
    assert expires_at.utcoffset() is not None


@scenario
async def test_artifact_needs_a_job_and_a_safe_name() -> None:
    store = make_store(FakeAsyncRedis())
    job_id = await create_job(store)

    with pytest.raises(JobLost):
        await store.put_artifact("f" * 32, "screenshot.png", PNG)
    for name in ("../meta", "Screenshot.PNG", "", "a" * 65, "x:y"):
        with pytest.raises(ValueError, match="artifact name"):
            await store.put_artifact(job_id, name, PNG)
        with pytest.raises(ValueError, match="artifact name"):
            await store.artifact(job_id, name)


@scenario
async def test_delete_removes_secrets_drafts_and_artifacts() -> None:
    valkey = FakeAsyncRedis()
    store = make_store(valkey)
    job_id = await store.create("extract", OWNER, BROWSER_OPTIONS, URLS[:1], secrets=SECRETS)
    await store.put_draft(job_id, 0, success())
    await store.put_artifact(job_id, "screenshot.png", PNG)
    await store.write_result(job_id, 0, success())

    await store.delete(job_id)

    assert [key async for key in valkey.scan_iter(match="job:*")] == []


@scenario
async def test_shorten_cuts_the_ttl_and_drops_secrets_and_drafts() -> None:
    valkey = FakeAsyncRedis()
    store = make_store(valkey, job_ttl_seconds=86400, artifact_ttl_seconds=3600)
    job_id = await store.create("screenshot", OWNER, BROWSER_OPTIONS, URLS[:1], secrets=SECRETS)
    await store.put_draft(job_id, 0, success())
    await store.put_artifact(job_id, "screenshot.png", PNG)
    await store.write_result(job_id, 0, success())

    await store.shorten(job_id, 600)

    for kind in ("meta", "urls", "results", "done", "artifact:screenshot.png"):
        assert 595 <= await valkey.ttl(f"job:{{{job_id}}}:{kind}") <= 600, kind
    assert await valkey.exists(f"job:{{{job_id}}}:secrets", f"job:{{{job_id}}}:drafts") == 0
    record = await store.get(job_id)
    assert record is not None
    assert record.state == "completed"
    assert await store.artifact(job_id, "screenshot.png") == PNG

    await store.shorten(job_id, 7200)  # never longer than before
    assert await valkey.ttl(f"job:{{{job_id}}}:meta") <= 600
    await store.shorten("f" * 32, 600)  # an unknown job: nothing happens
    assert await valkey.exists(f"job:{{{'f' * 32}}}:meta") == 0


@scenario
async def test_drop_draft_removes_one_items_draft() -> None:
    store = make_store(FakeAsyncRedis())
    job_id = await create_job(store)
    await store.put_draft(job_id, 0, success(URLS[0]))
    await store.put_draft(job_id, 1, success(URLS[1]))

    await store.drop_draft(job_id, 0)

    assert await store.draft(job_id, 0) is None
    assert await store.draft(job_id, 1) is not None
    await store.drop_draft("f" * 32, 0)  # an unknown job: no error


@scenario
async def test_clear_lease_keeps_the_job_running_without_a_claim() -> None:
    clock = Clock()
    store = make_store(FakeAsyncRedis(), clock)
    job_id = await create_job(store)
    await store.mark_running(job_id, lease_seconds=30)

    await store.clear_lease(job_id)

    record = await store.get(job_id)
    assert record is not None
    assert (record.state, record.lease_until) == ("running", None)
    with pytest.raises(JobLost):
        await store.clear_lease("f" * 32)


@scenario
async def test_has_result_follows_the_done_bit() -> None:
    store = make_store(FakeAsyncRedis())
    job_id = await create_job(store)

    assert not await store.has_result(job_id, 0)
    await store.write_result(job_id, 1, success())

    assert await store.has_result(job_id, 1)
    assert not await store.has_result(job_id, 0)
    assert not await store.has_result("f" * 32, 1)


async def _secrets_flag() -> None:
    store = JobStore(FakeAsyncRedis(), Settings(auth_mode="none"))
    options = JobOptions(compliance="enforce")
    plain = await store.create("extract", OWNER, options, URLS[:1])
    empty = await store.create("extract", OWNER, options, URLS[:1], secrets=BrowserSecrets())
    carrying = await store.create(
        "extract",
        OWNER,
        options,
        URLS[:1],
        secrets=BrowserSecrets(headers={"x-token": "header-secret-9"}),
    )
    records = [await store.get(job_id) for job_id in (plain, empty, carrying)]
    assert [record.has_secrets if record else None for record in records] == [False, False, True]
    await store.drop_secrets(carrying)
    record = await store.get(carrying)
    assert record is not None
    assert record.has_secrets is True  # the flag outlives the secrets


def test_has_secrets_is_recorded_and_outlives_the_secrets() -> None:
    asyncio.run(_secrets_flag())


# --- the caller's LLM key (ruling S9) ----------------------------------------------------------


@scenario
async def test_llm_key_round_trip_and_ttl() -> None:
    valkey = FakeAsyncRedis()
    store = make_store(valkey, job_ttl_seconds=86400)
    job_id = await create_job(store, kind="extract")

    assert await store.llm_key(job_id) is None
    await store.put_llm_key(job_id, "sk-caller")
    assert await store.llm_key(job_id) == "sk-caller"
    ttl = await valkey.ttl(f"job:{{{job_id}}}:llm_key")
    assert 86400 - 5 <= ttl <= 86400  # the draft it belongs to lives as long as the job

    await store.drop_llm_key(job_id)

    assert await store.llm_key(job_id) is None
    assert await valkey.exists(f"job:{{{job_id}}}:llm_key") == 0
    await store.drop_llm_key(job_id)  # nothing to drop is not an error
    assert await store.llm_key("f" * 32) is None  # an unknown job has none


@scenario
async def test_llm_key_never_outlives_the_job() -> None:
    valkey = FakeAsyncRedis()
    store = make_store(valkey, job_ttl_seconds=600)
    job_id = await create_job(store, kind="extract")
    await valkey.expire(f"job:{{{job_id}}}:meta", 100)

    await store.put_llm_key(job_id, "sk-caller")

    assert await valkey.ttl(f"job:{{{job_id}}}:llm_key") <= 100


@scenario
async def test_llm_key_of_a_gone_job_is_not_stored() -> None:
    valkey = FakeAsyncRedis()
    store = make_store(valkey)

    with pytest.raises(JobLost):
        await store.put_llm_key("f" * 32, "sk-caller")

    assert await valkey.keys("job:*") == []


@scenario
async def test_drop_secrets_keeps_llm_key() -> None:
    """The browser worker drops its secrets and the ASR key when it has finished its item; the
    LLM key is for the llm worker, which comes later (ruling S13)."""
    valkey = FakeAsyncRedis()
    store = make_store(valkey)
    job_id = await store.create("extract", OWNER, BROWSER_OPTIONS, URLS[:1], secrets=SECRETS)
    await store.put_asr_key(job_id, "sk-asr")
    await store.put_llm_key(job_id, "sk-llm")

    await store.drop_secrets(job_id)

    assert await store.secrets(job_id) == BrowserSecrets()
    assert await store.asr_key(job_id) is None
    assert await store.llm_key(job_id) == "sk-llm"


@scenario
async def test_delete_and_shorten_remove_the_llm_key() -> None:
    valkey = FakeAsyncRedis()
    store = make_store(valkey)
    deleted = await create_job(store, kind="extract")
    shortened = await create_job(store, kind="extract")
    for job_id in (deleted, shortened):
        await store.put_llm_key(job_id, "sk-llm")

    await store.delete(deleted)
    await store.shorten(shortened, 30)

    assert await valkey.keys("job:*:llm_key") == []


# --- inputs and URL-less items (ruling F12) ---------------------------------------------------


class Meta(BaseModel):
    kind: str
    name: str | None = None


@scenario
async def test_input_is_stored_read_and_deleted() -> None:
    valkey = FakeAsyncRedis()
    store = JobStore(valkey, Settings(auth_mode="none"))
    job_id = await store.create("file", OWNER, OPTIONS, [None])

    await store.put_input(job_id, 0, PNG, Meta(kind="png", name="a.png"), 600)

    assert await store.get_input(job_id, 0, Meta) == (PNG, Meta(kind="png", name="a.png"))
    assert await store.get_input(job_id, 1, Meta) is None
    assert 0 < await valkey.ttl(f"job:{{{job_id}}}:input:0") <= 600
    await store.delete_input(job_id, 0)
    assert await store.get_input(job_id, 0, Meta) is None


@scenario
async def test_input_never_outlives_its_job() -> None:
    valkey = FakeAsyncRedis()
    store = JobStore(valkey, Settings(auth_mode="none", job_ttl_seconds=300))
    job_id = await store.create("file", OWNER, OPTIONS, [None])

    await store.put_input(job_id, 0, PNG, Meta(kind="png"), 3600)

    assert 0 < await valkey.ttl(f"job:{{{job_id}}}:input:0") <= 300


@scenario
async def test_input_of_a_gone_job_is_not_stored() -> None:
    valkey = FakeAsyncRedis()
    store = JobStore(valkey, Settings(auth_mode="none"))

    with pytest.raises(JobLost):
        await store.put_input("f" * 32, 0, PNG, Meta(kind="png"), 600)

    assert await valkey.keys("*") == []


@scenario
async def test_delete_and_shorten_remove_the_input() -> None:
    valkey = FakeAsyncRedis()
    store = JobStore(valkey, Settings(auth_mode="none"))
    deleted = await store.create("file", OWNER, OPTIONS, [None])
    shortened = await store.create("file", OWNER, OPTIONS, [None])
    for job_id in (deleted, shortened):
        await store.put_input(job_id, 0, PNG, Meta(kind="png"), 600)

    await store.delete(deleted)
    await store.shorten(shortened, 30)

    assert await store.get_input(deleted, 0, Meta) is None
    assert await store.get_input(shortened, 0, Meta) is None


@scenario
async def test_an_upload_item_has_no_url() -> None:
    store = JobStore(FakeAsyncRedis(), Settings(auth_mode="none"))
    job_id = await store.create("file", OWNER, OPTIONS, [None])

    (item,) = await store.items(job_id, 0, 10)
    assert item.url is None
    assert await store.url(job_id, 0) == ""  # the item exists, without a URL
    assert await store.url(job_id, 1) is None


@scenario
async def test_open_docling_jobs_count_only_unfinished_ones() -> None:
    clock = Clock()
    store = JobStore(FakeAsyncRedis(), Settings(auth_mode="none"), clock=clock)
    open_job, done_job, gone_job = [
        await store.create("extract", OWNER, OPTIONS, [URLS[0]]) for _ in range(3)
    ]
    for job_id in (open_job, done_job, gone_job):
        assert await store.add_docling_job("alice", job_id) >= 1
    await store.write_result(done_job, 0, Envelope[ExtractResult](status="failed"))
    await store.delete(gone_job)

    assert await store.open_docling_jobs("alice") == 1
    assert await store.open_docling_jobs("bob") == 0
    await store.drop_docling_job("alice", open_job)
    assert await store.open_docling_jobs("alice") == 0


# --- rolling upgrades: the stored options of package 4 ------------------------------------------

# The options a package-4 pod knows (``JobOptions`` there is ``extra="forbid"``).
PACKAGE_4_OPTIONS = {
    "mode",
    "format",
    "metadata",
    "links",
    "compliance",
    "cache",
    "screenshot",
    "full_page",
}


async def stored_options(valkey: FakeAsyncRedis, job_id: str) -> dict[str, Any]:
    raw = await valkey.hget(f"job:{{{job_id}}}:meta", "options")
    assert raw is not None
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


@scenario
async def test_default_ocr_is_not_stored_so_older_pods_read_the_job() -> None:
    valkey = FakeAsyncRedis()
    store = make_store(valkey)
    job_id = await store.create("batch", OWNER, JobOptions(compliance="enforce"), URLS[:1])

    assert set(await stored_options(valkey, job_id)) == PACKAGE_4_OPTIONS
    record = await store.get(job_id)
    assert record is not None
    assert record.options.ocr == "auto"


@scenario
async def test_an_ocr_choice_is_stored() -> None:
    valkey = FakeAsyncRedis()
    store = make_store(valkey)
    options = JobOptions(compliance="enforce", ocr="always")
    job_id = await store.create("batch", OWNER, options, URLS[:1])

    assert (await stored_options(valkey, job_id))["ocr"] == "always"
    record = await store.get(job_id)
    assert record is not None
    assert record.options == options


@scenario
async def test_a_job_an_older_pod_stored_reads_with_ocr_auto() -> None:
    valkey = FakeAsyncRedis()
    store = make_store(valkey)
    job_id = await create_job(store)
    old = {"mode": "fast", "format": "text", "metadata": False, "links": True}
    old.update({"compliance": "report", "cache": True, "screenshot": False, "full_page": True})
    await valkey.hset(f"job:{{{job_id}}}:meta", "options", json.dumps(old))

    record = await store.get(job_id)

    assert record is not None
    assert record.options == OPTIONS
    assert record.options.ocr == "auto"


# --- the input budget (DOCLING_INPUT_BUDGET_BYTES) ----------------------------------------------

BUDGET = 10_000
BUDGET_TOTAL = "inputs:{budget}:total"
BUDGET_EXPIRY = "inputs:{budget}:expiry"


class InputMeta(BaseModel):
    kind: str = "pdf"


def budget_store(valkey: FakeAsyncRedis) -> JobStore:
    return make_store(valkey, docling_input_budget_bytes=BUDGET, document_max_bytes=BUDGET)


async def budget_used(valkey: FakeAsyncRedis) -> int:
    return int(await valkey.get(BUDGET_TOTAL) or 0)


def input_size(body: bytes) -> int:
    return len(body) + len(InputMeta().model_dump_json())


@scenario
async def test_inputs_over_the_budget_are_refused() -> None:
    valkey = FakeAsyncRedis()
    store = budget_store(valkey)
    first, second = await create_job(store), await create_job(store)

    await store.put_input(first, 0, b"x" * 6_000, InputMeta(), 600)
    with pytest.raises(InputBudgetExceeded):
        await store.put_input(second, 0, b"y" * 6_000, InputMeta(), 600)

    assert await store.get_input(second, 0, InputMeta) is None  # nothing was stored
    assert await budget_used(valkey) == input_size(b"x" * 6_000)
    await store.put_input(second, 1, b"z" * 3_000, InputMeta(), 600)  # what is left still fits


@scenario
async def test_a_deleted_input_gives_its_bytes_back() -> None:
    valkey = FakeAsyncRedis()
    store = budget_store(valkey)
    first, second, third = [await create_job(store) for _ in range(3)]
    for job_id in (first, second, third):
        await store.put_input(job_id, 0, b"x" * 9_000, InputMeta(), 600)
        assert await budget_used(valkey) == input_size(b"x" * 9_000)
        if job_id == first:
            await store.delete_input(job_id, 0)  # the item has its outcome
        elif job_id == second:
            await store.delete(job_id)  # a sync job answered
        else:
            await store.shorten(job_id, 60)  # a sync job with a screenshot

    assert await budget_used(valkey) == 0
    assert await valkey.zcard(BUDGET_EXPIRY) == 0


@scenario
async def test_an_input_that_expired_leaves_the_budget() -> None:
    """An input whose TTL ran out is not deleted by anyone: the next write prunes it by its
    expiry, so the budget does not leak."""
    valkey = FakeAsyncRedis()
    store = budget_store(valkey)
    first, second = await create_job(store), await create_job(store)
    await store.put_input(first, 0, b"x" * 9_000, InputMeta(), 600)
    # Its TTL runs out: the key is gone, and its expiry lies in the past.
    await valkey.delete(f"job:{{{first}}}:input:0")
    await valkey.zadd(BUDGET_EXPIRY, {f"{first}:0": 1})

    await store.put_input(second, 0, b"y" * 9_000, InputMeta(), 600)

    assert await budget_used(valkey) == input_size(b"y" * 9_000)
    assert await valkey.zrange(BUDGET_EXPIRY, 0, -1) == [f"{second}:0".encode()]


@scenario
async def test_a_replaced_input_counts_once() -> None:
    valkey = FakeAsyncRedis()
    store = budget_store(valkey)
    job_id = await create_job(store)

    await store.put_input(job_id, 0, b"x" * 6_000, InputMeta(), 600)
    await store.put_input(job_id, 0, b"y" * 7_000, InputMeta(), 600)  # an upload's, then OCR's

    assert await budget_used(valkey) == input_size(b"y" * 7_000)


@scenario
async def test_a_lost_job_takes_nothing_from_the_budget() -> None:
    valkey = FakeAsyncRedis()
    store = budget_store(valkey)

    with pytest.raises(JobLost):
        await store.put_input("0" * 32, 0, b"x" * 9_000, InputMeta(), 600)

    assert await budget_used(valkey) == 0


@scenario
async def test_the_budget_holds_under_concurrent_writes() -> None:
    valkey = FakeAsyncRedis()
    store = budget_store(valkey)
    jobs = [await create_job(store) for _ in range(10)]

    outcomes = await asyncio.gather(
        *(store.put_input(job_id, 0, b"x" * 2_000, InputMeta(), 600) for job_id in jobs),
        return_exceptions=True,
    )

    stored = [outcome for outcome in outcomes if outcome is None]
    refused = [outcome for outcome in outcomes if isinstance(outcome, InputBudgetExceeded)]
    assert len(stored) == BUDGET // input_size(b"x" * 2_000)
    assert len(stored) + len(refused) == len(jobs)
    assert await budget_used(valkey) <= BUDGET


@scenario
async def test_a_describe_images_choice_is_stored_and_read_back() -> None:
    """Issue #20 (V-S1): the option is stored when set, and a job without it keeps the keys of
    the release before it (``test_default_ocr_is_not_stored_so_older_pods_read_the_job``)."""
    valkey = FakeAsyncRedis()
    store = make_store(valkey)
    options = JobOptions(compliance="enforce", describe_images=DescribeImagesOptions())
    job_id = await store.create("batch", OWNER, options, URLS[:1])

    assert (await stored_options(valkey, job_id))["describe_images"] == {"max_images": 5}
    record = await store.get(job_id)
    assert record is not None
    assert record.options == options


def test_job_options_omit_unset_site() -> None:
    """P9-S1: pods of the release before package 9 parse the options strictly, so a job that is
    not a site job stores no ``site`` key."""
    stored = _options_json(JobOptions(compliance="enforce"))
    assert "site" not in json.loads(stored)
    assert JobOptions.model_validate_json(stored).site is None
    # The workers build the request of the other kinds from the dump; it has no ``site``.
    assert "site" not in JobOptions(compliance="enforce").model_dump()

    options = JobOptions(
        mode="fast", compliance="enforce", ocr="never", site=SiteOptions(depth=3, scope="path")
    )
    stored = _options_json(options)

    assert json.loads(stored)["site"] == {
        "depth": 3,
        "max_pages": 100,
        "sitemaps": True,
        "scope": "path",
        "metadata": True,
        "cache": True,
    }
    assert JobOptions.model_validate_json(stored) == options


@scenario
async def test_site_job_round_trip() -> None:
    store = make_store(FakeAsyncRedis())
    options = JobOptions(mode="fast", compliance="enforce", ocr="never", site=SiteOptions())

    job_id = await store.create("site", OWNER, options, ["https://a.example/"])

    job = await store.get(job_id)
    assert job is not None
    assert job.kind == "site"
    assert job.options.site == SiteOptions()
