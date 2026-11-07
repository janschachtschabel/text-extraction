"""Screenshots of the playground's states (package 15).

``python -m tests.ui.screenshots --out DIR`` drives the UI in a real Chromium against a local
site and writes one full-page PNG per state, language and width, named
``<nn>-<state>-<language>-<width>.png``. It is a developer tool for comparing the UI before and
after a redesign, not a test; ``tests/ui/test_screenshots_browser.py`` checks it. It uses only
selectors that outlive a restyling: ids, ``data-value`` and a few stable classes.
"""

import argparse
import contextlib
import hashlib
import re
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Final

import pytest
from fakeredis import FakeAsyncRedis
from playwright.sync_api import Page, sync_playwright
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from tests.ui.test_login_browser import _chromium
from tests.ui.test_playground_browser import PAGES, _page, serving
from text_extraction.settings import ApiKey, Settings
from text_extraction.ui.i18n import LANGUAGES, Language

STATES: Final[tuple[str, ...]] = (
    "login",
    "extract-form",
    "extract-result",
    "extract-result-expanded",
    "extract-text",
    "form-error",
    "batch-progress",
    "batch-file-preview",
    "site-tree",
    "screenshot-custom",
    "info",
)
WIDTHS: Final = {1440: 900, 390: 844}  # viewport width to height
ADMIN_KEY: Final = "admin-key-0123456789-raw-secret"
WAIT_MS: Final = 45_000  # a job's view refreshes every 3 s


# The shot site shows what the site tree can show: the start page links an event page one level
# too deep (its type is guessed from the URL), a page with an id-shaped path and a generic link
# text ("type open", a title from a link), and the sitemap lists two pages that are not crawled.
SHOT_PAGES: Final = {
    **PAGES,
    "/docs/a.html": ("Docs A", ["/veranstaltungen/herbst", "/docs/12345"]),
}
LINK_TEXTS: Final = {"/veranstaltungen/herbst": "Herbsttagung 2026", "/docs/12345": "Mehr"}
SITEMAP: Final = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>{origin}/docs/lernmaterial-mathematik</loc><lastmod>2026-05-01</lastmod></url>"
    "<url><loc>{origin}/category/schule</loc></url></urlset>"
)


def _serve_shot_site(httpserver: HTTPServer) -> str:
    """The local site of the shots (see ``SHOT_PAGES``); its origin."""
    origin = f"http://127.0.0.1:{httpserver.port}"

    def handle(request: Request) -> Response:
        if request.path == "/sitemap.xml":
            return Response(SITEMAP.format(origin=origin), content_type="application/xml")
        found = SHOT_PAGES.get(request.path)
        if found is None:
            return Response("Not found", status=404, content_type="text/plain")
        html = _page(*found).decode()
        for link, text in LINK_TEXTS.items():
            html = html.replace(f'href="{link}">{link}</a>', f'href="{link}">{text}</a>')
        return Response(html, content_type="text/html; charset=utf-8")

    httpserver.expect_request(re.compile(".*")).respond_with_handler(handle)
    return origin


@contextlib.contextmanager
def _scripts_without_flags() -> Iterator[None]:
    """As ``tests/conftest.py`` does outside pytest: fakeredis cannot parse the ``#!lua`` first
    line of the queue scripts, so it is dropped before a script is loaded."""
    original = FakeAsyncRedis.register_script

    def register_script(self: FakeAsyncRedis, script: Any) -> Any:
        text = script.decode() if isinstance(script, bytes) else script
        if text.startswith("#!"):
            text = text.split("\n", 1)[1]
        return original(self, text)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(FakeAsyncRedis, "register_script", register_script)
        yield


def _settings() -> Settings:
    return Settings(
        auth_mode="api_key",
        ui_enabled=True,
        rate_limit_per_minute=10_000,
        fetch_allow_private_networks=True,
        outbound_min_delay_ms=0,
        api_keys=[
            ApiKey(
                name="admin", role="admin", sha256=hashlib.sha256(ADMIN_KEY.encode()).hexdigest()
            )
        ],
    )


def _set(page: Page, selector: str, value: str) -> None:
    """Set a field's value and tell the page; works for a field in a closed section."""
    page.evaluate(
        """([selector, value]) => {
            const field = document.querySelector(selector);
            field.value = value;
            field.dispatchEvent(new Event("input", {bubbles: true}));
        }""",
        [selector, value],
    )


def _choose(page: Page, selector: str) -> None:
    """Check a radio button, also in a closed section."""
    page.evaluate(
        """selector => {
            const field = document.querySelector(selector);
            field.checked = true;
            field.dispatchEvent(new Event("change", {bubbles: true}));
        }""",
        selector,
    )


def _open_all(page: Page, selector: str) -> None:
    page.evaluate(
        "selector => document.querySelectorAll(selector).forEach(d => { d.open = true; })",
        selector,
    )


def _submit(page: Page, button: str, expect: str) -> None:
    """Click ``button`` and wait for ``expect`` in the cleared ``#result``."""
    page.evaluate("document.querySelector('#result').replaceChildren()")
    page.click(button)
    page.wait_for_selector(expect)


def _snap(page: Page, out: Path, number: int, state: str, language: str, width: int) -> Path:
    path = out / f"{number:02d}-{state}-{language}-{width}.png"
    page.screenshot(path=str(path), full_page=True)
    return path


def _run(page: Page, url: str, origin: str, out: Path, language: str, width: int) -> list[Path]:
    """All states in one page of one browser context."""
    paths: list[Path] = []

    def snap(state: str) -> None:
        paths.append(_snap(page, out, STATES.index(state) + 1, state, language, width))

    page.goto(f"{url}?lang={language}")
    page.wait_for_selector("#login-api-key")
    snap("login")
    page.fill("#login-api-key", ADMIN_KEY)
    page.click("#login-form button[type=submit]")
    page.wait_for_selector("#tab-extract")

    _set(page, "#extract-url", f"{origin}/docs/a.html")
    _open_all(page, "#panel-extract details.advanced")
    snap("extract-form")
    _submit(page, "#extract-form button[type=submit]", "#result h3.title")
    snap("extract-result")
    _open_all(page, "#result details")
    snap("extract-result-expanded")
    _choose(page, "#extract-form input[name=format][value=text]")
    _submit(page, "#extract-form button[type=submit]", "#result h3.title")
    snap("extract-text")
    _set(page, "#extract-options", "{not json")
    _submit(page, "#extract-form button[type=submit]", "#result [role=alert]")
    snap("form-error")

    page.click("#tab-batch")
    _set(page, "#batch-urls", f"{origin}/docs/a.html\n{origin}/docs/b.html\n{origin}/about.html")
    _submit(
        page,
        "#batch-form button[type=submit]",
        "#result section.job .state[data-value=completed]",
    )
    snap("batch-progress")

    page.click("#tab-batch-file")
    csv_file = f"url,title\n{origin}/docs/a.html,A\njavascript:alert(1),bad\n".encode()
    page.set_input_files(
        "#panel-batch-file input[type=file]",
        {"name": "urls.csv", "mimeType": "text/csv", "buffer": csv_file},
    )
    _submit(page, "#panel-batch-file button[type=submit]", "#result .batch-preview")
    snap("batch-file-preview")

    page.click("#tab-site-structure")
    _set(page, "#site-url", f"{origin}/")
    _set(page, "#site-depth", "1")
    _submit(
        page,
        "#site-form button[type=submit]",
        "#result section.job .state[data-value=completed]",
    )
    page.click("#result button.tree-button")
    page.wait_for_selector("#result .tree-view")
    page.click("#result [data-action=tree-unfold-all]")  # every row, so the columns show
    snap("site-tree")

    page.click("#tab-screenshot")
    page.select_option("#screenshot-preset", "custom")
    snap("screenshot-custom")

    page.click("#tab-info")
    _submit(page, "#info-form button[type=submit]", "#result :first-child")
    snap("info")
    return paths


def capture(
    out: Path,
    languages: Sequence[Language] = LANGUAGES,
    widths: Sequence[int] = tuple(WIDTHS),
) -> list[Path]:
    """Write one full-page PNG per state, language and width into ``out``; returns the paths in
    the order of language, width and ``STATES``. Serves the UI and a local site for the run."""
    out.mkdir(parents=True, exist_ok=True)
    executable = _chromium()
    written: list[Path] = []
    httpserver = HTTPServer(host="127.0.0.1")
    httpserver.start()
    try:
        origin = _serve_shot_site(httpserver)
        with _scripts_without_flags(), serving(_settings()) as url, sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=executable)
            try:
                for language in languages:
                    for width in widths:
                        context = browser.new_context(
                            viewport={"width": width, "height": WIDTHS[width]}
                        )
                        page = context.new_page()
                        page.set_default_timeout(WAIT_MS)
                        written += _run(page, url, origin, out, language, width)
                        context.close()
            finally:
                browser.close()
    finally:
        httpserver.stop()
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Screenshots of the playground's states.")
    parser.add_argument("--out", type=Path, required=True, help="directory for the PNG files")
    parser.add_argument("--lang", action="append", choices=LANGUAGES, help="repeatable")
    parser.add_argument(
        "--width", action="append", type=int, choices=list(WIDTHS), help="repeatable"
    )
    args = parser.parse_args(argv)
    # The app logs JSON to stdout; stdout is for the paths only.
    with contextlib.redirect_stdout(sys.stderr):
        written = capture(args.out, args.lang or LANGUAGES, args.width or tuple(WIDTHS))
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
