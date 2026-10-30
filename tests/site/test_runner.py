"""The site runner (rulings P9-S6, P9-S7, P9-S11, P9-S12; Review Focus 1-5): a ``site`` job's
pages, robots.txt and sitemaps as items of a growing job, run by the real worker on fakeredis
with a fake fetcher serving a small site, the queue read back after each item."""

import asyncio
import os
from collections.abc import Callable, Coroutine, Iterator
from functools import wraps
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
from fakeredis import FakeAsyncRedis
from prometheus_client import CollectorRegistry
from pydantic import TypeAdapter

from tests.fakes import PARAGRAPH, Answer, FakeFetcher, Redirect, RefusingGate, fetched
from text_extraction.auth import Principal
from text_extraction.compliance import ComplianceChecker, ComplianceMode
from text_extraction.conversion_pool import ConversionPool
from text_extraction.envelope import ErrorCode
from text_extraction.extract_models import ExtractResult
from text_extraction.fetch import Fetcher, FetchError, FetchResult
from text_extraction.fingerprint import FingerprintStore
from text_extraction.job_models import JobItem, JobOptions
from text_extraction.jobs import JobStore, job_key
from text_extraction.media.pool import MediaFetcher
from text_extraction.netguard import BlockedAddressError, is_public_address, resolve_allowed
from text_extraction.outbound import DomainGate, OutboundBusy
from text_extraction.pipeline import Pipeline
from text_extraction.result_cache import ResultCache
from text_extraction.scheduler import FairQueue, QueueItem
from text_extraction.settings import Settings
from text_extraction.site import runner as runner_module
from text_extraction.site.runner import SiteRunner
from text_extraction.site.store import SiteStore
from text_extraction.site.types import SiteInfo, SiteLink, SitemapEntry, SiteOptions, SiteScope
from text_extraction.worker import ExtractRunner, Worker

OWNER = Principal(name="alice", role="user", rate_limit_per_minute=None)
SITE = "https://lernen.example.org"
ROOT = f"{SITE}/"
ROBOTS = f"{SITE}/robots.txt"
ENTRIES = TypeAdapter(list[SitemapEntry])


def scenario(test: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., None]:
    @wraps(test)
    def run(*args: Any, **kwargs: Any) -> None:
        asyncio.run(test(*args, **kwargs))

    return run


def html(*links: str, body: str = PARAGRAPH * 3, head: str = "") -> str:
    anchors = "".join(f'<a href="{href}">Link {n}</a> ' for n, href in enumerate(links))
    return (
        f"<!doctype html><html><head><title>Page</title>{head}</head><body><main>"
        f"<h1>Page</h1><p>{body}</p><p>{anchors}</p></main></body></html>"
    )


def page(url: str, *links: str, **options: Any) -> FetchResult:
    return fetched(url, html(*links, **options))


def text(url: str, body: str | bytes, content_type: str = "application/xml") -> FetchResult:
    return fetched(url, body, content_type=content_type)


def urlset(*locs: str) -> str:
    urls = "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'


def index(*locs: str) -> str:
    maps = "".join(f"<sitemap><loc>{loc}</loc></sitemap>" for loc in locs)
    return (
        f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{maps}</sitemapindex>'
    )


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class Pressure:
    high = False


async def no_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


_POOLS: list[ConversionPool] = []


@pytest.fixture(autouse=True)
def _shut_pools_down() -> Iterator[None]:
    """Each test's conversion pools are shut down after it."""
    yield
    while _POOLS:
        _POOLS.pop().shutdown()


class Crawl:
    """A worker with the extract and site runners over the real pipeline, job store, crawl
    store and queue on fakeredis, and a fake fetcher."""

    def __init__(
        self,
        answers: dict[str, Answer] | None = None,
        *,
        fetcher: FakeFetcher | None = None,
        media_fetcher: Any = None,
        gate: Any = None,
        **overrides: Any,
    ) -> None:
        values: dict[str, Any] = {"auth_mode": "none", "outbound_min_delay_ms": 0}
        values.update(overrides)
        self.settings = Settings(**values)
        self.valkey = FakeAsyncRedis()
        self.clock = Clock()
        self.registry = CollectorRegistry()
        self.fetcher = fetcher if fetcher is not None else FakeFetcher(answers or {})
        used = cast(Fetcher, self.fetcher)
        self.pool = ConversionPool(2)
        _POOLS.append(self.pool)
        self.pipeline = Pipeline(
            used,
            gate if gate is not None else DomainGate(self.valkey, self.settings),
            ComplianceChecker(used, self.valkey, self.settings),
            ResultCache(self.valkey, self.settings),
            FingerprintStore(self.valkey, self.settings),
            self.settings,
            self.registry,
            self.pool,
            media_fetcher=cast(MediaFetcher | None, media_fetcher),
        )
        self.store = JobStore(self.valkey, self.settings, clock=self.clock)
        self.sites = SiteStore(self.valkey, self.store, self.settings)
        self.queue = FairQueue(self.valkey, self.settings, clock=self.clock)
        self.guard = Pressure()
        self.site = SiteRunner(
            self.pipeline,
            self.store,
            self.sites,
            self.queue,
            self.settings,
            self.pool,
            guard=self.guard,
            registry=self.registry,
        )
        self.runner = ExtractRunner(
            self.pipeline,
            self.store,
            self.settings,
            None,
            guard=self.guard,
            registry=self.registry,
            site=self.site,
        )
        self.worker = Worker(
            self.settings,
            self.valkey,
            self.runner,
            self.store,
            self.queue,
            self.registry,
            clock=self.clock,
            sleep=no_sleep,
            random=lambda: 0.5,
        )
        self.lease_seconds = self.runner.lease_seconds

    async def start(
        self, url: str = ROOT, *, compliance: ComplianceMode = "enforce", **options: Any
    ) -> str:
        job_options = JobOptions(
            mode="fast",
            format="text",
            metadata=options.pop("metadata", True),
            compliance=compliance,
            site=SiteOptions(**options),
        )
        job_id = await self.store.create("site", OWNER, job_options, [url])
        await self.sites.init(job_id, url)
        await self.queue.enqueue_items(job_id, OWNER.name, [(0, url)])
        return job_id

    async def claim(self) -> QueueItem | None:
        claim = await self.queue.claim(self.lease_seconds)
        return claim.item

    async def step(self) -> QueueItem | None:
        """Claim and process one item; None when nothing is claimable."""
        claim = await self.queue.claim(self.lease_seconds)
        if claim.item is None:
            return None
        await self.worker.process(claim.item, exhausted=claim.exhausted)
        return claim.item

    async def crawl(self, *, limit: int = 2_000) -> int:
        """Process items until none is left; the number processed."""
        for count in range(limit):
            if await self.step() is None:
                return count
        raise AssertionError("the crawl did not end")

    async def items(self, job_id: str) -> list[JobItem]:
        return await self.store.items(job_id, 0, 100_000)

    async def state(self, job_id: str) -> str:
        record = await self.store.get(job_id)
        assert record is not None
        return record.state

    async def queued(self, domain: str = "lernen.example.org") -> list[str]:
        raw = await self.valkey.lrange(f"q:{{extract}}:c:alice:d:{domain}", 0, -1)
        return [os.fsdecode(value) for value in raw]

    async def stored_size(self, job_id: str, index: int) -> int:
        raw = await self.valkey.hget(job_key(job_id, "results"), str(index))
        assert raw is not None
        return len(raw)

    def sample(self, name: str, labels: dict[str, str]) -> float:
        return self.registry.get_sample_value(name, labels) or 0.0


def data(item: JobItem) -> ExtractResult:
    assert item.result is not None
    assert item.result.data is not None
    return item.result.data


def site(item: JobItem) -> SiteInfo:
    found = data(item).site
    assert found is not None
    return found


def links(item: JobItem) -> list[SiteLink]:
    found = site(item).links
    assert found is not None
    return found


def link_to(item: JobItem, url: str) -> SiteLink:
    [found] = [link for link in links(item) if link.url == url]
    return found


def by_url(items: list[JobItem], url: str) -> JobItem:
    [found] = [item for item in items if item.url == url and site(item).role != "sitemap_part"]
    return found


def roles(items: list[JobItem]) -> list[str]:
    return [site(item).role for item in items]


# --- pages ---------------------------------------------------------------------------------------


@scenario
async def test_root_appends_robots_then_links_and_sets_scope() -> None:
    crawl = Crawl({ROOT: page(ROOT, "/a", "/b", "https://other.example/x", "/a")})
    job_id = await crawl.start(depth=1)

    await crawl.step()

    items = await crawl.items(job_id)
    assert [item.url for item in items] == [ROOT, ROBOTS, f"{SITE}/a", f"{SITE}/b"]
    assert [item.state for item in items] == ["done", "pending", "pending", "pending"]
    assert await crawl.queued() == [f"{job_id}:{n}:0" for n in (1, 2, 3)]
    context = await crawl.sites.context(job_id, 1)
    assert context.scope == SiteScope(host="lernen.example.org", path_prefix=None)
    assert context.node is not None
    assert (context.node.role, context.node.parent) == ("robots", 0)
    root = items[0]
    assert [(link.crawl, link.target) for link in links(root)] == [
        ("queued", 2),
        ("queued", 3),
        ("out_of_scope", None),
    ]
    assert await crawl.state(job_id) == "running"


@scenario
async def test_targets_and_seen_marks() -> None:
    crawl = Crawl(
        {
            ROOT: page(ROOT, "/a"),
            f"{SITE}/a": page(f"{SITE}/a", "/", "http://www.lernen.example.org/a", "/b"),
            f"{SITE}/b": page(f"{SITE}/b", "/a"),
        }
    )
    job_id = await crawl.start(depth=3)

    await crawl.crawl()

    items = await crawl.items(job_id)
    assert await crawl.state(job_id) == "completed"
    a = by_url(items, f"{SITE}/a")
    assert site(a).depth == 1
    assert site(a).parent == 0
    assert (link_to(a, ROOT).crawl, link_to(a, ROOT).target) == ("seen", 0)
    own = link_to(a, "http://www.lernen.example.org/a")
    assert (own.crawl, own.target) == ("seen", a.index)
    b = by_url(items, f"{SITE}/b")
    assert (link_to(a, f"{SITE}/b").crawl, link_to(a, f"{SITE}/b").target) == ("queued", b.index)
    assert (link_to(b, f"{SITE}/a").crawl, link_to(b, f"{SITE}/a").target) == ("seen", a.index)
    pages = [item.url for item in items if site(item).role == "page"]
    assert len(pages) == len(set(pages)) == 3


@scenario
async def test_path_scope_follows_only_the_start_directory() -> None:
    start = f"{SITE}/mathe/index.html"
    crawl = Crawl(
        {
            start: page(start, "/physik/", "/mathe/b", "b2", "/", "/mathematik"),
            f"{SITE}/mathe/b": page(f"{SITE}/mathe/b"),
            f"{SITE}/mathe/b2": page(f"{SITE}/mathe/b2"),
        }
    )
    job_id = await crawl.start(start, depth=1, scope="path", sitemaps=False)

    await crawl.crawl()

    items = await crawl.items(job_id)
    assert await crawl.state(job_id) == "completed"
    assert [item.url for item in items] == [start, f"{SITE}/mathe/b", f"{SITE}/mathe/b2"]
    context = await crawl.sites.context(job_id, 1)
    assert context.scope == SiteScope(host="lernen.example.org", path_prefix="/mathe/")
    decisions = {link.url: (link.crawl, link.target) for link in links(items[0])}
    assert decisions == {
        f"{SITE}/physik/": ("out_of_scope", None),
        f"{SITE}/mathe/b": ("queued", 1),
        f"{SITE}/mathe/b2": ("queued", 2),
        ROOT: ("out_of_scope", None),
        f"{SITE}/mathematik": ("out_of_scope", None),  # a prefix of the path, not the directory
    }
    assert [url for url, _ in crawl.fetcher.calls if "physik" in url or url == ROOT] == []


@scenario
async def test_depth_limit_looks_up_only() -> None:
    crawl = Crawl(
        {
            ROOT: page(ROOT, "/a"),
            f"{SITE}/a": page(f"{SITE}/a", "/deep", "/"),
        }
    )
    job_id = await crawl.start(depth=1, sitemaps=False)

    await crawl.crawl()

    items = await crawl.items(job_id)
    assert [item.url for item in items] == [ROOT, f"{SITE}/a"]
    a = items[1]
    assert (link_to(a, f"{SITE}/deep").crawl, link_to(a, f"{SITE}/deep").target) == ("depth", None)
    assert (link_to(a, ROOT).crawl, link_to(a, ROOT).target) == ("seen", 0)
    assert f"{SITE}/deep" not in [url for url, _ in crawl.fetcher.calls]


@scenario
async def test_depth_zero_fetches_the_start_page_and_sitemaps_only() -> None:
    crawl = Crawl({ROOT: page(ROOT, "/a")})
    job_id = await crawl.start(depth=0)

    await crawl.crawl()

    items = await crawl.items(job_id)
    assert roles(items) == ["page", "robots", "sitemap"]
    assert link_to(items[0], f"{SITE}/a").crawl == "depth"


@scenario
async def test_content_and_links_null_in_page_result() -> None:
    crawl = Crawl({ROOT: page(ROOT, "/a")})
    job_id = await crawl.start(depth=0, sitemaps=False)

    await crawl.crawl()

    [root] = await crawl.items(job_id)
    assert root.result is not None
    assert root.result.status == "success"
    result = data(root)
    assert result.content is None
    assert result.links is None
    assert result.title == "Page"
    assert result.metadata is not None
    assert result.quality is not None
    assert result.mode_used == "fast"
    assert site(root).role == "page"
    assert (site(root).depth, site(root).parent) == (0, None)


@scenario
async def test_failed_quality_page_still_followed() -> None:
    shell = (
        "<!doctype html><html><head><title>App</title></head><body>"
        '<div id="root"></div><noscript>Please enable JavaScript.</noscript>'
        '<script src="/app.js"></script><script>boot()</script>'
        '<nav><a href="/a">A</a></nav></body></html>'
    )
    crawl = Crawl({ROOT: fetched(ROOT, shell), f"{SITE}/a": page(f"{SITE}/a")})
    job_id = await crawl.start(depth=1, sitemaps=False)

    await crawl.crawl()

    items = await crawl.items(job_id)
    root = items[0]
    assert root.result is not None
    assert root.result.status == "failed"
    assert data(root).quality is not None
    assert data(root).quality.js_heavy  # type: ignore[union-attr]
    assert (link_to(root, f"{SITE}/a").crawl, link_to(root, f"{SITE}/a").target) == ("queued", 1)
    assert [item.url for item in items] == [ROOT, f"{SITE}/a"]


@scenario
async def test_failed_fetch_has_no_links_but_root_still_appends_robots() -> None:
    crawl = Crawl({ROOT: fetched(ROOT, "Gone", status=500)})
    job_id = await crawl.start(depth=2)

    await crawl.crawl()

    items = await crawl.items(job_id)
    assert roles(items) == ["page", "robots", "sitemap"]
    root = items[0]
    assert root.result is not None
    assert root.result.status == "failed"
    assert site(root).links is None
    assert await crawl.state(job_id) == "completed"


@scenario
async def test_platform_video_start_page_is_a_page_without_links() -> None:
    from tests.test_pipeline_media import YT, FakeMediaFetcher, probe

    crawl = Crawl({}, media_fetcher=FakeMediaFetcher(probe()))
    job_id = await crawl.start(YT, depth=2)

    await crawl.step()

    items = await crawl.items(job_id)
    video = items[0]
    assert site(video).role == "page"
    assert site(video).links is None
    assert data(video).content is None
    assert [item.url for item in items] == [YT, "https://www.youtube.com/robots.txt"]


# --- robots.txt and sitemaps ---------------------------------------------------------------------


@scenario
async def test_robots_item_lists_and_guesses() -> None:
    robots = f"User-agent: *\nAllow: /\nSitemap: {SITE}/one.xml\nSitemap: /two.xml\n"
    crawl = Crawl(
        {
            ROOT: page(ROOT),
            ROBOTS: text(ROBOTS, robots, "text/plain"),
            f"{SITE}/one.xml": text(f"{SITE}/one.xml", urlset(f"{SITE}/x")),
            f"{SITE}/two.xml": text(f"{SITE}/two.xml", urlset(f"{SITE}/y")),
        }
    )
    job_id = await crawl.start(depth=0)

    await crawl.crawl()

    items = await crawl.items(job_id)
    assert roles(items) == ["page", "robots", "sitemap", "sitemap"]
    robots_item = items[1]
    assert robots_item.result is not None
    assert robots_item.result.status == "success"
    assert site(robots_item).sitemaps == [f"{SITE}/one.xml", f"{SITE}/two.xml"]
    for item in items[2:]:
        assert site(item).sitemap is not None
        assert not site(item).sitemap.guessed  # type: ignore[union-attr]
        assert (site(item).depth, site(item).parent) == (0, 1)
    assert await crawl.state(job_id) == "completed"


@scenario
async def test_robots_item_guesses_without_sitemap_lines() -> None:
    crawl = Crawl(
        {
            ROOT: page(ROOT),
            ROBOTS: text(ROBOTS, "User-agent: *\nAllow: /\n", "text/plain"),
            f"{SITE}/sitemap.xml": text(f"{SITE}/sitemap.xml", urlset(f"{SITE}/x")),
        }
    )
    job_id = await crawl.start(depth=0)

    await crawl.crawl()

    items = await crawl.items(job_id)
    assert site(items[1]).sitemaps == [f"{SITE}/sitemap.xml"]
    sitemap = site(items[2]).sitemap
    assert sitemap is not None
    assert sitemap.guessed
    assert [entry.loc for entry in sitemap.entries] == [f"{SITE}/x"]


@scenario
async def test_robots_item_unavailable_fails_retryable_without_a_guess() -> None:
    crawl = Crawl({ROOT: page(ROOT), ROBOTS: fetched(ROBOTS, "Busy", status=503)})
    job_id = await crawl.start(depth=0, compliance="report")

    await crawl.crawl()

    items = await crawl.items(job_id)
    assert roles(items) == ["page", "robots"]
    result = items[1].result
    assert result is not None
    assert result.status == "failed"
    assert result.error is not None
    assert (result.error.code, result.error.retryable) == (ErrorCode.TARGET_HTTP_ERROR, True)
    assert result.error.message == "robots.txt could not be retrieved."
    assert await crawl.state(job_id) == "completed"


@scenario
async def test_guessed_sitemap_404_is_absent_success() -> None:
    crawl = Crawl({ROOT: page(ROOT)})
    job_id = await crawl.start(depth=0)

    await crawl.crawl()

    items = await crawl.items(job_id)
    sitemap_item = items[2]
    assert sitemap_item.result is not None
    assert sitemap_item.result.status == "success"
    sitemap = site(sitemap_item).sitemap
    assert sitemap is not None
    assert (sitemap.format, sitemap.guessed) == ("absent", True)
    assert "sitemap_not_found" in data(sitemap_item).warnings


@scenario
async def test_listed_sitemap_404_fails() -> None:
    robots = f"Sitemap: {SITE}/gone.xml\n"
    crawl = Crawl({ROOT: page(ROOT), ROBOTS: text(ROBOTS, robots, "text/plain")})
    job_id = await crawl.start(depth=0)

    await crawl.crawl()

    result = (await crawl.items(job_id))[2].result
    assert result is not None
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == ErrorCode.TARGET_HTTP_ERROR


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("<html><body>Oops</body></html>", ErrorCode.UNSUPPORTED_CONTENT_TYPE),
        ("<urlset><url><loc>", ErrorCode.CONVERSION_FAILED),
    ],
)
@scenario
async def test_unreadable_sitemaps_fail(body: str, code: ErrorCode) -> None:
    robots = f"Sitemap: {SITE}/bad.xml\n"
    crawl = Crawl(
        {
            ROOT: page(ROOT),
            ROBOTS: text(ROBOTS, robots, "text/plain"),
            f"{SITE}/bad.xml": text(f"{SITE}/bad.xml", body),
        }
    )
    job_id = await crawl.start(depth=0)

    await crawl.crawl()

    result = (await crawl.items(job_id))[2].result
    assert result is not None
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == code
    assert result.data is not None
    assert result.data.site is not None
    assert result.data.site.role == "sitemap"


@scenario
async def test_sitemap_index_children_and_nesting_cap() -> None:
    answers: dict[str, Answer] = {
        ROOT: page(ROOT),
        ROBOTS: text(ROBOTS, f"Sitemap: {SITE}/s0.xml\n", "text/plain"),
        f"{SITE}/s0.xml": text(
            f"{SITE}/s0.xml", index(f"{SITE}/s1.xml", "https://cdn.other.example/x.xml")
        ),
        f"{SITE}/s1.xml": text(f"{SITE}/s1.xml", index(f"{SITE}/s2.xml", f"{SITE}/u.xml")),
        f"{SITE}/s2.xml": text(f"{SITE}/s2.xml", index(f"{SITE}/s3.xml")),
        f"{SITE}/s3.xml": text(f"{SITE}/s3.xml", index(f"{SITE}/s4.xml")),
        f"{SITE}/u.xml": text(f"{SITE}/u.xml", urlset(f"{SITE}/p")),
    }
    crawl = Crawl(answers)
    job_id = await crawl.start(depth=0)

    await crawl.crawl()

    items = await crawl.items(job_id)
    assert await crawl.state(job_id) == "completed"
    s0 = by_url(items, f"{SITE}/s0.xml")
    children = site(s0).sitemap.children  # type: ignore[union-attr]
    s1 = by_url(items, f"{SITE}/s1.xml")
    assert [(c.url, c.crawl, c.target) for c in children] == [
        (f"{SITE}/s1.xml", "queued", s1.index),
        ("https://cdn.other.example/x.xml", "out_of_scope", None),
    ]
    assert [site(by_url(items, f"{SITE}/s{n}.xml")).depth for n in range(4)] == [0, 1, 2, 3]
    s3 = by_url(items, f"{SITE}/s3.xml")
    [deepest] = site(s3).sitemap.children  # type: ignore[union-attr]
    assert (deepest.url, deepest.crawl, deepest.target) == (f"{SITE}/s4.xml", "depth", None)
    assert f"{SITE}/s4.xml" not in [item.url for item in items]
    u = by_url(items, f"{SITE}/u.xml")
    assert [e.loc for e in site(u).sitemap.entries] == [f"{SITE}/p"]  # type: ignore[union-attr]
    assert site(u).sitemap.format == "urlset"  # type: ignore[union-attr]


@scenario
async def test_self_listing_index_is_seen() -> None:
    answers: dict[str, Answer] = {
        ROOT: page(ROOT),
        ROBOTS: text(ROBOTS, f"Sitemap: {SITE}/self.xml\n", "text/plain"),
        f"{SITE}/self.xml": text(f"{SITE}/self.xml", index(f"{SITE}/self.xml")),
    }
    crawl = Crawl(answers)
    job_id = await crawl.start(depth=0)

    await crawl.crawl()

    items = await crawl.items(job_id)
    own = by_url(items, f"{SITE}/self.xml")
    [child] = site(own).sitemap.children  # type: ignore[union-attr]
    assert (child.crawl, child.target) == ("seen", own.index)
    assert [item.url for item in items].count(f"{SITE}/self.xml") == 1
    assert await crawl.state(job_id) == "completed"


@scenario
async def test_sixty_thousand_entries_across_parts() -> None:
    locs = [f"{SITE}/kurs/{n}/lektion.html" for n in range(60_000)]
    big = f"{SITE}/big.xml"
    crawl = Crawl(
        {
            ROOT: page(ROOT),
            ROBOTS: text(ROBOTS, f"Sitemap: {big}\n", "text/plain"),
            big: text(big, urlset(*locs)),
        },
        job_item_max_bytes=2_000_000,
    )
    job_id = await crawl.start(depth=0)

    await crawl.crawl()

    items = [item for item in await crawl.items(job_id) if item.url == big]
    assert await crawl.state(job_id) == "completed"
    infos = [site(item).sitemap for item in items]
    assert all(info is not None for info in infos)
    parts = len(items)
    assert parts > 1
    assert [info.part for info in infos] == list(range(1, parts + 1))  # type: ignore[union-attr]
    assert {info.parts for info in infos} == {parts}  # type: ignore[union-attr]
    assert {info.entry_count for info in infos} == {60_000}  # type: ignore[union-attr]
    assert [site(item).role for item in items] == ["sitemap"] + ["sitemap_part"] * (parts - 1)
    assert [site(item).parent for item in items[1:]] == [items[0].index] * (parts - 1)
    found = [entry.loc for info in infos for entry in info.entries]  # type: ignore[union-attr]
    assert found == locs
    for item, info in zip(items, infos, strict=True):
        assert len(ENTRIES.dump_json(info.entries)) < 1_000_000  # type: ignore[union-attr]
        assert await crawl.stored_size(job_id, item.index) <= 2_000_000
        assert item.result is not None
        assert item.result.status == "success"


@scenario
async def test_sitemap_urls_cap_partial() -> None:
    locs = [f"{SITE}/p{n}" for n in range(80)]
    robots = f"Sitemap: {SITE}/one.xml\nSitemap: {SITE}/two.xml\n"
    crawl = Crawl(
        {
            ROOT: page(ROOT),
            ROBOTS: text(ROBOTS, robots, "text/plain"),
            f"{SITE}/one.xml": text(f"{SITE}/one.xml", urlset(*locs)),
            f"{SITE}/two.xml": text(f"{SITE}/two.xml", urlset(*locs)),
        },
        site_max_sitemap_urls=50,
    )
    job_id = await crawl.start(depth=0)

    await crawl.crawl()

    items = await crawl.items(job_id)
    # The second sitemap finds the crawl's budget used up: its parse stops at its first entry,
    # so its count is what was read by then.
    for item, kept, counted in zip(items[2:], (50, 0), (80, 1), strict=True):
        assert item.result is not None
        assert item.result.status == "partial"
        assert item.result.error is not None
        assert (item.result.error.code, item.result.error.retryable) == (ErrorCode.TOO_LARGE, False)
        sitemap = site(item).sitemap
        assert sitemap is not None
        assert len(sitemap.entries) == kept
        assert sitemap.entry_count == counted
        assert "sitemap_urls_truncated" in data(item).warnings
    assert crawl.sample("text_extraction_site_caps_total", {"cap": "urls"}) == 2


@scenario
async def test_gzip_sitemap_is_inflated() -> None:
    import gzip

    body = gzip.compress(urlset(f"{SITE}/x", f"{SITE}/y").encode())
    crawl = Crawl(
        {
            ROOT: page(ROOT),
            ROBOTS: text(ROBOTS, f"Sitemap: {SITE}/s.xml.gz\n", "text/plain"),
            f"{SITE}/s.xml.gz": fetched(f"{SITE}/s.xml.gz", body, content_type="application/gzip"),
        }
    )
    job_id = await crawl.start(depth=0)

    await crawl.crawl()

    sitemap = site((await crawl.items(job_id))[2]).sitemap
    assert sitemap is not None
    assert [entry.loc for entry in sitemap.entries] == [f"{SITE}/x", f"{SITE}/y"]


@scenario
async def test_large_index_children_cut_to_fit() -> None:
    children = [f"{SITE}/maps/{'x' * 150}-{n}.xml" for n in range(400)]
    crawl = Crawl(
        {
            ROOT: page(ROOT),
            ROBOTS: text(ROBOTS, f"Sitemap: {SITE}/index.xml\n", "text/plain"),
            f"{SITE}/index.xml": text(f"{SITE}/index.xml", index(*children)),
        },
        job_item_max_bytes=20_000,
        site_max_sitemap_files=1,
    )
    job_id = await crawl.start(depth=0)

    await crawl.crawl()

    items = await crawl.items(job_id)
    item = items[2]
    assert item.result is not None
    assert item.result.status == "partial"
    assert "sitemap_truncated" in data(item).warnings
    kept = site(item).sitemap.children  # type: ignore[union-attr]
    assert 0 < len(kept) < 400
    assert await crawl.stored_size(job_id, item.index) <= 20_000
    # The file cap (one sitemap: the index) refuses every child: they say max_files.
    assert {child.crawl for child in kept} == {"max_files"}
    assert "sitemap_files_truncated" in data(item).warnings
    assert len(items) == 3


# --- Review Focus 3: SSRF through the site ------------------------------------------------------


class GuardedFetcher(FakeFetcher):
    """A fake fetcher whose every request (redirect hops included) passes the address guard of
    the real fetcher first, with a stub resolver: the site's names are public, IP literals are
    what they are. ``reached`` lists the hosts a request was sent to."""

    def __init__(self, answers: dict[str, Answer]) -> None:
        super().__init__(answers)
        self.before_request = self._guard
        self.reached: list[str] = []

    async def _guard(self, host: str) -> None:
        address = {"lernen.example.org": "93.184.215.14"}.get(host, host)
        try:
            await resolve_allowed(address, 80, is_public_address)
        except BlockedAddressError:
            raise FetchError(ErrorCode.BLOCKED_URL, "The target address is not allowed.") from None
        self.reached.append(host)


@scenario
async def test_private_sitemap_and_redirect_blocked() -> None:
    robots = (
        f"Sitemap: http://169.254.169.254/latest\nSitemap: {SITE}/index.xml\n"
        f"Sitemap: {SITE}/moved.xml\n"
    )
    fetcher = GuardedFetcher(
        {
            ROOT: page(ROOT, "/go"),
            ROBOTS: text(ROBOTS, robots, "text/plain"),
            f"{SITE}/go": Redirect("http://10.0.0.7/admin"),
            f"{SITE}/moved.xml": Redirect("http://10.0.0.9/sitemap.xml"),
            f"{SITE}/index.xml": text(f"{SITE}/index.xml", index("http://127.0.0.1:6379/")),
        }
    )
    crawl = Crawl(fetcher=fetcher, fetch_allow_private_networks=False)
    job_id = await crawl.start(depth=1, compliance="report")

    await crawl.crawl()

    items = await crawl.items(job_id)
    assert await crawl.state(job_id) == "completed"
    for url in ("http://169.254.169.254/latest", f"{SITE}/go", f"{SITE}/moved.xml"):
        result = by_url(items, url).result
        assert result is not None
        assert result.status == "failed"
        assert result.error is not None
        assert result.error.code == ErrorCode.BLOCKED_URL
    assert site(by_url(items, f"{SITE}/moved.xml")).role == "sitemap"
    [child] = site(by_url(items, f"{SITE}/index.xml")).sitemap.children  # type: ignore[union-attr]
    assert child.crawl == "out_of_scope"
    assert set(fetcher.reached) == {"lernen.example.org"}


# --- Review Focus 1: crawler traps ---------------------------------------------------------------


class Calendar(dict[str, Answer]):
    """A calendar whose every month links to the next three, each with a fresh session id."""

    def get(self, url: str, default: Any = None) -> Any:
        parts = urlsplit(url)
        if parts.path == "/cal":
            month = int(parse_qs(parts.query).get("month", ["0"])[0])
            hrefs = [f"/cal?month={month + n}&sid={month}-{n}" for n in (1, 2, 3)]
            return page(url, *hrefs)
        return super().get(url, default)


@scenario
async def test_calendar_trap_stops_at_max_pages() -> None:
    answers = Calendar()
    answers[ROOT] = page(ROOT, "/cal?month=0", "/a/./b/../b", "/a/b")
    answers[f"{SITE}/a/b"] = page(f"{SITE}/a/b")
    crawl = Crawl(answers)
    job_id = await crawl.start(depth=10, max_pages=20, sitemaps=False)

    processed = await crawl.crawl()

    items = await crawl.items(job_id)
    assert await crawl.state(job_id) == "completed"
    assert processed == len(items) == 20
    assert roles(items) == ["page"] * 20
    assert [item.url for item in items].count(f"{SITE}/a/b") == 1
    capped = [item for item in items if "max_pages_reached" in data(item).warnings]
    assert capped
    assert any(link.crawl == "max_pages" for item in capped for link in links(item))
    assert crawl.sample("text_extraction_site_caps_total", {"cap": "pages"}) >= 1


@scenario
async def test_cycle_and_alias_are_one_item() -> None:
    crawl = Crawl(
        {
            ROOT: Redirect(f"{SITE}/home"),
            f"{SITE}/home": page(f"{SITE}/home", "/home", "/a", "/old"),
            f"{SITE}/a": page(f"{SITE}/a", "/b", "/"),
            f"{SITE}/b": page(f"{SITE}/b", "/a"),
            f"{SITE}/old": Redirect(f"{SITE}/a"),
        }
    )
    job_id = await crawl.start(depth=5, sitemaps=False)

    await crawl.crawl()

    items = await crawl.items(job_id)
    assert await crawl.state(job_id) == "completed"
    urls = [item.url for item in items]
    assert urls == [ROOT, f"{SITE}/a", f"{SITE}/old", f"{SITE}/b"]
    root = items[0]
    assert data(root).final_url == f"{SITE}/home"
    assert (link_to(root, f"{SITE}/home").crawl, link_to(root, f"{SITE}/home").target) == (
        "seen",
        0,
    )
    a, old, b = items[1], items[2], items[3]
    assert (link_to(b, f"{SITE}/a").crawl, link_to(b, f"{SITE}/a").target) == ("seen", a.index)
    assert data(old).final_url == f"{SITE}/a"
    assert (link_to(old, f"{SITE}/b").crawl, link_to(old, f"{SITE}/b").target) == (
        "seen",
        b.index,
    )


# --- the memory guard and lost leases (Review Focus 4) -----------------------------------------


@scenario
async def test_gate_refusal_defers_the_start_page_before_any_extend() -> None:
    crawl = Crawl({ROOT: page(ROOT, "/a")}, gate=RefusingGate(OutboundBusy(3.0)))
    job_id = await crawl.start(depth=1)

    item = await crawl.step()

    assert item is not None
    assert await crawl.queued() == [f"{job_id}:0:1"]  # back in the queue, one attempt more
    [start] = await crawl.items(job_id)  # nothing appended: no robots item, no links
    assert start.state == "pending"
    record = await crawl.store.get(job_id)
    assert record is not None
    assert (record.progress.total, record.progress.done) == (1, 0)
    assert await crawl.valkey.hlen(job_key(job_id, "children")) == 0
    assert await crawl.valkey.hkeys(job_key(job_id, "nodes")) == [b"0"]
    assert crawl.fetcher.calls == []
    assert (
        crawl.sample("text_extraction_site_items_total", {"role": "page", "outcome": "success"})
        == 0
    )


@scenario
async def test_memory_guard_appends_nothing() -> None:
    crawl = Crawl({ROOT: page(ROOT, "/a", "/")})
    crawl.guard.high = True
    job_id = await crawl.start(depth=2)

    await crawl.crawl()

    [root] = await crawl.items(job_id)
    assert await crawl.state(job_id) == "completed"
    assert [(link.crawl, link.target) for link in links(root)] == [
        ("memory_guard", None),
        ("seen", 0),
    ]
    assert "site_memory_guard" in data(root).warnings
    assert crawl.sample("text_extraction_site_caps_total", {"cap": "memory"}) == 1


class FailingEnqueue:
    """Makes the site runner's queueing fail from its ``fail_on``-th call (1-based), as a worker
    killed after an ``extend``; the calls before it queue as usual."""

    def __init__(self, crawl: Crawl, fail_on: int = 1) -> None:
        self.queue = crawl.queue  # the site runner's queue
        self.original = self.queue.enqueue_items
        self.fail_on = fail_on
        self.calls = 0

    async def fail(self, *args: Any) -> None:
        self.calls += 1
        if self.calls < self.fail_on:
            await self.original(*args)
            return
        raise RuntimeError("killed")

    def __enter__(self) -> None:
        self.queue.enqueue_items = self.fail  # type: ignore[method-assign, assignment]

    def __exit__(self, *args: object) -> None:
        self.queue.enqueue_items = self.original  # type: ignore[method-assign]


async def die_after_extend(crawl: Crawl, url: str, *, fail_on: int = 1) -> None:
    """A worker claims the next item, extends and dies before queueing (its ``fail_on``-th
    queueing); its lease expires."""
    item = await crawl.claim()
    assert item is not None
    job = await crawl.store.get(item.job_id)
    assert job is not None
    with FailingEnqueue(crawl, fail_on), pytest.raises(RuntimeError):
        await crawl.site.run(job, item, url, gate_refusals="raise")
    crawl.clock.now += crawl.lease_seconds + 1


@scenario
async def test_rerun_parent_requeues_children() -> None:
    crawl = Crawl({ROOT: page(ROOT, "/a", "/b"), f"{SITE}/a": page(f"{SITE}/a", "/b")})
    job_id = await crawl.start(depth=2, sitemaps=False)
    await die_after_extend(crawl, ROOT)
    assert len(await crawl.items(job_id)) == 3  # appended, never queued
    assert await crawl.queued() == []

    await crawl.crawl()

    items = await crawl.items(job_id)
    assert await crawl.state(job_id) == "completed"
    assert [item.url for item in items] == [ROOT, f"{SITE}/a", f"{SITE}/b"]
    assert all(item.state == "done" for item in items)
    assert [(link.crawl, link.target) for link in links(items[0])] == [("queued", 1), ("queued", 2)]


@scenario
async def test_exhausted_parent_requeues_children_and_job_completes() -> None:
    crawl = Crawl({ROOT: page(ROOT, "/a", "/b")}, job_item_max_attempts=1)
    job_id = await crawl.start(depth=2)
    for _ in range(2):  # two workers die after the extend: attempts 0, then 1
        await die_after_extend(crawl, ROOT)

    await crawl.crawl()

    items = await crawl.items(job_id)
    assert await crawl.state(job_id) == "completed"
    assert [item.url for item in items] == [
        ROOT,
        ROBOTS,
        f"{SITE}/a",
        f"{SITE}/b",
        f"{SITE}/sitemap.xml",
    ]
    root = items[0].result
    assert root is not None
    assert root.error is not None
    assert (root.error.code, root.error.retryable) == (ErrorCode.INTERNAL_ERROR, True)
    assert all(item.state == "done" for item in items)


@scenario
async def test_unknown_item_fails_internal_error() -> None:
    crawl = Crawl({ROOT: page(ROOT)})
    job_id = await crawl.start(depth=0, sitemaps=False)
    await crawl.valkey.delete(job_key(job_id, "nodes"))

    await crawl.crawl()

    [root] = await crawl.items(job_id)
    assert root.result is not None
    assert root.result.error is not None
    assert (root.result.error.code, root.result.error.retryable) == (
        ErrorCode.INTERNAL_ERROR,
        False,
    )


# --- Review Focus 5: huge pages ------------------------------------------------------------------


@scenario
async def test_page_item_stays_under_item_limit() -> None:
    hrefs = [f"/{'segment-' * 60}{n}" for n in range(1000)]
    crawl = Crawl({ROOT: page(ROOT, *hrefs)}, job_item_max_bytes=100_000)
    job_id = await crawl.start(depth=1, max_pages=2000, sitemaps=False)

    await crawl.step()

    items = await crawl.items(job_id)
    root = items[0]
    assert await crawl.stored_size(job_id, 0) <= 100_000
    kept = links(root)
    assert 0 < len(kept) < 1000
    assert "links_truncated" in data(root).warnings
    # Only the links the item keeps are followed.
    assert len(items) == 1 + len(kept)
    assert [link.target for link in kept] == list(range(1, len(kept) + 1))
    assert crawl.sample("text_extraction_site_caps_total", {"cap": "links"}) == 1


@scenario
async def test_metrics_count_items_and_links() -> None:
    crawl = Crawl({ROOT: page(ROOT, "/a", "https://other.example/")})
    job_id = await crawl.start(depth=0, sitemaps=False)

    await crawl.crawl()

    assert await crawl.state(job_id) == "completed"
    items = {"role": "page", "outcome": "success"}
    assert crawl.sample("text_extraction_site_items_total", items) == 1
    assert crawl.sample("text_extraction_site_links_total", {"crawl": "depth"}) == 1
    assert crawl.sample("text_extraction_site_links_total", {"crawl": "out_of_scope"}) == 1


# --- fix round 1 ---------------------------------------------------------------------------------


@scenario
async def test_parse_deadline_answers_what_it_read() -> None:
    locs = [f"{SITE}/kurs/{n}/lektion.html" for n in range(150_000)]
    big = f"{SITE}/big.xml"
    crawl = Crawl(
        {
            ROOT: page(ROOT),
            ROBOTS: text(ROBOTS, f"Sitemap: {big}\n", "text/plain"),
            big: text(big, urlset(*locs)),
        }
    )
    crawl.site._item_budget = 0.3  # the parse of 150,000 entries takes seconds
    job_id = await crawl.start(depth=0)

    await crawl.crawl()

    item = by_url(await crawl.items(job_id), big)
    assert item.result is not None
    assert item.result.status == "partial"
    assert item.result.error is not None
    assert (item.result.error.code, item.result.error.retryable) == (ErrorCode.TIMEOUT, False)
    assert "sitemap_truncated" in data(item).warnings
    sitemap = site(item).sitemap
    assert sitemap is not None
    assert 0 < len(sitemap.entries) < 150_000
    assert await crawl.state(job_id) == "completed"


@pytest.mark.parametrize("rerun_links", [2, 3])
@scenario
async def test_rerun_with_fewer_batches_requeues_the_missing_ones(
    rerun_links: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "EXTEND_BATCH", 2)
    hrefs = [f"/p{n}" for n in range(6)]
    crawl = Crawl({ROOT: page(ROOT, *hrefs)})
    job_id = await crawl.start(depth=1, sitemaps=False, cache=False)
    # Batches [p0 p1] [p2 p3] [p4 p5]: queued, extended, then the worker dies.
    await die_after_extend(crawl, ROOT, fail_on=2)
    assert [item.url for item in await crawl.items(job_id)][1:] == [
        f"{SITE}/p{n}" for n in range(4)
    ]
    # The page changed: the rerun sends fewer batches than the first run.
    crawl.fetcher.answers[ROOT] = page(ROOT, *hrefs[:rerun_links])

    await crawl.crawl()

    items = await crawl.items(job_id)
    assert await crawl.state(job_id) == "completed"
    assert [item.url for item in items] == [ROOT] + [f"{SITE}/p{n}" for n in range(4)]
    assert all(item.state == "done" for item in items)
    root = items[0]
    if rerun_links == 3:
        # Batch 1's stored answer is for two candidates: p2 keeps its own crawl, no target.
        assert (link_to(root, f"{SITE}/p2").crawl, link_to(root, f"{SITE}/p2").target) == (
            "queued",
            None,
        )
    assert "site_memory_guard" not in data(root).warnings
    assert "max_pages_reached" not in data(root).warnings


@scenario
async def test_multi_batch_extend_keeps_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_module, "EXTEND_BATCH", 2)
    hrefs = [f"/p{n}" for n in range(5)]
    crawl = Crawl({ROOT: page(ROOT, *hrefs, "/")})
    job_id = await crawl.start(depth=1)

    await crawl.step()

    items = await crawl.items(job_id)
    assert [item.url for item in items] == [ROOT, ROBOTS] + [f"{SITE}{href}" for href in hrefs]
    assert [(link.crawl, link.target) for link in links(items[0])] == [
        *(("queued", n) for n in range(2, 7)),
        ("seen", 0),
    ]
    assert await crawl.queued() == [f"{job_id}:{n}:0" for n in range(1, 7)]
    context = await crawl.sites.context(job_id, 3)
    assert context.scope == SiteScope(host="lernen.example.org", path_prefix=None)


@scenario
async def test_blocked_link_is_neither_queued_nor_fetched() -> None:
    robots = "User-agent: *\nDisallow: /intern/\n"
    crawl = Crawl(
        {
            ROOT: page(ROOT, "/intern/geheim.html", "/ok"),
            ROBOTS: text(ROBOTS, robots, "text/plain"),
            f"{SITE}/ok": page(f"{SITE}/ok"),
        }
    )
    job_id = await crawl.start(depth=1, sitemaps=False)

    await crawl.crawl()

    items = await crawl.items(job_id)
    assert [item.url for item in items] == [ROOT, f"{SITE}/ok"]
    blocked = link_to(items[0], f"{SITE}/intern/geheim.html")
    assert (blocked.crawl, blocked.target) == ("blocked", None)
    assert f"{SITE}/intern/geheim.html" not in [url for url, _ in crawl.fetcher.calls]


@scenario
async def test_disallowed_sitemap_is_blocked_by_robots_under_enforce() -> None:
    robots = f"User-agent: *\nDisallow: /intern/\nSitemap: {SITE}/intern/map.xml\n"
    crawl = Crawl({ROOT: page(ROOT), ROBOTS: text(ROBOTS, robots, "text/plain")})
    job_id = await crawl.start(depth=0)

    await crawl.crawl()

    item = by_url(await crawl.items(job_id), f"{SITE}/intern/map.xml")
    assert item.result is not None
    assert item.result.status == "failed"
    assert item.result.error is not None
    assert item.result.error.code == ErrorCode.BLOCKED_BY_ROBOTS
    assert site(item).role == "sitemap"
    assert f"{SITE}/intern/map.xml" not in [url for url, _ in crawl.fetcher.calls]
