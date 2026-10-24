"""The pipeline with a renderer: browser mode, auto escalation, screenshots and unblock mode."""

import asyncio
import json
from collections.abc import Awaitable, Callable
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
    FakeArtifacts,
    FakeClearance,
    FakeFetcher,
    FakeRenderer,
    RecordingGate,
    fetched,
    rendered,
    response,
)
from text_extraction.auth import Principal
from text_extraction.compliance import ComplianceChecker
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.escalation import needs_browser
from text_extraction.extract_models import ExtractRequest, ExtractResult
from text_extraction.fetch import Fetcher, FetchError
from text_extraction.fingerprint import FingerprintStore
from text_extraction.outbound import DomainGate
from text_extraction.pipeline import Pipeline
from text_extraction.render import ArtifactSink, ClearanceStore, CookieData, Renderer
from text_extraction.result_cache import ResultCache
from text_extraction.settings import Settings

USER = Principal(name="alice", role="user", rate_limit_per_minute=None)
ADMIN = Principal(name="root", role="admin", rate_limit_per_minute=None)

OTHER = "https://other.example"
MOVED = f"{ORIGIN}/private/moved.html"
NEXT = f"{ORIGIN}/learn/next.html"
DISALLOW_PRIVATE = "User-agent: *\nDisallow: /private/\n"

SHELL = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'
# A JavaScript shell that still shows a few words: partial, so it would be cached in fast mode.
SHELL_WITH_WORDS = (
    "<html><head><title>Learning app</title></head><body><div id='root'></div>"
    "<p>The learning app about photosynthesis loads here in a moment, please wait for it.</p>"
    "<script src='/app.js'></script></body></html>"
)
SHORT_PAGE = """<!doctype html><html lang="en"><head><title>Short</title></head>
<body><article><h1>Short</h1><p>Only a few words of text are on this page today.</p>
</article></body></html>"""
CHALLENGE_PAGE = """<!doctype html><html><head><title>Just a moment...</title></head>
<body><p>Checking your browser before accessing the site.</p></body></html>"""
NOAI_PAGE = ARTICLE.replace("<head>", '<head>\n<meta name="robots" content="noai">', 1)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "auth_mode": "none",
        "outbound_min_delay_ms": 0,
        "browser_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


class RenderHarness:
    """A pipeline over fakes; ``renderer=None`` builds one without a renderer."""

    def __init__(
        self,
        answers: dict[str, Answer] | None = None,
        *,
        renderer: FakeRenderer | None = None,
        unblock_renderer: FakeRenderer | None = None,
        clearance: FakeClearance | None = None,
        timeout_seconds: float | None = None,
        **overrides: Any,
    ) -> None:
        self.valkey = FakeAsyncRedis()
        self.settings = make_settings(**overrides)
        self.gate = RecordingGate(DomainGate(self.valkey, self.settings))
        self.fetcher = FakeFetcher(answers or {})
        self.fetcher.before_request = self.gate.before_request
        self.fetcher.on_request_sent = self.gate.mark_sent
        self.renderer = renderer
        fetcher = cast(Fetcher, self.fetcher)
        self.pipeline = Pipeline(
            fetcher,
            cast(DomainGate, self.gate),
            ComplianceChecker(fetcher, self.valkey, self.settings),
            ResultCache(self.valkey, self.settings),
            FingerprintStore(self.valkey, self.settings),
            self.settings,
            CollectorRegistry(),
            renderer=cast(Renderer | None, renderer),
            unblock_renderer=cast(Renderer | None, unblock_renderer),
            clearance=cast(ClearanceStore | None, clearance),
            timeout_seconds=timeout_seconds,
        )

    async def run(
        self,
        *,
        principal: Principal = USER,
        artifacts: FakeArtifacts | None = None,
        draft: Envelope[ExtractResult] | None = None,
        **request: Any,
    ) -> Envelope[ExtractResult]:
        request.setdefault("url", PAGE)
        return await self.pipeline.run(
            ExtractRequest(**request),
            principal,
            artifacts=cast(ArtifactSink | None, artifacts),
            draft=draft,
        )

    def run_once(self, **request: Any) -> Envelope[ExtractResult]:
        return asyncio.run(self.run(**request))

    def run_all(self, *requests: dict[str, Any]) -> list[Envelope[ExtractResult]]:
        async def all_of() -> list[Envelope[ExtractResult]]:
            return [await self.run(**request) for request in requests]

        return asyncio.run(all_of())

    def keys(self, pattern: str) -> list[bytes]:
        async def scan() -> list[bytes]:
            return [key async for key in self.valkey.scan_iter(pattern)]

        return asyncio.run(scan())

    def leases(self) -> int:
        async def count() -> int:
            return sum(
                [
                    await self.valkey.zcard(key)
                    async for key in self.valkey.scan_iter("out:inflight:*")
                ]
            )

        return asyncio.run(count())

    def fetched_urls(self) -> list[str]:
        return [url for url, _ in self.fetcher.calls]


def data_of(envelope: Envelope[ExtractResult]) -> ExtractResult:
    assert envelope.data is not None
    return envelope.data


def code_of(envelope: Envelope[ExtractResult]) -> ErrorCode:
    assert envelope.error is not None
    return envelope.error.code


# --- which path ---------------------------------------------------------------------------------


def test_browser_mode_renders_after_compliance_and_turn() -> None:
    seen: dict[str, Any] = {}
    harness: RenderHarness

    async def during() -> None:
        seen["held"] = harness.gate.held()
        seen["fetched"] = harness.fetched_urls()
        await asyncio.sleep(0.05)

    renderer = FakeRenderer(rendered(), during=during)
    harness = RenderHarness(renderer=renderer)

    envelope = harness.run_once(mode="browser")

    assert envelope.status == "success"
    data = data_of(envelope)
    assert data.mode_used == "browser"
    assert data.unblocked is False
    assert data.final_url == PAGE
    assert data.upstream_status == 200
    assert data.content_type == "text/html"
    assert data.content is not None
    assert "Photosynthesis is the process" in data.content
    assert data.title == "Photosynthesis explained"
    assert data.attempts == 1
    assert data.timings.render_ms >= 50
    assert data.compliance.robots == "allowed"
    assert data.metadata is not None
    assert data.metadata.license is not None
    assert data.fingerprint is not None
    # The origin's files were checked first, in the turn the render then held.
    assert ROBOTS in seen["fetched"]
    assert seen["held"] == ["example.org"]
    assert harness.fetcher.page_calls() == []  # the browser loads the page, not the fetcher
    assert harness.gate.held() == []
    assert ("record", "example.org") in harness.gate.events
    assert harness.leases() == 0
    (call,) = renderer.calls
    assert call.url == PAGE
    assert call.options.header_host == "example.org:443"
    assert call.options.unblock is False
    assert call.options.blocked_resources == frozenset({"image", "media", "font"})
    assert call.options.screenshot is False


def test_robots_block_of_start_url_never_renders() -> None:
    renderer = FakeRenderer()
    harness = RenderHarness(
        {ROBOTS: fetched(ROBOTS, "User-agent: *\nDisallow: /")}, renderer=renderer
    )

    envelope = harness.run_once(mode="browser")

    assert code_of(envelope) == ErrorCode.BLOCKED_BY_ROBOTS
    assert data_of(envelope).content is None
    assert data_of(envelope).attempts == 0
    assert renderer.calls == []
    assert harness.gate.events == [("take", "example.org"), ("give back", "example.org")]


def test_auto_returns_fast_result_without_render_when_not_needed() -> None:
    renderer = FakeRenderer()
    harness = RenderHarness({PAGE: fetched()}, renderer=renderer)

    envelope = harness.run_once(mode="auto")

    assert envelope.status == "success"
    assert data_of(envelope).mode_used == "fast"
    assert data_of(envelope).timings.render_ms == 0
    assert renderer.calls == []
    assert len(harness.keys("res:*")) == 1


@pytest.mark.parametrize(
    ("fast_page", "render_page", "winner"),
    [(SHELL, ARTICLE, "browser"), (SHELL_WITH_WORDS, SHELL, "fast")],
)
def test_auto_renders_js_heavy_page_and_keeps_the_better_result(
    fast_page: str, render_page: str, winner: str
) -> None:
    renderer = FakeRenderer(rendered(render_page))
    harness = RenderHarness({PAGE: fetched(body=fast_page)}, renderer=renderer)

    first, second = harness.run_all({"mode": "auto"}, {"mode": "auto"})

    assert len(renderer.calls) == 1  # the second run is a cache hit
    data = data_of(first)
    assert data.mode_used == winner
    assert data.attempts == 2  # the fast fetch and the rendered document
    assert first.status != "failed"
    assert data.fingerprint is not None
    assert data_of(second).cache == "hit"
    assert data_of(second).mode_used == winner
    assert len(harness.keys("fp:*")) == 1


def test_auto_with_draft_renders_without_fast_fetch() -> None:
    drafting = RenderHarness({PAGE: fetched(body=SHELL)})  # an API pod: no renderer
    draft = drafting.run_once(mode="auto")
    assert needs_browser(draft, unblock=False)
    renderer = FakeRenderer(rendered())
    harness = RenderHarness(renderer=renderer)

    envelope = harness.run_once(mode="auto", draft=draft)

    assert envelope.status == "success"
    data = data_of(envelope)
    assert data.mode_used == "browser"
    assert data.attempts == 2  # the draft's fetch and the rendered document
    assert harness.fetcher.page_calls() == []
    assert len(renderer.calls) == 1


def test_auto_keeps_a_better_draft_and_stores_it() -> None:
    drafting = RenderHarness({PAGE: fetched(body=SHELL_WITH_WORDS)})
    draft = drafting.run_once(mode="auto")
    assert draft.status == "partial"
    assert drafting.keys("res:*") == []  # it will escalate, so it was not stored
    harness = RenderHarness(renderer=FakeRenderer(rendered(SHELL)))

    envelope = harness.run_once(mode="auto", draft=draft)

    assert envelope.status == "partial"
    data = data_of(envelope)
    assert data.mode_used == "fast"
    assert data.fingerprint is not None
    assert len(harness.keys("res:*")) == 1
    assert len(harness.keys("fp:*")) == 1


def test_auto_with_browser_options_always_renders() -> None:
    renderer = FakeRenderer(rendered())
    harness = RenderHarness({PAGE: fetched()}, renderer=renderer)  # fast would be enough

    envelope = harness.run_once(
        mode="auto", actions=[{"type": "click", "selector": "#more"}], cache=True
    )

    assert envelope.status == "success"
    assert data_of(envelope).mode_used == "browser"
    assert data_of(envelope).cache == "bypass"
    assert harness.fetcher.page_calls() == []  # rendered directly, like a screenshot
    (call,) = renderer.calls
    assert [action.type for action in call.options.actions] == ["click"]


def test_a_screenshot_is_never_compared_with_a_draft() -> None:
    drafting = RenderHarness({PAGE: fetched()})  # a fine fast result with many words
    draft = drafting.run_once(mode="auto")
    assert draft.status == "success"
    harness = RenderHarness(
        renderer=FakeRenderer(rendered(SHORT_PAGE, screenshot=PNG, screenshot_size=(1920, 1080)))
    )
    artifacts = FakeArtifacts()

    envelope = harness.run_once(mode="auto", screenshot=True, artifacts=artifacts, draft=draft)

    data = data_of(envelope)
    assert data.mode_used == "browser"
    assert data.screenshot is not None
    assert len(artifacts.screenshots) == 1


# --- later navigations and redirects ----------------------------------------------------------


def test_navigation_to_robots_disallowed_url_is_refused_before_send() -> None:
    renderer = FakeRenderer(rendered(), navigations=[MOVED])
    harness = RenderHarness({ROBOTS: fetched(ROBOTS, DISALLOW_PRIVATE)}, renderer=renderer)

    envelope = harness.run_once(mode="browser")

    assert code_of(envelope) == ErrorCode.BLOCKED_BY_ROBOTS
    assert data_of(envelope).compliance.robots == "disallowed"
    assert data_of(envelope).content is None
    assert renderer.sent == []
    assert harness.leases() == 0


@pytest.mark.parametrize(
    ("target", "code"),
    [
        ("http://169.254.169.254/latest/meta-data/", ErrorCode.BLOCKED_URL),
        ("http://[::1]:8080/admin", ErrorCode.BLOCKED_URL),
        ("ftp://example.org/file.txt", ErrorCode.INVALID_URL),
    ],
)
def test_navigation_to_private_address_is_refused(target: str, code: ErrorCode) -> None:
    renderer = FakeRenderer(rendered(), navigations=[target])
    harness = RenderHarness(renderer=renderer)

    envelope = harness.run_once(mode="browser")

    assert code_of(envelope) == code
    assert renderer.sent == []
    assert all("169.254" not in url and "::1" not in url for url in harness.fetched_urls())
    assert harness.leases() == 0


def test_navigation_to_other_host_moves_the_turn() -> None:
    target = f"{OTHER}/learn/page.html"
    renderer = FakeRenderer(
        rendered(url=target, responses=[response(PAGE), response(target)]),
        navigations=[target],
    )
    harness = RenderHarness(renderer=renderer)

    envelope = harness.run_once(mode="browser")

    assert envelope.status == "success"
    assert data_of(envelope).final_url == target
    assert renderer.sent == [target]
    assert f"{OTHER}/robots.txt" in harness.fetched_urls()
    assert harness.gate.held() == []  # and never two turns at once
    assert harness.gate.events == [
        ("take", "example.org"),
        ("give back", "example.org"),
        ("take", "other.example"),
        ("record", "example.org"),
        ("record", "other.example"),
        ("give back", "other.example"),
    ]
    assert harness.leases() == 0


def test_http_redirect_to_disallowed_path_discards_the_render() -> None:
    renderer = FakeRenderer(
        rendered(url=MOVED, responses=[response(MOVED, redirects=[PAGE])]),
    )
    harness = RenderHarness({ROBOTS: fetched(ROBOTS, DISALLOW_PRIVATE)}, renderer=renderer)

    envelope = harness.run_once(mode="browser")

    assert code_of(envelope) == ErrorCode.BLOCKED_BY_ROBOTS
    data = data_of(envelope)
    assert data.content is None
    assert data.word_count == 0
    assert data.attempts == 1
    assert harness.keys("res:*") == []
    assert harness.keys("fp:*") == []


def test_cross_host_http_redirect_is_checked_with_its_origins_files() -> None:
    target = f"{OTHER}/private/page.html"
    other_robots = f"{OTHER}/robots.txt"
    renderer = FakeRenderer(
        rendered(url=target, responses=[response(target, redirects=[PAGE])]),
    )
    harness = RenderHarness(
        {other_robots: fetched(other_robots, DISALLOW_PRIVATE)}, renderer=renderer
    )

    envelope = harness.run_once(mode="browser")

    assert code_of(envelope) == ErrorCode.BLOCKED_BY_ROBOTS
    assert other_robots in harness.fetched_urls()
    assert harness.gate.held() == []
    assert harness.leases() == 0


@pytest.mark.parametrize(("compliance", "status"), [("enforce", "failed"), ("report", "success")])
def test_noai_meta_in_rendered_dom_blocks_under_enforce(compliance: str, status: str) -> None:
    harness = RenderHarness(renderer=FakeRenderer(rendered(NOAI_PAGE)))

    envelope = harness.run_once(mode="browser", compliance=compliance, principal=ADMIN)

    assert envelope.status == status
    assert data_of(envelope).compliance.noai is True
    if status == "failed":
        assert code_of(envelope) == ErrorCode.TDM_RESERVED
        assert data_of(envelope).content is None


def test_attempts_count_main_responses() -> None:
    renderer = FakeRenderer(
        rendered(
            url=NEXT,
            responses=[response(PAGE), response(NEXT, redirects=[f"{ORIGIN}/learn/old.html"])],
        ),
        navigations=[f"{ORIGIN}/learn/old.html"],
    )
    harness = RenderHarness(renderer=renderer)

    envelope = harness.run_once(mode="browser")

    assert envelope.status == "success"
    assert data_of(envelope).attempts == 2
    assert harness.gate.events.count(("record", "example.org")) == 2


@pytest.mark.parametrize(
    "outcome", ["rendered", "fetch_error", "crash"], ids=["ok", "fetch_error", "crash"]
)
def test_turn_is_held_during_render_and_released_after(outcome: str) -> None:
    seen: dict[str, Any] = {}
    harness: RenderHarness

    async def inflight() -> int:
        return sum(
            [
                await harness.valkey.zcard(key)
                async for key in harness.valkey.scan_iter("out:inflight:*")
            ]
        )

    async def during() -> None:
        seen["held"] = harness.gate.held()
        seen["leases"] = await inflight()

    result: Any = {
        "rendered": rendered(),
        "fetch_error": FetchError(ErrorCode.TIMEOUT, "The page did not load.", retryable=True),
        "crash": RuntimeError("browser gone"),
    }[outcome]
    harness = RenderHarness(renderer=FakeRenderer(result, during=during))

    async def scenario() -> Envelope[ExtractResult] | None:
        # Looked at in the same loop: a slot left open would only be closed at its shutdown.
        envelope = None
        if outcome == "crash":
            with pytest.raises(RuntimeError):
                await harness.run(mode="browser")
        else:
            envelope = await harness.run(mode="browser")
        seen["held after"] = harness.gate.held()
        seen["leases after"] = await inflight()
        return envelope

    envelope = asyncio.run(scenario())

    if outcome == "fetch_error":
        assert envelope is not None
        assert code_of(envelope) == ErrorCode.TIMEOUT
        assert envelope.error is not None
        assert envelope.error.retryable is True
        assert data_of(envelope).mode_used == "browser"
    assert seen["held"] == ["example.org"]
    assert seen["leases"] >= 1
    assert seen["held after"] == []
    assert seen["leases after"] == 0


@pytest.mark.parametrize(
    ("error", "recorded", "paused"),
    [
        (FetchError(ErrorCode.TIMEOUT, "The page did not load.", retryable=True), True, False),
        (FetchError(ErrorCode.CONNECTION_ERROR, "Reset.", retryable=True), True, False),
        (
            FetchError(ErrorCode.TIMEOUT, "Still waiting.", retryable=True, upstream_status=429),
            True,
            True,
        ),
        (FetchError(ErrorCode.CONVERSION_FAILED, "The page kept navigating."), False, False),
    ],
    ids=["timeout", "connection_error", "timeout-after-429", "not-the-hosts-fault"],
)
def test_a_failed_render_is_reported_to_the_gate(
    error: FetchError, recorded: bool, paused: bool
) -> None:
    harness = RenderHarness(renderer=FakeRenderer(error))

    envelope = harness.run_once(mode="browser")

    assert code_of(envelope) == error.code
    assert data_of(envelope).upstream_status == error.upstream_status
    records = [event for event in harness.gate.events if event[0] == "record"]
    assert records == ([("record", "example.org")] if recorded else [])
    # Recorded inside the turn, before it was given back.
    if recorded:
        assert harness.gate.events.index(records[0]) < harness.gate.events.index(
            ("give back", "example.org")
        )
    assert bool(harness.keys("out:cooldown:*")) is paused


def test_a_navigation_refused_by_the_check_is_not_reported_to_the_gate() -> None:
    harness = RenderHarness(
        {ROBOTS: fetched(ROBOTS, DISALLOW_PRIVATE)},
        renderer=FakeRenderer(rendered(), navigations=[MOVED]),
    )

    envelope = harness.run_once(mode="browser")

    assert code_of(envelope) == ErrorCode.BLOCKED_BY_ROBOTS
    assert ("record", "example.org") not in harness.gate.events


def test_a_navigation_back_takes_the_hosts_own_crawl_delay() -> None:
    target = f"{OTHER}/learn/page.html"
    renderer = FakeRenderer(
        rendered(responses=[response(PAGE), response(target), response(PAGE)]),
        navigations=[target, PAGE],  # A -> B -> A
    )
    harness = RenderHarness(
        {ROBOTS: fetched(ROBOTS, "User-agent: *\nCrawl-delay: 0.1\n")}, renderer=renderer
    )

    envelope = harness.run_once(mode="browser")

    assert envelope.status == "success"
    assert renderer.sent == [target, PAGE]
    # A's first turn was taken for its robots.txt load, before its delay was known, and is
    # kept for the page; back on A, the turn has A's own delay, not B's.
    assert harness.gate.crawl_delays == [
        ("example.org", None),
        ("other.example", None),
        ("example.org", 0.1),
    ]
    assert harness.gate.held() == []


def test_http_redirect_hop_to_a_private_address_is_blocked_url() -> None:
    hop = "http://10.0.0.5/hop"
    renderer = FakeRenderer(rendered(url=NEXT, responses=[response(NEXT, redirects=[PAGE, hop])]))
    harness = RenderHarness(renderer=renderer)

    envelope = harness.run_once(mode="browser")

    assert code_of(envelope) == ErrorCode.BLOCKED_URL
    assert data_of(envelope).content is None
    assert all("10.0.0.5" not in url for url in harness.fetched_urls())
    assert harness.leases() == 0


# --- screenshots --------------------------------------------------------------------------------


def test_screenshot_is_stored_and_referenced() -> None:
    renderer = FakeRenderer(rendered(SHORT_PAGE, screenshot=PNG, screenshot_size=(1920, 3000)))
    harness = RenderHarness(renderer=renderer)
    artifacts = FakeArtifacts()

    envelope = harness.run_once(mode="browser", screenshot=True, artifacts=artifacts)

    # A stored screenshot is a success; the thin page's quality is reported, not failed.
    assert envelope.status == "success"
    assert envelope.error is None
    data = data_of(envelope)
    assert data.screenshot is not None
    assert data.screenshot.url == "/v1/jobs/job-1/artifacts/screenshot.png"
    assert data.screenshot.bytes == len(PNG)
    assert (data.screenshot.width, data.screenshot.height) == (1920, 3000)
    assert data.quality is not None
    assert data.quality.word_count < 50
    assert artifacts.screenshots == [(PNG, 1920, 3000)]
    (call,) = renderer.calls
    assert call.options.screenshot is True
    assert call.options.full_page is True
    assert call.options.blocked_resources == frozenset()  # a screenshot shows the images
    assert data.cache == "bypass"  # the artifact belongs to this item only
    assert harness.keys("res:*") == []


def test_screenshot_sink_refusal_is_retryable_internal_error() -> None:
    harness = RenderHarness(
        renderer=FakeRenderer(rendered(screenshot=PNG, screenshot_size=(1920, 1080)))
    )

    envelope = harness.run_once(
        mode="browser", screenshot=True, artifacts=FakeArtifacts(refuse=True)
    )

    assert code_of(envelope) == ErrorCode.INTERNAL_ERROR
    assert envelope.error is not None
    assert envelope.error.retryable is True


def test_screenshot_over_the_byte_limit_is_too_large() -> None:
    big = PNG + b"\x00" * 2048
    harness = RenderHarness(
        renderer=FakeRenderer(rendered(screenshot=big, screenshot_size=(1920, 1080))),
        screenshot_max_bytes=1024,
    )
    artifacts = FakeArtifacts()

    envelope = harness.run_once(mode="browser", screenshot=True, artifacts=artifacts)

    assert code_of(envelope) == ErrorCode.TOO_LARGE
    assert artifacts.screenshots == []


def test_screenshot_needs_an_artifact_sink() -> None:
    harness = RenderHarness(renderer=FakeRenderer(rendered(screenshot=PNG)))

    with pytest.raises(ValueError, match="artifact"):
        harness.run_once(mode="browser", screenshot=True)


# --- cache and fingerprints ---------------------------------------------------------------------


def test_requests_with_secrets_bypass_cache_and_fingerprints() -> None:
    renderer = FakeRenderer(rendered())
    harness = RenderHarness(renderer=renderer)

    plain, with_cookie, with_header = harness.run_all(
        {"mode": "browser"},
        {"mode": "browser", "cookies": [{"name": "session", "value": "s3cret"}]},
        {"mode": "browser", "headers": {"Authorization": "Bearer token"}},
    )

    assert data_of(plain).cache == "miss"
    assert data_of(with_cookie).cache == "bypass"  # not served from the plain run's entry
    assert data_of(with_header).cache == "bypass"
    fingerprint = data_of(with_cookie).fingerprint
    assert fingerprint is not None
    assert fingerprint.changed is None  # not compared
    assert len(renderer.calls) == 3
    assert len(harness.keys("res:*")) == 1  # the plain run's only
    cookie_call, header_call = renderer.calls[1], renderer.calls[2]
    assert cookie_call.options.cookies == (
        CookieData(name="session", value="s3cret", domain="example.org"),
    )
    assert header_call.options.headers == {"authorization": "Bearer token"}
    assert "s3cret" not in repr(cookie_call.options)
    assert "token" not in repr(header_call.options)


def test_browser_results_are_never_revalidated() -> None:
    renderer = FakeRenderer(rendered(responses=[response(headers={"etag": '"v1"'})]))
    harness = RenderHarness(renderer=renderer, result_cache_ttl_seconds=0)  # never fresh

    first, second = harness.run_all({"mode": "browser"}, {"mode": "browser"})

    assert data_of(first).cache == "miss"
    assert data_of(second).cache == "miss"  # rendered again, not revalidated
    assert len(renderer.calls) == 2
    (key,) = harness.keys("res:*")

    async def stored() -> dict[str, Any]:
        raw = await harness.valkey.get(key)
        assert raw is not None
        return cast(dict[str, Any], json.loads(raw))

    entry = asyncio.run(stored())
    assert entry["validators"] == {"etag": None, "last_modified": None}


def test_a_stale_rendered_result_is_not_revalidated_by_the_same_bytes() -> None:
    harness = RenderHarness(
        {PAGE: fetched(body=SHELL)}, renderer=FakeRenderer(rendered()), result_cache_ttl_seconds=0
    )
    first = harness.run_once(mode="auto")
    assert data_of(first).mode_used == "browser"
    # The fast fetch now gets bytes whose hash is the stored rendered HTML's.
    harness.fetcher.answers[PAGE] = fetched()

    second = harness.run_once(mode="auto")

    assert data_of(second).cache == "miss"
    assert data_of(second).mode_used == "fast"


def test_auto_result_that_will_escalate_is_not_cached() -> None:
    escalating = RenderHarness({PAGE: fetched(body=SHELL_WITH_WORDS)})  # no renderer

    first, second = escalating.run_all({"mode": "auto"}, {"mode": "auto"})

    for envelope in (first, second):
        assert envelope.status == "partial"
        assert data_of(envelope).cache == "miss"
        assert needs_browser(envelope, unblock=False)
    assert escalating.keys("res:*") == []
    assert escalating.keys("fp:*") == []
    assert len(escalating.fetcher.page_calls()) == 2

    disabled = RenderHarness({PAGE: fetched(body=SHELL_WITH_WORDS)}, browser_enabled=False)
    _, again = disabled.run_all({"mode": "auto"}, {"mode": "auto"})
    assert data_of(again).cache == "hit"  # nothing escalates, so it is stored as before


@pytest.mark.parametrize(
    ("principal", "compliance", "unblock", "assessed"),
    [
        (ADMIN, "ignore", True, True),
        (ADMIN, "ignore", False, False),
        (ADMIN, "enforce", True, False),
    ],
)
def test_challenge_answer_is_assessed_only_under_unblock(
    principal: Principal, compliance: str, unblock: bool, assessed: bool
) -> None:
    harness = RenderHarness(
        {PAGE: fetched(body=CHALLENGE_PAGE, status=403)}, browser_unblock_enabled=unblock
    )

    envelope = harness.run_once(mode="auto", compliance=compliance, principal=principal)

    assert code_of(envelope) == ErrorCode.TARGET_HTTP_ERROR
    quality = data_of(envelope).quality
    if assessed:
        assert quality is not None
        assert quality.challenge_status is True
        assert needs_browser(envelope, unblock=True)
    else:
        assert quality is None
        assert not needs_browser(envelope, unblock=unblock)


# --- the rendered document ----------------------------------------------------------------------


@pytest.mark.parametrize("content_type", ["application/pdf", "text/plain", None])
def test_download_or_non_html_is_unsupported_content_type(content_type: str | None) -> None:
    harness = RenderHarness(
        renderer=FakeRenderer(rendered("", responses=[response(content_type=content_type)]))
    )

    envelope = harness.run_once(mode="browser")

    assert code_of(envelope) == ErrorCode.UNSUPPORTED_CONTENT_TYPE
    assert data_of(envelope).mode_used == "browser"
    assert data_of(envelope).upstream_status == 200


@pytest.mark.parametrize(("status", "retryable"), [(404, False), (503, True)])
def test_final_non_2xx_render_is_target_http_error(status: int, retryable: bool) -> None:
    harness = RenderHarness(
        renderer=FakeRenderer(rendered(responses=[response(status=status)], warnings=["w"]))
    )

    envelope = harness.run_once(mode="browser")

    assert code_of(envelope) == ErrorCode.TARGET_HTTP_ERROR
    assert envelope.error is not None
    assert envelope.error.retryable is retryable
    assert data_of(envelope).upstream_status == status
    assert data_of(envelope).warnings == ["w"]


def test_rendered_html_over_the_conversion_limit_is_too_large() -> None:
    harness = RenderHarness(
        renderer=FakeRenderer(rendered(ARTICLE + "x" * 4096)), convert_max_bytes=4096
    )

    envelope = harness.run_once(mode="browser")

    assert code_of(envelope) == ErrorCode.TOO_LARGE


def test_renderer_warnings_are_appended() -> None:
    harness = RenderHarness(renderer=FakeRenderer(rendered(warnings=["browser_bytes_capped"])))

    envelope = harness.run_once(mode="browser")

    assert data_of(envelope).warnings[-1] == "browser_bytes_capped"


@pytest.mark.parametrize(
    ("page", "hosts", "code"),
    [
        (SHORT_PAGE, ["challenges.cloudflare.com"], ErrorCode.BOT_PROTECTION),
        (SHORT_PAGE, ["newassets.hcaptcha.com"], ErrorCode.BOT_PROTECTION),
        (SHORT_PAGE, ["www.google.com"], ErrorCode.CONTENT_TOO_SHORT),
        (ARTICLE, ["challenges.cloudflare.com"], None),
    ],
)
def test_challenge_host_marks_thin_page_as_bot_challenge(
    page: str, hosts: list[str], code: ErrorCode | None
) -> None:
    harness = RenderHarness(renderer=FakeRenderer(rendered(page, hosts=hosts)))

    envelope = harness.run_once(mode="browser")

    quality = data_of(envelope).quality
    assert quality is not None
    if code is None:
        assert envelope.status == "success"
        assert quality.bot_challenge is False
    else:
        assert code_of(envelope) == code
        assert quality.bot_challenge is (code == ErrorCode.BOT_PROTECTION)
    if code == ErrorCode.BOT_PROTECTION:
        assert quality.browser_recommended is True
        assert data_of(envelope).fingerprint is None
        assert harness.keys("res:*") == []


# --- unblock mode -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("principal", "compliance", "enabled", "unblocked"),
    [
        (ADMIN, "ignore", True, True),
        (ADMIN, "enforce", True, False),
        (ADMIN, "ignore", False, False),
        (USER, None, True, False),  # the operator's default is ignore, but the caller a user
    ],
    ids=["admin-ignore-on", "admin-enforce", "switch-off", "user-default-ignore"],
)
def test_unblock_renderer_used_only_with_ignore_and_switch(
    principal: Principal, compliance: str | None, enabled: bool, unblocked: bool
) -> None:
    standard = FakeRenderer(rendered())
    unblock = FakeRenderer(rendered())
    harness = RenderHarness(
        renderer=standard,
        unblock_renderer=unblock,
        browser_unblock_enabled=enabled,
        compliance_default="ignore" if principal is USER else "enforce",
    )

    envelope = harness.run_once(mode="browser", compliance=compliance, principal=principal)

    assert envelope.status == "success"
    assert data_of(envelope).unblocked is unblocked
    used, unused = (unblock, standard) if unblocked else (standard, unblock)
    assert unused.calls == []
    (call,) = used.calls
    assert call.options.unblock is unblocked
    if unblocked:
        assert call.options.blocked_resources == frozenset()  # unblock mode blocks nothing


def test_clearance_cookies_loaded_and_stored_only_for_unblock() -> None:
    old = CookieData(name="cf_clearance", value="old", domain=".example.org", expires=2e9)
    new = CookieData(name="cf_clearance", value="new", domain=".example.org", expires=2e9)
    clearance = FakeClearance({"example.org": [old]})
    standard = FakeRenderer(rendered(cookies=[new]))
    unblock = FakeRenderer(
        rendered(cookies=[new]),
        rendered(CHALLENGE_PAGE, cookies=[new], warnings=["challenge_not_cleared"]),
    )
    harness = RenderHarness(
        renderer=standard,
        unblock_renderer=unblock,
        clearance=clearance,
        browser_unblock_enabled=True,
    )

    standard_run, unblocked_run, challenged_run = harness.run_all(
        {"mode": "browser", "principal": ADMIN},
        {"mode": "browser", "principal": ADMIN, "compliance": "ignore"},
        {"mode": "browser", "principal": ADMIN, "compliance": "ignore", "cache": False},
    )

    assert data_of(standard_run).unblocked is False
    assert standard.calls[0].options.cookies == ()
    assert data_of(unblocked_run).unblocked is True
    assert unblock.calls[0].options.cookies == (old,)
    assert code_of(challenged_run) == ErrorCode.BOT_PROTECTION
    assert clearance.gets == ["example.org", "example.org"]  # the unblock renders only
    assert clearance.puts == [("example.org", [new])]  # not after the challenge stayed


def unblock_harness(*results: Any, **overrides: Any) -> RenderHarness:
    return RenderHarness(
        renderer=FakeRenderer(rendered()),
        unblock_renderer=FakeRenderer(*results),
        clearance=FakeClearance(),
        browser_unblock_enabled=True,
        **overrides,
    )


def gate_records(harness: RenderHarness) -> list[tuple[str, str]]:
    return [event for event in harness.gate.events if event[0] == "record"]


@pytest.mark.parametrize("status", [403, 429, 503])
def test_a_cleared_challenge_is_kept_out_of_the_hosts_backoff(status: int) -> None:
    answers = [response(status=status), response(status=200)]
    harness = unblock_harness(rendered(responses=answers, challenge_cleared=True))

    envelope = harness.run_once(mode="browser", principal=ADMIN, compliance="ignore")

    assert envelope.status == "success", envelope.error
    assert data_of(envelope).attempts == 2
    assert gate_records(harness) == [("record", "example.org")]  # the final answer only
    assert harness.keys("out:cooldown:*") == []


def test_a_challenge_that_stays_keeps_its_backoff() -> None:
    harness = unblock_harness(
        rendered(
            CHALLENGE_PAGE,
            responses=[response(status=503)],
            warnings=["challenge_not_cleared"],
        )
    )

    envelope = harness.run_once(mode="browser", principal=ADMIN, compliance="ignore")

    assert code_of(envelope) == ErrorCode.BOT_PROTECTION
    assert gate_records(harness) == [("record", "example.org")]
    assert harness.keys("out:cooldown:*") != []  # 503: the host is paused


def test_the_cleared_flag_counts_only_under_unblock() -> None:
    answers = [response(status=503), response(status=200)]
    harness = RenderHarness(
        renderer=FakeRenderer(rendered(responses=answers, challenge_cleared=True))
    )

    envelope = harness.run_once(mode="browser")

    assert envelope.status == "success"
    assert gate_records(harness) == [("record", "example.org")] * 2
    assert harness.keys("out:cooldown:*") != []


@pytest.mark.parametrize("status", [403, 429, 503, 200])
def test_a_challenge_not_cleared_is_bot_protection_whatever_its_status(status: int) -> None:
    clearance = FakeClearance()
    harness = RenderHarness(
        renderer=FakeRenderer(rendered()),
        unblock_renderer=FakeRenderer(
            rendered(
                ARTICLE,  # the page itself need not look like a challenge: the renderer says so
                responses=[response(status=status)],
                warnings=["challenge_not_cleared"],
            )
        ),
        clearance=clearance,
        browser_unblock_enabled=True,
    )

    envelope = harness.run_once(mode="browser", principal=ADMIN, compliance="ignore")

    assert code_of(envelope) == ErrorCode.BOT_PROTECTION
    data = data_of(envelope)
    assert data.unblocked is True
    assert data.upstream_status == status
    assert "challenge_not_cleared" in data.warnings
    assert data.fingerprint is None
    assert clearance.puts == []
    assert harness.keys("res:*") == []


# --- deadline and fast mode ---------------------------------------------------------------------


def capture_deadline_left(seen: list[float]) -> Callable[[], Awaitable[None]]:
    async def during() -> None:
        seen.append(asyncio.get_running_loop().time())

    return during


@pytest.mark.parametrize(
    ("timeout_seconds", "unblocked", "expected"),
    [(None, False, 25), (40, False, 40), (40, True, 60)],
)
def test_render_deadline_follows_the_timeouts(
    timeout_seconds: float | None, unblocked: bool, expected: float
) -> None:
    now: list[float] = []
    standard = FakeRenderer(rendered(), during=capture_deadline_left(now))
    unblock = FakeRenderer(rendered(), during=capture_deadline_left(now))
    harness = RenderHarness(
        renderer=standard,
        unblock_renderer=unblock,
        timeout_seconds=timeout_seconds,
        browser_unblock_enabled=True,
    )

    harness.run_once(
        mode="browser", principal=ADMIN, compliance="ignore" if unblocked else "enforce"
    )

    (call,) = (unblock if unblocked else standard).calls
    left = call.deadline - now[0]
    assert expected - 3 < left < expected  # a margin stays for the conversion


@pytest.mark.parametrize("browser_enabled", [False, True])
def test_fast_mode_unchanged(browser_enabled: bool) -> None:
    renderer = FakeRenderer()
    harness = RenderHarness(
        {PAGE: fetched(body=SHELL)}, renderer=renderer, browser_enabled=browser_enabled
    )
    plain = RenderHarness({PAGE: fetched()}, renderer=renderer, browser_enabled=browser_enabled)

    shell = harness.run_once(mode="fast")
    article, hit = plain.run_all({"mode": "fast"}, {"mode": "fast"})

    assert code_of(shell) == ErrorCode.EMPTY_CONTENT
    assert data_of(shell).mode_used == "fast"
    quality = data_of(shell).quality
    assert quality is not None
    assert quality.browser_recommended is True
    assert article.status == "success"
    assert data_of(article).attempts == 1
    assert PARAGRAPH.strip()[:40] in (data_of(article).content or "")
    assert data_of(hit).cache == "hit"
    assert renderer.calls == []
