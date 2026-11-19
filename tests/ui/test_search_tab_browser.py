"""The Search tab in a real Chromium (package 25, D4 a; ``pytest -m browser``): reached and used
with the keyboard only, the hits ticked with "Select all", "Select none" and the space bar,
"Extract selected" with nothing ticked answers the form error, with two ticked a batch that the
worker completes; axe finds nothing on the tab and on its hits. SearXNG is the local site's
server, whose ``/search`` answers with three of its pages."""

import hashlib
from collections.abc import Iterator

import pytest
from playwright.sync_api import expect
from pytest_httpserver import HTTPServer

from tests.ui.test_a11y_browser import _axe
from tests.ui.test_playground_browser import USER_KEY, browsing, log_in, serve_site, serving
from text_extraction.settings import ApiKey, Settings
from text_extraction.ui.i18n import translate

pytestmark = pytest.mark.browser
expect.set_options(timeout=45_000)  # a job's view refreshes every 3 s

HIT_PATHS = ("/docs/a.html", "/docs/b.html", "/about.html")


@pytest.fixture
def search_ui(httpserver: HTTPServer) -> Iterator[tuple[str, str]]:
    """The /ui URL of a service with the search on, and the site's origin."""
    origin = httpserver.url_for("/").rstrip("/")
    hits = [
        {"url": f"{origin}{path}", "title": f"Page {path}", "content": "A snippet."}
        for path in HIT_PATHS
    ]
    answer = {"query": "docs", "results": hits, "suggestions": [], "unresponsive_engines": []}
    httpserver.expect_request("/search").respond_with_json(answer)  # before the site's catch-all
    serve_site(httpserver)
    settings = Settings(
        auth_mode="api_key",
        ui_enabled=True,
        rate_limit_per_minute=10_000,
        fetch_allow_private_networks=True,
        outbound_min_delay_ms=0,
        search_enabled=True,
        search_url=origin,
        search_sources=["wikipedia", "arxiv"],
        api_keys=[
            ApiKey(name="alice", role="user", sha256=hashlib.sha256(USER_KEY.encode()).hexdigest())
        ],
    )
    with serving(settings) as url:
        yield url, origin


def test_search_tab_by_keyboard(search_ui: tuple[str, str]) -> None:
    url, _origin = search_ui
    found: list[str] = []
    with browsing(url) as (page, console):
        log_in(page)
        page.wait_for_selector("#app-shell")

        # The tabs: End goes to Info, the arrow to the left to Search, before it.
        page.focus("#tab-extract")
        page.keyboard.press("End")
        page.keyboard.press("ArrowLeft")
        expect(page.locator("#tab-search")).to_be_focused()
        expect(page.locator("#panel-search")).to_be_visible()
        found += _axe(page, "tab search")

        page.focus("#search-query")
        page.keyboard.type("docs")
        page.keyboard.press("Enter")
        hits = page.locator("#result ol.search-hits > li")
        expect(hits).to_have_count(3)
        found += _axe(page, "search hits")
        counter = page.locator("#result .hits-counter")

        page.locator("[data-action=hits-all]").press("Enter")
        expect(counter).to_have_text("3 selected")
        page.locator("[data-action=hits-none]").press("Enter")
        expect(counter).to_have_text("0 selected")

        extract = page.locator("#search-hits-form button[type=submit]")
        extract.press("Enter")
        expect(page.locator("#result .search-jobs [role=alert]")).to_have_text(
            translate("en", "jobs.no_urls")
        )

        boxes = page.locator("#result ol.search-hits input[name=url]")
        boxes.nth(0).focus()
        page.keyboard.press("Space")
        boxes.nth(2).focus()
        page.keyboard.press("Space")
        expect(counter).to_have_text("2 selected")
        extract.press("Enter")
        started = page.locator("#result .search-jobs section.job")
        expect(started.locator(".state")).to_have_text("completed")
        expect(started.locator(".done")).to_have_text("2/2")
        expect(started.locator(".success")).to_have_text("2")
    assert found == []
    assert console == []
