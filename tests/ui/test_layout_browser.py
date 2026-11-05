"""The layout of the playground in a real Chromium (package 15, ``pytest -m browser``): two
columns on a wide screen, one on a phone, the petrol underline of the selected tab, the main
button and the reading card's serif text."""

import re
import struct
import zlib
from collections.abc import Iterator
from typing import Final

import pytest
from playwright.sync_api import Page, ViewportSize, expect
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
from tests.ui.test_playground_browser import ui as ui
from text_extraction.ui.i18n import LANGUAGES, Language
from text_extraction.ui.routes import TABS

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


def test_pills_use_the_status_colours(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, _):
        page.set_viewport_size({"width": 1440, "height": 900})
        log_in(page)
        page.wait_for_selector("#app-shell")
        open_tab(page, "batch-file")
        panel = page.locator("#panel-batch-file")
        csv_file = f"url\n{origin}/docs/a.html\n".encode()
        panel.locator("input[type=file]").set_input_files(
            {"name": "urls.csv", "mimeType": "text/csv", "buffer": csv_file}
        )
        panel.locator("button[type=submit]").click()
        expect(page.locator("#result section.batch-preview")).to_be_visible()
        card = computed(page, "#result section.batch-preview", "backgroundColor", "borderRadius")
        assert card == ["rgb(255, 255, 255)", "12px"]
        page.locator("#result .batch-preview button[type=submit]", has_text="Start batch").click()
        expect(page.locator("#result section.job .state")).to_have_text("completed")
        expect(page.locator("#result section.job .counts .failed")).to_be_visible()
        assert computed(page, "#result section.job .state", "backgroundColor", "color") == [
            "rgb(230, 242, 240)",
            "rgb(21, 90, 86)",
        ]
        assert computed(page, "#result section.job progress", "accentColor") == [
            "rgb(29, 111, 106)"
        ]
        assert computed(page, "#result section.job", "borderRadius") == ["12px"]
        assert computed(page, "#result section.job .counts .failed", "color") == [
            "rgb(163, 34, 43)"
        ]


def _start_batch(page: Page, origin: str) -> None:
    log_in(page)
    page.wait_for_selector("#app-shell")
    open_tab(page, "batch")
    page.fill("#batch-urls", f"{origin}/docs/a.html\n{origin}/docs/b.html")
    page.click("#panel-batch button[type=submit]")
    expect(page.locator("#result section.job .state")).to_have_text("completed")
    expect(page.locator("#result section.job .done")).to_have_text("2/2")


def test_items_table_keeps_its_words_whole_on_a_phone(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, _):
        page.set_viewport_size({"width": 390, "height": 844})
        _start_batch(page, origin)
        state = boxes(page, "#result section.job .state", "#result table.items .badge")
        assert [round(box["height"]) for box in state] == [26, 26]
        header = boxes(page, "#result table.items th:nth-child(4)")[0]
        line = page.evaluate("getComputedStyle(document.querySelector('table.items th')).fontSize")
        assert header["height"] < 2 * float(line[:-2]) + 16
        show = boxes(page, "#result table.items td.open button")[0]
        assert show["height"] < 30
        assert page.evaluate("document.documentElement.scrollWidth") <= 390


def test_tree_and_preview_pills_are_26px_and_aligned(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, _):
        page.set_viewport_size({"width": 1440, "height": 900})
        log_in(page)
        page.wait_for_selector("#app-shell")
        open_tab(page, "site-structure")
        page.fill("#site-url", f"{origin}/")
        page.fill("#site-depth", "1")
        page.click("#panel-site-structure button[type=submit]")
        job = page.locator("#result section.job", has=page.locator("button.tree-button"))
        expect(job.locator(".state")).to_have_text("completed")
        job.locator("button.tree-button").click()
        expect(page.locator("#result .tree-view .page-type").first).to_be_visible()
        kind, found = boxes(page, "#result .tree-view .page-type", "#result .tree-view .found")
        assert round(kind["height"]) == 26
        assert abs((kind["top"] + kind["bottom"]) - (found["top"] + found["bottom"])) <= 2
        page.evaluate(
            "document.querySelector('#result').insertAdjacentHTML('beforeend',"
            ' \'<div class="preview-grid"><figure class="preview"><p class="skipped-line">'
            '<span class="skipped">too big</span></p></figure></div>\')'
        )
        assert round(boxes(page, "#result .skipped")[0]["height"]) == 26


PHONE: Final = ViewportSize(width=390, height=844)
OVERFLOW: Final = "document.documentElement.scrollWidth"


def _png() -> bytes:
    """A valid 2 x 2 PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + b"\x1d\x6f\x6a" * 2 for _ in range(2))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


GALLERY: Final = (
    b"<!doctype html><html lang='en'><head><title>Gallery</title></head><body><main>"
    b"<h1>Gallery</h1><p>Pictures.</p><img src='/pic.png' alt='A small petrol square with a"
    b" rather long alt text that has to wrap'><img src='/missing.png' alt='Gone'></main>"
    b"</body></html>"
)


@pytest.fixture
def gallery_ui(httpserver: HTTPServer) -> Iterator[tuple[str, str]]:
    """The /ui URL and the origin of a site whose /gallery.html shows a picture and a broken one."""

    def handle(request: Request) -> Response:
        if request.path == "/gallery.html":
            return Response(GALLERY, content_type="text/html; charset=utf-8")
        if request.path == "/pic.png":
            return Response(_png(), content_type="image/png")
        found = PAGES.get(request.path)
        if found is None:
            return Response("Not found", status=404, content_type="text/plain")
        return Response(_page(*found), content_type="text/html; charset=utf-8")

    httpserver.expect_request(re.compile(".*")).respond_with_handler(handle)
    with serving(_settings()) as url:
        yield url, httpserver.url_for("/").rstrip("/")


@pytest.mark.parametrize("language", LANGUAGES)
def test_no_horizontal_scroll_on_a_phone(ui: tuple[str, str], language: Language) -> None:
    url, origin = ui
    with browsing(f"{url}?lang={language}") as (page, _):
        page.set_viewport_size(PHONE)
        assert page.evaluate(OVERFLOW) <= PHONE["width"]
        log_in(page)
        page.wait_for_selector("#app-shell")
        for tab in TABS:
            open_tab(page, tab.name)
            page.evaluate(
                "name => document.querySelectorAll(`#panel-${name} details`)"
                ".forEach(d => { d.open = true; })",
                tab.name,
            )
            assert page.evaluate(OVERFLOW) <= PHONE["width"], tab.name
        open_tab(page, "extract")
        page.fill("#extract-url", f"{origin}/docs/a.html")
        page.click("#panel-extract button[type=submit]")
        expect(page.locator("#result h3.title")).to_have_text("Docs A")
        page.evaluate(
            "document.querySelectorAll('#result details').forEach(d => { d.open = true; })"
        )
        assert page.evaluate(OVERFLOW) <= PHONE["width"], "extract result"

        open_tab(page, "batch")
        page.fill("#batch-urls", f"{origin}/docs/a.html\n{origin}/docs/b.html")
        page.click("#panel-batch button[type=submit]")
        expect(page.locator("#result section.job .done")).to_have_text("2/2")
        assert page.evaluate(OVERFLOW) <= PHONE["width"], "batch progress"

        open_tab(page, "site-structure")
        page.fill("#site-url", f"{origin}/")
        page.fill("#site-depth", "1")
        page.click("#panel-site-structure button[type=submit]")
        job = page.locator("#result section.job", has=page.locator("button.tree-button"))
        expect(job.locator(".state")).to_have_attribute("data-value", "completed")
        job.locator("button.tree-button").click()
        expect(page.locator("#result .tree-view input[name=url]")).to_have_count(5)
        assert page.evaluate(OVERFLOW) <= PHONE["width"], "site tree"


def test_tables_and_text_scroll_inside_their_card(ui: tuple[str, str]) -> None:
    """A table wider than the phone scrolls in its card; a long word in ``dd`` or a ``.url``
    cell breaks instead of widening the page."""
    url, origin = ui
    long_word = "x" * 120
    with browsing(url) as (page, _):
        page.set_viewport_size(PHONE)
        _extract(page, origin)
        page.evaluate(
            """word => {
                const card = document.querySelector('#result .card');
                card.insertAdjacentHTML('beforeend',
                    '<table class="values"><tr><td>' + word + word + '</td></tr></table>'
                    + '<dl class="meta"><dt>k</dt><dd>' + word + word + '</dd></dl>'
                    + '<pre>' + word + word + '</pre>');
            }""",
            long_word,
        )
        assert page.evaluate(OVERFLOW) <= PHONE["width"]
        page.evaluate(
            "document.querySelector('#result .card').insertAdjacentHTML('beforeend',"
            ' \'<table class="items"><tr><td class="url">u</td></tr></table>'
            '<table class="images"><tr><th>Description</th></tr></table>\')'
        )
        for table in ("values", "items", "images"):
            assert computed(page, f"#result table.{table}", "display", "overflowX") == [
                "block",
                "auto",
            ]


def test_items_url_column_and_images_header_on_a_phone(ui: tuple[str, str]) -> None:
    """The URL column keeps 12rem, so a URL does not break every few characters: the table
    scrolls in its card, the page does not. A header word stays whole."""
    url, origin = ui
    with browsing(url) as (page, _):
        page.set_viewport_size(PHONE)
        _start_batch(page, origin)
        cell = boxes(page, "#result table.items td.url")[0]
        assert cell["width"] >= 12 * 16 - 1
        assert page.evaluate(OVERFLOW) <= PHONE["width"]
        scrolls = page.evaluate(
            "() => { const t = document.querySelector('#result table.items');"
            " return t.scrollWidth >= t.clientWidth; }"
        )
        assert scrolls
        page.evaluate(
            "document.querySelector('#result').insertAdjacentHTML('beforeend',"
            ' \'<table class="images"><tr><th>Description</th></tr></table>'
            '<form class="previews"><button class="secondary">x</button></form>\')'
        )
        assert computed(page, "#result table.images th", "whiteSpace") == ["nowrap"]
        table, form = boxes(page, "#result table.images", "#result form.previews")
        assert form["top"] - table["bottom"] >= 8


def test_phone_gutter_is_16px(ui: tuple[str, str]) -> None:
    url, _ = ui
    with browsing(url) as (page, _):
        page.set_viewport_size(PHONE)
        assert (
            page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--gutter')")
            == "16px"
        )
        page.set_viewport_size(ViewportSize(width=600, height=844))
        assert (
            page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--gutter')")
            == "24px"
        )


FOCUS_RING: Final = ["solid", "3px", "rgb(29, 111, 106)", "2px"]
RING = ("outlineStyle", "outlineWidth", "outlineColor", "outlineOffset")


def _focus_by_keyboard(page: Page, selector: str) -> None:
    """Focus ``selector`` as a keyboard user does, so that :focus-visible matches."""
    page.locator(selector).first.focus()
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")


@pytest.mark.parametrize(
    "selector",
    [
        "#panel-extract details.advanced > summary",
        "#extract-form .segmented input",
        "#tab-extract",
        "#extract-url",
    ],
)
def test_focus_ring_on_summary_segment_tab_and_field(ui: tuple[str, str], selector: str) -> None:
    url, _ = ui
    with browsing(url) as (page, _):
        log_in(page)
        page.wait_for_selector("#app-shell")
        _focus_by_keyboard(page, selector)
        assert computed(page, ":focus", *RING) == FOCUS_RING


def test_focus_ring_on_download_and_tree_checkbox(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, _):
        page.set_viewport_size(PHONE)
        _extract(page, origin)
        _focus_by_keyboard(page, "#result a.download")
        assert computed(page, ":focus", *RING) == FOCUS_RING
        open_tab(page, "site-structure")
        page.fill("#site-url", f"{origin}/")
        page.fill("#site-depth", "1")
        page.click("#panel-site-structure button[type=submit]")
        job = page.locator("#result section.job", has=page.locator("button.tree-button"))
        expect(job.locator(".state")).to_have_text("completed")
        job.locator("button.tree-button").click()
        expect(page.locator("#result .tree-view input[name=url]")).to_have_count(5)
        _focus_by_keyboard(page, "#result ul.tree input[type=checkbox]")
        assert computed(page, ":focus", *RING) == FOCUS_RING


def test_reduced_motion_turns_transitions_off(ui: tuple[str, str]) -> None:
    url, _ = ui
    with browsing(url) as (page, _):
        log_in(page)
        page.wait_for_selector("#app-shell")
        page.emulate_media(reduced_motion="no-preference")
        props, duration = computed(page, "#tab-file", "transitionProperty", "transitionDuration")
        assert "color" in props
        assert "all" not in props.split(", ")
        assert duration.split(", ")[0] == "0.12s"
        page.emulate_media(reduced_motion="reduce")
        (duration,) = computed(page, "#tab-file", "transitionDuration")
        assert float(duration.split(", ")[0].rstrip("s")) <= 0.001


def test_disabled_button_does_not_change_on_hover(ui: tuple[str, str]) -> None:
    url, _ = ui
    with browsing(url) as (page, _):
        log_in(page)
        page.wait_for_selector("#app-shell")
        open_tab(page, "screenshot")
        page.locator("#screenshot-quality").evaluate("field => field.blur()")
        page.evaluate(
            "document.querySelector('#panel-screenshot form').insertAdjacentHTML('beforeend',"
            ' \'<button type="button" id="probe" disabled>Probe</button>'
            '<button type="button" id="probe2" class="secondary" disabled>Probe</button>\')'
        )
        for selector in ("#probe", "#probe2"):
            before = computed(page, selector, "backgroundColor", "borderColor", "cursor")
            page.locator(selector).hover(force=True)
            assert computed(page, selector, "backgroundColor", "borderColor", "cursor") == before
            assert before[2] == "not-allowed"


def test_long_legend_wraps_inside_the_settings_column(ui: tuple[str, str]) -> None:
    url, _ = ui
    with browsing(url) as (page, _):
        page.set_viewport_size(PHONE)
        log_in(page)
        page.wait_for_selector("#app-shell")
        page.evaluate(
            "document.querySelector('#extract-form fieldset.choice > legend').textContent ="
            " 'Averyveryveryverylonguppercaselegendwithoutanyspaceatall ' + 'abc '.repeat(30)"
        )
        legend, column = boxes(page, "#extract-form fieldset.choice > legend", ".settings")
        assert legend["right"] <= column["right"]
        assert legend["height"] > 30
        assert page.evaluate(OVERFLOW) <= PHONE["width"]


def test_checked_segment_keeps_its_border_width_and_shows_2px_in_accent(
    ui: tuple[str, str],
) -> None:
    """The 1 px border stays 1 px, so the content does not shift by 1 px when a segment is
    checked; an inset shadow of 1 px in the accent adds up to the visible 2 px."""
    url, _ = ui
    with browsing(url) as (page, _):
        log_in(page)
        page.wait_for_selector("#app-shell")
        labels = "#extract-form .segmented label.inline"
        names = ("borderTopWidth", "paddingLeft", "paddingTop")
        unchecked = computed(page, f"{labels}:nth-child(2)", *names)
        page.locator(f"{labels}:nth-child(2) input").check()
        checked = f"{labels}:has(input:checked)"
        page.wait_for_timeout(300)  # the 120 ms colour transitions end
        assert computed(page, checked, *names) == unchecked
        border, shadow = computed(page, checked, "borderTopColor", "boxShadow")
        assert border == "rgb(29, 111, 106)"
        assert shadow.startswith("rgb(29, 111, 106) 0px 0px 0px 1px inset")


def test_preview_images_fit_a_phone(gallery_ui: tuple[str, str]) -> None:
    url, origin = gallery_ui
    with browsing(url) as (page, _):
        page.set_viewport_size(PHONE)
        log_in(page)
        page.wait_for_selector("#app-shell")
        page.fill("#extract-url", f"{origin}/gallery.html")
        page.click("#panel-extract button[type=submit]")
        expect(page.locator("#result h3.title")).to_have_text("Gallery")
        page.locator("#result details.card", has=page.locator("table.images")).evaluate(
            "d => { d.open = true; }"
        )
        page.locator("#result form.previews button").click()
        expect(page.locator("#result figure.preview")).to_have_count(2)
        expect(page.locator("#result figure.preview img")).to_have_count(1)
        expect(page.locator("#result figure.preview .skipped")).to_have_count(1)
        page.locator("#result .previews-out").scroll_into_view_if_needed()
        assert page.evaluate(OVERFLOW) <= PHONE["width"]
        for box in [
            page.evaluate(
                "e => { const r = e.getBoundingClientRect(); return [r.left, r.right]; }", el
            )
            for el in page.locator("#result figure.preview").element_handles()
        ]:
            assert box[0] >= 0
            assert box[1] <= PHONE["width"]
