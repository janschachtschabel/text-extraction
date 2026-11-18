"""The site tree loads itself and refreshes while the job runs (package 18, ``pytest -m
browser``): a site whose pages answer slowly, each page linking one address of its own under
``/docs/``. The tree appears without a click in place of a job card (package 23); the user ticks
a box, unfolds a section, presses the "Content" chip and scrolls; the next refresh adds addresses
and changes none of that, and the focus stays on the chip. A text field with the focus holds the
refresh back. The tree's head says the job's state and cancels it; the single fetches fold."""

import re
import time
from collections.abc import Iterator
from typing import Final

import pytest
from playwright.sync_api import Locator, Page, Route, expect
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from tests.ui.test_a11y_browser import _RECORD_STATUS, _axe
from tests.ui.test_playground_browser import (
    _page,
    _settings,
    browsing,
    log_in,
    open_tab,
    serving,
)

pytestmark = pytest.mark.browser

FAST: Final = 30  # answered at once: the tree has rows to scroll soon
SLOW: Final = 12  # answered after SLOW_SECONDS: the tree keeps growing while the user works
SLOW_SECONDS: Final = 2.0
# Words for the addresses under /docs/: a name says nothing about a type, so they stay typed
# by the default guess like the pages themselves.
WORDS: Final = [f"lesson-{chr(97 + number)}" for number in range(FAST)]
LATE: Final = [f"late-{chr(97 + number)}" for number in range(SLOW)]
SITE: Final = {
    "/": (
        "Start page",
        [f"/p{number}.html" for number in range(FAST)]
        + [f"/s{number}.html" for number in range(SLOW)],
    ),
    **{
        f"/p{number}.html": (f"Page {number}", [f"/docs/{WORDS[number]}"]) for number in range(FAST)
    },
    **{f"/s{number}.html": (f"Slow {number}", [f"/docs/{LATE[number]}"]) for number in range(SLOW)},
}


@pytest.fixture
def ui(httpserver: HTTPServer) -> Iterator[tuple[str, str]]:
    def handle(request: Request) -> Response:
        found = SITE.get(request.path)
        if found is None:
            return Response("Not found", status=404, content_type="text/plain")
        if request.path.startswith("/s"):
            time.sleep(SLOW_SECONDS)
        return Response(_page(*found), content_type="text/html; charset=utf-8")

    httpserver.expect_request(re.compile(".*")).respond_with_handler(handle)
    with serving(_settings()) as url:
        yield url, httpserver.url_for("/").rstrip("/")


def _start(page: Page, origin: str) -> Locator:
    """Log in, start the site job and wait for its tree, which loads without a click."""
    log_in(page)
    page.wait_for_selector("#app-shell")
    open_tab(page, "site-structure")
    panel = page.locator("#panel-site-structure")
    panel.locator("input[name=url]").fill(f"{origin}/")
    panel.locator("input[name=depth]").fill("1")
    panel.locator("input[name=max_pages]").fill(str(FAST + SLOW + 1))
    panel.locator("button[type=submit]").click()
    view = page.locator("#result .tree-view")
    expect(view.locator("input[name=url]").first).to_be_attached()
    return view


def _wrapper_mark(page: Page) -> None:
    page.evaluate("window.__wrapper = document.querySelector('[data-tree-live]')")


def _refreshed(page: Page) -> None:
    """Wait until the tree wrapper is not the one marked: a refresh has swapped it."""
    page.wait_for_function(
        "() => window.__wrapper && document.querySelector('[data-tree-live]') !== window.__wrapper"
    )


def _urls(view: Locator) -> int:
    return view.locator("input[name=url]").count()


def _more_than(page: Page, count: int) -> None:
    """Wait until the tree has more URL boxes than ``count``."""
    page.wait_for_function(
        "(n) => document.querySelectorAll('#result .tree-view input[name=url]').length > n",
        arg=count,
    )


def _box(view: Locator, word: str) -> Locator:
    return view.locator(f"input[name=url][value$='/docs/{word}']")


def test_the_tree_keeps_the_users_work_across_a_refresh(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, console):
        view = _start(page, origin)
        expect(view.locator(".tree-live")).to_have_text("Updates while the job runs")
        expect(view.locator(".tree-live")).to_be_visible()
        expect(view.locator(".tree-snapshot")).to_be_hidden()  # the note says it already

        # The first page's address is under /docs/; its section starts folded: the user opens it.
        name = page.locator(
            ":scope > .cell-name > label > span.label", has_text=re.compile("^docs/$")
        )
        docs = view.locator("li.node-section", has=name)
        fold = docs.locator(":scope > .cell-name > button.fold")
        _more_than(page, FAST)
        fold.click()
        _box(view, WORDS[0]).check()
        view.locator("textarea[name=options]").evaluate("(e) => (e.closest('details').open = true)")
        view.locator("input[name=format][value=text]").check()
        view.locator("input[name=metadata]").uncheck()

        # The filter opens the sections with a visible leaf; the user then folds docs/ again.
        view.locator("button.chip[data-filter-type=content]").click()
        expect(fold).to_have_attribute("aria-expanded", "true")
        fold.click()
        expect(fold).to_have_attribute("aria-expanded", "false")
        box = view.locator(".tree-box")
        page.evaluate("(box) => (box.scrollTop = 120)", box.element_handle())
        scrolled = box.evaluate("(e) => e.scrollTop")
        assert scrolled > 0

        before = _urls(view)
        view.locator("button.chip[data-filter-type=content]").focus()
        _wrapper_mark(page)
        _refreshed(page)
        _more_than(page, before)

        assert _box(view, WORDS[0]).is_checked()
        expect(view.locator("input[name=url]:checked")).to_have_count(1)
        expect(fold).to_have_attribute("aria-expanded", "false")
        expect(view.locator("button.chip[data-filter-type=content]")).to_have_attribute(
            "aria-pressed", "true"
        )
        expect(view.locator("button.chip[data-filter-type=all]")).to_have_attribute(
            "aria-pressed", "false"
        )
        assert box.evaluate("(e) => e.scrollTop") == scrolled
        expect(view.locator("input[name=format][value=text]")).to_be_checked()
        expect(view.locator("input[name=metadata]")).not_to_be_checked()
        expect(view.locator("details.advanced")).to_have_attribute("open", "")
        assert page.evaluate("document.activeElement.dataset.filterType") == "content"
        # What the filter hides stays hidden, also what arrived with the refresh.
        assert view.locator("li[data-type]:not([data-type=content]):not([hidden])").count() == 0
        assert view.locator("li[data-type][hidden]").count() > 0
        assert view.locator("[aria-live] .tree-view").count() == 0
        assert _axe(page, "live note shown") == []
    assert console == []


def test_a_text_field_with_the_focus_holds_the_refresh_back(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, console):
        view = _start(page, origin)
        view.locator("textarea[name=options]").evaluate("(e) => (e.closest('details').open = true)")
        field = view.locator("textarea[name=options]")
        field.click()
        field.fill('{"timeout": 5}')
        _wrapper_mark(page)
        page.wait_for_timeout(9_000)  # two refresh intervals
        assert page.evaluate("window.__wrapper === document.querySelector('[data-tree-live]')")
        expect(field).to_be_focused()
        expect(field).to_have_value('{"timeout": 5}')

        view.locator("button.chip[data-filter-type=all]").click()  # the focus leaves the field
        _refreshed(page)
        expect(view.locator("textarea[name=options]")).to_have_value('{"timeout": 5}')
    assert console == []


def test_the_focus_stays_on_a_checkbox_across_a_refresh(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, console):
        view = _start(page, origin)
        _more_than(page, FAST)
        view.locator("input[name=url][value$='/p3.html']").focus()
        _wrapper_mark(page)
        _refreshed(page)

        assert page.evaluate("document.activeElement.name") == "url"
        assert page.evaluate("document.activeElement.value").endswith("/p3.html")

        # A button of the tools, which has no id, keeps the focus as well.
        view.locator("[data-action=tree-none]").focus()
        _wrapper_mark(page)
        _refreshed(page)
        assert page.evaluate("document.activeElement.dataset.action") == "tree-none"

        # So do controls without an id: a radio and a checkbox of the options, their summary.
        for selector in (
            "input[name=format][value=text]",
            "input[name=metadata]",
            "details.advanced > summary",
        ):
            view.locator(selector).focus()
            _wrapper_mark(page)
            _refreshed(page)
            assert page.evaluate("(s) => document.activeElement.matches(s)", selector), selector
    assert console == []


def test_the_batch_view_of_the_tree_survives_a_refresh(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, console):
        view = _start(page, origin)
        _more_than(page, FAST)
        view.locator("input[name=url][value$='/p3.html']").check()
        view.locator(".tree-actions button[type=submit]").click()
        job = view.locator(".tree-jobs section.job")
        expect(job).to_have_count(1)
        page.evaluate("window.__jobs = document.querySelector('.tree-jobs')")
        _wrapper_mark(page)
        _refreshed(page)

        assert page.evaluate("window.__jobs === document.querySelector('.tree-jobs')")
        expect(job).to_have_count(1)
        expect(job.locator(".state")).to_have_attribute(
            "data-value", "completed"
        )  # it kept polling
    assert console == []


def test_a_refresh_that_failed_is_asked_for_again(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, _console):  # the aborted request logs a console error
        view = _start(page, origin)
        _more_than(page, FAST)
        view.locator("input[name=url][value$='/p3.html']").check()
        answers = ["abort", "retry", "pass"]
        seen: list[str] = []

        def answer(route: Route) -> None:
            how = answers[len(seen)] if len(seen) < len(answers) else "pass"
            seen.append(how)
            if how == "abort":
                route.abort()
            elif how == "retry":
                route.fulfill(status=204, headers={"Retry-After": "6"})
            else:
                route.continue_()

        page.route(re.compile(r".*/ui/run/site-tree\?.*live=1"), answer)
        _wrapper_mark(page)
        _refreshed(page)  # the network error, then the 204 left the tree alone; the third swapped

        assert seen[:3] == answers
        assert page.locator("input[name=url]:checked").count() == 1
        expect(view.locator("input[name=url]").first).to_be_attached()


def test_a_failing_refresh_announces_its_error_once(ui: tuple[str, str]) -> None:
    """Ruling 1: a refresh announces nothing; a retried failure keeps the first alert."""
    url, origin = ui
    with browsing(url) as (page, _console):  # the aborted requests log console errors
        _start(page, origin)
        _more_than(page, FAST)
        page.evaluate(
            """() => {
              window.alertWrites = 0;
              new MutationObserver((records) => {
                for (const record of records) {
                  if (record.target.closest && record.target.closest(".request-status")) {
                    window.alertWrites += 1;
                  }
                }
              }).observe(document.body, { childList: true, subtree: true });
            }"""
        )
        answers = ["abort", "abort", "pass"]
        seen: list[str] = []

        def answer(route: Route) -> None:
            how = answers[len(seen)] if len(seen) < len(answers) else "pass"
            seen.append(how)
            if how == "abort":
                route.abort()
            else:
                route.continue_()

        page.route(re.compile(r".*/ui/run/site-tree\?.*live=1"), answer)
        _wrapper_mark(page)
        _refreshed(page)

        assert seen[:3] == answers
        assert page.evaluate("window.alertWrites") == 1


def test_the_site_run_shows_the_tree_and_announces_the_end_once(ui: tuple[str, str]) -> None:
    """Package 23: no job card, the tree's head says the state; the change to "completed" is
    announced once, the refreshes before it say nothing."""
    url, origin = ui
    with browsing(url) as (page, console):
        log_in(page)
        page.wait_for_selector("#app-shell")
        page.evaluate(_RECORD_STATUS)
        open_tab(page, "site-structure")
        panel = page.locator("#panel-site-structure")
        panel.locator("input[name=url]").fill(f"{origin}/")
        panel.locator("input[name=depth]").fill("1")
        panel.locator("input[name=max_pages]").fill(str(FAST + SLOW + 1))
        panel.locator("button[type=submit]").click()
        view = page.locator("#result .tree-view")
        expect(view.locator("input[name=url]").first).to_be_attached()
        expect(page.locator("#result section.job")).to_have_count(0)
        head = view.locator(".tree-head-line")
        expect(head.locator(".state")).to_have_attribute("data-value", "running")
        expect(view.locator(".tree-progress")).to_contain_text("fetches done so far")
        expect(head.locator(".state")).to_have_attribute("data-value", "completed", timeout=60_000)
        expect(head.locator("form.tree-cancel")).to_have_count(0)
        expect(page.locator("[data-tree-live]")).to_have_count(0)
        page.wait_for_timeout(500)
        texts: list[str] = page.evaluate("window.uiStatusTexts")
        assert _axe(page, "finished site run") == []

    ends = [text for text in texts if text.endswith(": completed.")]
    assert len(ends) == 1, texts
    assert not [text for text in texts if text.endswith(": running.")][1:], texts
    assert console == []


def test_cancel_in_the_head_ends_the_site_run(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, console):
        view = _start(page, origin)
        cancel = view.locator("form.tree-cancel button")
        button_id = cancel.get_attribute("id") or ""
        cancel.focus()
        _wrapper_mark(page)
        _refreshed(page)
        assert page.evaluate("document.activeElement.id") == button_id  # kept across a refresh

        page.once("dialog", lambda dialog: dialog.accept())
        cancel.click()
        expect(page.locator("#result .site-run .cancelled")).to_be_visible()
        expect(page.locator("#result .tree-view")).to_have_count(0)
        page.wait_for_timeout(5_000)  # past a refresh interval: nothing brings the tree back
        expect(page.locator("#result .tree-view")).to_have_count(0)
    assert console == []


def test_the_single_fetches_fold_lists_the_fetches_and_stays_open(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, console):
        view = _start(page, origin)
        fold = view.locator("details.tree-fetches")
        expect(fold).not_to_have_attribute("open", "")
        fold.locator("summary").click()
        rows = fold.locator("table.items tbody tr")
        expect(rows.first).to_be_visible()
        expect(fold.locator("h3")).to_have_count(0)  # no second job card in the fold
        fold.locator("table.items tbody tr").first.locator("button").click()
        detail = fold.locator(".job-detail")
        expect(detail.locator(".result-view")).to_be_visible()

        page.evaluate("window.__fold = document.querySelector('details.tree-fetches')")
        _wrapper_mark(page)
        _refreshed(page)
        assert page.evaluate("window.__fold === document.querySelector('details.tree-fetches')")
        expect(fold).to_have_attribute("open", "")
        expect(detail.locator(".result-view")).to_be_visible()
        assert _axe(page, "single fetches open") == []
    assert console == []
