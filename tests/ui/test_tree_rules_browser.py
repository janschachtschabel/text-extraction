"""The site tree under the site rules switch (package 16, issue #26; ``pytest -m browser``): a
site whose robots.txt disallows one linked page. Under "Respect the site's rules" the blocked
URL is marked and cannot be selected; "Check and report only" lets it be ticked, and switching
back unticks and disables it again."""

import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import expect
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

ROBOTS = "User-agent: *\nDisallow: /docs/b.html\n"


ROBOTS_DOCS = "User-agent: *\nDisallow: /docs/\n"


@pytest.fixture
def ui(httpserver: HTTPServer) -> Iterator[tuple[str, str]]:
    """The /ui URL and the origin of a site that disallows ``/docs/b.html``."""
    yield from _site(httpserver, ROBOTS)


@pytest.fixture
def docs_blocked(httpserver: HTTPServer) -> Iterator[tuple[str, str]]:
    """The same for a site that disallows everything under ``/docs/``."""
    yield from _site(httpserver, ROBOTS_DOCS)


def _site(httpserver: HTTPServer, robots: str) -> Iterator[tuple[str, str]]:
    def handle(request: Request) -> Response:
        if request.path == "/robots.txt":
            return Response(robots, content_type="text/plain")
        found = PAGES.get(request.path)
        if found is None:
            return Response("Not found", status=404, content_type="text/plain")
        return Response(_page(*found), content_type="text/html; charset=utf-8")

    httpserver.expect_request(re.compile(".*")).respond_with_handler(handle)
    origin = httpserver.url_for("/").rstrip("/")
    with serving(_settings()) as url:
        yield url, origin


def test_blocked_url_follows_the_site_rules_switch(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, console):
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
        expect(view.locator("input[name=url]")).to_have_count(5)

        blocked = view.locator(f"input[name=url][value='{origin}/docs/b.html']")
        expect(blocked).to_have_attribute("data-blocked", "robots")
        expect(blocked).to_be_disabled()
        badge = blocked.locator("xpath=ancestor::label[1]/span[@class='blocked-badge']")
        expect(badge).to_contain_text("robots.txt")
        expect(view.locator(".tree-blocked")).to_contain_text("1 URL blocked")

        # Under "Respect the site's rules" the section selects only the allowed URL.
        label = page.locator(":scope > label > span.label", has_text=re.compile("^docs/$"))
        docs = view.locator("li.node-section", has=label)
        docs.locator(":scope > label > input").check()
        expect(view.locator(".tree-counter")).to_have_text("1 selected")
        assert not blocked.is_checked()

        # "Check and report only": the box can be ticked by hand, and "Select all" takes it.
        view.get_by_label("Check and report only").check()
        expect(blocked).to_be_enabled()
        docs.locator(":scope > button.fold").click()  # folded at first (package 17)
        blocked.check()
        expect(view.locator(".tree-counter")).to_have_text("2 selected")
        view.locator("[data-action=tree-none]").click()
        view.locator("[data-action=tree-all]").click()
        expect(view.locator(".tree-counter")).to_have_text("5 selected")
        assert blocked.is_checked()

        # Back to the default step: the box is unticked and disabled, the counter follows.
        view.get_by_label("Respect the site's rules").check()
        expect(blocked).to_be_disabled()
        assert not blocked.is_checked()
        expect(view.locator(".tree-counter")).to_have_text("4 selected")
        view.locator("[data-action=tree-all]").click()
        expect(view.locator(".tree-counter")).to_have_text("4 selected")
    assert console == []


def test_section_of_blocked_urls_cannot_be_ticked(docs_blocked: tuple[str, str]) -> None:
    url, origin = docs_blocked
    with browsing(url) as (page, console):
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
        expect(view.locator("input[name=url]")).to_have_count(5)

        root = view.locator("ul.tree > li > label > input")
        label = page.locator(":scope > label > span.label", has_text=re.compile("^docs/$"))
        docs = view.locator("li.node-section", has=label)
        section = docs.locator(":scope > label > input")
        # Under "Respect the site's rules" every URL below docs/ is blocked: no tick possible.
        expect(section).to_be_disabled()
        section.evaluate("box => box.click()")
        assert not section.is_checked()
        assert root.evaluate("box => box.indeterminate") is False
        assert not root.is_checked()
        expect(view.locator(".tree-counter")).to_have_text("0 selected")

        view.get_by_label("Check and report only").check()
        expect(section).to_be_enabled()
        section.check()
        expect(view.locator(".tree-counter")).to_have_text("2 selected")
        view.get_by_label("Respect the site's rules").check()
        expect(section).to_be_disabled()
        assert not section.is_checked()
        expect(view.locator(".tree-counter")).to_have_text("0 selected")
    assert console == []
