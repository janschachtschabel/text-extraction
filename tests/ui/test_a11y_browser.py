"""The playground's accessibility in a real Chromium (rulings P12-S8, P12-S9; marker
``browser``): axe-core finds no WCAG 2.2 A or AA violation in any state of the UI in either
language; the tabs follow the WAI-ARIA keyboard pattern; focus is visible; the 3 s progress
refresh keeps focus and announces each state once; ``#ui-status`` says when a request runs and
how it ended; a form error is tied to its field; the language switch keeps the login.

axe-core is the vendored, pinned release (``tests/ui/vendor``), injected with ``page.evaluate``
so the page's CSP stays as it is. No rule and no element is excluded."""

import hashlib
import re
import time
from collections.abc import Iterator
from typing import Any, Final

import pytest
from playwright.sync_api import Page, expect
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from tests.ui.test_a11y_markup import AXE_FILE
from tests.ui.test_playground_browser import PAGES, _page, browsing, serving
from text_extraction.settings import ApiKey, Settings
from text_extraction.ui.i18n import LANGUAGES, Language, translate
from text_extraction.ui.routes import TABS

pytestmark = pytest.mark.browser
expect.set_options(timeout=45_000)  # a job's view refreshes every 3 s

ADMIN_KEY = "admin-key-0123456789-raw-secret"
# The WCAG levels the playground meets (Global Constraints: WCAG 2.2 A and AA).
AXE_TAGS: Final = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]
# A page that answers after this many seconds, so a request or a job stays visibly running.
SLOW_PATH = "/slow.html"
SLOW_SECONDS = 10.0
_AXE_RUN: Final = (
    "async tags => { const result = await axe.run(document, {runOnly: {type: 'tag', values:"
    " tags}, resultTypes: ['violations']}); return result.violations.map(violation => ({id:"
    " violation.id, help: violation.help, targets: violation.nodes.map(node =>"
    " node.target.join(' '))})); }"
)
# Records every text #ui-status shows, in order.
_RECORD_STATUS: Final = (
    "() => { window.uiStatusTexts = []; const status = document.getElementById('ui-status');"
    " new MutationObserver(() => window.uiStatusTexts.push(status.textContent)).observe(status,"
    " {childList: true, characterData: true, subtree: true}); }"
)


def _serve(httpserver: HTTPServer) -> str:
    def handle(request: Request) -> Response:
        if request.path == SLOW_PATH:
            time.sleep(SLOW_SECONDS)
            return Response(_page("Slow page", []), content_type="text/html; charset=utf-8")
        found = PAGES.get(request.path)
        if found is None:
            return Response("Not found", status=404, content_type="text/plain")
        return Response(_page(*found), content_type="text/html; charset=utf-8")

    httpserver.expect_request(re.compile(".*")).respond_with_handler(handle)
    return httpserver.url_for("/").rstrip("/")


@pytest.fixture
def ui(httpserver: HTTPServer) -> Iterator[tuple[str, str]]:
    """The /ui URL of a service with one admin key (so the forms show "Site rules", disabled
    here: no browser) and the local site's origin."""
    origin = _serve(httpserver)
    settings = Settings(
        auth_mode="api_key",
        ui_enabled=True,
        rate_limit_per_minute=10_000,
        fetch_allow_private_networks=True,
        outbound_min_delay_ms=0,
        api_keys=[
            ApiKey(name="root", role="admin", sha256=hashlib.sha256(ADMIN_KEY.encode()).hexdigest())
        ],
    )
    with serving(settings) as url:
        yield url, origin


@pytest.fixture
def ui_unblock() -> Iterator[str]:
    """The /ui URL of a service with one admin key and ``BROWSER_ENABLED`` and
    ``BROWSER_UNBLOCK_ENABLED`` on, so the forms offer "Site rules" enabled (ruling R-P12-6).
    Nothing is rendered: the app needs no browser worker to show its forms."""
    settings = Settings(
        auth_mode="api_key",
        ui_enabled=True,
        rate_limit_per_minute=10_000,
        browser_enabled=True,
        browser_unblock_enabled=True,
        api_keys=[
            ApiKey(name="root", role="admin", sha256=hashlib.sha256(ADMIN_KEY.encode()).hexdigest())
        ],
    )
    with serving(settings) as url:
        yield url


def _axe(page: Page, state: str) -> list[str]:
    """The axe violations of ``page`` as "<state>: <rule> <target>: <help>" lines."""
    if not page.evaluate("typeof window.axe !== 'undefined'"):
        page.evaluate(AXE_FILE.read_text(encoding="utf-8"))
    violations: list[dict[str, Any]] = page.evaluate(_AXE_RUN, AXE_TAGS)
    return [
        f"{state}: {violation['id']} {target}: {violation['help']}"
        for violation in violations
        for target in violation["targets"]
    ]


def _log_in(page: Page, key: str = ADMIN_KEY) -> None:
    page.fill("#login-api-key", key)
    page.click("#login-form button[type=submit]")


def _submit(page: Page, tab: str) -> None:
    page.locator(f"#panel-{tab} button[type=submit]").click()


def test_axe_reports_a_violation() -> None:
    """The harness itself: the injected axe-core is 4.14.0 and reports a known violation."""
    with browsing("about:blank") as (page, console):
        page.set_content(
            "<html lang='en'><head><title>Probe</title></head>"
            "<body><main><img src='x.png'></main></body></html>"
        )
        found = _axe(page, "probe")
        version = page.evaluate("axe.version")
    assert version == "4.14.0"
    assert [line.split(" ")[1] for line in found] == ["image-alt"]
    assert [line for line in console if "x.png" not in line] == []


@pytest.mark.parametrize("language", LANGUAGES)
def test_axe_finds_no_violations(ui: tuple[str, str], language: Language) -> None:
    url, origin = ui
    found: list[str] = []
    with browsing(f"{url}?lang={language}") as (page, console):
        found += _axe(page, "login")
        _log_in(page, "wrong-key-0123456789")
        expect(page.locator("#login-form .error")).to_be_visible()
        found += _axe(page, "login with an error")

        _log_in(page)
        page.wait_for_selector("#app-shell")
        for tab in TABS:
            page.click(f"#tab-{tab.name}")
            expect(page.locator(f"#panel-{tab.name}")).to_be_visible()
            found += _axe(page, f"tab {tab.name}")

        page.click("#tab-extract")
        page.fill("#extract-url", f"{origin}/docs/a.html")
        _submit(page, "extract")
        expect(page.locator("#result h3.title")).to_have_text("Docs A")
        found += _axe(page, "extract result")

        page.fill("#extract-options", "{")
        _submit(page, "extract")
        expect(page.locator("#result [data-error-for]")).to_be_visible()
        found += _axe(page, "form error")
        page.fill("#extract-options", "")

        page.click("#tab-batch")
        page.fill("#batch-urls", f"{origin}{SLOW_PATH}")
        _submit(page, "batch")
        job = page.locator("#result section.job")
        expect(job.locator(".state")).to_have_attribute("data-value", re.compile("queued|running"))
        found += _axe(page, "running progress")
        expect(job.locator(".state")).to_have_attribute("data-value", "completed")
        found += _axe(page, "completed progress")

        page.click("#tab-site-structure")
        page.fill("#site-url", f"{origin}/")
        page.fill("#site-depth", "1")
        _submit(page, "site-structure")
        site = page.locator("#result section.job", has=page.locator("button.tree-button"))
        expect(site.locator(".state")).to_have_attribute("data-value", "completed")
        site.locator("button.tree-button").click()
        expect(page.locator("#result .tree-view input[name=url]")).to_have_count(4)
        found += _axe(page, "site tree")

    assert found == [], "\n".join(found)
    assert console == []


@pytest.mark.parametrize("language", LANGUAGES)
def test_axe_finds_no_violations_with_maximum_extraction_on(
    ui_unblock: str, language: Language
) -> None:
    with browsing(f"{ui_unblock}?lang={language}") as (page, console):
        _log_in(page)
        page.wait_for_selector("#app-shell")
        expect(page.locator("#panel-extract")).to_be_visible()
        expect(page.locator("#extract-access-maximum")).to_be_enabled()
        expect(page.locator("#extract-mode-fast-hint")).to_be_visible()
        found = _axe(page, "extract tab, maximum extraction on")

    assert found == [], "\n".join(found)
    assert console == []


def test_tabs_follow_the_aria_keyboard_pattern(ui: tuple[str, str]) -> None:
    url, _ = ui
    names = [tab.name for tab in TABS]

    def selected(page: Page) -> list[Any]:
        state: list[Any] = page.evaluate(
            "() => [document.activeElement.id,"
            " document.querySelector('[role=tab][aria-selected=true]').id,"
            " [...document.querySelectorAll('[role=tabpanel]:not([hidden])')].map(p => p.id)]"
        )
        return state

    def at(name: str) -> list[Any]:
        return [f"tab-{name}", f"tab-{name}", [f"panel-{name}"]]

    with browsing(url) as (page, console):
        _log_in(page)
        page.wait_for_selector("#app-shell")
        page.focus("#tab-extract")
        page.keyboard.press("ArrowRight")
        assert selected(page) == at(names[1])
        page.keyboard.press("ArrowLeft")
        assert selected(page) == at(names[0])
        page.keyboard.press("ArrowLeft")  # wraps around
        assert selected(page) == at(names[-1])
        page.keyboard.press("Home")
        assert selected(page) == at(names[0])
        page.keyboard.press("End")
        assert selected(page) == at(names[-1])
        page.keyboard.press("ArrowRight")  # wraps around
        assert selected(page) == at(names[0])
        tabindex = page.evaluate(
            "() => [...document.querySelectorAll('[role=tab]')].map(t => t.tabIndex)"
        )
        assert tabindex == [0] + [-1] * (len(names) - 1)
        page.keyboard.press("Tab")  # out of the tab list, into the panel
        assert page.evaluate("document.activeElement.id") == "panel-extract"
    assert console == []


def test_focus_is_visible(ui: tuple[str, str]) -> None:
    url, _ = ui
    with browsing(url) as (page, console):
        page.keyboard.press("Tab")
        skip = page.evaluate(
            "() => { const link = document.activeElement; const box ="
            " link.getBoundingClientRect(); return [link.getAttribute('href'), box.top >= 0 &&"
            " box.width > 1 && box.height > 1]; }"
        )
        assert skip == ["#app", True]
        for _ in range(10):
            page.keyboard.press("Tab")
            if page.evaluate("document.activeElement.tagName") == "BUTTON":
                break
        outline = page.evaluate(
            "() => { const style = getComputedStyle(document.activeElement);"
            " return [style.outlineStyle, parseFloat(style.outlineWidth)]; }"
        )
        assert outline[0] != "none"
        assert outline[1] >= 2
    assert console == []


def test_progress_refresh_keeps_focus_and_announces_once(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, console):
        _log_in(page)
        page.wait_for_selector("#app-shell")
        page.evaluate(_RECORD_STATUS)
        page.click("#tab-batch")
        page.fill("#batch-urls", f"{origin}{SLOW_PATH}")
        _submit(page, "batch")
        job = page.locator("#result section.job")
        job_id = job.get_attribute("data-job")
        cancel = f"job-{job_id}-cancel"
        page.focus(f"[id='{cancel}']")
        for _ in range(2):  # two refreshes while the job runs
            with page.expect_response(lambda response: "/ui/run/progress" in response.url):
                pass
            page.wait_for_timeout(200)  # the swap settles
            assert page.evaluate("document.activeElement.id") == cancel
        expect(job.locator(".state")).to_have_attribute("data-value", "completed")
        page.wait_for_timeout(200)
        texts: list[str] = page.evaluate("window.uiStatusTexts")

    sending = translate("en", "status.sending")
    announced = [text for text in texts if text != sending]
    states = [
        translate("en", "status.job", id=job_id, state=translate("en", f"job_state.{state}"))
        for state in ("queued", "running", "completed")
    ]
    assert texts.count(sending) == 1  # the refresh is no request of the user's
    assert announced[0] == states[0]
    assert announced[-1] == states[-1]
    assert set(announced) <= set(states)
    assert len(announced) == len(set(announced))  # each state once
    assert console == []


def test_busy_status_while_a_request_runs(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, console):
        _log_in(page)
        page.wait_for_selector("#app-shell")
        status = page.locator("#ui-status[role=status][aria-live=polite]")
        page.fill("#extract-url", f"{origin}{SLOW_PATH}")
        _submit(page, "extract")
        expect(status).to_have_text(translate("en", "status.sending"))
        expect(page.locator("#result")).to_have_attribute("aria-busy", "true")
        expect(status).to_have_text(re.compile(r"^Result: success, \d+\.\d\d s$"))
        expect(page.locator("#result")).not_to_have_attribute("aria-busy", "true")
    assert console == []


def test_error_is_associated_with_its_field(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, console):
        _log_in(page)
        page.wait_for_selector("#app-shell")
        options = page.locator("#extract-options")
        hint = options.get_attribute("aria-describedby")
        page.fill("#extract-url", f"{origin}/docs/a.html")
        page.fill("#extract-options", "{")
        _submit(page, "extract")
        error = page.locator("#result [data-error-for=extract-options]")
        expect(error).to_have_attribute("role", "alert")
        expect(options).to_have_attribute("aria-invalid", "true")
        expect(options).to_have_attribute("aria-describedby", f"{hint} extract-options-error")
        assert page.evaluate("document.activeElement.id") == "extract-options"

        page.fill("#extract-options", "")
        _submit(page, "extract")
        expect(page.locator("#result h3.title")).to_have_text("Docs A")
        expect(options).not_to_have_attribute("aria-invalid", "true")
        expect(options).to_have_attribute("aria-describedby", str(hint))
    assert console == []


def test_an_error_that_replaces_its_target_is_not_announced_as_done(
    ui: tuple[str, str],
) -> None:
    """The batch preview's form swaps its answer over the preview itself (outerHTML): a form
    error then is the swapped root, and #ui-status must not say "Done." beside it."""
    url, origin = ui
    csv_file = f"url\n{origin}/docs/a.html\n".encode()
    with browsing(url) as (page, console):
        _log_in(page)
        page.wait_for_selector("#app-shell")
        page.click("#tab-batch-file")
        panel = page.locator("#panel-batch-file")
        panel.locator("input[type=file]").set_input_files(
            {"name": "urls.csv", "mimeType": "text/csv", "buffer": csv_file}
        )
        _submit(page, "batch-file")
        page.wait_for_selector("#result .batch-preview #preview-options")
        page.evaluate(_RECORD_STATUS)
        page.fill("#preview-options", "{")
        page.locator("#preview-form button[type=submit]").click()
        expect(page.locator("#result > [role=alert]")).to_have_count(1)
        page.wait_for_timeout(200)  # the swap settles
        texts: list[str] = page.evaluate("window.uiStatusTexts")
        status = page.locator("#ui-status").text_content()

    assert translate("en", "status.sending") in texts
    assert translate("en", "status.done") not in texts
    assert status == ""
    assert console == []


def test_language_switch_keeps_the_login(ui: tuple[str, str]) -> None:
    url, _ = ui
    with browsing(url) as (page, console):
        _log_in(page)
        page.wait_for_selector("#app-shell")
        page.click(".language-switch a[hreflang=de]")
        page.wait_for_url("**/ui?lang=de")
        page.wait_for_selector("#app-shell")  # logged in again with the stored key
        assert page.evaluate("document.documentElement.lang") == "de"
        assert page.inner_text("#tab-extract") == translate("de", "tab.extract")
        assert page.inner_text("#panel-extract h2") == translate("de", "tab.extract")
    assert console == []
