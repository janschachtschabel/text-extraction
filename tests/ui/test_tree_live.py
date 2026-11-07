"""The site tree refreshes itself while its job runs (package 18): a running job's tree sits in
a wrapper that fetches the tree again every ``TREE_REFRESH_SECONDS``, a finished job's tree does
not; the progress view's tree button says when it may load the tree by itself; the rule
judgement is reused for 30 s per job and origin set (a bounded in-process memo, derived data
only)."""

import asyncio
import threading
import time
from collections.abc import Callable
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from tests.ui.conftest import run, running
from tests.ui.site_items import page_item, sitemap_item
from tests.ui.test_jobs_views import (
    HEADERS,
    ORIGIN,
    SMALL_SITE,
    fake_reads,
    progress,
    rules_fetcher,
    rules_site,
    texts,
    tree,
)
from text_extraction import compliance
from text_extraction.compliance import OriginRules
from text_extraction.conversion_pool import ConversionPool
from text_extraction.envelope import ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.job_models import JobItem
from text_extraction.ui import job_views
from text_extraction.ui import site_tree as tree_module
from text_extraction.ui.i18n import LANGUAGE_HEADER
from text_extraction.ui.job_views import TREE_REFRESH_SECONDS, _RulesMemo

JOB = "a" * 32


def site_tree(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: str,
    live: bool = False,
    language: str = "en",
) -> Any:
    fake_reads(monkeypatch, 1, lambda offset, limit: SMALL_SITE[:1], state=state)
    params = {"id": JOB, **({"live": "1"} if live else {})}
    headers = {**HEADERS, LANGUAGE_HEADER: language}
    response = client.get("/ui/run/site-tree", params=params, headers=headers)
    assert response.status_code == 200
    return tree(response.text)


def test_the_refresh_interval_is_four_seconds() -> None:
    assert TREE_REFRESH_SECONDS == 4


@pytest.mark.parametrize("live", [False, True])
def test_a_running_jobs_tree_has_the_trigger_and_the_note(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, live: bool
) -> None:
    root = site_tree(client, monkeypatch, state="running", live=live)

    [wrapper] = root.xpath("//div[@data-tree-live]")
    assert wrapper.get("hx-get") == f"/ui/run/site-tree?id={JOB}&live=1"
    assert wrapper.get("hx-trigger") == "load delay:4s"
    assert wrapper.get("hx-swap") == "outerHTML"
    assert wrapper.xpath("./section[@class='tree-view']")
    assert texts(root, "//p[contains(@class, 'tree-live')]") == ["Updates while the job runs"]
    assert root.xpath("//*[@aria-live]//p[contains(@class, 'tree-live')]") == []


def test_a_finished_jobs_tree_has_neither(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = site_tree(client, monkeypatch, state="completed", live=True)

    assert root.xpath("//*[@hx-trigger]") == []
    assert root.xpath("//*[@data-tree-live]") == []
    assert root.xpath("//p[contains(@class, 'tree-live')]") == []


def test_the_note_is_german_too(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    root = site_tree(client, monkeypatch, state="running", language="de")

    assert texts(root, "//p[contains(@class, 'tree-live')]") == [
        "Wird aktualisiert, solange der Job läuft"
    ]


def tree_button(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Any:
    fake_reads(
        monkeypatch, 3, lambda offset, limit: SMALL_SITE[offset : offset + limit], state="running"
    )
    html = progress(client, JOB)
    [button] = tree(html).xpath("//button[contains(@class, 'tree-button')]")
    return button


def test_the_tree_button_of_a_site_job_with_results_may_load_the_tree(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    button = tree_button(client, monkeypatch)

    assert button.get("data-tree-auto") is not None
    assert button.get("hx-get") == f"/ui/run/site-tree?id={JOB}"


def test_the_tree_button_of_a_job_without_results_does_not(client: TestClient) -> None:
    html = run(client, "site-structure", url=f"{ORIGIN}/", depth="1").text

    [button] = tree(html).xpath("//button[contains(@class, 'tree-button')]")
    assert button.get("data-tree-auto") is None


def fresh(items: list[JobItem]) -> Callable[[int, int], list[JobItem]]:
    """Pages of copies: the fake read checks that the items of the read before are gone."""
    return lambda offset, limit: [
        item.model_copy(deep=True) for item in items[offset : offset + limit]
    ]


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_the_memo_expires_entries_after_thirty_seconds() -> None:
    clock = Clock()
    memo = _RulesMemo(clock)
    memo.put(("job", frozenset({"o"})), {"o": None})

    assert memo.get(("job", frozenset({"o"}))) == {"o": None}
    clock.now += 29.9
    assert memo.get(("job", frozenset({"o"}))) == {"o": None}
    clock.now += 0.2
    assert memo.get(("job", frozenset({"o"}))) is None
    assert len(memo) == 0


def test_the_memo_holds_at_most_64_entries_and_drops_the_oldest() -> None:
    clock = Clock()
    memo = _RulesMemo(clock)
    for number in range(70):
        clock.now += 0.1
        memo.put((f"job-{number}", frozenset()), {})

    assert len(memo) == 64
    assert memo.get(("job-0", frozenset())) is None
    assert memo.get(("job-69", frozenset())) == {}


def test_the_memo_merges_a_later_put_and_keeps_the_first_expiry() -> None:
    clock = Clock()
    memo = _RulesMemo(clock)
    key = ("job", frozenset({"a", "b"}))
    memo.put(key, {"a": None})
    clock.now += 20
    memo.put(key, {"b": None})

    assert memo.get(key) == {"a": None, "b": None}
    clock.now += 11  # thirty seconds after the first put
    assert memo.get(key) is None


def test_only_the_origins_without_rules_are_read_again(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    monkeypatch.setattr(job_views, "_RULES_MEMO", _RulesMemo(clock))
    loaded: list[str] = []
    answers: dict[str, Any] = {}

    async def origin_rules(pipeline: Any, origin: str, url: str, contacted: bool) -> Any:
        loaded.append(origin)
        return answers[origin]

    monkeypatch.setattr(job_views, "_origin_rules", origin_rules)
    real = rules_site()
    other = "https://other.example"
    items = [*real, page_item(9, f"{other}/x", depth=1)]
    with running(rules_fetcher()) as client:
        pipeline = cast(FastAPI, client.app).state.pipeline
        good = asyncio.run(pipeline.origin_rules(f"{ORIGIN}/"))
        answers.update({ORIGIN: good, other: None})
        fake_reads(monkeypatch, len(items), fresh(items), state="running")
        params = {"id": JOB, "live": "1"}
        client.get("/ui/run/site-tree", params=params, headers=HEADERS)
        assert sorted(loaded) == sorted([ORIGIN, other])
        loaded.clear()
        client.get("/ui/run/site-tree", params=params, headers=HEADERS)
        assert loaded == [other]


def test_live_refreshes_load_the_rules_once_in_thirty_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    monkeypatch.setattr(job_views, "_RULES_MEMO", _RulesMemo(clock))
    loaded: list[str] = []
    with running(rules_fetcher()) as client:
        pipeline = cast(FastAPI, client.app).state.pipeline
        real = pipeline.origin_rules

        async def recording(url: str, **kwargs: Any) -> Any:
            loaded.append(url)
            return await real(url, **kwargs)

        monkeypatch.setattr(pipeline, "origin_rules", recording)
        items = rules_site()
        fake_reads(monkeypatch, len(items), fresh(items), state="running")
        params = {"id": JOB, "live": "1"}
        for _ in range(3):
            assert (
                client.get("/ui/run/site-tree", params=params, headers=HEADERS).status_code == 200
            )
        assert len(loaded) == 1
        clock.now += 31
        client.get("/ui/run/site-tree", params=params, headers=HEADERS)
        assert len(loaded) == 2
        # A page load without live=1 always judges afresh.
        client.get("/ui/run/site-tree", params={"id": JOB}, headers=HEADERS)
        assert len(loaded) == 3


def test_a_cached_judgement_still_marks_the_blocked_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(job_views, "_RULES_MEMO", _RulesMemo(Clock()))
    with running(rules_fetcher()) as client:
        items = rules_site()
        fake_reads(monkeypatch, len(items), fresh(items), state="running")
        params = {"id": JOB, "live": "1"}
        first = tree(client.get("/ui/run/site-tree", params=params, headers=HEADERS).text)
        second = tree(client.get("/ui/run/site-tree", params=params, headers=HEADERS).text)

    assert first.xpath("//input[@data-blocked]")
    assert [b.get("value") for b in second.xpath("//input[@data-blocked]")] == [
        b.get("value") for b in first.xpath("//input[@data-blocked]")
    ]
    assert ORIGIN in second.xpath("//input[@name='url']/@value")[0]


def test_a_retryable_refusal_of_a_live_refresh_swaps_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refused(request: Any) -> Any:
        raise ServiceError(
            ErrorCode.RATE_LIMITED, "slow down", 429, True, headers={"Retry-After": "17"}
        )

    monkeypatch.setattr(job_views, "ui_principal", refused)
    live = client.get("/ui/run/site-tree", params={"id": JOB, "live": "1"}, headers=HEADERS)
    plain = client.get("/ui/run/site-tree", params={"id": JOB}, headers=HEADERS)

    assert live.status_code == 204
    assert live.content == b""
    assert live.headers["Retry-After"] == "17"
    assert plain.status_code == 200  # a click shows the refusal as before
    assert "slow down" in plain.text or "rate" in plain.text.lower()


def test_a_missing_job_stops_a_live_refresh(client: TestClient) -> None:
    response = client.get(
        "/ui/run/site-tree", params={"id": "f" * 32, "live": "1"}, headers=HEADERS
    )

    assert response.status_code == 200
    assert "HTTP 404" in response.text


def tree_delay(client: TestClient, monkeypatch: pytest.MonkeyPatch, items: int) -> str:
    fake_reads(
        monkeypatch,
        items,
        lambda offset, limit: [
            page_item(offset + k, f"{ORIGIN}/p/{offset + k}") for k in range(limit)
        ],
        state="running",
    )
    response = client.get("/ui/run/site-tree", params={"id": JOB, "live": "1"}, headers=HEADERS)
    [wrapper] = tree(response.text).xpath("//div[@data-tree-live]")
    assert wrapper.get("data-tree-live") == wrapper.get("hx-trigger").split(":")[1].rstrip("s")
    return str(wrapper.get("hx-trigger"))


def test_the_refresh_interval_grows_with_the_rate_limit_hits_of_a_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert tree_delay(client, monkeypatch, 1) == "load delay:4s"
    assert tree_delay(client, monkeypatch, 500) == "load delay:4s"
    assert tree_delay(client, monkeypatch, 1001) == "load delay:12s"  # three hits


def test_the_live_note_is_hidden_until_the_script_shows_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = site_tree(client, monkeypatch, state="running")

    [note] = root.xpath("//p[contains(@class, 'tree-live')]")
    assert note.get("hidden") is not None
    [snapshot] = root.xpath("//p[contains(@class, 'tree-snapshot')]")
    assert snapshot.get("hidden") is None


def test_the_batch_view_of_the_tree_is_kept_across_a_refresh(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = site_tree(client, monkeypatch, state="running")

    [jobs] = root.xpath("//div[contains(@class, 'tree-jobs')]")
    assert jobs.get("id") == f"tree-{JOB}-jobs"
    assert jobs.get("hx-preserve") is not None
    assert root.xpath("//form[@id='tree-form']/@hx-target") == ["next .tree-jobs"]


# --- bounded rule checks (third audit S1, S2) ----------------------------------------------------


def test_the_judge_has_one_second_and_the_tree_reads_a_capped_copy_of_the_rules() -> None:
    assert job_views.TREE_JUDGE_SECONDS == 1.0
    assert job_views.TREE_RULES_MAX_RULES == 1000


def test_the_tree_loads_rules_only_for_the_origins_the_job_contacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sitemap listing URLs on 30 foreign host:port origins: their rule files are read from
    the cache only (not cached: unchecked); only the job's own origin is loaded."""
    monkeypatch.setattr(job_views, "_RULES_MEMO", _RulesMemo(Clock()))
    foreign = [f"https://f{i:02d}.example:{8000 + i}" for i in range(30)]
    items = [
        page_item(0, f"{ORIGIN}/sammlung/", depth=0),
        sitemap_item(1, f"{ORIGIN}/sitemap.xml", [{"loc": f"{origin}/p"} for origin in foreign]),
    ]
    calls: list[tuple[str, bool, int | None]] = []
    fetcher = rules_fetcher()
    with running(fetcher) as client:
        pipeline = cast(FastAPI, client.app).state.pipeline
        real = pipeline.origin_rules

        async def recording(url: str, **kwargs: Any) -> Any:
            calls.append((url, kwargs["cached_only"], kwargs["max_rules"]))
            return await real(url, **kwargs)

        monkeypatch.setattr(pipeline, "origin_rules", recording)
        fake_reads(monkeypatch, len(items), fresh(items))
        response = client.get("/ui/run/site-tree", params={"id": JOB}, headers=HEADERS)
        root = tree(response.text)

    assert [(url, cached) for url, cached, _ in calls if not cached] == [
        (f"{ORIGIN}/sammlung/", False)
    ]
    assert len([call for call in calls if call[1]]) == 19  # the cap of 20 origins, own first
    assert {cap for _, _, cap in calls} == {1000}
    assert all(url.startswith(ORIGIN) for url, _ in fetcher.calls)
    [unchecked] = texts(root, "//p[@class='tree-unchecked']")
    assert foreign[0] in unchecked
    assert texts(root, "//p[@class='tree-unchecked-more']") == ["11 more sites were not checked."]


def test_a_live_refresh_judges_only_the_urls_not_judged_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(job_views, "_RULES_MEMO", _RulesMemo(Clock()))
    judged: list[str] = []
    real = OriginRules.judge

    def counting(self: OriginRules, url: str, mode: Any) -> Any:
        judged.append(url)
        return real(self, url, mode)

    monkeypatch.setattr(OriginRules, "judge", counting)
    items = rules_site()
    with running(rules_fetcher()) as client:
        fake_reads(monkeypatch, 10, lambda offset, limit: [i.model_copy(deep=True) for i in items])
        params = {"id": JOB, "live": "1"}
        first = tree(client.get("/ui/run/site-tree", params=params, headers=HEADERS).text)
        assert len(judged) == 3
        judged.clear()
        items.append(page_item(2, f"{ORIGIN}/sammlung/_page9"))
        second = tree(client.get("/ui/run/site-tree", params=params, headers=HEADERS).text)

    assert judged == [f"{ORIGIN}/sammlung/_page9"]
    assert len(first.xpath("//input[@data-blocked]")) == 2
    assert len(second.xpath("//input[@data-blocked]")) == 3


def test_the_tree_is_judged_on_a_pool_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    threads: list[str] = []
    real = tree_module.judge_leaves

    def recording(*args: Any) -> Any:
        threads.append(threading.current_thread().name)
        return real(*args)

    monkeypatch.setattr(job_views, "judge_leaves", recording)
    with running(rules_fetcher()) as client:
        items = rules_site()
        fake_reads(monkeypatch, len(items), fresh(items))
        client.get("/ui/run/site-tree", params={"id": JOB}, headers=HEADERS)

    assert len(threads) == 1
    assert threads[0].startswith("tree")


def hold_tree_pool(client: TestClient) -> ConversionPool:
    """The app's tree pool with its one slot taken (nothing gives it back)."""
    pool = cast(FastAPI, client.app).state.ui_tree_pool
    assert isinstance(pool, ConversionPool)
    assert asyncio.run(pool._slots.acquire())  # free: taken at once, bound to no loop
    return pool


def test_a_busy_tree_pool_answers_soon_with_the_origins_unchecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(job_views, "TREE_JUDGE_SECONDS", 0.05)
    with running(rules_fetcher()) as client, capture_logs() as logs:
        hold_tree_pool(client)
        items = rules_site()
        fake_reads(monkeypatch, len(items), fresh(items))
        started = time.monotonic()
        response = client.get("/ui/run/site-tree", params={"id": JOB}, headers=HEADERS)
        elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert elapsed < 5
    root = tree(response.text)
    assert root.xpath("//input[@data-blocked]") == []
    assert texts(root, "//p[@class='tree-unchecked']") == [
        "The rules of https://example.org could not be read or checked in time; "
        "their URLs carry no mark."
    ]
    assert [log for log in logs if log["event"] == "tree_judge_timeout"]


def test_a_tree_without_rules_to_judge_does_not_wait_for_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with running(rules_fetcher()) as client, capture_logs() as logs:
        hold_tree_pool(client)

        async def none(url: str, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(cast(FastAPI, client.app).state.pipeline, "origin_rules", none)
        items = rules_site()
        fake_reads(monkeypatch, len(items), fresh(items))
        response = client.get("/ui/run/site-tree", params={"id": JOB}, headers=HEADERS)

    assert response.status_code == 200
    assert texts(tree(response.text), "//p[@class='tree-unchecked']")
    assert not [log for log in logs if log["event"] == "tree_judge_timeout"]


def test_without_protegos_reader_the_tree_leaves_the_origin_unchecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(compliance, "_PROTEGO_PARSER", "protego._no_such_module")
    compliance._protego_grouping.cache_clear()
    try:
        with running(rules_fetcher()) as client:
            items = rules_site()
            fake_reads(monkeypatch, len(items), fresh(items))
            response = client.get("/ui/run/site-tree", params={"id": JOB}, headers=HEADERS)
    finally:
        monkeypatch.undo()
        compliance._protego_grouping.cache_clear()

    root = tree(response.text)
    assert root.xpath("//input[@data-blocked]") == []
    assert texts(root, "//p[@class='tree-unchecked']") == [
        "The rules of https://example.org could not be read or checked in time; "
        "their URLs carry no mark."
    ]


def test_the_memo_keeps_at_most_its_judgements_and_drops_the_oldest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(job_views, "TREE_JUDGE_MEMO_URLS", 10)
    clock = Clock()
    memo = _RulesMemo(clock)
    for number in range(3):
        clock.now += 0.1
        memo.put((f"job-{number}", frozenset()), {}, {f"u{number}-{i}": None for i in range(4)})

    assert memo.get(("job-0", frozenset())) is None  # 12 judgements: the oldest entry went
    assert memo.judged(("job-1", frozenset())) == {f"u1-{i}": None for i in range(4)}
    assert len(memo.judged(("job-2", frozenset()))) == 4
    assert memo.judged(("job-0", frozenset())) == {}


def _unchecked_without_marks(response: Any) -> None:
    root = tree(response.text)
    assert root.xpath("//input[@data-blocked]") == []
    assert root.xpath("//span[@class='blocked-badge']") == []
    assert texts(root, "//p[@class='tree-unchecked']") == [
        "The rules of https://example.org could not be read or checked in time; "
        "their URLs carry no mark."
    ]


def test_a_judge_that_waits_for_the_thread_has_its_time_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one slot is busy for 0.3 s and the judge has 0.2 s: its deadline is taken before it
    waits, so it starts too late and judges nothing."""
    monkeypatch.setattr(job_views, "TREE_JUDGE_SECONDS", 0.2)
    with running(rules_fetcher()) as client:
        app = cast(FastAPI, client.app)
        pipeline = app.state.pipeline
        real_rules = pipeline.origin_rules
        holders: list[Any] = []

        async def holding(url: str, **kwargs: Any) -> Any:
            holders.append(asyncio.ensure_future(app.state.ui_tree_pool.run(time.sleep, 0.3)))
            await asyncio.sleep(0)  # the holder takes the one slot
            return await real_rules(url, **kwargs)

        monkeypatch.setattr(pipeline, "origin_rules", holding)
        items = rules_site()
        fake_reads(monkeypatch, len(items), fresh(items))
        response = client.get("/ui/run/site-tree", params={"id": JOB}, headers=HEADERS)

    assert response.status_code == 200
    _unchecked_without_marks(response)


def test_a_judgement_that_ends_after_the_view_gave_up_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A judge thread that overruns (here: it starts 0.3 s late and ignores its deadline) still
    runs after the view answered with the origins unchecked; it only reads the tree, so the
    rendered tree does not change afterwards."""
    monkeypatch.setattr(job_views, "TREE_JUDGE_SECONDS", 0.2)
    monkeypatch.setattr(job_views, "TREE_JUDGE_GRACE_SECONDS", 0.0)
    trees: list[Any] = []
    results: list[Any] = []
    finished = threading.Event()
    real_judge = tree_module.judge_leaves

    def overrunning(tree_: Any, rules: Any, deadline: float, judged: Any) -> Any:
        trees.append(tree_)
        try:
            time.sleep(0.3)
            results.append(real_judge(tree_, rules, None, judged))
            return results[-1]
        finally:
            finished.set()

    monkeypatch.setattr(job_views, "judge_leaves", overrunning)
    with running(rules_fetcher()) as client:
        items = rules_site()
        fake_reads(monkeypatch, len(items), fresh(items))
        response = client.get("/ui/run/site-tree", params={"id": JOB}, headers=HEADERS)
        assert response.status_code == 200
        [rendered] = trees
        snapshot = [(node.url, node.blocked) for node in tree_module._leaves(rendered)]
        assert finished.wait(5)  # the stray thread is done

    _unchecked_without_marks(response)
    [late] = results
    assert "robots" in late.reasons.values()  # it did judge, after the answer
    assert [(node.url, node.blocked) for node in tree_module._leaves(rendered)] == snapshot
