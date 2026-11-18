"""The site run (package 23): the site-structure form opens the live tree at once, the tree's
head carries the job's state, progress and cancel button, and the single fetches sit in a fold
under the tree that loads only when opened."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.ui.conftest import run
from tests.ui.site_items import page_item
from tests.ui.test_jobs_views import (
    HEADERS,
    ORIGIN,
    SMALL_SITE,
    fake_reads,
    progress,
    texts,
    tree,
)
from text_extraction.job_models import JobProgress
from text_extraction.ui.i18n import LANGUAGE_HEADER

JOB = "a" * 32


def site_tree(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: str,
    items: int = 1,
    job_progress: JobProgress | None = None,
    language: str = "en",
) -> Any:
    site = SMALL_SITE[:items]
    fake_reads(
        monkeypatch,
        len(site),
        lambda offset, limit: site[offset : offset + limit],
        state=state,
        progress=job_progress,
    )
    headers = {**HEADERS, LANGUAGE_HEADER: language}
    response = client.get("/ui/run/site-tree", params={"id": JOB, "live": "1"}, headers=headers)
    assert response.status_code == 200
    return tree(response.text)


# --- task 1: the tree's head ---------------------------------------------------------------------


def test_a_running_trees_head_has_state_progress_and_cancel(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    grown = JobProgress(total=7, done=3, success=2, partial=0, failed=1)
    root = site_tree(client, monkeypatch, state="running", job_progress=grown)

    [view] = root.xpath("//section[@class='tree-view']")
    assert view.get("data-announce") == f"Job {JOB}: running."
    [title] = view.xpath(".//h3[@class='tree-title']")
    assert JOB in title.text_content()
    assert texts(view, ".//span[contains(@class, 'state')][@data-value='running']") == ["running"]
    [line] = texts(view, ".//p[@class='tree-progress']")
    assert line.startswith("3 of 7 fetches done so far · running for ")
    assert texts(view, ".//*[@class='fetches-failed']") == ["1 fetch failed"]
    [form] = view.xpath(".//form[contains(@class, 'cancel')]")
    assert form.get("hx-post") == "/ui/run/cancel"
    assert form.get("hx-target") == "closest .tree-host"
    assert form.get("hx-swap") == "innerHTML"
    assert form.get("hx-confirm")
    assert form.xpath(".//input[@name='id']/@value") == [JOB]
    assert form.xpath(".//button/@id") == [f"tree-{JOB}-cancel"]


def test_a_finished_trees_head_has_the_duration_and_no_cancel(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = site_tree(client, monkeypatch, state="completed")

    [view] = root.xpath("//section[@class='tree-view']")
    assert view.get("data-announce") == f"Job {JOB}: completed."
    assert view.xpath(".//form[contains(@class, 'cancel')]") == []
    assert texts(view, ".//p[@class='tree-progress']") == ["finished in 58 s"]
    assert view.xpath(".//*[@class='fetches-failed']") == []


def test_a_crawl_without_pages_yet_is_starting(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = site_tree(client, monkeypatch, state="running", items=0)

    assert texts(root, "//p[@class='tree-starting']") == ["The crawl is starting …"]
    assert root.xpath("//p[@class='tree-empty']") == []
    assert root.xpath("//form[contains(@class, 'cancel')]")


def test_a_finished_crawl_without_pages_says_so(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = site_tree(client, monkeypatch, state="completed", items=0)

    assert texts(root, "//p[@class='tree-empty']") == [
        "The crawl found no pages. The single fetches below say why."
    ]
    assert root.xpath("//p[@class='tree-starting']") == []


def test_the_head_is_german_too(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    grown = JobProgress(total=7, done=3, success=1, partial=0, failed=2)
    root = site_tree(client, monkeypatch, state="running", job_progress=grown, language="de")

    [line] = texts(root, "//p[@class='tree-progress']")
    assert line.startswith("3 von bisher 7 Abrufen erledigt · läuft seit ")
    assert texts(root, "//*[@class='fetches-failed']") == ["2 Abrufe fehlgeschlagen"]
    assert texts(root, "//span[contains(@class, 'state')][@data-value='running']") == ["läuft"]


# --- task 2: the form opens the site run ---------------------------------------------------------


def test_the_site_form_opens_the_site_run(client: TestClient) -> None:
    html = run(client, "site-structure", url=f"{ORIGIN}/", depth="1", max_pages="5").text
    root = tree(html)

    assert root.xpath("//section[@class='job']") == []
    [view] = root.xpath("//section[contains(@class, 'site-run')]")
    assert view.get("tabindex") == "-1"
    [wrapper] = view.xpath("./div[@data-tree-live]")
    job_id = wrapper.get("hx-get").split("id=")[1].split("&")[0]
    assert len(job_id) == 32
    assert wrapper.get("hx-get") == f"/ui/run/site-tree?id={job_id}&live=1"
    assert wrapper.get("hx-trigger") == "load"
    assert wrapper.get("hx-swap") == "outerHTML"
    assert wrapper.get("data-refresh") is not None
    assert texts(view, ".//p[@class='tree-starting']") == ["The crawl is starting …"]
    assert view.xpath(".//span[contains(@class, 'state')]/@data-value") == ["queued"]


def test_a_refused_site_form_still_shows_the_rest_answer(client: TestClient) -> None:
    root = tree(run(client, "site-structure", url=f"{ORIGIN}/", depth="99").text)

    assert texts(root, "//*[@class='http-status']") == ["HTTP 422"]
    assert root.xpath("//section[contains(@class, 'site-run')]") == []


# --- task 3: the single fetches ------------------------------------------------------------------


def test_the_tree_holds_the_fold_of_single_fetches(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    for items in (1, 0):
        root = site_tree(client, monkeypatch, state="running", items=items)

        [fold] = root.xpath("//details[contains(@class, 'tree-fetches')]")
        assert fold.get("id") == f"tree-{JOB}-fetches"
        assert fold.get("hx-preserve") is not None
        assert fold.get("open") is None
        assert texts(fold, "./summary") == ["Single fetches"]
        [body] = fold.xpath("./div[@hx-get]")
        assert body.get("hx-get") == f"/ui/run/progress?id={JOB}&items=1"
        assert body.get("hx-trigger") == "toggle once from:closest details"


def test_the_fetch_list_shows_the_items_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = [page_item(index, f"{ORIGIN}/p{index}", depth=1) for index in range(25)]
    fake_reads(
        monkeypatch, len(site), lambda offset, limit: site[offset : offset + limit], state="running"
    )

    root = tree(progress(client, JOB, items="1"))

    assert root.xpath("//h3") == []
    assert root.xpath("//progress") == []
    assert root.xpath("//form[contains(@class, 'cancel')]") == []
    assert root.xpath("//button[contains(@class, 'tree-button')]") == []
    assert len(root.xpath("//table[@class='items']/tbody/tr")) == 20
    assert root.xpath("//div[contains(@class, 'job-detail')]")
    [live] = root.xpath("//div[contains(@class, 'progress-live')]")
    assert "items=1" in live.get("hx-get")
    [next_page] = root.xpath("//button[contains(@class, 'next')]")
    assert "items=1" in next_page.get("hx-get")
