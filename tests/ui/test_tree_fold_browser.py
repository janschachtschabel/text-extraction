"""Folding the site tree (package 17, ``pytest -m browser``): a site whose robots.txt disallows
``/docs/b.html``, whose ``/docs/a.html`` links a page one level too deep (a "type open" leaf)
and whose start page links a social network profile. The host stays open and its sections
start folded; a fold button works with the keyboard; "Unfold all" and "Fold all" set every
button; the selection reaches the pages of a folded section, but not a blocked one under
"Respect the site's rules"."""

import re
from collections.abc import Iterator
from typing import Final

import pytest
from playwright.sync_api import Locator, Page, expect
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from tests.ui.test_playground_browser import (
    PAGES,
    _page,
    _settings,
    browsing,
    log_in,
    open_tab,
    serving,
)

pytestmark = pytest.mark.browser

ROBOTS: Final = "User-agent: *\nDisallow: /docs/b.html\n"
SOCIAL: Final = "https://www.youtube.com/@a-rather-long-channel-name-that-has-to-wrap-somewhere"
SITE: Final = {
    **PAGES,
    "/": ("Start page", [*PAGES["/"][1], SOCIAL]),
    "/docs/a.html": ("Docs A", ["/docs/deeper.html"]),
}
URLS: Final = 6  # the five pages and /docs/deeper.html, linked only


@pytest.fixture
def ui(httpserver: HTTPServer) -> Iterator[tuple[str, str]]:
    """The /ui URL and the origin of ``SITE``."""

    def handle(request: Request) -> Response:
        if request.path == "/robots.txt":
            return Response(ROBOTS, content_type="text/plain")
        found = SITE.get(request.path)
        if found is None:
            return Response("Not found", status=404, content_type="text/plain")
        return Response(_page(*found), content_type="text/html; charset=utf-8")

    httpserver.expect_request(re.compile(".*")).respond_with_handler(handle)
    with serving(_settings()) as url:
        yield url, httpserver.url_for("/").rstrip("/")


def _open_tree(page: Page, origin: str) -> Locator:
    """Log in, read the site's structure and show its tree; the tree view."""
    log_in(page)
    page.wait_for_selector("#app-shell")
    open_tab(page, "site-structure")
    panel = page.locator("#panel-site-structure")
    panel.locator("input[name=url]").fill(f"{origin}/")
    panel.locator("input[name=depth]").fill("1")
    panel.locator("button[type=submit]").click()
    job = page.locator("#result section.job")
    expect(job.locator(".state")).to_have_text("completed")
    job.locator("button.tree-button").click()
    view = page.locator("#result .tree-view")
    expect(view.locator("input[name=url]")).to_have_count(URLS)
    return view


def _section(view: Locator, label: str) -> Locator:
    name = view.page.locator(":scope > label > span.label", has_text=re.compile(f"^{label}$"))
    return view.locator("li.node-section", has=name)


def _box(view: Locator, origin: str, path: str) -> Locator:
    return view.locator(f"input[name=url][value='{origin}{path}']")


def test_top_level_open_sections_folded(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, console):
        view = _open_tree(page, origin)
        host = view.locator("ul.tree > li")
        host_button = host.locator(":scope > button.fold")
        expect(host_button).to_be_visible()
        expect(host_button).to_have_attribute("aria-expanded", "true")
        expect(host.locator(":scope > ul")).to_be_visible()
        expect(_box(view, origin, "/about.html")).to_be_visible()

        docs = _section(view, "docs/")
        button = docs.locator(":scope > button.fold")
        expect(button).to_have_attribute("aria-expanded", "false")
        expect(button).to_have_accessible_name("Pages under docs/")
        controlled = button.get_attribute("aria-controls")
        assert controlled == docs.locator(":scope > ul").get_attribute("id")
        expect(docs.locator(":scope > ul")).to_be_hidden()
        expect(_box(view, origin, "/docs/a.html")).to_be_hidden()
        expect(docs.locator(":scope > label > input")).to_be_visible()

        for action in ("tree-unfold-all", "tree-fold-all"):
            expect(view.locator(f"[data-action={action}]")).to_be_visible()
        social = view.locator(".tree-social")
        expect(social.get_by_role("link")).to_have_attribute("href", SOCIAL)

        # On a phone the social list wraps instead of widening the page.
        page.set_viewport_size({"width": 390, "height": 844})
        expect(social).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth") <= 390
    assert console == []


def test_fold_button_toggles_with_keyboard(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, console):
        view = _open_tree(page, origin)
        docs = _section(view, "docs/")
        button = docs.locator(":scope > button.fold")
        # The fold button is the next tab stop after the row before it.
        _box(view, origin, "/about.html").focus()
        page.keyboard.press("Tab")
        expect(button).to_be_focused()
        page.keyboard.press("Space")
        expect(button).to_have_attribute("aria-expanded", "true")
        expect(docs.locator(":scope > ul")).to_be_visible()
        expect(_box(view, origin, "/docs/deeper.html")).to_be_visible()
        page.keyboard.press("Enter")
        expect(button).to_have_attribute("aria-expanded", "false")
        expect(docs.locator(":scope > ul")).to_be_hidden()
        expect(button).to_be_focused()
        # A click works too, and leaves the selection alone.
        button.click()
        expect(button).to_have_attribute("aria-expanded", "true")
        expect(view.locator(".tree-counter")).to_have_text("0 selected")
    assert console == []


def test_unfold_all_and_fold_all(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, console):
        view = _open_tree(page, origin)
        buttons = view.locator("ul.tree button.fold")
        lists = view.locator("ul.tree ul")
        count = buttons.count()
        assert count == lists.count() == 2  # the host and docs/

        view.locator("[data-action=tree-unfold-all]").click()
        for index in range(count):
            expect(buttons.nth(index)).to_have_attribute("aria-expanded", "true")
            expect(lists.nth(index)).to_be_visible()
        expect(_box(view, origin, "/docs/deeper.html")).to_be_visible()

        view.locator("[data-action=tree-fold-all]").click()
        for index in range(count):
            expect(buttons.nth(index)).to_have_attribute("aria-expanded", "false")
            expect(lists.nth(index)).to_be_hidden()
        expect(view.locator("ul.tree > li > label > input")).to_be_visible()
        expect(_box(view, origin, "/about.html")).to_be_hidden()
    assert console == []


def test_ticking_a_folded_section_ticks_its_pages(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, console):
        view = _open_tree(page, origin)
        docs = _section(view, "docs/")
        expect(docs.locator(":scope > ul")).to_be_hidden()
        docs.locator(":scope > label > input").check()
        # a.html and deeper.html; b.html is blocked under "Respect the site's rules".
        expect(view.locator(".tree-counter")).to_have_text("2 selected")
        assert _box(view, origin, "/docs/a.html").is_checked()
        assert _box(view, origin, "/docs/deeper.html").is_checked()
        assert not _box(view, origin, "/docs/b.html").is_checked()
        expect(docs.locator(":scope > ul")).to_be_hidden()  # ticking does not unfold
        host = view.locator("ul.tree > li > label > input")
        assert host.evaluate("box => box.indeterminate") is True

        docs.locator(":scope > label > input").uncheck()
        expect(view.locator(".tree-counter")).to_have_text("0 selected")
        assert not _box(view, origin, "/docs/a.html").is_checked()
    assert console == []


def test_select_all_reaches_folded_pages_but_not_blocked_under_enforce(
    ui: tuple[str, str],
) -> None:
    url, origin = ui
    with browsing(url) as (page, console):
        view = _open_tree(page, origin)
        blocked = _box(view, origin, "/docs/b.html")
        expect(blocked).to_have_attribute("data-blocked", "robots")
        expect(blocked).to_be_disabled()

        view.locator("[data-action=tree-all]").click()
        expect(view.locator(".tree-counter")).to_have_text(f"{URLS - 1} selected")
        assert _box(view, origin, "/docs/a.html").is_checked()
        assert _box(view, origin, "/docs/deeper.html").is_checked()
        assert not blocked.is_checked()
        docs = _section(view, "docs/")
        assert docs.locator(":scope > label > input").is_checked()
        expect(docs.locator(":scope > ul")).to_be_hidden()

        view.locator("[data-action=tree-none]").click()
        view.locator("[data-action=tree-content]").click()  # the imprint is left out
        expect(view.locator(".tree-counter")).to_have_text(f"{URLS - 2} selected")
        assert _box(view, origin, "/docs/deeper.html").is_checked()
        assert not blocked.is_checked()
    assert console == []
