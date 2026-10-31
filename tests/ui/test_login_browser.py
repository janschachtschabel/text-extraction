"""The login of the UI in a real Chromium (ruling P10-S13; ``pytest -m browser``): ui.js keeps
the key in sessionStorage, sends it as a header, logs in again on a reload and forgets it on
"Log out"; a wrong key shows the message and no application; no cookie, no CSP error."""

import contextlib
import glob
import hashlib
import os
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fakeredis import FakeAsyncRedis
from playwright.sync_api import ConsoleMessage, Request, sync_playwright
from prometheus_client import CollectorRegistry

from text_extraction.app import create_public_app
from text_extraction.settings import ApiKey, Settings

pytestmark = pytest.mark.browser

USER_KEY = "user-key-0123456789-raw-secret"
LLM_KEY = "sk-llm-caller-key-ui-7"
_SHELL_GLOB = (
    "/opt/pw-browsers/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"
)


def _chromium() -> str | None:
    """The Chromium to launch: ``BROWSER_EXECUTABLE_PATH``, else Playwright's own, else a headless
    shell of another Playwright release; skips without one."""
    configured = os.environ.get("BROWSER_EXECUTABLE_PATH")
    if configured:
        return configured
    with sync_playwright() as driver:
        if Path(driver.chromium.executable_path).exists():
            return None
    shells = sorted(glob.glob(_SHELL_GLOB))
    if not shells:
        pytest.skip("no Chromium")
    return shells[-1]


@contextlib.contextmanager
def serving(settings: Settings) -> Iterator[str]:
    """The public app on a free port of 127.0.0.1 (lifespan included); yields the /ui URL."""
    app = create_public_app(settings, FakeAsyncRedis(), CollectorRegistry())
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert time.monotonic() < deadline, "the server did not start"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}/ui"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_login_keeps_the_key_in_the_tab_only() -> None:
    executable = _chromium()
    settings = Settings(
        auth_mode="api_key",
        ui_enabled=True,
        rate_limit_per_minute=1000,
        api_keys=[
            ApiKey(name="alice", role="user", sha256=hashlib.sha256(USER_KEY.encode()).hexdigest())
        ],
    )
    console: list[str] = []
    logins: list[dict[str, str]] = []

    def on_request(request: Request) -> None:
        if request.url.endswith("/ui/run/login"):
            logins.append(request.headers)

    with serving(settings) as url, sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=executable)
        context = browser.new_context()
        page = context.new_page()
        page.on("console", lambda message: console.append(_text(message)))
        page.on("pageerror", lambda error: console.append(str(error)))
        page.on("request", on_request)
        page.goto(url)

        page.fill("#login-api-key", "wrong-key-0123456789")
        page.click("#login-form button[type=submit]")
        page.wait_for_selector("text=This key is not valid.")
        assert page.locator("#app-shell").count() == 0
        assert page.evaluate("sessionStorage.getItem('te.apiKey')") is None

        page.fill("#login-api-key", USER_KEY)
        page.click("#login-form summary")
        page.fill("#login-llm-key", LLM_KEY)
        page.click("#login-form button[type=submit]")
        page.wait_for_selector("#app-shell")
        assert "alice" in page.inner_text(".who")
        assert logins[-1]["authorization"] == f"Bearer {USER_KEY}"
        assert logins[-1]["x-llm-api-key"] == LLM_KEY
        assert logins[-1]["hx-request"] == "true"

        page.reload()
        page.wait_for_selector("#app-shell")
        assert len(logins) == 3
        assert context.cookies() == []
        assert USER_KEY not in page.content()

        # The keys go only to /ui/run/ of the page's own origin.
        headers_for = (
            "path => { const detail = {path, headers: {}};"
            " document.dispatchEvent(new CustomEvent('htmx:configRequest', {detail}));"
            " return detail.headers; }"
        )
        for foreign in (
            "https://evil.example/ui/run/extract",
            "//evil.example/ui/run/extract",
            f"{url.replace('127.0.0.1', 'localhost')}/../run/extract",
            "/ui/static/ui.js",
        ):
            assert page.evaluate(headers_for, foreign) == {}, foreign
        own = page.evaluate(headers_for, "/ui/run/extract")
        assert own["Authorization"] == f"Bearer {USER_KEY}"

        page.click("[data-action=logout]")
        page.wait_for_selector("#login-form")
        stored = page.evaluate("Object.keys(sessionStorage)")
        assert [name for name in stored if name.startswith("te.")] == []
        assert page.locator("#app-shell").count() == 0
        browser.close()

    assert console == []


def _text(message: ConsoleMessage) -> str:
    return f"{message.type}: {message.text}"
