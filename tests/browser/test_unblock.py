"""The unblock renderer (B14): Patchright against the local test site.

The tests run Patchright's Chromium (``channel="chromium"``) headless, so CI needs no Google
Chrome; one test runs it headful under Xvfb where Xvfb is installed. Without Patchright's
Chromium (or a full Chromium under ``/opt/pw-browsers``) they are skipped.
"""

import asyncio
import glob
import os
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import pytest
from fakeredis import FakeAsyncRedis
from patchright.async_api import Error as PatchrightError
from patchright.async_api import async_playwright as async_patchright
from playwright.async_api import BrowserContext
from prometheus_client import CollectorRegistry
from structlog.testing import capture_logs

from tests.browser.conftest import SiteProxy
from tests.browser.site import (
    ARTICLE_TEXT,
    COOKIE_DIALOG_TEXT,
    OPTED_OUT,
    OVERLAY_TEXT,
    PAGE_GLOBAL,
    SPECULATED,
    Site,
)
from tests.fakes import FakeFetcher
from text_extraction.auth import Principal
from text_extraction.browser import BrowserPool, ChromiumRenderer, PageProxy, PlaywrightLauncher
from text_extraction.browser_options import EvaluateAction
from text_extraction.browser_unblock import PatchrightLauncher, UnblockRenderer, Xvfb
from text_extraction.clearance import ValkeyClearance
from text_extraction.compliance import ComplianceChecker
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.extract_models import ExtractRequest, ExtractResult
from text_extraction.fetch import Fetcher, FetchError
from text_extraction.fingerprint import FingerprintStore
from text_extraction.outbound import DomainGate
from text_extraction.pipeline import Pipeline
from text_extraction.render import CookieData, Rendered, RenderOptions, contacted_challenge_host
from text_extraction.render import site as site_key
from text_extraction.result_cache import ResultCache
from text_extraction.settings import Settings

pytestmark = pytest.mark.browser

ADMIN = Principal(name="root", role="admin", rate_limit_per_minute=None)
_FULL_GLOB = "/opt/pw-browsers/chromium-*/chrome-linux*/chrome"
_SHELL_GLOB = (
    "/opt/pw-browsers/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"
)
CHALLENGE_SECONDS = 10.0
# Host names the test proxy dials as 127.0.0.1: a site with a parent domain for cookies.
ALIASED = ("example.test", "www.example.test", "learn.example.test")


async def _patchright_own_chromium_works() -> bool:
    try:
        async with async_patchright() as driver:
            # A launch that fails leaves its /tmp/playwright-artifacts-* dir: look first.
            if not await asyncio.to_thread(Path(driver.chromium.executable_path).exists):
                return False
            browser = await driver.chromium.launch(channel="chromium")
            await browser.close()
    except Exception:
        return False
    return True


@pytest.fixture(scope="session")
def patchright_chromium() -> Path | None:
    """The Chromium Patchright launches: None for its own (``patchright install chromium``),
    else a full Chromium, else a headless shell under ``/opt/pw-browsers``; skips without one."""
    if asyncio.run(_patchright_own_chromium_works()):
        return None
    for pattern in (_FULL_GLOB, _SHELL_GLOB):
        found = sorted(glob.glob(pattern))
        if found:
            return Path(found[-1])
    pytest.skip("no Chromium for Patchright: run `patchright install chromium`")


class RecordingPatchright(PatchrightLauncher):
    """The Patchright launcher, noting each context (each its own browser) it opens."""

    def __init__(self, settings: Settings, **kwargs: Any) -> None:
        super().__init__(settings, **kwargs)
        self.contexts: list[BrowserContext] = []
        self.fail_next_launch = False

    async def _launch(self, profile: Path, proxy: PageProxy) -> BrowserContext:
        if self.fail_next_launch:
            self.fail_next_launch = False
            raise PatchrightError("the launch failed")
        return await super()._launch(profile, proxy)

    async def open(
        self, browser: Any, proxy: PageProxy, options: RenderOptions
    ) -> tuple[Any, BrowserContext]:
        found = await super().open(browser, proxy, options)
        self.contexts.append(found[1])
        return found


@dataclass
class UnblockStack:
    settings: Settings
    registry: CollectorRegistry
    launcher: RecordingPatchright
    pool: BrowserPool
    renderer: UnblockRenderer
    profiles: Path
    standard: ChromiumRenderer | None = None

    def metric(self, name: str, **labels: str) -> float:
        return self.registry.get_sample_value(name, labels) or 0.0


@pytest.fixture
def unblock_settings(patchright_chromium: Path | None) -> Callable[..., Settings]:
    def make(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "auth_mode": "none",
            "outbound_min_delay_ms": 0,
            "browser_enabled": True,
            "browser_unblock_enabled": True,
            "browser_unblock_channel": "chromium",
            "browser_unblock_headless": True,
            "browser_unblock_challenge_wait_seconds": CHALLENGE_SECONDS,
            "fetch_allow_private_networks": True,  # R9, as in conftest.py
            "browser_idle_wait_seconds": 1,
        }
        values.update(overrides)
        return Settings(**values)

    return make


@asynccontextmanager
async def open_unblock_stack(
    settings: Settings,
    site: Site,
    executable: Path | None,
    profiles: Path,
    *,
    checkbox_frame: Callable[[str], bool] | None = None,
    standard: bool = False,
) -> AsyncIterator[UnblockStack]:
    registry = CollectorRegistry()
    ports = {urlsplit(site.base).port or 0, urlsplit(site.third).port or 0}
    aliases = {name: "127.0.0.1" for name in ALIASED}
    proxy = SiteProxy(settings, ports, registry, aliases=aliases)
    await proxy.start()
    slots = asyncio.Semaphore(settings.browser_concurrency)
    launcher = RecordingPatchright(settings, executable_path=executable, profile_root=profiles)
    pool = BrowserPool(settings, proxy, registry, launcher=launcher, slots=slots)
    standard_pool = (
        BrowserPool(settings, proxy, registry, launcher=PlaywrightLauncher(settings), slots=slots)
        if standard
        else None
    )
    widget = checkbox_frame or (lambda url: url.startswith(f"{site.third}/turnstile"))
    try:
        await pool.start()
        if standard_pool is not None:
            await standard_pool.start()
        yield UnblockStack(
            settings,
            registry,
            launcher,
            pool,
            UnblockRenderer(pool, settings, checkbox_frame=widget),
            profiles,
            ChromiumRenderer(standard_pool, settings) if standard_pool is not None else None,
        )
    finally:
        await pool.stop()
        if standard_pool is not None:
            await standard_pool.stop()
        await proxy.stop()


@pytest.fixture
def run_unblock(
    unblock_settings: Callable[..., Settings],
    site: Site,
    patchright_chromium: Path | None,
    tmp_path: Path,
) -> Callable[..., None]:
    """Runs ``body(stack)`` with a launched unblock stack (settings ``overrides``)."""

    def run(
        body: Callable[[UnblockStack], Awaitable[None]],
        *,
        standard: bool = False,
        **overrides: Any,
    ) -> None:
        async def main() -> None:
            async with open_unblock_stack(
                unblock_settings(**overrides),
                site,
                patchright_chromium,
                tmp_path,
                standard=standard,
            ) as stack:
                await body(stack)

        asyncio.run(main())

    return run


async def allow(url: str) -> None:
    pass


def deadline_in(seconds: float) -> float:
    return asyncio.get_running_loop().time() + seconds


async def render(
    stack: UnblockStack,
    url: str,
    options: RenderOptions | None = None,
    *,
    check: Callable[[str], Awaitable[None]] = allow,
    seconds: float = 30,
) -> Rendered:
    return await stack.renderer.render(
        url, check, options or RenderOptions(unblock=True), deadline=deadline_in(seconds)
    )


def pipeline_for(stack: UnblockStack, valkey: FakeAsyncRedis) -> Pipeline:
    """The real pipeline with the unblock renderer, everything (gate, clearance) in ``valkey``."""
    fetcher = cast(Fetcher, FakeFetcher({}))
    standard = stack.standard or stack.renderer
    return Pipeline(
        fetcher,
        DomainGate(valkey, stack.settings),
        ComplianceChecker(fetcher, valkey, stack.settings),
        ResultCache(valkey, stack.settings),
        FingerprintStore(valkey, stack.settings),
        stack.settings,
        stack.registry,
        renderer=standard,
        unblock_renderer=stack.renderer,
        clearance=ValkeyClearance(valkey, stack.settings),
        timeout_seconds=stack.settings.browser_timeout_seconds,
    )


async def run_admin(
    stack: UnblockStack, valkey: FakeAsyncRedis | None = None, **request: Any
) -> Envelope[ExtractResult]:
    request.setdefault("mode", "browser")
    request.setdefault("compliance", "ignore")
    pipeline = pipeline_for(stack, valkey or FakeAsyncRedis())
    return await pipeline.run(ExtractRequest(**request), ADMIN)


def kill_browser_of(profiles: Path) -> None:
    """Kill the main process of the browser running on a profile under ``profiles``."""
    killed = 0
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            args = cmdline.read_bytes().split(b"\0")
        except OSError:
            continue
        uses_profile = any(arg.startswith(f"--user-data-dir={profiles}".encode()) for arg in args)
        if uses_profile and not any(arg.startswith(b"--type=") for arg in args):
            os.kill(int(cmdline.parent.name), signal.SIGKILL)
            killed += 1
    assert killed == 1


async def wait_until(condition: Callable[[], bool], seconds: float = 5.0) -> bool:
    for _ in range(int(seconds / 0.05)):
        if condition():
            return True
        await asyncio.sleep(0.05)
    return condition()


# --- challenges -------------------------------------------------------------------------------


def test_challenge_page_clears_within_wait(run_unblock: Any, site: Site) -> None:
    async def body(stack: UnblockStack) -> None:
        rendered = await render(stack, f"{site.base}/challenge")
        assert "<title>Cleared</title>" in rendered.html
        assert ARTICLE_TEXT.split(".")[0] in rendered.html
        assert "challenge_not_cleared" not in rendered.warnings
        assert "challenge_checkbox_clicked" not in rendered.warnings
        assert [answer.status for answer in rendered.responses] == [503, 200]
        assert [cookie.name for cookie in rendered.cookies] == ["cleared"]

    run_unblock(body)


def test_challenge_not_cleared_adds_warning(run_unblock: Any, site: Site) -> None:
    async def body(stack: UnblockStack) -> None:
        rendered = await render(stack, f"{site.base}/challenge")
        assert "challenge_not_cleared" in rendered.warnings

        envelope = await run_admin(stack, url=f"{site.base}/checkbox?stubborn=1")
        assert envelope.status == "failed"
        assert envelope.error is not None
        assert envelope.error.code is ErrorCode.BOT_PROTECTION  # though the answer was a 403
        assert envelope.data is not None
        assert envelope.data.unblocked
        assert "challenge_not_cleared" in envelope.data.warnings

    run_unblock(body, browser_unblock_challenge_wait_seconds=0)


def test_checkbox_challenge_is_clicked_once_and_clears(run_unblock: Any, site: Site) -> None:
    async def body(stack: UnblockStack) -> None:
        with capture_logs() as logs:
            rendered = await render(stack, f"{site.base}/checkbox")
        assert "<title>Cleared</title>" in rendered.html
        assert rendered.warnings.count("challenge_checkbox_clicked") == 1
        assert "challenge_not_cleared" not in rendered.warnings
        assert len(site.seen(site.third, "/turnstile-clicked")) == 1
        assert "cf_clearance" in [cookie.name for cookie in rendered.cookies]
        clicked = [log for log in logs if log["event"] == "challenge_checkbox_clicked"]
        assert len(clicked) == 1
        assert "turnstile" not in repr(logs)  # hosts only, never a URL or path

    run_unblock(body)


def test_checkbox_is_clicked_at_most_twice(run_unblock: Any, site: Site) -> None:
    async def body(stack: UnblockStack) -> None:
        rendered = await render(stack, f"{site.base}/checkbox?stubborn=1")
        assert rendered.warnings.count("challenge_checkbox_clicked") == 1  # warnings are unique
        assert "challenge_not_cleared" in rendered.warnings
        assert len(site.seen(site.third, "/turnstile-clicked")) == 2

    # Long enough for a third click, were there no cap (clicks come 3 s apart).
    run_unblock(body, browser_unblock_challenge_wait_seconds=15)


def test_clearance_is_reused_by_the_next_render(run_unblock: Any, site: Site) -> None:
    async def body(stack: UnblockStack) -> None:
        valkey = FakeAsyncRedis()
        first = await run_admin(stack, valkey, url=f"{site.base}/challenge", cache=False)
        assert first.status == "success", first.error
        assert first.data is not None
        assert first.data.unblocked
        # Stored for the host and never shown to the caller.
        assert await valkey.exists("unblock:cookies:{127.0.0.1}") == 1
        assert "cleared" not in first.data.model_dump_json()

        # The challenge's 503 did not pause the host: it cleared (only the 200 was reported).
        assert [key async for key in valkey.scan_iter("out:cooldown:*")] == []
        site.clear()
        stack.settings.browser_unblock_challenge_wait_seconds = 0
        second = await run_admin(stack, valkey, url=f"{site.base}/challenge", cache=False)
        assert second.status == "success", second.error
        (request,) = site.seen(site.base, "/challenge")
        assert "cleared=1" in request.headers.get("cookie", "")

    run_unblock(body)


def test_domain_clearance_cookies_reach_the_subdomains(run_unblock: Any, site: Site) -> None:
    port = urlsplit(site.base).port

    async def body(stack: UnblockStack) -> None:
        first = await render(stack, f"http://www.example.test:{port}/set-domain-cookie")
        (cookie,) = [cookie for cookie in first.cookies if cookie.name == "cf_clearance"]
        assert cookie.domain == ".example.test"

        # Through the clearance store and back, as the next unblock render gets them.
        valkey = FakeAsyncRedis()
        clearance = ValkeyClearance(valkey, stack.settings)
        await clearance.put("www.example.test", list(first.cookies))
        reused = tuple(await clearance.get("www.example.test"))
        second = await render(
            stack,
            f"http://learn.example.test:{port}/echo-headers",
            RenderOptions(unblock=True, cookies=reused),
        )
        assert "cf_clearance=domain-wide" in second.html

    run_unblock(body)


# --- what the target sees ---------------------------------------------------------------------


def test_navigator_webdriver_is_false(run_unblock: Any, site: Site) -> None:
    async def body(stack: UnblockStack) -> None:
        rendered = await render(stack, f"{site.base}/navigator")
        assert '<p id="webdriver">webdriver=false</p>' in rendered.html

    run_unblock(body)


def test_unmodified_user_agent(run_unblock: Any, site: Site) -> None:
    async def body(stack: UnblockStack) -> None:
        await render(stack, f"{site.base}/static")
        (seen,) = site.seen(site.base, "/static")
        agent = seen.headers["user-agent"]
        assert "text-extraction" not in agent
        assert "Chrome/" in agent
        assert stack.renderer.user_agent == agent

        # Chrome's own background requests (Google services) are no challenge hosts (B12).
        rendered = await render(stack, f"{site.base}/static")
        assert not contacted_challenge_host(rendered.hosts)
        assert rendered.warnings == ()

    run_unblock(body)


def test_evaluate_runs_in_the_main_world(run_unblock: Any, site: Site) -> None:
    script = "document.getElementById('out').textContent = String(window.pageGlobal);"
    options = RenderOptions(
        unblock=True, actions=(EvaluateAction(script=script),), allow_evaluate=True
    )

    async def body(stack: UnblockStack) -> None:
        rendered = await render(stack, f"{site.base}/global", options)
        assert f'<p id="out">{PAGE_GLOBAL}</p>' in rendered.html
        assert rendered.warnings == ()

    run_unblock(body)


def test_consent_banner_is_opted_out(run_unblock: Any, site: Site) -> None:
    async def body(stack: UnblockStack) -> None:
        rendered = await render(stack, f"{site.base}/consent")
        assert OPTED_OUT in rendered.html

    run_unblock(body)


def test_mediawiki_cookie_dialog_is_closed(run_unblock: Any, site: Site) -> None:
    async def body(stack: UnblockStack) -> None:
        rendered = await render(stack, f"{site.base}/mw-consent")
        assert ARTICLE_TEXT.split(".")[0] in rendered.html
        assert COOKIE_DIALOG_TEXT not in rendered.html
        assert "Cookie consent: necessary" in rendered.html

    run_unblock(body)


def test_overlay_about_cookies_is_removed(run_unblock: Any, site: Site) -> None:
    async def body(stack: UnblockStack) -> None:
        rendered = await render(stack, f"{site.base}/overlay")
        assert OVERLAY_TEXT not in rendered.html
        assert "consent_overlay_removed" in rendered.warnings

    run_unblock(body)


# --- what stays in place ----------------------------------------------------------------------


def test_private_addresses_still_refused(run_unblock: Any, site: Site) -> None:
    async def body(stack: UnblockStack) -> None:
        navigated = await run_admin(stack, url=f"{site.base}/to-private")
        assert navigated.error is not None
        assert navigated.error.code is ErrorCode.BLOCKED_URL

        with pytest.raises(FetchError) as caught:
            await render(stack, f"{site.forbidden}/static")
        assert caught.value.code is ErrorCode.BLOCKED_URL

        rendered = await render(stack, f"{site.base}/img-private")
        assert ARTICLE_TEXT.split(".")[0] in rendered.html
        assert site.seen(site.forbidden) == []
        assert (
            stack.metric("text_extraction_browser_proxy_refused_total", reason="blocked_address")
            >= 3
        )

    run_unblock(body)


def test_caller_headers_reach_target_host_only(run_unblock: Any, site: Site) -> None:
    url = f"{site.base}/third-party"
    options = RenderOptions(
        unblock=True,
        headers={"x-partner-token": "s3cret"},
        header_host=site_key(url),
        cookies=(CookieData(name="pref", value="cookie-value", domain="127.0.0.1"),),
    )

    async def body(stack: UnblockStack) -> None:
        await render(stack, url, options)
        assert await wait_until(lambda: bool(site.seen(site.third, "/echo-headers")))
        (own,) = site.seen(site.base, "/third-party")
        assert own.headers["x-partner-token"] == "s3cret"
        assert "pref=cookie-value" in own.headers.get("cookie", "")
        (other,) = site.seen(site.third, "/echo-headers")
        assert "x-partner-token" not in other.headers

        # Requests that carry the caller's headers still go through the egress proxy.
        forbidden = f"{site.forbidden}/static"
        with pytest.raises(FetchError) as caught:
            await render(
                stack,
                forbidden,
                RenderOptions(unblock=True, headers={"x": "y"}, header_host=site_key(forbidden)),
            )
        assert caught.value.code is ErrorCode.BLOCKED_URL
        assert site.seen(site.forbidden) == []

    run_unblock(body)


@pytest.mark.parametrize("path", ["/speculation", "/speculation-header"])
def test_speculated_documents_still_pass_the_check(run_unblock: Any, site: Site, path: str) -> None:
    checked: list[str] = []

    async def check(target: str) -> None:
        checked.append(target)

    async def body(stack: UnblockStack) -> None:
        rendered = await render(stack, f"{site.base}{path}", check=check)
        target = f"{site.base}{SPECULATED}"
        assert checked == [target]
        assert rendered.final_url == target
        assert [s.path for s in site.seen(site.base) if "sec-purpose" in s.headers] == []
        assert site.seen(site.base, "/rules.json") == []

    run_unblock(body, browser_idle_wait_seconds=3)


def test_prefetch_and_rules_requests_are_refused(run_unblock: Any, site: Site) -> None:
    async def body(stack: UnblockStack) -> None:
        await render(stack, f"{site.base}/prefetch-link")
        await asyncio.sleep(0.5)  # a prefetch starts after the load
        assert [s.path for s in site.seen(site.base) if "sec-purpose" in s.headers] == []
        assert site.seen(site.base, "/static") == []

        rendered = await render(stack, f"{site.base}/accept-rules")
        assert '<p id="outcome">refused</p>' in rendered.html
        assert site.seen(site.base, "/api") == []

    run_unblock(body)


def test_form_post_navigation_is_allowed(run_unblock: Any, site: Site) -> None:
    checked: list[str] = []

    async def check(target: str) -> None:
        checked.append(target)

    async def body(stack: UnblockStack) -> None:
        rendered = await render(stack, f"{site.base}/form-post", check=check)
        assert "<title>Posted</title>" in rendered.html
        assert checked == [f"{site.base}/static"]
        assert "navigation_refused_non_get" not in rendered.warnings

    run_unblock(body)


# --- the pool -----------------------------------------------------------------------------------


def test_profile_dir_is_removed(run_unblock: Any, site: Site) -> None:
    async def body(stack: UnblockStack) -> None:
        await render(stack, f"{site.base}/static")
        assert len(stack.launcher.contexts) == 2  # the start check, then the page
        assert await wait_until(lambda: not any(stack.profiles.iterdir()))

        # A failed launch leaves no profile behind either.
        stack.launcher.fail_next_launch = True
        with pytest.raises(FetchError) as caught:
            await render(stack, f"{site.base}/static")
        assert caught.value.code is ErrorCode.INTERNAL_ERROR
        assert await wait_until(lambda: not any(stack.profiles.iterdir()))

    run_unblock(body)


def test_browser_crash_fails_the_page_alone(run_unblock: Any, site: Site) -> None:
    async def body(stack: UnblockStack) -> None:
        async def crash_soon() -> None:
            await wait_until(lambda: bool(site.seen(site.base, "/poll")), 10)
            kill_browser_of(stack.profiles)

        crasher = asyncio.create_task(crash_soon())
        with pytest.raises(FetchError) as caught:
            await render(stack, f"{site.base}/never-idle")
        await crasher
        assert caught.value.code is ErrorCode.INTERNAL_ERROR
        assert caught.value.retryable
        assert (
            stack.metric(
                "text_extraction_browser_pages_total", driver="patchright", outcome="crash"
            )
            == 1
        )
        rendered = await render(stack, f"{site.base}/static")
        assert ARTICLE_TEXT.split(".")[0] in rendered.html
        assert (
            stack.metric("text_extraction_browser_pages_total", driver="patchright", outcome="ok")
            == 1
        )
        assert await wait_until(lambda: not any(stack.profiles.iterdir()))

    run_unblock(body, browser_idle_wait_seconds=10)


def test_unblock_and_standard_pages_share_the_slots(
    run_unblock: Any, site: Site, chromium: Path | None
) -> None:
    async def body(stack: UnblockStack) -> None:
        assert stack.standard is not None
        both_open = False

        def gauge(driver: str) -> float:
            return stack.metric("text_extraction_browser_pages_in_use", driver=driver)

        async def watch() -> None:
            nonlocal both_open
            while True:
                both_open = both_open or (gauge("playwright") + gauge("patchright") > 1)
                await asyncio.sleep(0.02)

        watcher = asyncio.create_task(watch())
        try:
            standard = stack.standard.render(
                f"{site.base}/never-idle", allow, RenderOptions(), deadline=deadline_in(20)
            )
            unblocked = render(stack, f"{site.base}/never-idle")
            await asyncio.gather(standard, unblocked)
        finally:
            watcher.cancel()
        assert not both_open
        assert stack.metric("text_extraction_browser_render_seconds_count", driver="patchright")
        assert stack.metric("text_extraction_browser_render_seconds_count", driver="playwright")

    run_unblock(body, standard=True, browser_concurrency=1, browser_executable_path=chromium)


@pytest.mark.skipif(not os.path.exists("/usr/bin/Xvfb"), reason="Xvfb is not installed")
def test_headful_under_xvfb(run_unblock: Any, site: Site) -> None:
    async def body(stack: UnblockStack) -> None:
        rendered = await render(stack, f"{site.base}/navigator")
        assert '<p id="webdriver">webdriver=false</p>' in rendered.html
        assert "HeadlessChrome" not in stack.renderer.user_agent

    async def main() -> None:
        xvfb = Xvfb()
        await xvfb.start(":97")
        try:
            await asyncio.to_thread(run_unblock, body, browser_unblock_headless=False)
        finally:
            await xvfb.stop()

    asyncio.run(main())
