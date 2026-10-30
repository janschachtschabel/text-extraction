"""The crawl store (ruling P9-S3): nodes, seen URLs, children and counters of a ``site`` job."""

import asyncio
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis

from text_extraction.auth import Principal
from text_extraction.compliance import ComplianceReport
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractResult, Timings
from text_extraction.job_models import JobOptions
from text_extraction.jobs import JobLost, JobStore
from text_extraction.settings import Settings
from text_extraction.site.store import Candidate, ExtendAnswer, Finished, SiteStore
from text_extraction.site.types import SiteInfo, SiteNode, SiteOptions, SiteScope
from text_extraction.site.urls import seen_key

START = 1_000_000.0
OWNER = Principal(name="alice", role="user", rate_limit_per_minute=None)
OPTIONS = JobOptions(mode="fast", compliance="enforce", ocr="never", site=SiteOptions())
ROOT = "https://www.example.org/"
SCOPE = SiteScope(host="example.org", path_prefix=None)


def scenario(test: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., None]:
    """Run an async test body on its own event loop (the suite has no async plugin)."""

    @wraps(test)
    def run(*args: Any, **kwargs: Any) -> None:
        asyncio.run(test(*args, **kwargs))

    return run


def make(valkey: FakeAsyncRedis, **overrides: Any) -> tuple[JobStore, SiteStore]:
    settings = Settings(auth_mode="none", **overrides)
    jobs = JobStore(valkey, settings, clock=lambda: START)
    return jobs, SiteStore(valkey, jobs, settings)


async def start(valkey: FakeAsyncRedis, **overrides: Any) -> tuple[JobStore, SiteStore, str]:
    jobs, sites = make(valkey, **overrides)
    job_id = await jobs.create("site", OWNER, OPTIONS, [ROOT])
    await sites.init(job_id, ROOT)
    return jobs, sites, job_id


def page(url: str, *, depth: int = 1, parent: int = 0, add: bool = True) -> Candidate:
    return Candidate(
        key=seen_key(url), url=url, node=SiteNode(role="page", depth=depth, parent=parent), add=add
    )


def sitemap(url: str, *, parent: int = 1) -> Candidate:
    return Candidate(
        key=seen_key(url), url=url, node=SiteNode(role="sitemap", depth=0, parent=parent), add=True
    )


def part_result(url: str, part: int) -> Envelope[ExtractResult]:
    result = ExtractResult(
        url=url,
        final_url=url,
        upstream_status=200,
        content_type="application/xml",
        mode_used="fast",
        attempts=1,
        language=None,
        word_count=0,
        title=None,
        format="text",
        content=None,
        quality=None,
        timings=Timings(total_ms=1, compliance_ms=0, fetch_ms=0, convert_ms=1),
        compliance=ComplianceReport(mode="enforce"),
        metadata=None,
        links=None,
        fingerprint=None,
        cache="miss",
        warnings=[],
        site=SiteInfo(role="sitemap_part", depth=0, parent=2, links=None),
    )
    return Envelope[ExtractResult](status="success", data=result)


def part(url: str, number: int) -> Finished:
    return Finished(
        url=url,
        node=SiteNode(role="sitemap_part", depth=0, parent=2, part=number),
        envelope=part_result(url, number),
    )


@scenario
async def test_init_sets_root_node_and_seen() -> None:
    valkey = FakeAsyncRedis()
    _, sites, job_id = await start(valkey)

    context = await sites.context(job_id, 0)

    assert context.node == SiteNode(role="page", depth=0, parent=None)
    assert context.scope is None
    assert await valkey.hget(f"job:{{{job_id}}}:seen", seen_key(ROOT)) == b"0"
    assert await valkey.hget(f"job:{{{job_id}}}:site", "pages") == b"1"
    # http, https and www. are one page: the root is seen under every spelling.
    answer = await sites.extend(
        job_id, 0, [page("http://example.org/"), page("https://example.org")], max_pages=10
    )
    assert answer.indexes == [0, 0]
    assert answer.new == []
    unknown = await sites.context(job_id, 7)
    assert unknown.node is None


@scenario
async def test_init_on_gone_job_raises_job_lost() -> None:
    _, sites = make(FakeAsyncRedis())
    with pytest.raises(JobLost):
        await sites.init("f" * 32, ROOT)


@scenario
async def test_extend_appends_new_answers_seen() -> None:
    valkey = FakeAsyncRedis()
    jobs, sites, job_id = await start(valkey)
    new_url = "https://example.org/a"

    answer = await sites.extend(job_id, 0, [page(ROOT), page(new_url)], max_pages=10)

    assert answer == ExtendAnswer(indexes=[0, 1], new=[(1, new_url)], capped=False, repeat=False)
    record = await jobs.get(job_id)
    assert record is not None
    assert record.progress.total == 2
    assert record.progress.done == 0
    assert await jobs.url(job_id, 1) == new_url
    context = await sites.context(job_id, 1)
    assert context.node == SiteNode(role="page", depth=1, parent=0)
    assert await valkey.hget(f"job:{{{job_id}}}:site", "pages") == b"2"
    # The new item is seen from now on: the next page's link to it answers its index.
    later = await sites.extend(job_id, 1, [page("http://www.example.org/a", depth=2)], max_pages=9)
    assert later.indexes == [1]
    assert later.new == []


async def seen_index(valkey: FakeAsyncRedis, job_id: str, key: str) -> int | None:
    raw = await valkey.hget(f"job:{{{job_id}}}:seen", key)
    return int(raw) if raw is not None else None


@scenario
async def test_extend_caps_pages_and_files() -> None:
    valkey = FakeAsyncRedis()
    jobs, sites, job_id = await start(valkey, site_max_sitemap_files=2)
    urls = [f"https://example.org/p{n}" for n in range(3)]

    answer = await sites.extend(job_id, 0, [page(url) for url in urls], max_pages=2)

    assert answer.indexes == [1, None, None]
    assert answer.new == [(1, urls[0])]
    assert answer.capped
    assert await seen_index(valkey, job_id, seen_key(urls[1])) is None  # a capped one is not seen

    # The robots item counts against no cap; sitemaps against SITE_MAX_SITEMAP_FILES.
    robots_url = "https://example.org/robots.txt"
    robots = Candidate(
        key=seen_key(robots_url),
        url=robots_url,
        node=SiteNode(role="robots", depth=0, parent=0),
        add=True,
    )
    jobs, sites, job_id = await start(valkey, site_max_sitemap_files=2)
    first = await sites.extend(job_id, 0, [robots], max_pages=1)
    assert first.indexes == [1]
    assert not first.capped
    maps = [f"https://example.org/sitemap-{n}.xml" for n in range(3)]

    answer = await sites.extend(job_id, 1, [sitemap(url) for url in maps], max_pages=1)

    assert answer.indexes == [2, 3, None]
    assert answer.new == [(2, maps[0]), (3, maps[1])]
    assert answer.capped
    assert await valkey.hget(f"job:{{{job_id}}}:site", "files") == b"2"
    record = await jobs.get(job_id)
    assert record is not None
    assert record.progress.total == 4
    assert (await sites.context(job_id, 2)).node == SiteNode(role="sitemap", depth=0, parent=1)


@scenario
async def test_sitemaps_and_pages_are_seen_apart() -> None:
    """A sitemap file and a page at the same URL are two items: sitemaps are seen under
    ``s:<key>``, and the caller passes the plain seen key for both."""
    valkey = FakeAsyncRedis()
    _, sites, job_id = await start(valkey)
    url = "https://example.org/feed.xml"

    answer = await sites.extend(job_id, 0, [page(url), sitemap(url, parent=0)], max_pages=10)

    assert answer.indexes == [1, 2]
    assert await seen_index(valkey, job_id, seen_key(url)) == 1
    assert await seen_index(valkey, job_id, f"s:{seen_key(url)}") == 2


@scenario
async def test_duplicate_candidates_in_one_call_are_one_item() -> None:
    valkey = FakeAsyncRedis()
    jobs, sites, job_id = await start(valkey)
    url = "https://example.org/a"

    answer = await sites.extend(job_id, 0, [page(url), page(url)], max_pages=10)

    assert answer.indexes == [1, 1]
    assert answer.new == [(1, url)]
    record = await jobs.get(job_id)
    assert record is not None
    assert record.progress.total == 2


@scenario
async def test_lookup_only_candidate_never_appends() -> None:
    valkey = FakeAsyncRedis()
    jobs, sites, job_id = await start(valkey)
    deep = "https://example.org/deep"

    answer = await sites.extend(
        job_id, 0, [page(ROOT, add=False), page(deep, add=False)], max_pages=10
    )

    assert answer == ExtendAnswer(indexes=[0, None], new=[], capped=False, repeat=False)
    record = await jobs.get(job_id)
    assert record is not None
    assert record.progress.total == 1
    assert await seen_index(valkey, job_id, seen_key(deep)) is None


@scenario
async def test_aliases_point_to_parent() -> None:
    valkey = FakeAsyncRedis()
    _, sites, job_id = await start(valkey)
    old, new = "https://example.org/old", "https://example.org/new"
    await sites.extend(job_id, 0, [page(old)], max_pages=10)

    # Item 1 redirected to /new: /new is item 1 from now on, under every spelling.
    await sites.extend(job_id, 1, [], aliases=[new, ROOT], max_pages=10)

    assert await seen_index(valkey, job_id, seen_key(new)) == 1
    assert await seen_index(valkey, job_id, seen_key("http://www.example.org/new")) == 1
    # An alias never takes the key of another item: the first claim stays.
    assert await seen_index(valkey, job_id, seen_key(ROOT)) == 0


@scenario
async def test_finished_items_counted_done() -> None:
    valkey = FakeAsyncRedis()
    jobs, sites, job_id = await start(valkey)
    map_url = "https://example.org/sitemap.xml"
    await sites.extend(
        job_id, 0, [page("https://example.org/a"), sitemap(map_url, parent=0)], max_pages=10
    )
    child = "https://example.org/b"

    answer = await sites.extend(
        job_id,
        2,
        [page(child, parent=2)],
        finished=[part(map_url, 2), part(map_url, 3)],
        max_pages=10,
    )

    # The finished parts come first, then the new children; only the children are queued.
    assert answer.indexes == [5]
    assert answer.new == [(5, child)]
    record = await jobs.get(job_id)
    assert record is not None
    assert record.progress.total == 6
    assert record.progress.done == 2
    assert record.progress.success == 2
    assert record.state != "completed"
    items = await jobs.items(job_id, 3, 3)
    assert [item.state for item in items] == ["done", "done", "pending"]
    assert [item.url for item in items] == [map_url, map_url, child]
    assert items[0].result == part_result(map_url, 2)
    assert await jobs.has_result(job_id, 4)
    assert (await sites.context(job_id, 4)).node == SiteNode(
        role="sitemap_part", depth=0, parent=2, part=3
    )
    # Parts are no files: they count against no cap, and are not seen.
    assert await valkey.hget(f"job:{{{job_id}}}:site", "files") == b"1"
    assert await seen_index(valkey, job_id, f"s:{seen_key(map_url)}") == 2
    # A finished item has its done bit: a later write of it is a repeat.
    assert await jobs.write_result(job_id, 3, part_result(map_url, 2)) == "repeat"


@scenario
async def test_job_not_completed_while_parent_pending() -> None:
    valkey = FakeAsyncRedis()
    jobs, sites, job_id = await start(valkey)
    child = "https://example.org/a"
    await sites.extend(job_id, 0, [page(child)], finished=[part(ROOT, 2)], max_pages=10)

    # Item 1 is the finished part, item 2 the child.
    assert await jobs.write_result(job_id, 2, part_result(child, 1)) == "first"

    record = await jobs.get(job_id)
    assert record is not None
    assert record.progress.done == 2
    assert record.state != "completed"
    assert await jobs.write_result(job_id, 0, part_result(ROOT, 1)) == "last"
    record = await jobs.get(job_id)
    assert record is not None
    assert record.state == "completed"
    assert record.progress.done == record.progress.total == 3


@scenario
async def test_extend_repeat_returns_stored_answer() -> None:
    """Review Focus 4: a parent that runs again (a lost lease) gets the answer of its first
    run and appends nothing, so its children can be queued again without new items."""
    valkey = FakeAsyncRedis()
    jobs, sites, job_id = await start(valkey)
    urls = ["https://example.org/a", "https://example.org/b"]
    first = await sites.extend(
        job_id, 0, [page(url) for url in urls], finished=[part(ROOT, 2)], max_pages=2
    )
    assert first == ExtendAnswer(indexes=[2, None], new=[(2, urls[0])], capped=True, repeat=False)

    again = await sites.extend(
        job_id,
        0,
        [page(url) for url in [*urls, "https://example.org/c"]],
        finished=[part(ROOT, 2)],
        max_pages=100,
    )

    assert again == ExtendAnswer(indexes=first.indexes, new=first.new, capped=True, repeat=True)
    record = await jobs.get(job_id)
    assert record is not None
    assert record.progress.total == 3
    assert record.progress.done == 1
    assert await sites.stored_children(job_id, 0) == [(2, urls[0])]
    assert await sites.stored_children(job_id, 2) == []


@scenario
async def test_extend_after_the_parent_has_its_result_appends_nothing() -> None:
    """A worker whose lease was taken back may finish after the item was failed (exhausted):
    its links count no more, so nothing is appended to a job that may be completed."""
    valkey = FakeAsyncRedis()
    jobs, sites, job_id = await start(valkey)
    assert await jobs.write_result(job_id, 0, part_result(ROOT, 1)) == "last"

    answer = await sites.extend(
        job_id,
        0,
        [page(ROOT), page("https://example.org/a")],
        finished=[part(ROOT, 2)],
        max_pages=10,
    )

    assert answer == ExtendAnswer(indexes=[0, None], new=[], capped=False, repeat=True)
    record = await jobs.get(job_id)
    assert record is not None
    assert record.state == "completed"
    assert record.progress.total == 1
    assert await sites.stored_children(job_id, 0) == []


@scenario
async def test_extend_rejects_a_parent_outside_the_job() -> None:
    _, sites, job_id = await start(FakeAsyncRedis())
    with pytest.raises(ValueError, match="outside"):
        await sites.extend(job_id, 1, [page("https://example.org/a")], max_pages=10)


@scenario
async def test_reserve_entries_idempotent_and_capped() -> None:
    valkey = FakeAsyncRedis()
    _, sites, job_id = await start(valkey, site_max_sitemap_urls=100)
    await sites.extend(
        job_id,
        0,
        [sitemap(f"https://example.org/s{n}.xml", parent=0) for n in range(3)],
        max_pages=10,
    )

    assert await sites.reserve_entries(job_id, 1, 60) == 60
    assert await sites.reserve_entries(job_id, 1, 60) == 60  # a rerun gets its grant again
    assert await sites.reserve_entries(job_id, 1, 10) == 60
    assert await sites.reserve_entries(job_id, 2, 60) == 40  # only what is left
    assert await sites.reserve_entries(job_id, 3, 60) == 0
    assert await valkey.hget(f"job:{{{job_id}}}:site", "entries") == b"100"
    with pytest.raises(JobLost):
        await sites.reserve_entries("f" * 32, 1, 10)


@scenario
async def test_scope_set_once() -> None:
    _, sites, job_id = await start(FakeAsyncRedis())

    await sites.extend(job_id, 0, [page("https://example.org/a")], max_pages=10, scope=SCOPE)
    other = SiteScope(host="other.example", path_prefix="/x/")
    await sites.extend(job_id, 1, [], max_pages=10, scope=other)

    assert (await sites.context(job_id, 1)).scope == SCOPE
    assert (await sites.context(job_id, 0)).scope == SCOPE


@scenario
async def test_keys_expire_with_job_and_delete_removes_them() -> None:
    valkey = FakeAsyncRedis()
    jobs, sites, job_id = await start(valkey, job_ttl_seconds=3600)
    await valkey.expire(f"job:{{{job_id}}}:meta", 1800)  # the job has half its life left
    await sites.extend(
        job_id,
        0,
        [page("https://example.org/a"), sitemap("https://example.org/s.xml", parent=0)],
        finished=[part(ROOT, 2)],
        max_pages=10,
        scope=SCOPE,
    )
    await sites.reserve_entries(job_id, 2, 5)

    for kind in ("nodes", "seen", "children", "site", "results", "done"):
        ttl = await valkey.ttl(f"job:{{{job_id}}}:{kind}")
        assert 1795 <= ttl <= 1800, kind

    await jobs.delete(job_id)

    assert [key async for key in valkey.scan_iter(match=f"job:{{{job_id}}}:*")] == []


@scenario
async def test_extend_on_gone_job_raises_job_lost() -> None:
    valkey = FakeAsyncRedis()
    _, sites, job_id = await start(valkey)
    await valkey.delete(f"job:{{{job_id}}}:meta")

    with pytest.raises(JobLost):
        await sites.extend(job_id, 0, [page("https://example.org/a")], max_pages=10)

    assert await valkey.exists(f"job:{{{job_id}}}:children") == 0
    assert await valkey.llen(f"job:{{{job_id}}}:urls") == 1


def test_scripts_carry_script_flags() -> None:
    """The scripts start with ``#!lua``, as the queue's: in cluster mode the server then
    refuses a key outside the job's slot."""
    from text_extraction.site import store

    for name in ("_INIT", "_EXTEND", "_RESERVE_ENTRIES"):
        assert getattr(store, name).startswith("#!lua\n"), name
