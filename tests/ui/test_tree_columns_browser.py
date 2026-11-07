"""The site tree in columns with type filter chips (package 18, ``pytest -m browser``): a site
with four types (home, content, imprint and, linked only, two events in a section of their own).
A chip leaves the leaves of its types and their sections; "All" restores everything; the type
column lines up in every row at 1440 px; no row overflows at 390 px; axe-core finds nothing in
either language and width with a filter active."""

import re
from collections.abc import Iterator
from typing import Final

import pytest
from playwright.sync_api import Locator, Page, ViewportSize, expect
from pytest_httpserver import HTTPServer

from tests.ui.test_a11y_browser import _axe
from tests.ui.test_playground_browser import (
    _settings,
    browsing,
    log_in,
    open_tab,
    serve_site,
    serving,
)
from text_extraction.ui.i18n import LANGUAGES, Language, translate

pytestmark = pytest.mark.browser

# The events are one level too deep to be fetched: linked only, typed from their URL.
SITE: Final = {
    "/": ("Start page", ["/docs/a.html", "/impressum.html"]),
    "/docs/a.html": ("Docs A", ["/veranstaltungen/herbst", "/veranstaltungen/fruehling"]),
    "/impressum.html": ("Impressum", []),
}
URLS: Final = 5
DESKTOP: Final[ViewportSize] = {"width": 1440, "height": 900}
PHONE: Final[ViewportSize] = {"width": 390, "height": 844}


@pytest.fixture
def ui(httpserver: HTTPServer) -> Iterator[tuple[str, str]]:
    origin = serve_site(httpserver, SITE)
    with serving(_settings()) as url:
        yield url, origin


def _open_tree(page: Page, origin: str) -> Locator:
    log_in(page)
    page.wait_for_selector("#app-shell")
    open_tab(page, "site-structure")
    panel = page.locator("#panel-site-structure")
    panel.locator("input[name=url]").fill(f"{origin}/")
    panel.locator("input[name=depth]").fill("1")
    panel.locator("button[type=submit]").click()
    job = page.locator("#result section.job")
    expect(job.locator(".state")).to_have_attribute("data-value", "completed")
    job.locator("button.tree-button").click()
    view = page.locator("#result .tree-view")
    expect(view.locator("input[name=url]")).to_have_count(URLS)
    return view


def _chip(view: Locator, kind: str) -> Locator:
    return view.locator(f"button.chip[data-filter-type={kind}]")


def test_a_chip_filters_the_leaves_and_all_restores_them(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, console):
        page.set_viewport_size(DESKTOP)
        view = _open_tree(page, origin)
        group = view.locator("div.tree-filter")
        expect(group).to_be_visible()
        expect(view.locator("p.tree-types")).to_be_hidden()  # the chips replace the text line
        expect(_chip(view, "all")).to_have_attribute("aria-pressed", "true")
        expect(_chip(view, "event")).to_have_attribute("aria-pressed", "false")

        _chip(view, "event").click()
        expect(_chip(view, "event")).to_have_attribute("aria-pressed", "true")
        expect(_chip(view, "all")).to_have_attribute("aria-pressed", "false")
        events = view.locator("li[data-type=event]")
        expect(events).to_have_count(2)
        for index in range(2):
            expect(events.nth(index)).to_be_visible()  # their folded section is unfolded
        expect(view.locator("li[data-type=home]")).to_be_hidden()
        expect(view.locator("li[data-type=imprint]")).to_be_hidden()
        expect(view.locator("li.node-section[data-level='1']", has_text="docs/")).to_be_hidden()
        expect(
            view.locator("li.node-section[data-level='1']", has_text="veranstaltungen/")
        ).to_be_visible()

        # Several types make a union; a click on a pressed chip lifts it again.
        _chip(view, "imprint").click()
        expect(view.locator("li[data-type=imprint]")).to_be_visible()
        _chip(view, "imprint").click()
        expect(view.locator("li[data-type=imprint]")).to_be_hidden()

        # A hidden box keeps its state, and "Select content pages" leaves it alone.
        home = view.locator(f"input[name=url][value='{origin}/']")
        view.locator("[data-action=tree-all]").click()
        expect(view.locator(".tree-counter")).to_have_text(f"{URLS} selected")
        view.locator("[data-action=tree-content]").click()
        expect(home).to_be_checked()  # hidden by the filter, so untouched
        expect(view.locator("input[name=url]:checked")).to_have_count(URLS)
        view.locator("[data-action=tree-none]").click()
        view.locator("[data-action=tree-content]").click()
        expect(view.locator("input[name=url]:checked")).to_have_count(2)  # the events only

        # Ticking a section ticks its visible leaves only while a filter is active.
        view.locator("[data-action=tree-none]").click()
        host = view.locator("ul.tree > li > .cell-name > label > input")
        host.click()  # it shows indeterminate afterwards: not all its leaves are ticked
        expect(view.locator("input[name=url]:checked")).to_have_count(2)
        expect(home).not_to_be_checked()
        view.locator("[data-action=tree-none]").click()

        _chip(view, "all").click()
        expect(view.locator("ul.tree li[hidden]")).to_have_count(0)
        expect(_chip(view, "all")).to_have_attribute("aria-pressed", "true")
        expect(_chip(view, "event")).to_have_attribute("aria-pressed", "false")
        expect(view.locator("li[data-type=home]")).to_be_visible()
        # Without a filter a section ticks every leaf below it.
        host.click()
        expect(view.locator("input[name=url]:checked")).to_have_count(URLS)
    assert console == []


def test_the_type_column_lines_up_at_1440(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, console):
        page.set_viewport_size(DESKTOP)
        view = _open_tree(page, origin)
        view.locator("[data-action=tree-unfold-all]").click()
        cells = view.locator("ul.tree .cell-type")
        expect(cells).to_have_count(
            URLS + 3
        )  # every row: the sections host, docs/ and veranstaltungen/ too
        lefts = [cells.nth(index).bounding_box() for index in range(URLS + 3)]
        assert all(box is not None for box in lefts)
        assert max(box["x"] for box in lefts if box) - min(box["x"] for box in lefts if box) <= 1
        head = view.locator(".tree-head .head-type").bounding_box()
        assert head is not None
        assert abs(head["x"] - min(box["x"] for box in lefts if box)) <= 1
    assert console == []


@pytest.mark.parametrize("width", [1024, 700])
def test_the_name_column_keeps_its_room_at_common_widths(ui: tuple[str, str], width: int) -> None:
    """A tree box too narrow for five columns switches to the compact rows (name and type, the
    rest under the name) instead of squeezing the name to a few pixels."""
    url, origin = ui
    with browsing(url) as (page, console):
        page.set_viewport_size({"width": width, "height": 900})
        view = _open_tree(page, origin)
        view.locator("[data-action=tree-unfold-all]").click()
        names = page.evaluate(
            "() => [...document.querySelectorAll('#result ul.tree li > .cell-name')]"
            ".map((cell) => cell.getBoundingClientRect().width"
            " - parseFloat(getComputedStyle(cell).paddingLeft))"
        )
        rem = page.evaluate("parseFloat(getComputedStyle(document.documentElement).fontSize)")
        assert min(names) >= 12 * rem
        rows = page.evaluate(
            "() => [...document.querySelectorAll('#result ul.tree li')]"
            ".map((row) => row.scrollWidth - row.clientWidth)"
        )
        assert max(rows) <= 0
        assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert console == []


def test_no_row_overflows_at_390(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, console):
        page.set_viewport_size(PHONE)
        view = _open_tree(page, origin)
        view.locator("[data-action=tree-unfold-all]").click()
        rows = page.evaluate(
            "() => [...document.querySelectorAll('#result ul.tree li')]"
            ".map((row) => row.scrollWidth - row.clientWidth)"
        )
        assert rows
        assert max(rows) <= 0
        box = page.evaluate(
            "() => { const box = document.querySelector('#result .tree-box');"
            " return box.scrollWidth - box.clientWidth; }"
        )
        assert box <= 0
        assert page.evaluate("document.documentElement.scrollWidth") <= PHONE["width"]
        expect(view.locator(".tree-head")).to_be_hidden()  # the columns are gone: so is its head
        # Found, status and depth sit in a line under the name.
        name = view.locator("li[data-type=home] > .cell-name").bounding_box()
        found = view.locator("li[data-type=home] .cell-found").bounding_box()
        assert name is not None
        assert found is not None
        assert found["y"] >= name["y"] + name["height"] - 1
    assert console == []


@pytest.mark.parametrize("language", LANGUAGES)
def test_axe_finds_nothing_in_the_columns_with_a_filter(
    ui: tuple[str, str], language: Language
) -> None:
    url, origin = ui
    found: list[str] = []
    with browsing(f"{url}?lang={language}") as (page, console):
        view = _open_tree(page, origin)
        for size in (DESKTOP, PHONE):
            page.set_viewport_size(size)
            _chip(view, "all").click()
            found += _axe(page, f"{language} {size['width']} unfiltered")
            _chip(view, "event").click()
            expect(_chip(view, "event")).to_have_attribute("aria-pressed", "true")
            found += _axe(page, f"{language} {size['width']} filtered")
        label = translate(language, "tree.filter")
        expect(view.get_by_role("group", name=label)).to_be_visible()
        expect(
            view.get_by_role("button", name=re.compile(translate(language, "page_type.event")))
        ).to_have_attribute("aria-pressed", "true")
    assert found == []
    assert console == []
