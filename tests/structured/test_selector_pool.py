"""Selector rules in killable child processes (ruling R19, final review C1).

soupsieve backtracks without a budget: on ten chains of 60 nested ``<div>`` the CSS selector
``span div div div div`` takes about two minutes, each further ``div`` about ten times more. A
caller's rule, a learned rule and the learning check all run in a selector child that is killed
at ``STRUCTURED_SELECTOR_TIMEOUT_SECONDS`` (0.5 s here): the fields stay missing with
``selectors_timed_out``, the result is not failed, and a fresh child serves the next request."""

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis
from prometheus_client import CollectorRegistry
from pytest_httpserver import HTTPServer

from tests.fakes import PAGE, FakeFetcher, fetched
from text_extraction.auth import Principal
from text_extraction.compliance import ComplianceChecker
from text_extraction.conversion_pool import ConversionPool
from text_extraction.documents.pool import DocumentPool
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractRequest, ExtractResult
from text_extraction.fetch import Fetcher
from text_extraction.fingerprint import FingerprintStore
from text_extraction.llm.client import LlmClient
from text_extraction.outbound import DomainGate
from text_extraction.pipeline import Pipeline
from text_extraction.result_cache import ResultCache
from text_extraction.settings import Settings
from text_extraction.structured.post import apply_post
from text_extraction.structured.rules import RuleRequest
from text_extraction.structured.selector_pool import (
    SELECTOR_TARGET,
    SELECTORS_FAILED_WARNING,
    SELECTORS_TIMED_OUT_WARNING,
    SelectorRunner,
    create_selector_runner,
)
from text_extraction.structured.store import SelectorStore, selector_host
from text_extraction.structured.types import LearnReport, SelectorRule, StructuredOptions

from .post_helpers import answer, chat_requests, dom_result, envelope, llm_settings, serve_chat

USER = Principal(name="alice", role="user", rate_limit_per_minute=None)
TIMEOUT = 0.5
# The finish of a killed run: the timeout, the child's start (an interpreter that imports lxml
# and BeautifulSoup) and the page's conversion, with room for a slow CI machine.
MARGIN = 4.0
CHAIN = "<div>" * 60 + "deep" + "</div>" * 60
DEEP_PAGE = (
    "<!doctype html><html><head><title>Deep</title>"
    '<meta name="description" content="A deeply nested page.">'
    "</head><body><h1>Deep title</h1>"
    + CHAIN * 10
    + "<p>Some words of text so that the page is not too short to keep.</p>" * 5
    + "</body></html>"
)
DEEP_SELECTOR = "span div div div div div"  # hours on this page without a kill
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slow": {"type": "string"},
        "title": {"type": "string"},
        "description": {"type": "string"},
    },
}


def settings_with(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "auth_mode": "none",
        "outbound_min_delay_ms": 0,
        "structured_selector_timeout_seconds": TIMEOUT,
    }
    values.update(overrides)
    return Settings(**values)


def rule(field: str, css: str) -> RuleRequest:
    return RuleRequest(field=field, css=css, all=False)


# --- the runner -----------------------------------------------------------------------------


def test_a_run_over_the_deadline_is_killed_and_its_child_replaced() -> None:
    runner = create_selector_runner(settings_with())

    async def scenario() -> tuple[Any, float, Any, list[int], list[int]]:
        try:
            await runner.start()
            before = runner.child_pids()
            started = time.perf_counter()
            slow = await runner.run(DEEP_PAGE, PAGE, [rule("slow", DEEP_SELECTOR)])
            elapsed = time.perf_counter() - started
            fast = await runner.run(DEEP_PAGE, PAGE, [rule("title", "h1")])
            return slow, elapsed, fast, before, runner.child_pids()
        finally:
            await runner.shutdown()

    slow, elapsed, fast, before, after = asyncio.run(scenario())

    assert slow.warning == SELECTORS_TIMED_OUT_WARNING
    assert slow.found == {}
    assert elapsed < TIMEOUT + MARGIN
    assert fast.warning is None
    assert fast.found == {"title": (["Deep title"], False)}
    assert len(before) == len(after) == 1
    assert before != after  # the killed child was replaced


def test_the_caller_deadline_caps_a_run() -> None:
    runner = create_selector_runner(settings_with(structured_selector_timeout_seconds=30))

    async def scenario() -> tuple[Any, float]:
        try:
            await runner.start()
            started = time.perf_counter()
            deadline = asyncio.get_running_loop().time() + TIMEOUT
            outcome = await runner.run(
                DEEP_PAGE, PAGE, [rule("slow", DEEP_SELECTOR)], deadline=deadline
            )
            return outcome, time.perf_counter() - started
        finally:
            await runner.shutdown()

    outcome, elapsed = asyncio.run(scenario())

    assert outcome.warning == SELECTORS_TIMED_OUT_WARNING
    assert elapsed < TIMEOUT + 1


def test_a_crashed_child_is_selectors_failed() -> None:
    def crashing() -> DocumentPool:
        return DocumentPool(1, 512, target="tests.structured.selector_pool_targets")

    runner = SelectorRunner(crashing, timeout_seconds=5)

    async def scenario() -> Any:
        try:
            return await runner.run("<p>x</p>", PAGE, [rule("a", "p")])
        finally:
            await runner.shutdown()

    outcome = asyncio.run(scenario())

    assert outcome.warning == SELECTORS_FAILED_WARNING
    assert outcome.found == {}


def test_a_runner_follows_the_event_loop() -> None:
    """Tests run each request on an event loop of its own: the runner starts fresh children
    on each, and the earlier ones are killed."""
    runner = create_selector_runner(settings_with())

    async def once() -> tuple[Any, list[int]]:
        return await runner.run("<h1>A</h1>", PAGE, [rule("title", "h1")]), runner.child_pids()

    first, first_pids = asyncio.run(once())
    try:
        second, second_pids = asyncio.run(once())
        until = time.monotonic() + 5
        while any(Path(f"/proc/{pid}").exists() for pid in first_pids):
            if time.monotonic() > until:
                break
            time.sleep(0.05)
    finally:
        asyncio.run(runner.shutdown())

    assert first.found == second.found == {"title": (["A"], False)}
    assert first_pids != second_pids
    # Issue #19 (N-m2): the earlier children are reaped, not left as zombies.
    assert [pid for pid in first_pids if Path(f"/proc/{pid}").exists()] == []


def test_a_failed_start_is_retried_with_a_fresh_pool() -> None:
    """A lazy start that fails (a child that cannot load its target) shuts its pool down; the
    next run starts a fresh one instead of failing on the closed pool for good (N-m1)."""
    made: list[DocumentPool] = []

    def pools() -> DocumentPool:
        target = "tests.structured.no_such_target" if not made else SELECTOR_TARGET
        made.append(DocumentPool(1, 512, target=target))
        return made[-1]

    runner = SelectorRunner(pools, timeout_seconds=5)

    async def scenario() -> tuple[Any, Any]:
        try:
            first = await runner.run("<h1>A</h1>", PAGE, [rule("title", "h1")])
            second = await runner.run("<h1>A</h1>", PAGE, [rule("title", "h1")])
            return first, second
        finally:
            await runner.shutdown()

    first, second = asyncio.run(scenario())

    assert first.warning == SELECTORS_FAILED_WARNING
    assert second.warning is None
    assert second.found == {"title": (["A"], False)}
    assert len(made) == 2


def test_no_rules_start_no_child() -> None:
    runner = create_selector_runner(settings_with())

    async def scenario() -> Any:
        return await runner.run(DEEP_PAGE, PAGE, [])

    assert asyncio.run(scenario()).warning is None
    assert runner.child_pids() == []


# --- through the pipeline: a caller's rule and a learned one --------------------------------


class Service:
    """A pipeline whose selector children are the test's, with a fake fetcher of the deep
    page; all requests of a test run on one event loop."""

    def __init__(self, runner: SelectorRunner | None = None) -> None:
        self.settings = settings_with()
        self.valkey = FakeAsyncRedis()
        self.store = SelectorStore(self.valkey, 3600)
        self.runner = runner if runner is not None else create_selector_runner(self.settings)
        self.pool = ConversionPool(1)
        fetcher = cast(Fetcher, FakeFetcher({PAGE: fetched(body=DEEP_PAGE)}))
        self.pipeline = Pipeline(
            fetcher,
            DomainGate(self.valkey, self.settings),
            ComplianceChecker(fetcher, self.valkey, self.settings),
            ResultCache(self.valkey, self.settings),
            FingerprintStore(self.valkey, self.settings),
            self.settings,
            CollectorRegistry(),
            self.pool,
            selector_store=self.store,
            selector_runner=self.runner,
        )

    async def extract(
        self, structured: dict[str, Any], *, cache: bool = False
    ) -> tuple[Envelope[ExtractResult], float]:
        request = ExtractRequest.model_validate(
            {"url": PAGE, "structured": structured, "cache": cache}
        )
        started = time.perf_counter()
        envelope = await self.pipeline.run(request, USER)
        return envelope, time.perf_counter() - started

    async def close(self) -> None:
        await self.runner.shutdown()
        self.pool.shutdown()


def _values(envelope: Envelope[ExtractResult]) -> dict[str, Any]:
    assert envelope.data is not None
    assert envelope.data.structured is not None
    return envelope.data.structured.values


def test_a_caller_rule_on_a_deeply_nested_page_times_out() -> None:
    service = Service()
    slow_options = {
        "schema": SCHEMA,
        "strategy": "auto",
        "selectors": {"slow": {"css": DEEP_SELECTOR}, "title": {"css": "h1"}},
    }

    async def scenario() -> tuple[Any, float, Any, list[int], list[int]]:
        try:
            await service.runner.start()
            before = service.runner.child_pids()
            slow, elapsed = await service.extract(slow_options)
            fast, _ = await service.extract(
                {"schema": SCHEMA, "strategy": "selectors", "selectors": {"title": {"css": "h1"}}}
            )
            return slow, elapsed, fast, before, service.runner.child_pids()
        finally:
            await service.close()

    slow, elapsed, fast, before, after = asyncio.run(scenario())

    assert slow.status == "success"  # the result itself is not failed
    assert elapsed < TIMEOUT + MARGIN
    assert slow.data is not None
    assert SELECTORS_TIMED_OUT_WARNING in slow.data.warnings
    # All rules of the run are lost, metadata still answers for `auto`.
    assert _values(slow) == {
        "slow": None,
        "title": "Deep",
        "description": "A deeply nested page.",
    }
    assert fast.status == "success"
    assert fast.data is not None
    assert SELECTORS_TIMED_OUT_WARNING not in fast.data.warnings
    assert _values(fast)["title"] == "Deep title"
    assert before != after  # the killed child was replaced


def _crashing_runner() -> SelectorRunner:
    return SelectorRunner(
        lambda: DocumentPool(1, 512, target="tests.structured.selector_pool_targets"),
        timeout_seconds=5,
    )


@pytest.mark.parametrize(
    ("make_runner", "warning"),
    [
        (lambda: None, SELECTORS_TIMED_OUT_WARNING),
        (_crashing_runner, SELECTORS_FAILED_WARNING),
    ],
    ids=["timed_out", "failed"],
)
def test_a_result_whose_selectors_did_not_run_is_not_cached(
    make_runner: Callable[[], SelectorRunner | None], warning: str
) -> None:
    """A selector run that timed out or whose child failed is transient (a queue wait, a
    respawn): such a result is not stored in the result cache, and the next identical request
    runs the rules again (N-I1). A result whose rules ran is cached as before."""
    service = Service(make_runner())
    slow = {
        "schema": SCHEMA,
        "strategy": "selectors",
        "selectors": {"slow": {"css": DEEP_SELECTOR}},
    }
    fast = {"schema": SCHEMA, "strategy": "selectors", "selectors": {"title": {"css": "h1"}}}
    runs: list[int] = []
    run = service.runner.run

    async def counted(*args: Any, **kwargs: Any) -> Any:
        runs.append(1)
        return await run(*args, **kwargs)

    service.runner.run = counted  # type: ignore[method-assign]

    async def scenario() -> list[Envelope[ExtractResult]]:
        try:
            first, _ = await service.extract(slow, cache=True)
            second, _ = await service.extract(slow, cache=True)
            if warning == SELECTORS_FAILED_WARNING:
                return [first, second]
            third, _ = await service.extract(fast, cache=True)
            fourth, _ = await service.extract(fast, cache=True)
            return [first, second, third, fourth]
        finally:
            await service.close()

    answers = asyncio.run(scenario())

    for degraded in answers[:2]:
        assert degraded.data is not None
        assert warning in degraded.data.warnings
        assert degraded.data.cache == "miss"  # never served from the cache
    assert len(runs) >= 2  # the second request ran its rules again
    if warning == SELECTORS_TIMED_OUT_WARNING:  # the control: a clean result is cached
        third, fourth = answers[2:]
        assert third.data is not None
        assert fourth.data is not None
        assert third.data.cache == "miss"
        assert fourth.data.cache == "hit"
        assert len(runs) == 3


def test_a_learned_rule_on_a_deeply_nested_page_times_out() -> None:
    service = Service()
    options = StructuredOptions.model_validate({"schema": SCHEMA, "strategy": "selectors"})
    assert options.compiled is not None
    host = selector_host(PAGE)
    assert host is not None
    schema_id = options.compiled.schema_id

    async def scenario() -> tuple[Any, float]:
        try:
            await service.store.put(
                USER.name, host, schema_id, {"slow": SelectorRule(css=DEEP_SELECTOR)}
            )
            return await service.extract({"schema": SCHEMA, "strategy": "selectors"})
        finally:
            await service.close()

    envelope, elapsed = asyncio.run(scenario())

    assert envelope.status == "success"
    assert elapsed < TIMEOUT + MARGIN
    assert envelope.data is not None
    assert SELECTORS_TIMED_OUT_WARNING in envelope.data.warnings
    assert _values(envelope) == {"slow": None, "title": None, "description": None}


# --- the learning check ---------------------------------------------------------------------


def test_the_learning_check_of_a_deep_selector_times_out(httpserver: HTTPServer) -> None:
    """A page can talk the LLM into proposing ``span div div div div div``: its check is killed
    with the run, the rule is not stored and the values stand."""
    settings = llm_settings(httpserver, structured_selector_timeout_seconds=TIMEOUT)
    options = StructuredOptions.model_validate(
        {
            "schema": {"type": "object", "properties": {"name": {"type": "string"}}},
            "strategy": "llm",
            "learn_selectors": True,
        }
    )
    serve_chat(
        httpserver,
        answer({"name": "deep"}),
        answer({"name": {"css": DEEP_SELECTOR, "attr": None}}),
    )
    draft = envelope("# Deep\n\ndeep", structured=dom_result({"name": None}, learn_html=DEEP_PAGE))
    store = SelectorStore(FakeAsyncRedis(), ttl_seconds=60)
    threads = ConversionPool(1, name="post")
    runner = create_selector_runner(settings)

    async def scenario() -> tuple[Envelope[ExtractResult], float, dict[str, SelectorRule]]:
        client = LlmClient(settings, None, registry=CollectorRegistry())
        try:
            started = time.perf_counter()
            done = await apply_post(
                draft,
                options,
                None,
                settings=settings,
                llm=client,
                store=store,
                owner="alice",
                threads=threads,
                selectors=runner,
            )
            elapsed = time.perf_counter() - started
            host = selector_host("https://shop.example.org/item/1")
            assert host is not None
            assert options.compiled is not None
            return done, elapsed, await store.get("alice", host, options.compiled.schema_id)
        finally:
            await client.aclose()
            await runner.shutdown()
            threads.shutdown()

    done, elapsed, stored = asyncio.run(scenario())

    assert done.status == "success"
    assert elapsed < TIMEOUT + MARGIN
    assert done.data is not None
    assert done.data.structured is not None
    assert done.data.structured.values == {"name": "deep"}  # the values stand
    assert done.data.structured.learned == LearnReport(stored=[], rejected=["name"])
    assert done.data.warnings == ["selector_learning_failed"]
    assert done.data.structured.llm is not None
    assert done.data.structured.llm.calls == 2  # the learning call counts
    assert stored == {}
    assert len(chat_requests(httpserver)) == 2
