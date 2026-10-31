"""The playground in a real Chromium (rulings P10-S13 to P10-S20; ``pytest -m browser``): the
public app on a free port in a thread, on one fakeredis server with an extract worker thread,
and a small site served by pytest-httpserver on 127.0.0.1. It logs in and extracts a page,
reads a site structure into the tree and extracts a selection of it, and starts a batch from a
CSV file, each until its progress is complete. No cookie and no console message (so no CSP
violation) on the way."""

import contextlib
import hashlib
import re
import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from fakeredis import FakeAsyncRedis, FakeServer
from playwright.sync_api import Page, expect, sync_playwright
from prometheus_client import CollectorRegistry
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from tests.site.test_done_when import TEXT, ExtractWorker
from tests.ui.test_login_browser import _chromium
from text_extraction.app import create_public_app
from text_extraction.settings import ApiKey, Settings

pytestmark = pytest.mark.browser
expect.set_options(timeout=45_000)  # a job's view refreshes every 3 s

USER_KEY = "user-key-0123456789-raw-secret"
WAIT_MS = 45_000
# The site: a start page that links three pages, two of them under /docs/.
PAGES = {
    "/": ("Start page", ["/docs/a.html", "/docs/b.html", "/about.html"]),
    "/docs/a.html": ("Docs A", []),
    "/docs/b.html": ("Docs B", []),
    "/about.html": ("About us", []),
}


def _page(title: str, links: list[str]) -> bytes:
    anchors = "".join(f'<li><a href="{link}">{link}</a></li>' for link in links)
    return (
        f"<!doctype html><html lang='de'><head><title>{title}</title></head><body><main>"
        f"<h1>{title}</h1><p>{TEXT}</p><ul>{anchors}</ul></main></body></html>"
    ).encode()


def serve_site(httpserver: HTTPServer) -> str:
    def handle(request: Request) -> Response:
        found = PAGES.get(request.path)
        if found is None:
            return Response("Not found", status=404, content_type="text/plain")
        return Response(_page(*found), content_type="text/html; charset=utf-8")

    httpserver.expect_request(re.compile(".*")).respond_with_handler(handle)
    return httpserver.url_for("/").rstrip("/")


@contextlib.contextmanager
def serving(settings: Settings) -> Iterator[str]:
    """The public app (lifespan included) on a free port of 127.0.0.1 and an extract worker,
    both on one fakeredis server; yields the /ui URL."""
    server = FakeServer()
    app = create_public_app(settings, FakeAsyncRedis(server=server), CollectorRegistry())
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not uvicorn_server.started:
        assert time.monotonic() < deadline, "the server did not start"
        time.sleep(0.05)
    try:
        with ExtractWorker(settings, server):
            yield f"http://127.0.0.1:{port}/ui"
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def ui(httpserver: HTTPServer) -> Iterator[tuple[str, str]]:
    """The /ui URL and the site's origin."""
    origin = serve_site(httpserver)
    settings = Settings(
        auth_mode="api_key",
        ui_enabled=True,
        rate_limit_per_minute=10_000,
        fetch_allow_private_networks=True,
        outbound_min_delay_ms=0,
        api_keys=[
            ApiKey(name="alice", role="user", sha256=hashlib.sha256(USER_KEY.encode()).hexdigest())
        ],
    )
    with serving(settings) as url:
        yield url, origin


@contextlib.contextmanager
def browsing(url: str) -> Iterator[tuple[Page, list[str]]]:
    """A Chromium page at ``url`` and the console messages and page errors it collects."""
    executable = _chromium()
    console: list[str] = []
    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=executable)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(WAIT_MS)
        page.on("console", lambda message: console.append(f"{message.type}: {message.text}"))
        page.on("pageerror", lambda error: console.append(str(error)))
        page.goto(url)
        yield page, console
        assert context.cookies() == []
        browser.close()


def log_in(page: Page, key: str = USER_KEY) -> None:
    page.fill("#login-api-key", key)
    page.click("#login-form button[type=submit]")


def open_tab(page: Page, name: str) -> None:
    page.click(f"[data-tab={name}]")


def test_login_and_extract(ui: tuple[str, str]) -> None:
    url, origin = ui
    with browsing(url) as (page, console):
        log_in(page, "wrong-key-0123456789")
        page.wait_for_selector("text=This key is not valid.")
        assert page.locator("#app-shell").count() == 0
        assert page.locator("form.op").count() == 0

        log_in(page)
        page.wait_for_selector("#app-shell")
        assert page.locator("[role=tablist] [data-tab]").count() == 9

        panel = page.locator("#panel-extract")
        panel.locator("input[name=url]").fill(f"{origin}/docs/a.html")
        panel.locator("input[name=format][value=text]").check()
        panel.locator("button[type=submit]").click()
        expect(page.locator("#result h3.title")).to_have_text("Docs A")
        expect(page.locator("#result a.download")).to_have_attribute("download", "result.txt")

        page.reload()
        page.wait_for_selector("#app-shell")
    assert console == []


def test_tree_select_to_batch(ui: tuple[str, str]) -> None:
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
        expect(view.locator("input[name=url]")).to_have_count(4)

        root = view.locator("ul.tree > li > label > input")
        label = page.locator(":scope > label > span.label", has_text=re.compile("^docs/$"))
        docs = view.locator("li.node-section", has=label)
        docs.locator(":scope > label > input").check()
        docs_pages = docs.locator("input[name=url]")
        assert [docs_pages.nth(n).is_checked() for n in range(docs_pages.count())] == [True] * 2
        assert root.evaluate("box => box.indeterminate") is True
        assert not root.is_checked()
        expect(view.locator(".tree-counter")).to_have_text("2 selected")
        view.locator("[data-action=tree-all]").click()
        expect(view.locator(".tree-counter")).to_have_text("4 selected")
        assert root.evaluate("box => box.indeterminate") is False
        assert root.is_checked()
        view.locator("[data-action=tree-none]").click()
        expect(view.locator(".tree-counter")).to_have_text("0 selected")
        docs.locator(":scope > label > input").check()
        docs_pages.first.uncheck()
        expect(view.locator(".tree-counter")).to_have_text("1 selected")
        assert docs.locator(":scope > label > input").evaluate("box => box.indeterminate")

        docs_pages.first.check()
        view.locator("button[type=submit]", has_text="Extract selected").click()
        started = view.locator(".tree-jobs section.job")
        expect(started.locator(".state")).to_have_text("completed")
        expect(started.locator(".done")).to_have_text("2/2")
        expect(started.locator(".success")).to_have_text("2")
    assert console == []


def test_csv_upload_to_progress(ui: tuple[str, str]) -> None:
    url, origin = ui
    csv_file = (
        f"url,title\n{origin}/docs/a.html,A\n{origin}/docs/b.html,B\n"
        f"javascript:alert(1),bad\n{origin}/about.html,About\n"
    ).encode()
    with browsing(url) as (page, console):
        log_in(page)
        page.wait_for_selector("#app-shell")
        open_tab(page, "batch-file")
        panel = page.locator("#panel-batch-file")
        panel.locator("input[type=file]").set_input_files(
            {"name": "urls.csv", "mimeType": "text/csv", "buffer": csv_file}
        )
        panel.locator("button[type=submit]").click()

        preview = page.locator("#result .batch-preview")
        expect(preview.locator(".problems li")).to_have_text(
            ["Row 4: URL scheme must be http or https"]
        )
        expect(preview.locator("dd.urls")).to_have_text("3")
        preview.locator("button[type=submit]", has_text="Start batch").click()

        job = page.locator("#result section.job")
        expect(job.locator(".state")).to_have_text("completed")
        expect(job.locator("progress")).to_have_attribute("value", "3")
        expect(job.locator(".done")).to_have_text("3/3")
    assert console == []


def test_an_answer_htmx_does_not_swap_shows_a_status_line(ui_small: str) -> None:
    """A file over the upload route's limit is answered 413 (JSON) before the UI's view runs, so
    htmx does not swap it; ui.js writes "Request failed: <status> <reason>" into the target
    instead, the reason being the text of the error's code, with the API's English message
    below it, and "Network error" when no answer comes (rulings R-P10-F3, P12-S9). Only text: a
    status line holds no markup of the answer's."""
    with browsing(ui_small) as (page, console):
        log_in(page)
        page.wait_for_selector("#app-shell")
        open_tab(page, "file")
        panel = page.locator("#panel-file")
        panel.locator("input[type=file]").set_input_files(
            {"name": "big.txt", "mimeType": "text/plain", "buffer": b"<b>x</b>" * 50_000}
        )
        panel.locator("button[type=submit]").click()
        status = page.locator("#result > .request-status[role=alert]")
        line = status.locator(":scope > p")
        expect(line).to_have_text("Request failed: 413 The request is not valid.")
        detail = status.locator(".api-detail .error-message[lang=en]")
        expect(detail).to_have_text(
            "The request body is too large: at most 206608 bytes are accepted."
        )
        expect(status.locator(".api-detail-label")).to_have_text("Details from the API (English)")
        assert detail.evaluate("line => line.children.length") == 0

        page.route("**/ui/run/extract", lambda route: route.abort())
        open_tab(page, "extract")
        extract = page.locator("#panel-extract")
        extract.locator("input[name=url]").fill("https://example.org/")
        extract.locator("button[type=submit]").click()
        expect(line).to_have_text("Network error")
        expect(status.locator(".api-detail")).to_have_count(0)
        expect(page.locator("#result > .request-status")).to_have_count(1)
    # Chromium reports the failed loads, htmx the error events; nothing else (no CSP violation).
    assert not [line for line in console if "Content Security Policy" in line or "Refused" in line]


@pytest.fixture
def ui_small() -> Iterator[str]:
    """The /ui URL of a service whose upload route takes at most 10,000 bytes of a file."""
    settings = Settings(
        auth_mode="api_key",
        ui_enabled=True,
        rate_limit_per_minute=10_000,
        outbound_min_delay_ms=0,
        document_max_bytes=10_000,
        api_keys=[
            ApiKey(name="alice", role="user", sha256=hashlib.sha256(USER_KEY.encode()).hexdigest())
        ],
    )
    with serving(settings) as url:
        yield url
