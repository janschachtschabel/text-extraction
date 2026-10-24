"""The Chromium pool and the standard renderer, against the local test site."""

import asyncio
import struct
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from playwright.async_api import Browser, BrowserContext
from playwright.async_api import Error as PlaywrightError

import text_extraction.browser_render
from tests.browser.conftest import Stack, open_stack, run_pipeline
from tests.browser.site import (
    ARTICLE_TEXT,
    FORM_TEXT,
    HIDDEN_LEAF,
    HIDDEN_TAB,
    OPTED_OUT,
    SCRIPT_TEXT,
    SHOWN_CHILD,
    SPECULATED,
    VISIBLE_TAB,
    XHR_TEXT,
    Site,
)
from tests.fakes import FakeArtifacts
from text_extraction import __version__
from text_extraction.envelope import ErrorCode
from text_extraction.fetch import FetchError
from text_extraction.render import CookieData, Rendered, RenderOptions
from text_extraction.render import site as site_key

pytestmark = pytest.mark.browser

SettingsFor = Callable[..., Any]


def scenario(
    settings_for: SettingsFor,
    site_: Site,
    body: Callable[[Stack], Awaitable[None]],
    **overrides: Any,
) -> None:
    async def run() -> None:
        async with open_stack(settings_for(**overrides), site_) as stack:
            await body(stack)

    asyncio.run(run())


async def allow(url: str) -> None:
    pass


def deadline_in(seconds: float) -> float:
    return asyncio.get_running_loop().time() + seconds


async def render(
    stack: Stack,
    url: str,
    options: RenderOptions | None = None,
    *,
    check: Callable[[str], Awaitable[None]] = allow,
    seconds: float = 20,
) -> Rendered:
    return await stack.renderer.render(
        url, check, options or RenderOptions(), deadline=deadline_in(seconds)
    )


def png_size(png: bytes) -> tuple[int, int]:
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", png[16:24])
    return width, height


# --- pages ----------------------------------------------------------------------------------


def test_static_page_renders(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        envelope = await run_pipeline(stack, url=f"{site.base}/static")
        assert envelope.status == "success", envelope.error
        assert envelope.data is not None
        assert envelope.data.mode_used == "browser"
        assert envelope.data.upstream_status == 200
        assert ARTICLE_TEXT.split(".")[0] in (envelope.data.content or "")
        assert (
            stack.metric("text_extraction_browser_pages_total", driver="playwright", outcome="ok")
            == 1
        )
        assert stack.metric("text_extraction_browser_pages_in_use", driver="playwright") == 0

    scenario(settings_for, site, body)


def test_script_written_text_is_extracted(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        rendered = await render(stack, f"{site.base}/js")
        assert SCRIPT_TEXT.strip() in rendered.html

    scenario(settings_for, site, body)


def test_xhr_content_is_extracted(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        rendered = await render(stack, f"{site.base}/xhr")
        assert XHR_TEXT.strip() in rendered.html

    scenario(settings_for, site, body)


def test_http_redirect_is_reported_as_main_responses(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        rendered = await render(stack, f"{site.base}/redirect-http")
        assert [(r.url, r.status, r.redirects) for r in rendered.responses] == [
            (f"{site.base}/static", 200, (f"{site.base}/redirect-http",))
        ]
        assert rendered.final_url == f"{site.base}/static"
        assert rendered.responses[0].content_type == "text/html"
        assert rendered.responses[0].charset == "utf-8"
        assert rendered.hosts == {"127.0.0.1"}

    scenario(settings_for, site, body)


@pytest.mark.parametrize("path", ["/redirect-js", "/meta-refresh"])
def test_js_redirect_and_meta_refresh_go_through_check(
    settings_for: SettingsFor, site: Site, path: str
) -> None:
    checked: list[str] = []

    async def check(url: str) -> None:
        checked.append(url)

    async def body(stack: Stack) -> None:
        rendered = await render(stack, f"{site.base}{path}", check=check)
        assert checked == [f"{site.base}/static"]
        assert [r.url for r in rendered.responses] == [f"{site.base}{path}", f"{site.base}/static"]
        assert rendered.final_url == f"{site.base}/static"
        assert ARTICLE_TEXT.split(".")[0] in rendered.html

    scenario(settings_for, site, body)


def test_navigation_refused_by_check_is_never_sent(settings_for: SettingsFor, site: Site) -> None:
    async def check(url: str) -> None:
        raise FetchError(ErrorCode.BLOCKED_BY_ROBOTS, "robots.txt disallows it.")

    async def body(stack: Stack) -> None:
        with pytest.raises(FetchError) as caught:
            await render(stack, f"{site.base}/redirect-js", check=check)
        assert caught.value.code is ErrorCode.BLOCKED_BY_ROBOTS
        assert [s.path for s in site.seen(site.base)] == ["/redirect-js"]

    scenario(settings_for, site, body)


def test_navigation_to_metadata_ip_is_refused(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        envelope = await run_pipeline(stack, url=f"{site.base}/to-private")
        assert envelope.status == "failed"
        assert envelope.error is not None
        assert envelope.error.code is ErrorCode.BLOCKED_URL
        # The proxy refused it before dialing: nothing was sent.
        assert (
            stack.metric("text_extraction_browser_proxy_refused_total", reason="blocked_address")
            >= 1
        )

    scenario(settings_for, site, body)


def test_private_subresources_are_refused_and_page_still_renders(
    settings_for: SettingsFor, site: Site
) -> None:
    async def body(stack: Stack) -> None:
        envelope = await run_pipeline(
            stack, url=f"{site.base}/img-private", screenshot=True, artifacts=FakeArtifacts()
        )
        assert envelope.status == "success", envelope.error
        assert site.seen(site.forbidden) == []
        assert (
            stack.metric("text_extraction_browser_proxy_refused_total", reason="blocked_address")
            >= 2
        )

    scenario(settings_for, site, body)


def test_caller_headers_reach_target_host_only(settings_for: SettingsFor, site: Site) -> None:
    url = f"{site.base}/third-party"
    options = RenderOptions(
        headers={"x-partner-token": "s3cret"},
        header_host=site_key(url),
        cookies=(CookieData(name="pref", value="cookie-value", domain="127.0.0.1"),),
    )

    async def body(stack: Stack) -> None:
        await render(stack, url, options)
        for _ in range(20):  # the third-party fetch runs after load
            if site.seen(site.third, "/echo-headers"):
                break
            await asyncio.sleep(0.1)
        (own,) = site.seen(site.base, "/third-party")
        assert own.headers["x-partner-token"] == "s3cret"
        assert "pref=cookie-value" in own.headers.get("cookie", "")
        (other,) = site.seen(site.third, "/echo-headers")
        assert "x-partner-token" not in other.headers

    scenario(settings_for, site, body)


def test_caller_headers_do_not_follow_a_redirect_to_another_site(
    settings_for: SettingsFor, site: Site
) -> None:
    url = f"{site.base}/redirect-third"
    options = RenderOptions(headers={"x-partner-token": "s3cret"}, header_host=site_key(url))
    checked: list[str] = []

    async def check(target: str) -> None:
        checked.append(target)

    async def body(stack: Stack) -> None:
        rendered = await render(stack, url, options, check=check)
        target = f"{site.third}/echo-headers"
        assert [(r.url, r.status, r.redirects) for r in rendered.responses] == [
            (target, 200, (url,))
        ]
        assert rendered.final_url == target
        assert checked == [target]  # the hop is checked before it is sent
        (own,) = site.seen(site.base, "/redirect-third")
        assert own.headers["x-partner-token"] == "s3cret"
        (other,) = site.seen(site.third, "/echo-headers")
        assert "x-partner-token" not in other.headers

    scenario(settings_for, site, body)


def test_caller_headers_stay_on_a_same_site_redirect(settings_for: SettingsFor, site: Site) -> None:
    url = f"{site.base}/redirect-same"
    options = RenderOptions(headers={"x-partner-token": "s3cret"}, header_host=site_key(url))

    async def body(stack: Stack) -> None:
        rendered = await render(stack, url, options)
        target = f"{site.base}/echo-headers"
        assert [(r.url, r.status, r.redirects) for r in rendered.responses] == [
            (target, 200, (url,))
        ]
        assert "x-partner-token: s3cret" in rendered.html
        assert [s.headers.get("x-partner-token") for s in site.seen(site.base)] == [
            "s3cret",
            "s3cret",
        ]

    scenario(settings_for, site, body)


def test_requests_with_caller_headers_still_pass_the_egress_proxy(
    settings_for: SettingsFor, site: Site
) -> None:
    url = f"{site.forbidden}/static"
    options = RenderOptions(headers={"x-partner-token": "s3cret"}, header_host=site_key(url))

    async def body(stack: Stack) -> None:
        with pytest.raises(FetchError) as caught:
            await render(stack, url, options)
        assert caught.value.code is ErrorCode.BLOCKED_URL
        assert site.seen(site.forbidden) == []

    scenario(settings_for, site, body)


def test_redirect_loop_is_a_target_error(settings_for: SettingsFor, site: Site) -> None:
    url = f"{site.base}/loop"
    with_headers = RenderOptions(headers={"x-partner-token": "s3cret"}, header_host=site_key(url))

    async def body(stack: Stack) -> None:
        for options in (with_headers, RenderOptions()):  # hops of our own, and Chromium's
            with pytest.raises(FetchError) as caught:
                await render(stack, url, options)
            assert caught.value.code is ErrorCode.TARGET_HTTP_ERROR
            assert not caught.value.retryable
        assert len(site.seen(site.base, "/loop")) <= 11 + 21  # ten hops of ours, 20 of Chromium

    scenario(settings_for, site, body)


def test_a_fault_in_the_route_handler_ends_the_render_at_once(
    settings_for: SettingsFor, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"{site.base}/static"
    options = RenderOptions(headers={"x-partner-token": "s3cret"}, header_host=site_key(url))

    def broken(_url: str) -> str:
        raise RuntimeError("a bug")

    async def body(stack: Stack) -> None:
        monkeypatch.setattr(text_extraction.browser_render, "site", broken)
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(FetchError) as caught:
            await render(stack, url, options, seconds=15)
        assert caught.value.code is ErrorCode.INTERNAL_ERROR
        assert caught.value.retryable
        assert loop.time() - started < 5  # not left hanging until the deadline
        assert site.seen(site.base) == []

    scenario(settings_for, site, body)


@pytest.mark.parametrize("path", ["/speculation", "/speculation-header"])
def test_speculated_documents_still_pass_the_check(
    settings_for: SettingsFor, site: Site, path: str
) -> None:
    checked: list[str] = []

    async def check(target: str) -> None:
        checked.append(target)

    async def body(stack: Stack) -> None:
        rendered = await render(stack, f"{site.base}{path}", check=check)
        target = f"{site.base}{SPECULATED}"
        assert checked == [target]
        assert rendered.final_url == target
        assert [s.path for s in site.seen(site.base) if "sec-purpose" in s.headers] == []
        assert site.seen(site.base, "/rules.json") == []

    scenario(settings_for, site, body, browser_idle_wait_seconds=3)


def test_hidden_tabs_are_stripped(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        rendered = await render(stack, f"{site.base}/hidden-tabs")
        assert VISIBLE_TAB in rendered.html
        assert SHOWN_CHILD in rendered.html
        assert HIDDEN_TAB not in rendered.html
        assert HIDDEN_LEAF not in rendered.html
        assert "<style>" in rendered.html  # the rules that hide them stay

    scenario(settings_for, site, body)


def test_consent_banner_is_opted_out(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        rendered = await render(stack, f"{site.base}/consent")
        assert f'<p id="state">{OPTED_OUT}</p>' in rendered.html
        assert "cookiebanner" not in rendered.html

    scenario(settings_for, site, body)


def test_consent_is_skipped_when_switched_off(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        rendered = await render(stack, f"{site.base}/consent")
        assert '<p id="state"></p>' in rendered.html
        assert "cookiebanner" in rendered.html

    scenario(settings_for, site, body, browser_autoconsent=False)


def test_full_page_screenshot_is_1920_wide_and_clipped(
    settings_for: SettingsFor, site: Site
) -> None:
    async def body(stack: Stack) -> None:
        rendered = await render(stack, f"{site.base}/tall", RenderOptions(screenshot=True))
        assert rendered.screenshot is not None
        assert png_size(rendered.screenshot) == (1920, 16384)
        assert rendered.screenshot_size == (1920, 16384)
        assert "screenshot_clipped" in rendered.warnings

        short = await render(stack, f"{site.base}/static", RenderOptions(screenshot=True))
        assert short.screenshot is not None
        assert png_size(short.screenshot) == (1920, 1080)
        assert "screenshot_clipped" not in short.warnings

        viewport = await render(
            stack, f"{site.base}/tall", RenderOptions(screenshot=True, full_page=False)
        )
        assert viewport.screenshot is not None
        assert png_size(viewport.screenshot) == (1920, 1080)
        assert "screenshot_clipped" not in viewport.warnings

    scenario(settings_for, site, body)


def test_never_idle_page_finishes_within_deadline(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        rendered = await render(stack, f"{site.base}/never-idle", seconds=6)
        assert loop.time() - started < 6
        assert ARTICLE_TEXT.split(".")[0] in rendered.html

    scenario(settings_for, site, body, browser_idle_wait_seconds=30)


def test_render_that_cannot_load_in_time_is_a_retryable_timeout(
    settings_for: SettingsFor, site: Site
) -> None:
    async def body(stack: Stack) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(FetchError) as caught:
            await render(stack, f"{site.base}/poll", seconds=1.5)
        assert caught.value.code is ErrorCode.TIMEOUT
        assert caught.value.retryable
        assert loop.time() - started < 2.5
        assert (
            stack.metric(
                "text_extraction_browser_pages_total", driver="playwright", outcome="timeout"
            )
            == 1
        )

    scenario(settings_for, site, body)


def test_form_post_navigation_is_refused(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        rendered = await render(stack, f"{site.base}/form-post")
        assert "navigation_refused_non_get" in rendered.warnings
        assert FORM_TEXT in rendered.html
        assert [s.method for s in site.seen(site.base)] == ["GET"]

    scenario(settings_for, site, body)


def test_download_is_reported_as_non_html(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        rendered = await render(stack, f"{site.base}/download.pdf")
        assert [(r.url, r.content_type) for r in rendered.responses] == [
            (f"{site.base}/download.pdf", "application/pdf")
        ]
        envelope = await run_pipeline(stack, url=f"{site.base}/download.pdf")
        assert envelope.error is not None
        assert envelope.error.code is ErrorCode.UNSUPPORTED_CONTENT_TYPE

    scenario(settings_for, site, body)


def test_images_blocked_for_extraction_not_for_screenshot(
    settings_for: SettingsFor, site: Site
) -> None:
    async def body(stack: Stack) -> None:
        extraction = await run_pipeline(stack, url=f"{site.base}/image")
        assert extraction.status == "success", extraction.error
        assert site.seen(site.base, "/pixel.png") == []

        artifacts = FakeArtifacts()
        shot = await run_pipeline(
            stack, url=f"{site.base}/image", screenshot=True, artifacts=artifacts
        )
        assert shot.status == "success", shot.error
        assert len(site.seen(site.base, "/pixel.png")) == 1
        assert len(artifacts.screenshots) == 1

    scenario(settings_for, site, body)


def test_cookies_of_the_target_host_are_returned(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        rendered = await render(stack, f"{site.base}/set-cookie")
        assert [(c.name, c.value, c.domain, c.http_only) for c in rendered.cookies] == [
            ("session", "abc123", "127.0.0.1", True)
        ]

    scenario(settings_for, site, body)


def test_byte_cap_ends_the_page_with_a_warning(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        rendered = await render(stack, f"{site.base}/heavy")
        assert "browser_bytes_capped" not in rendered.warnings
        stack.proxy._max_bytes = 50_000  # the setting's floor is far above the test pages
        capped = await render(stack, f"{site.base}/heavy")
        assert "browser_bytes_capped" in capped.warnings
        assert ARTICLE_TEXT.split(".")[0] in capped.html

    scenario(settings_for, site, body)


# --- the pool -------------------------------------------------------------------------------


def test_browser_crash_fails_page_and_pool_relaunches(
    settings_for: SettingsFor, site: Site
) -> None:
    async def body(stack: Stack) -> None:
        (first,) = stack.launcher.browsers

        async def crash_soon() -> None:
            for _ in range(200):
                if site.seen(site.base, "/poll"):
                    break
                await asyncio.sleep(0.05)
            await first.close()

        crasher = asyncio.create_task(crash_soon())
        with pytest.raises(FetchError) as caught:
            await render(stack, f"{site.base}/never-idle")
        await crasher
        assert caught.value.code is ErrorCode.INTERNAL_ERROR
        assert caught.value.retryable
        assert (
            stack.metric(
                "text_extraction_browser_pages_total", driver="playwright", outcome="crash"
            )
            == 1
        )

        rendered = await render(stack, f"{site.base}/static")
        assert ARTICLE_TEXT.split(".")[0] in rendered.html
        assert len(stack.launcher.browsers) == 2
        assert (
            stack.metric(
                "text_extraction_browser_launches_total", driver="playwright", reason="crash"
            )
            == 1
        )

    scenario(settings_for, site, body, browser_idle_wait_seconds=10)


def test_launch_given_up_by_its_render_leaves_no_browser(
    settings_for: SettingsFor, site: Site
) -> None:
    async def body(stack: Stack) -> None:
        (first,) = stack.launcher.browsers
        await first.close()  # the next page relaunches
        stack.launcher.launch_delay = 1.0
        with pytest.raises(FetchError) as caught:
            await render(stack, f"{site.base}/static", seconds=1.0)
        assert caught.value.code is ErrorCode.TIMEOUT
        for _ in range(100):  # the launch finishes after the render gave up
            if len(stack.launcher.browsers) == 2 and not stack.launcher.browsers[1].is_connected():
                break
            await asyncio.sleep(0.05)
        _, abandoned = stack.launcher.browsers
        assert not abandoned.is_connected()

        stack.launcher.launch_delay = 0.0
        rendered = await render(stack, f"{site.base}/static")
        assert ARTICLE_TEXT.split(".")[0] in rendered.html

    scenario(settings_for, site, body)


def test_a_launch_whose_context_fails_closes_its_browser(
    settings_for: SettingsFor, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing(self: Browser, **kwargs: Any) -> BrowserContext:
        raise PlaywrightError("no context")

    async def body(stack: Stack) -> None:
        (first,) = stack.launcher.browsers
        await first.close()  # the next page relaunches
        with monkeypatch.context() as patch:
            patch.setattr(Browser, "new_context", failing)
            with pytest.raises(FetchError) as caught:
                await render(stack, f"{site.base}/static")
        assert caught.value.code is ErrorCode.INTERNAL_ERROR
        _, failed = stack.launcher.browsers
        assert not failed.is_connected()

        rendered = await render(stack, f"{site.base}/static")
        assert ARTICLE_TEXT.split(".")[0] in rendered.html

    scenario(settings_for, site, body)


def test_a_hanging_close_does_not_hold_the_render_past_its_deadline(
    settings_for: SettingsFor, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    close = BrowserContext.close

    async def hanging(self: BrowserContext, **kwargs: Any) -> None:
        await asyncio.sleep(30)

    async def body(stack: Stack) -> None:
        loop = asyncio.get_running_loop()
        with monkeypatch.context() as patch:
            patch.setattr(BrowserContext, "close", hanging)
            started = loop.time()
            rendered = await render(stack, f"{site.base}/static", seconds=3)
            assert loop.time() - started < 3  # the close waits a moment, then goes on alone
        assert ARTICLE_TEXT.split(".")[0] in rendered.html
        assert BrowserContext.close is close

    scenario(settings_for, site, body)


def test_recycle_launches_new_browser(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        for _ in range(3):
            await render(stack, f"{site.base}/static")
        first, second = stack.launcher.browsers
        assert not first.is_connected()
        assert second.is_connected()
        assert (
            stack.metric(
                "text_extraction_browser_launches_total", driver="playwright", reason="start"
            )
            == 1
        )
        assert (
            stack.metric(
                "text_extraction_browser_launches_total", driver="playwright", reason="recycle"
            )
            == 1
        )

    scenario(settings_for, site, body, browser_recycle_after_pages=2)


def test_concurrent_pages_are_capped(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        peak = 0

        async def watch() -> None:
            nonlocal peak
            while True:
                peak = max(
                    peak,
                    int(stack.metric("text_extraction_browser_pages_in_use", driver="playwright")),
                )
                await asyncio.sleep(0.01)

        watcher = asyncio.create_task(watch())
        try:
            await asyncio.gather(*(render(stack, f"{site.base}/static") for _ in range(4)))
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        assert peak == 2

    scenario(settings_for, site, body, browser_concurrency=2)


def test_user_agent_is_honest(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        await render(stack, f"{site.base}/echo-headers")
        (seen,) = site.seen(site.base, "/echo-headers")
        agent = seen.headers["user-agent"]
        assert agent == stack.pool.user_agent == stack.renderer.user_agent
        assert agent.startswith("Mozilla/5.0 ")
        assert "Chrome/" in agent
        assert agent.endswith(
            f" text-extraction/{__version__} (+https://github.com/janschachtschabel/text-extraction)"
        )

    scenario(settings_for, site, body)
