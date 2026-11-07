"""Playground details from the audit of 2026-11-05 in a real Chromium (``pytest -m browser``), on
a phone: the disabled "Custom capture" box draws a thin frame and dims its fields (U1), a 422 of an
Options key marks the Options field and not a disabled custom field (U2, U6), and a page type
guessed from the URL stays inside the tree (U3)."""

import re
from collections.abc import Iterator
from typing import Final

import pytest
from playwright.sync_api import Page, ViewportSize, expect
from pytest_httpserver import HTTPServer

from tests.ui.test_layout_browser import boxes, computed
from tests.ui.test_playground_browser import (
    _settings,
    browsing,
    log_in,
    open_tab,
    serve_site,
    serving,
)
from tests.ui.test_playground_browser import ui as ui
from text_extraction.ui.i18n import LANGUAGES, Language, translate

pytestmark = pytest.mark.browser
PHONE: Final[ViewportSize] = {"width": 390, "height": 844}
LINE: Final = "rgb(221, 225, 230)"  # --line
ERROR: Final = "rgb(163, 34, 43)"  # --error
# A start page, a page under it, and a terms page linked only from there, deep in sections: its
# type (legal) is guessed from its URL.
GUESSED_PAGES: Final = {
    "/": ("Start page", ["/docs/a.html"]),
    "/docs/a.html": ("Docs A", ["/docs/kurse/recht/agb.html"]),
}


def _screenshot_tab(page: Page) -> None:
    page.set_viewport_size(PHONE)
    log_in(page)
    page.wait_for_selector("#app-shell")
    open_tab(page, "screenshot")


def test_disabled_custom_capture_draws_a_thin_frame(ui: tuple[str, str]) -> None:
    url, _ = ui
    with browsing(url) as (page, console):
        _screenshot_tab(page)
        width = page.locator("#screenshot-width")
        expect(width).to_be_disabled()
        assert computed(page, "#screenshot-custom", "borderTopWidth", "borderTopStyle") == [
            "1px",
            "dashed",
        ]
        assert computed(page, "#screenshot-custom", "borderTopColor") == [LINE]
        assert float(computed(page, "#screenshot-width", "opacity")[0]) < 1
        disabled = boxes(page, "#screenshot-custom")[0]

        page.select_option("#screenshot-preset", "custom")
        expect(width).to_be_enabled()
        assert computed(page, "#screenshot-custom", "borderTopWidth", "borderTopStyle") == [
            "1px",
            "solid",
        ]
        assert computed(page, "#screenshot-width", "opacity") == ["1"]
        enabled = boxes(page, "#screenshot-custom")[0]
        assert (enabled["width"], enabled["height"]) == (disabled["width"], disabled["height"])
    assert console == []


def test_a_422_of_an_options_key_marks_the_options_field(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, console):
        _screenshot_tab(page)
        page.fill("#screenshot-url", f"{origin}/docs/a.html")
        page.click("#panel-screenshot details.advanced > summary")
        page.fill("#screenshot-options", '{"width": 3000}')
        page.evaluate("window.scrollTo(0, 0)")
        page.click("#panel-screenshot button[type=submit]")

        options = page.locator("#screenshot-options")
        expect(page.locator("#result [data-error-for=screenshot-options]")).to_be_visible()
        expect(options).to_have_attribute("aria-invalid", "true")
        assert page.evaluate("document.activeElement.id") == "screenshot-options"
        expect(options).to_have_css("border-top-color", ERROR)
        expect(options).to_have_css("border-top-width", "2px")
        assert page.locator("[aria-invalid]").count() == 1
        field = boxes(page, "#screenshot-options")[0]
        assert field["top"] >= 0
        assert field["bottom"] <= PHONE["height"]
    assert console == []


# The guessed pill's right edge against the tree box's inner edge, and whether the box or a row
# scrolls sideways.
_BADGE: Final = (
    "() => { const badge = document.querySelector('#result li[data-type] .page-type.guessed');"
    " const box = badge.closest('.tree-box'); const row = badge.closest('li');"
    " return {right: badge.getBoundingClientRect().right,"
    " inner: box.getBoundingClientRect().left + box.clientLeft + box.clientWidth,"
    " scrolls: box.scrollWidth > box.clientWidth || row.scrollWidth > row.clientWidth}; }"
)


# A wider face than the sandbox's: the runner's DejaVu Sans, which made the German tree scroll on
# GitHub (package 17), spaced a tenth wider still to leave a margin for other fonts.
_WIDER: Final = (
    "() => { document.documentElement.style"
    ".setProperty('--font-ui', '\"DejaVu Sans\", sans-serif');"
    " document.querySelector('#result .tree-box').style.letterSpacing = '0.1em'; }"
)


@pytest.fixture
def guessed_ui(httpserver: HTTPServer) -> Iterator[tuple[str, str]]:
    origin = serve_site(httpserver, GUESSED_PAGES)
    with serving(_settings()) as url:
        yield url, origin


@pytest.mark.parametrize("language", LANGUAGES)
def test_guessed_page_type_fits_the_tree(guessed_ui: tuple[str, str], language: Language) -> None:
    url, origin = guessed_ui
    with browsing(f"{url}?lang={language}") as (page, console):
        page.set_viewport_size(PHONE)
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
        # The badge's leaf is in a section, folded at first (package 17).
        page.locator("#result [data-action=tree-unfold-all]").click()

        badge = page.locator("#result li[data-type] .page-type.guessed")
        expect(badge).to_have_count(1)
        text = " ".join(badge.inner_text().split())
        legal = re.escape(translate(language, "page_type.legal"))
        assert re.match(rf"{legal} ~", text)  # then the hidden reason
        for _ in range(2):
            measured = page.evaluate(_BADGE)
            assert measured["right"] <= measured["inner"]
            assert measured["scrolls"] is False
            page.evaluate(_WIDER)
    assert console == []
