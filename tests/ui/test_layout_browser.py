"""The layout of the playground in a real Chromium (package 15, ``pytest -m browser``): two
columns on a wide screen, one on a phone, the petrol underline of the selected tab, the main
button and the reading card's serif text."""

from typing import Final

import pytest
from playwright.sync_api import Page, expect

from tests.ui.test_playground_browser import browsing, log_in
from tests.ui.test_playground_browser import ui as ui

pytestmark = pytest.mark.browser
# The width of a block and the width of a "0" in the block's font (what CSS calls 1ch).
ZERO_WIDTH: Final = (
    "() => { const block = document.querySelector('#result .reading .markdown');"
    " const zero = document.createElement('span'); zero.textContent = '0';"
    " block.appendChild(zero); const widths = [block.getBoundingClientRect().width,"
    " zero.getBoundingClientRect().width]; zero.remove(); return widths; }"
)


def boxes(page: Page, *selectors: str) -> list[dict[str, float]]:
    return [
        page.evaluate(
            "s => { const r = document.querySelector(s).getBoundingClientRect();"
            " return {left: r.left, right: r.right, top: r.top,"
            " bottom: r.bottom, width: r.width, height: r.height}; }",
            selector,
        )
        for selector in selectors
    ]


def computed(page: Page, selector: str, *properties: str) -> list[str]:
    values: list[str] = page.evaluate(
        "([s, props]) => { const c = getComputedStyle(document.querySelector(s));"
        " return props.map(p => c[p]); }",
        [selector, list(properties)],
    )
    return values


def _extract(page: Page, origin: str) -> None:
    log_in(page)
    page.wait_for_selector("#app-shell")
    page.fill("#panel-extract input[name=url]", f"{origin}/docs/a.html")
    page.click("#panel-extract button[type=submit]")
    expect(page.locator("#result h3.title")).to_have_text("Docs A")


def test_two_columns_on_a_wide_screen(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, _):
        page.set_viewport_size({"width": 1440, "height": 900})
        _extract(page, origin)
        settings, result = boxes(page, ".settings", "#result")
        assert settings["right"] <= result["left"]
        assert abs(settings["top"] - result["top"]) <= 1
        assert 300 <= settings["width"] <= 380
        assert computed(page, "#result", "borderTopWidth") == ["0px"]


def test_one_column_on_a_phone(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, _):
        page.set_viewport_size({"width": 390, "height": 844})
        _extract(page, origin)
        settings, result = boxes(page, ".settings", "#result")
        assert result["top"] >= settings["bottom"]
        assert page.evaluate("document.documentElement.scrollWidth") <= 390
        assert computed(page, "#result", "borderTopWidth") == ["1px"]


def test_selected_tab_has_the_petrol_underline(ui: tuple[str, str]) -> None:
    url, _ = ui
    with browsing(url) as (page, _):
        log_in(page)
        page.wait_for_selector("#app-shell")
        style = computed(page, "#tab-extract", "borderBottomColor", "borderBottomWidth")
        assert style == ["rgb(29, 111, 106)", "3px"]


def test_main_button_is_wide_and_petrol(ui: tuple[str, str]) -> None:
    url, _ = ui
    with browsing(url) as (page, _):
        page.set_viewport_size({"width": 1440, "height": 900})
        log_in(page)
        page.wait_for_selector("#app-shell")
        button, form = boxes(page, "#extract-form > button[type=submit]", "#extract-form")
        assert button["width"] == form["width"]
        assert button["height"] >= 48
        assert computed(page, "#extract-form > button[type=submit]", "backgroundColor") == [
            "rgb(29, 111, 106)"
        ]


def test_reading_card_typography(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, _):
        page.set_viewport_size({"width": 1440, "height": 900})
        _extract(page, origin)
        family, size, rule, colour = computed(
            page,
            "#result .reading .markdown",
            "fontFamily",
            "fontSize",
            "borderLeftWidth",
            "borderLeftColor",
        )
        assert family.startswith("Charter")
        assert (size, rule) == ("19px", "3px")
        assert colour == "rgb(29, 111, 106)"
        (width, zero) = page.evaluate(ZERO_WIDTH)
        assert width <= 70 * zero + 1
