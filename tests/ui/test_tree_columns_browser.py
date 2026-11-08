"""The site tree in columns with type filter chips (package 18, ``pytest -m browser``): a site
with four types (home, content, imprint and, linked only, two events in a section of their own).
A chip leaves the leaves of its types and their sections; "All" restores everything; the type
column lines up in every row at 1440 px; no row overflows at 390 px; axe-core finds nothing in
either language and width with a filter active."""

import html
import re
from collections.abc import Iterator
from typing import Final

import pytest
from playwright.sync_api import Locator, Page, ViewportSize, expect
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from tests.site.test_done_when import TEXT
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

        # "Select all" and "Select content pages" tick the visible leaves only.
        home = view.locator(f"input[name=url][value='{origin}/']")
        view.locator("[data-action=tree-all]").click()
        expect(view.locator(".tree-counter")).to_have_text("2 selected")
        expect(home).not_to_be_checked()  # hidden by the filter, so not ticked

        # A hidden box keeps its state.
        _chip(view, "all").click()
        home.click()
        _chip(view, "event").click()
        expect(view.locator("input[name=url]:checked")).to_have_count(3)
        view.locator("[data-action=tree-content]").click()
        expect(home).to_be_checked()
        expect(view.locator("input[name=url]:checked")).to_have_count(3)

        # "Select none" clears everything, the hidden boxes included.
        view.locator("[data-action=tree-none]").click()
        expect(view.locator("input[name=url]:checked")).to_have_count(0)
        expect(home).not_to_be_checked()
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


# --- a deep path and a long title on a phone (third audit R3, U3) ---------------------------------

DEEP_PATH: Final = "/" + "/".join(f"level{n}" for n in range(1, 13)) + "/leaf.html"
LONG_TITLE: Final = "Lernmaterial " + "W" * 307  # 320 characters, most of them one long word
RLO_TITLE: Final = "‮Free download gnp.exe"  # a right-to-left override: "exe.png" spoofed
DEEP_SITE: Final = {
    "/": ("Start page", [DEEP_PATH, "/docs/a.html"]),
    DEEP_PATH: (LONG_TITLE, []),
    "/docs/a.html": ("Docs A", ["/docs/rlo"]),
}
LINK_TEXTS: Final = {"/docs/rlo": RLO_TITLE}
# The boxes of one name cell's parts that must not overlap: fold button, checkbox, path label,
# title and a section's page count.
OVERLAPS: Final = """() => {
  const found = [];
  for (const row of document.querySelectorAll('#result ul.tree li')) {
    const cell = row.querySelector(':scope > .cell-name');
    const parts = [':scope > button.fold:not([hidden])', ':scope > label > input',
      ':scope > label > .label', ':scope > label > .title', ':scope > .pages']
      .map((s) => cell.querySelector(s)).filter(Boolean)
      .map((e) => [e.className || e.tagName, e.getBoundingClientRect()]);
    for (let i = 0; i < parts.length; i++) {
      for (let j = i + 1; j < parts.length; j++) {
        const [a, ra] = parts[i];
        const [b, rb] = parts[j];
        const x = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
        const y = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
        if (x > 0.5 && y > 0.5) found.push(`level ${row.dataset.level}: ${a} / ${b}`);
      }
    }
    const right = cell.getBoundingClientRect().right;
    for (const [name, rect] of parts) {
      if (rect.right > right + 0.5) found.push(`level ${row.dataset.level}: ${name} overflows`);
    }
  }
  return found;
}"""


def _deep_page(title: str, links: list[str]) -> bytes:
    anchors = "".join(
        f'<li><a href="{link}">{html.escape(LINK_TEXTS.get(link, link))}</a></li>' for link in links
    )
    return (
        f"<!doctype html><html lang='en'><head><title>{html.escape(title)}</title></head><body>"
        f"<main><h1>{html.escape(title)}</h1><p>{TEXT}</p><ul>{anchors}</ul></main></body></html>"
    ).encode()


@pytest.fixture
def deep_ui(httpserver: HTTPServer) -> Iterator[tuple[str, str]]:
    """The /ui URL and the origin of a site with a page twelve sections deep whose title is 320
    characters long, and a link whose text holds a right-to-left override."""

    def handle(request: Request) -> Response:
        found = DEEP_SITE.get(request.path)
        if found is None:
            return Response("Not found", status=404, content_type="text/plain")
        return Response(_deep_page(*found), content_type="text/html; charset=utf-8")

    httpserver.expect_request(re.compile(".*")).respond_with_handler(handle)
    with serving(_settings()) as url:
        yield url, httpserver.url_for("/").rstrip("/")


def _open_deep_tree(page: Page, origin: str) -> Locator:
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
    expect(view.locator("input[name=url]")).to_have_count(4)
    view.locator("[data-action=tree-unfold-all]").click()
    expect(view.locator("li[data-level='12']").first).to_be_visible()
    return view


@pytest.mark.parametrize("language", LANGUAGES)
def test_a_deep_path_and_a_long_title_fit_a_phone(
    deep_ui: tuple[str, str], language: Language
) -> None:
    """At 390 px the checkbox, the path, the title and a section's page count of every level do
    not overlap, nothing scrolls sideways and every checkbox keeps its target size."""
    url, origin = deep_ui
    with browsing(f"{url}?lang={language}") as (page, console):
        page.set_viewport_size(PHONE)
        view = _open_deep_tree(page, origin)
        assert page.evaluate(OVERLAPS) == []
        assert page.evaluate("document.scrollingElement.scrollWidth") <= PHONE["width"]
        rows = page.evaluate(
            "() => [...document.querySelectorAll('#result ul.tree li')]"
            ".map((row) => row.scrollWidth - row.clientWidth)"
        )
        assert max(rows) <= 0
        leaf = view.locator(f"input[name=url][value='{origin}{DEEP_PATH}']")
        expect(leaf).to_be_visible()
        assert _axe(page, f"{language} deep tree at 390") == []
    assert console == []


def test_a_deep_path_keeps_its_columns_at_1440(deep_ui: tuple[str, str]) -> None:
    url, origin = deep_ui
    with browsing(url) as (page, console):
        page.set_viewport_size(DESKTOP)
        _open_deep_tree(page, origin)
        assert page.evaluate(OVERLAPS) == []
        assert page.evaluate("document.scrollingElement.scrollWidth") <= DESKTOP["width"]
    assert console == []


def test_paths_and_titles_are_isolated_from_their_neighbours(deep_ui: tuple[str, str]) -> None:
    """A right-to-left override in a title or link text cannot reorder the text around it: the
    path and the title are bidi isolates, and a link text sits in its own ``bdi`` inside its
    quotes."""
    url, origin = deep_ui
    with browsing(url) as (page, console):
        view = _open_deep_tree(page, origin)
        isolation = page.evaluate(
            "() => [...document.querySelectorAll('#result ul.tree .label, #result ul.tree .title')]"
            ".map((e) => getComputedStyle(e).unicodeBidi)"
        )
        assert isolation
        assert set(isolation) == {"isolate"}
        linked = view.locator(f"li:has(> .cell-name input[value='{origin}/docs/rlo']) .title-link")
        expect(linked.locator("bdi")).to_have_text(RLO_TITLE)
        expect(linked).to_have_text(f"“{RLO_TITLE}”")
    assert console == []
