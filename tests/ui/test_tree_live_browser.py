"""The site tree loads itself and refreshes while the job runs (package 18, ``pytest -m
browser``): a site whose pages answer slowly, each page linking one address of its own under
``/docs/``. The tree appears without a click once the job has a result; the user ticks a box,
unfolds a section, presses the "Content" chip and scrolls; the next refresh adds addresses and
changes none of that, and the focus stays on the chip. A text field with the focus holds the
refresh back."""

import re
import time
from collections.abc import Iterator
from typing import Final

import pytest
from playwright.sync_api import Locator, Page, Route, expect
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from tests.ui.test_a11y_browser import _axe
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
        expect(page.locator("button.tree-button[data-refresh]")).to_have_count(0)

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


def test_a_click_on_the_tree_button_and_the_auto_load_ask_for_one_tree(
    ui: tuple[str, str],
) -> None:
    url, origin = ui
    with browsing(url) as (page, console):
        requests: list[str] = []
        page.on(
            "request",
            lambda request: (
                requests.append(request.url)
                if "/ui/run/site-tree" in request.url and "live=1" not in request.url
                else None
            ),
        )
        log_in(page)
        page.wait_for_selector("#app-shell")
        open_tab(page, "site-structure")
        panel = page.locator("#panel-site-structure")
        panel.locator("input[name=url]").fill(f"{origin}/")
        panel.locator("input[name=depth]").fill("1")
        panel.locator("input[name=max_pages]").fill(str(FAST + SLOW + 1))
        panel.locator("button[type=submit]").click()
        # The user's click comes as early as there is a button, before the job has a result.
        page.locator("#result button.tree-button").click()
        expect(page.locator("#result .tree-view input[name=url]").first).to_be_attached()
        page.wait_for_timeout(8_000)  # past the progress view's next refreshes, which auto-load

        assert len(requests) == 1, requests
    assert console == []
