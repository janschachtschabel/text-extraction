"""The result toolbar in a real Chromium (ruling P15-R1, P15-S4; ``pytest -m browser``): "Copy"
puts the shown text on the clipboard and says so in ``#ui-status``, a refused write says that
instead, both "Save" links download their files, and without JavaScript "Copy" stays hidden.
The app runs on 127.0.0.1, a secure context, so the clipboard API exists."""

import contextlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect, sync_playwright

from tests.ui.test_login_browser import _chromium
from tests.ui.test_playground_browser import USER_KEY, WAIT_MS, log_in
from tests.ui.test_playground_browser import ui as ui
from text_extraction.ui.i18n import translate

pytestmark = pytest.mark.browser


@contextlib.contextmanager
def opened(
    url: str, *, clipboard: bool = True, javascript: bool = True
) -> Iterator[tuple[Page, list[str]]]:
    """A Chromium page at ``url`` (with the clipboard permissions when ``clipboard``) and the
    console messages and page errors it collects."""
    executable = _chromium()
    console: list[str] = []
    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=executable)
        context = browser.new_context(java_script_enabled=javascript)
        if clipboard:
            context.grant_permissions(["clipboard-read", "clipboard-write"])
        page = context.new_page()
        page.set_default_timeout(WAIT_MS)
        page.on("console", lambda message: console.append(f"{message.type}: {message.text}"))
        page.on("pageerror", lambda error: console.append(str(error)))
        page.goto(url)
        yield page, console
        browser.close()


def _extract(page: Page, origin: str) -> None:
    """Logs in and extracts ``/docs/a.html`` as Markdown (the form's default)."""
    log_in(page)
    page.wait_for_selector("#app-shell")
    page.fill("#extract-url", f"{origin}/docs/a.html")
    page.click("#panel-extract button[type=submit]")
    expect(page.locator("#result h3.title")).to_have_text("Docs A")


def test_copy_puts_the_text_on_the_clipboard(ui: tuple[str, str]) -> None:
    url, origin = ui
    with opened(url) as (page, console):
        _extract(page, origin)
        copy = page.locator("#result button[data-action=copy]")
        expect(copy).to_be_visible()
        expect(copy).to_have_text(translate("en", "result.copy_markdown"))
        copy.click()
        expect(page.locator("#ui-status")).to_have_text(translate("en", "status.copied"))
        shown = page.locator("#result pre.content").text_content()
        assert shown is not None
        assert "Docs A" in shown
        assert page.evaluate("navigator.clipboard.readText()") == shown
    assert console == []


def test_copy_refused_says_so(ui: tuple[str, str]) -> None:
    url, origin = ui
    with opened(url, clipboard=False) as (page, console):
        _extract(page, origin)
        page.evaluate(
            "() => { navigator.clipboard.writeText = () =>"
            " Promise.reject(new DOMException('refused', 'NotAllowedError')); }"
        )
        page.click("#result button[data-action=copy]")
        expect(page.locator("#ui-status")).to_have_text(translate("en", "status.copy_failed"))
    assert console == []


def test_both_saves_download_files(ui: tuple[str, str]) -> None:
    url, origin = ui
    with opened(url) as (page, console):
        _extract(page, origin)
        with page.expect_download() as text_file:
            page.click("#result a.download")
        assert text_file.value.suggested_filename == "result.md"
        assert "Docs A" in Path(text_file.value.path()).read_text(encoding="utf-8")
        with page.expect_download() as answer:
            page.click("#result a.download-response")
        assert answer.value.suggested_filename == "response.json"
        saved = json.loads(Path(answer.value.path()).read_text(encoding="utf-8"))
        assert (saved["status"], saved["data"]["title"]) == ("success", "Docs A")
    assert console == []


def test_copy_stays_hidden_without_javascript(ui: tuple[str, str]) -> None:
    """A page without JavaScript cannot log in (the login is script), so the fragment is fetched
    as htmx would fetch it and shown as the page's content."""
    url, origin = ui
    with opened(url, javascript=False) as (page, _):
        answer = page.request.post(
            f"{url}/run/extract",
            form={"url": f"{origin}/docs/a.html", "format": "markdown", "mode": "auto"},
            headers={"Authorization": f"Bearer {USER_KEY}", "HX-Request": "true"},
        )
        assert answer.ok
        page.set_content(answer.text())
        expect(page.locator("h3.title")).to_have_text("Docs A")
        expect(page.locator("button[data-action=copy]")).to_have_count(1)
        expect(page.locator("button[data-action=copy]")).to_be_hidden()
