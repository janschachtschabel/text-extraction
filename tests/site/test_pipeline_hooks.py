"""The pipeline's hooks for the site crawl (ruling P9-S10): typed links in a page's result,
raw fetches under the pipeline's checks, robots.txt's sitemaps and the cached block verdict."""

import asyncio
from collections.abc import Coroutine
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis
from prometheus_client import CollectorRegistry

from tests.fakes import (
    ARTICLE,
    ORIGIN,
    PAGE,
    PARAGRAPH,
    ROBOTS,
    Answer,
    FakeFetcher,
    RecordingGate,
    Redirect,
    RefusingGate,
    fetched,
)
from text_extraction.auth import Principal
from text_extraction.compliance import ComplianceChecker, RobotsSitemaps
from text_extraction.documents.pool import DocumentPool
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.extract_models import ExtractRequest, ExtractResult
from text_extraction.fetch import Fetcher, FetchError
from text_extraction.fingerprint import FingerprintStore
from text_extraction.outbound import DomainGate, OutboundBusy
from text_extraction.pipeline import Pipeline, RawFetch
from text_extraction.result_cache import ResultCache, cache_key
from text_extraction.settings import Settings

USER = Principal(name="alice", role="user", rate_limit_per_minute=None)
SITEMAP = f"{ORIGIN}/sitemap.xml"
MOVED = f"{ORIGIN}/private/sitemap.xml"
DISALLOW_PRIVATE = "User-agent: *\nDisallow: /private/\n"


class FakeDocumentPool:
    """A MarkItDown pool that answers every document with one converted text."""

    async def start(self) -> None:
        pass

    async def run(self, function_name: str, payload: dict[str, Any], deadline: float) -> Any:
        return {
            "content": "# Lesson\n\n" + PARAGRAPH * 3,
            "title": "Lesson",
            "warnings": [],
            "pages": 1,
            "pages_converted": 1,
            "needs_ocr": False,
            "metadata": None,
        }

    async def shutdown(self) -> None:
        pass


class Hooks:
    """A pipeline around a fake fetcher, with a recording gate unless one is given."""

    def __init__(
        self, answers: dict[str, Answer], *, gate: object | None = None, **overrides: Any
    ) -> None:
        self.fetcher = FakeFetcher(answers)
        self.valkey = FakeAsyncRedis()
        values: dict[str, Any] = {"auth_mode": "none", "outbound_min_delay_ms": 0}
        values.update(overrides)
        self.settings = Settings(**values)
        self.gate = (
            gate if gate is not None else RecordingGate(DomainGate(self.valkey, self.settings))
        )
        self.fetcher.before_request = getattr(self.gate, "before_request", None)
        self.fetcher.on_request_sent = getattr(self.gate, "mark_sent", None)
        fetcher = cast(Fetcher, self.fetcher)
        self.pipeline = Pipeline(
            fetcher,
            cast(DomainGate, self.gate),
            ComplianceChecker(fetcher, self.valkey, self.settings),
            ResultCache(self.valkey, self.settings),
            FingerprintStore(self.valkey, self.settings),
            self.settings,
            CollectorRegistry(),
            document_pool=cast(DocumentPool, FakeDocumentPool()),
        )

    def go[T](self, work: Coroutine[Any, Any, T]) -> T:
        return asyncio.run(work)

    def run(self, *, site_links: bool = False, **request: Any) -> Envelope[ExtractResult]:
        request.setdefault("url", PAGE)
        request.setdefault("mode", "fast")
        return self.go(self.pipeline.run(ExtractRequest(**request), USER, site_links=site_links))

    def fetch_raw(self, url: str = SITEMAP, **options: Any) -> RawFetch:
        options.setdefault("max_bytes", 1_000_000)
        options.setdefault("truncate", True)
        compliance = options.pop("compliance", None)
        return self.go(self.pipeline.fetch_raw(url, USER, compliance, **options))

    def robots_sitemaps(self, url: str = PAGE, **options: Any) -> RobotsSitemaps:
        return self.go(self.pipeline.robots_sitemaps(url, **options))

    def urls(self) -> list[str]:
        return [url for url, _ in self.fetcher.calls]

    def events(self) -> list[tuple[str, str]]:
        return cast(RecordingGate, self.gate).events

    def has_key(self, key: str) -> bool:
        async def read() -> bool:
            return bool(await self.valkey.exists(key))

        return self.go(read())

    def keys(self, pattern: str) -> list[bytes]:
        async def scan() -> list[bytes]:
            return [key async for key in self.valkey.scan_iter(pattern)]

        return self.go(scan())


def data_of(envelope: Envelope[ExtractResult]) -> ExtractResult:
    assert envelope.data is not None
    return envelope.data


# --- typed links in the result -----------------------------------------------------------------


def test_site_links_in_result_for_html() -> None:
    hooks = Hooks({PAGE: fetched()})

    data = data_of(hooks.run(site_links=True))

    assert data.links is None  # the request's own `links` stays off
    assert data.site is not None
    assert data.site.role == "page"
    assert data.site.depth == 0
    assert data.site.parent is None
    assert data.site.nofollow is False
    links = {link.url: link for link in data.site.links or []}
    home = links[f"{ORIGIN}/"]
    assert (home.kind, home.source, home.region, home.crawl) == ("internal", "a", "nav", "listed")
    assert links[f"{ORIGIN}/learn/respiration.html"].region == "main"
    assert links["https://other.example/plants"].kind == "external"
    assert links[f"{ORIGIN}/files/worksheet.pdf"].kind == "file"
    assert "links_truncated" not in data.warnings


def test_no_site_without_site_links() -> None:
    hooks = Hooks({PAGE: fetched()})

    assert data_of(hooks.run()).site is None


def test_site_links_capped_with_warning_and_nofollow() -> None:
    anchors = "".join(f'<a href="/p/{n}">Page {n}</a> ' for n in range(30))
    page = ARTICLE.replace("</head>", '<meta name="robots" content="nofollow"></head>').replace(
        "</article>", f"<p>{anchors}</p></article>"
    )
    hooks = Hooks({PAGE: fetched(body=page)}, site_max_links_per_page=10)

    data = data_of(hooks.run(site_links=True))

    assert data.site is not None
    assert len(data.site.links or []) == 10
    assert data.site.nofollow is True
    assert "links_truncated" in data.warnings


def test_site_links_empty_for_text_none_for_document() -> None:
    document = f"{ORIGIN}/files/lesson.pdf"
    hooks = Hooks(
        {
            PAGE: fetched(body=PARAGRAPH * 3, content_type="text/plain"),
            document: fetched(document, b"%PDF-1.4\n%fake\n", content_type="application/pdf"),
        }
    )

    text = hooks.run(site_links=True)
    pdf = hooks.run(url=document, site_links=True)

    assert text.status == "success"
    site = data_of(text).site
    assert site is not None
    assert site.role == "page"
    assert site.links == []
    assert pdf.status == "success"
    assert data_of(pdf).document is not None
    assert data_of(pdf).site is None


# --- the cache key -----------------------------------------------------------------------------

# The options of a fast request with the defaults, as package 8 keyed them.
PACKAGE_8_OPTIONS = {
    "mode": "fast",
    "format": "markdown",
    "metadata": True,
    "links": False,
    "compliance": "enforce",
}


def test_cache_key_unchanged_without_site_links() -> None:
    hooks = Hooks({PAGE: fetched()})

    hooks.run()

    assert hooks.has_key(cache_key(PAGE, PACKAGE_8_OPTIONS))
    assert len(hooks.keys("res:*")) == 1


def test_cache_key_differs_with_site_links() -> None:
    hooks = Hooks({PAGE: fetched()})

    first = hooks.run(site_links=True)
    again = hooks.run(site_links=True)
    plain = hooks.run()

    assert hooks.has_key(cache_key(PAGE, {**PACKAGE_8_OPTIONS, "site_links": True}))
    assert hooks.has_key(cache_key(PAGE, PACKAGE_8_OPTIONS))
    assert data_of(first).cache == "miss"
    assert data_of(again).cache == "hit"
    assert data_of(again).site == data_of(first).site  # the stored result keeps its links
    assert data_of(plain).cache == "miss"  # not served the entry with links
    assert data_of(plain).site is None


# --- raw fetches -------------------------------------------------------------------------------


def test_fetch_raw_checks_robots_before_and_on_redirect() -> None:
    hooks = Hooks(
        {
            ROBOTS: fetched(ROBOTS, DISALLOW_PRIVATE, content_type="text/plain"),
            MOVED: fetched(MOVED, "<urlset/>", content_type="application/xml"),
            SITEMAP: Redirect(MOVED),
        }
    )

    direct = hooks.fetch_raw(MOVED)
    redirected = hooks.fetch_raw(SITEMAP)

    for raw in (direct, redirected):
        assert raw.fetched is None
        assert raw.failure is not None
        assert raw.failure.status == "failed"
        assert raw.failure.error is not None
        assert raw.failure.error.code == ErrorCode.BLOCKED_BY_ROBOTS
        assert raw.report.robots == "disallowed"
        assert raw.report.decision == "blocked"
    assert direct.attempts == 0  # blocked before the fetch
    assert redirected.attempts == 1
    assert MOVED not in hooks.urls()  # the hop was blocked before its request


def test_fetch_raw_in_gate_turn_and_records_429() -> None:
    hooks = Hooks({SITEMAP: fetched(SITEMAP, "", status=429, headers={"retry-after": "120"})})

    raw = hooks.fetch_raw()

    assert raw.failure is None
    assert raw.fetched is not None
    assert raw.fetched.status == 429
    assert hooks.events() == [
        ("take", "example.org"),  # robots.txt, tdmrep.json and ai.txt, then the sitemap
        ("record", "example.org"),
        ("give back", "example.org"),
    ]
    assert hooks.keys("out:cooldown:*") == [b"out:cooldown:{example.org}"]


def test_fetch_raw_returns_any_status() -> None:
    gone = f"{ORIGIN}/gone.xml"
    broken = f"{ORIGIN}/broken.xml"
    hooks = Hooks(
        {
            gone: fetched(gone, "Not here", status=404, content_type="text/plain"),
            broken: fetched(broken, "Oops", status=500, content_type="text/plain"),
            SITEMAP: fetched(SITEMAP, "<urlset/>", content_type="application/xml"),
        }
    )

    found = hooks.fetch_raw(max_bytes=4096, truncate=True)
    missing, failing = hooks.fetch_raw(gone), hooks.fetch_raw(broken)

    assert found.failure is None
    assert found.fetched is not None
    assert (found.fetched.status, found.fetched.content) == (200, b"<urlset/>")
    assert found.report.robots == "allowed"
    assert found.attempts == 1
    assert found.timings.total_ms >= 0
    for raw, status in ((missing, 404), (failing, 500)):
        assert raw.failure is None
        assert raw.fetched is not None
        assert raw.fetched.status == status
    options = next(options for url, options in hooks.fetcher.calls if url == SITEMAP)
    assert options["max_bytes"] == 4096
    assert options["truncate"] is True


def test_fetch_raw_failures_are_envelopes() -> None:
    hooks = Hooks(
        {
            SITEMAP: FetchError(ErrorCode.CONNECTION_ERROR, "Refused.", retryable=True),
            f"{ORIGIN}/reserved.xml": fetched(
                f"{ORIGIN}/reserved.xml", "<urlset/>", headers={"tdm-reservation": "1"}
            ),
        }
    )

    refused = hooks.fetch_raw()
    reserved = hooks.fetch_raw(f"{ORIGIN}/reserved.xml")
    invalid = hooks.fetch_raw("ftp://example.org/sitemap.xml")

    assert refused.failure is not None
    assert refused.failure.error is not None
    assert refused.failure.error.code == ErrorCode.CONNECTION_ERROR
    assert refused.failure.error.retryable is True
    assert refused.fetched is None
    assert reserved.failure is not None
    assert reserved.failure.error is not None
    assert reserved.failure.error.code == ErrorCode.TDM_RESERVED  # the header, after the fetch
    assert reserved.report.tdm_reserved is True
    assert invalid.failure is not None
    assert invalid.failure.error is not None
    assert invalid.failure.error.code == ErrorCode.INVALID_URL


def test_fetch_raw_under_report_and_ignore() -> None:
    admin = Principal(name="root", role="admin", rate_limit_per_minute=None)
    hooks = Hooks(
        {
            ROBOTS: fetched(ROBOTS, DISALLOW_PRIVATE, content_type="text/plain"),
            MOVED: fetched(MOVED, "<urlset/>", content_type="application/xml"),
        },
        compliance_allow_override=True,
    )

    reported = hooks.fetch_raw(MOVED, compliance="report")
    ignored = hooks.go(
        hooks.pipeline.fetch_raw(MOVED, admin, "ignore", max_bytes=1000, truncate=False)
    )

    assert reported.failure is None
    assert reported.report.decision == "reported"
    assert ignored.failure is None
    assert ignored.report.decision == "skipped"


def test_fetch_raw_raises_gate_refusal_when_asked() -> None:
    hooks = Hooks({}, gate=RefusingGate(OutboundBusy(30)))

    answered = hooks.fetch_raw()
    with pytest.raises(OutboundBusy):
        hooks.fetch_raw(gate_refusals="raise")

    assert answered.fetched is None
    assert answered.failure is not None
    assert answered.failure.error is not None
    assert answered.failure.error.code == ErrorCode.RATE_LIMITED
    assert answered.failure.error.retryable is True
    assert hooks.urls() == []


# --- robots.txt's sitemaps ---------------------------------------------------------------------

ROBOTS_WITH_SITEMAPS = (
    "User-agent: *\nDisallow: /private/\n"
    "Sitemap: https://Example.ORG:443/sitemap.xml#top\n"
    "Sitemap: https://cdn.example.net/maps/index.xml.gz\n"
    "Sitemap: http://169.254.169.254/latest\n"
    "Sitemap: ftp://example.org/sitemap.xml\n"
    "Sitemap: /relative.xml\n"
)
LISTED = [
    f"{ORIGIN}/sitemap.xml",
    "https://cdn.example.net/maps/index.xml.gz",
    "http://169.254.169.254/latest",  # kept as text: the fetch's guard acts on it
]


def robots(body: str = ROBOTS_WITH_SITEMAPS, status: int = 200) -> dict[str, Answer]:
    return {ROBOTS: fetched(ROBOTS, body, status=status, content_type="text/plain")}


def test_robots_sitemaps_from_cache_without_request() -> None:
    hooks = Hooks({**robots(), PAGE: fetched()})
    hooks.run()  # its compliance checks cache robots.txt
    hooks.fetcher.calls.clear()
    hooks.events().clear()

    answer = hooks.robots_sitemaps()

    assert answer == RobotsSitemaps(state="found", urls=LISTED)
    assert hooks.urls() == []
    assert hooks.events() == []


def test_robots_sitemaps_loads_in_turn_once() -> None:
    hooks = Hooks(robots())

    first = hooks.robots_sitemaps()
    second = hooks.robots_sitemaps(f"{ORIGIN}/other/page.html")

    assert first == second == RobotsSitemaps(state="found", urls=LISTED)
    assert hooks.urls() == [ROBOTS]  # robots.txt only, once
    assert hooks.events() == [("take", "example.org"), ("give back", "example.org")]


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        (robots("Not found", 404), RobotsSitemaps(state="absent", urls=[])),
        (robots("Down", 503), RobotsSitemaps(state="unavailable", urls=[])),
        (robots("User-agent: *\nDisallow:\n"), RobotsSitemaps(state="found", urls=[])),
        (robots(), RobotsSitemaps(state="found", urls=LISTED)),
    ],
)
def test_robots_sitemaps_states(answers: dict[str, Answer], expected: RobotsSitemaps) -> None:
    hooks = Hooks(answers)

    assert hooks.robots_sitemaps() == expected


def test_robots_sitemaps_under_ignore_still_reads() -> None:
    hooks = Hooks(robots(), compliance_default="ignore")

    assert hooks.robots_sitemaps() == RobotsSitemaps(state="found", urls=LISTED)
    assert hooks.urls() == [ROBOTS]


def test_robots_sitemaps_gate_refusal() -> None:
    hooks = Hooks(robots(), gate=RefusingGate(OutboundBusy(30)))

    answered = hooks.robots_sitemaps()
    with pytest.raises(OutboundBusy):
        hooks.robots_sitemaps(gate_refusals="raise")

    assert answered == RobotsSitemaps(state="unavailable", urls=[])
    assert hooks.urls() == []


# --- the cached block verdict ------------------------------------------------------------------


def test_blocked_from_cache() -> None:
    hooks = Hooks({**robots(DISALLOW_PRIVATE), PAGE: fetched()})
    private = f"{ORIGIN}/private/page.html"

    uncached = hooks.go(hooks.pipeline.blocked_from_cache(private, "enforce"))
    hooks.run()  # caches the origin's files
    hooks.fetcher.calls.clear()
    enforced = hooks.go(hooks.pipeline.blocked_from_cache(private, "enforce"))
    reported = hooks.go(hooks.pipeline.blocked_from_cache(private, "report"))
    allowed = hooks.go(hooks.pipeline.blocked_from_cache(f"{ORIGIN}/learn/x.html", "enforce"))

    assert uncached is None
    assert enforced is True
    assert reported is False
    assert allowed is False
    assert hooks.urls() == []  # the cache alone
