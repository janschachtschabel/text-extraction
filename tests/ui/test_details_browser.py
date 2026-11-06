"""Playground details from the audit of 2026-11-05 in a real Chromium (``pytest -m browser``), on
a phone: the disabled "Custom capture" box draws a thin frame and dims its fields (U1), a 422 of an
Options key marks the Options field and not a disabled custom field (U2, U6), and a page type
guessed from the URL keeps its space and stays inside the tree (U3)."""

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


# The gap between the type's text and its "(guessed …)" note, and the badge's right edge against
# the tree box's inner edge.
_BADGE: Final = (
    "() => { const badge = document.querySelector('#result .page-type-guessed');"
    " const note = badge.querySelector('.type-guessed'); const range = document.createRange();"
    " range.selectNodeContents(badge.firstChild); const tree = badge.closest('ul.tree');"
    " return {gap: note.getBoundingClientRect().left - range.getBoundingClientRect().right,"
    " size: parseFloat(getComputedStyle(badge).fontSize),"
    " right: badge.getBoundingClientRect().right,"
    " inner: tree.getBoundingClientRect().left + tree.clientLeft + tree.clientWidth,"
    " scrolls: tree.scrollWidth > tree.clientWidth}; }"
)


# The computed flex-wrap of the tree's labels with a page-type badge and of those without.
_LABEL_WRAPS: Final = (
    "() => { const labels = [...document.querySelectorAll('#result ul.tree label.inline')];"
    " const wrap = (label) => getComputedStyle(label).flexWrap;"
    " return {badge: labels.filter((l) => l.querySelector('.page-type')).map(wrap),"
    " plain: labels.filter((l) => !l.querySelector('.page-type')).map(wrap)}; }"
)


@pytest.fixture
def guessed_ui(httpserver: HTTPServer) -> Iterator[tuple[str, str]]:
    origin = serve_site(httpserver, GUESSED_PAGES)
    with serving(_settings()) as url:
        yield url, origin


@pytest.mark.parametrize("language", LANGUAGES)
def test_guessed_page_type_keeps_its_space_and_fits_the_tree(
    guessed_ui: tuple[str, str], language: Language
) -> None:
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

        badge = page.locator("#result .page-type-guessed")
        expect(badge).to_have_count(1)
        text = " ".join(badge.inner_text().split())
        legal = re.escape(translate(language, "page_type.legal"))
        guessed = re.escape(translate(language, "tree.type_guessed"))
        assert re.fullmatch(rf"{legal} \({guessed}\)", text)
        measured = page.evaluate(_BADGE)
        assert measured["gap"] >= 0.25 * measured["size"]
        assert measured["right"] <= measured["inner"]
        assert measured["scrolls"] is False
        # Only a label with a badge wraps; a long name without one stays beside its checkbox.
        wraps = page.evaluate(_LABEL_WRAPS)
        assert wraps["badge"]
        assert set(wraps["badge"]) == {"wrap"}
        assert wraps["plain"]
        assert set(wraps["plain"]) == {"nowrap"}
    assert console == []
