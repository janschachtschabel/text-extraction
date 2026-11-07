"""The site tree refreshes itself while its job runs (package 18): a running job's tree sits in
a wrapper that fetches the tree again every ``TREE_REFRESH_SECONDS``, a finished job's tree does
not; the progress view's tree button says when it may load the tree by itself; the rule
judgement is reused for 30 s per job and origin set (a bounded in-process memo, derived data
only)."""

from collections.abc import Callable
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.ui.conftest import run, running
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
from text_extraction.job_models import JobItem
from text_extraction.ui import job_views
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


def test_live_refreshes_load_the_rules_once_in_thirty_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    monkeypatch.setattr(job_views, "_RULES_MEMO", _RulesMemo(clock))
    loaded: list[str] = []
    with running(rules_fetcher()) as client:
        pipeline = cast(FastAPI, client.app).state.pipeline
        real = pipeline.origin_rules

        async def recording(url: str) -> Any:
            loaded.append(url)
            return await real(url)

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
