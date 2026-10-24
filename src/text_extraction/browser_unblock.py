"""Unblock mode (B14, Jan's decision D3): Patchright with Google Chrome, for partners who allowed
extraction by contract but keep their bot protection. Only the browser worker imports this
module (it needs the extra ``browser``); the pipeline uses the renderer only for an admin's
``compliance: ignore`` request while ``BROWSER_UNBLOCK_ENABLED`` is on.

**Launcher.** ``PatchrightLauncher`` opens every page in a persistent context of its own:
``channel=BROWSER_UNBLOCK_CHANNEL`` (``chrome``: Google Chrome; ``chromium``: Patchright's
build), headful under Xvfb unless ``BROWSER_UNBLOCK_HEADLESS``, ``no_viewport`` with a 1920 by
1080 window, Chrome's own user agent and no extra headers. Its profile is a fresh temporary
directory, removed once the pool has closed the context (``release``; the context's ``close``
event comes before Chrome has exited and stopped writing to it), and, for any left, when the
launcher stops. The B4 flags stay, and
the context's ``proxy`` holds the page's own egress-proxy credentials: every byte still passes
the proxy, which refuses private addresses and counts the page's bytes.

**Renderer.** ``UnblockRenderer`` is the standard renderer (``browser.ChromiumRenderer``) with
the unblock options: nothing blocked, non-GET navigations allowed. Its route handler stays in
place in every render, as in standard mode: later main-frame navigations still pass the
pipeline's check (and move the outbound gate's turn to their host), speculation is refused,
and the caller's headers reach the request URL's site only. Patchright itself injects its init
scripts by intercepting documents, so the handler costs nothing in stealth.

**Challenges.** After the load, while the page is a bot challenge (``assess``, or a thin page
that contacted a challenge host, B12) and ``BROWSER_UNBLOCK_CHALLENGE_WAIT_SECONDS`` are not
over, the render looks again every second, following the challenge's own navigations. A
visible checkbox widget (a Cloudflare Turnstile frame, an hCaptcha checkbox frame) still there
after 3 s is clicked, with a mouse move to the box first: at most twice per render, 3 s apart
(warning ``challenge_checkbox_clicked``). Nothing else is attempted: no image or puzzle
captcha, no solver service (B15). A wait that ends on a challenge adds ``challenge_not_cleared``,
and the pipeline ends the item as ``bot_protection`` whatever the last status; a challenge that
cleared sets ``Rendered.challenge_cleared``, and the pipeline then keeps the challenge's
403/429/503 out of the host's backoff in the outbound gate.

**Chrome's own traffic.** A real Chrome contacts Google services in the background (for
example ``www.google.com``, ``accounts.google.com``, ``android.clients.google.com``). It goes
through the egress proxy like everything else, counts towards the page's bytes and shows in
``Rendered.hosts``; it is left as it is, since it is what a visitor's Chrome does. Those hosts
are no challenge hosts, so they never make a page a challenge (B12).

**Display.** ``Xvfb`` runs the virtual display headful Chrome needs; the browser worker starts
it before the pool when unblock mode is on and not headless.

Logs carry hosts and codes only, never a URL, cookie, header or script.
"""

import asyncio
import contextlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Final, cast
from urllib.parse import urlsplit

import structlog
from patchright.async_api import Playwright as PatchrightPlaywright
from patchright.async_api import async_playwright as async_patchright
from playwright.async_api import Browser, BrowserContext, Frame, Page

from text_extraction.browser import (
    BrowserPool,
    ChromiumRenderer,
    Driver,
    PageProxy,
    chromium_args,
)
from text_extraction.browser_driver import DRIVER_ERRORS
from text_extraction.browser_render import AfterLoad, AfterLoadOutcome, Loaded
from text_extraction.convert.markup import parse_html, strip_non_content
from text_extraction.pipeline_render import CHALLENGE_NOT_CLEARED
from text_extraction.quality import THIN_PAGE_BELOW_WORDS, assess
from text_extraction.render import RenderOptions, contacted_challenge_host
from text_extraction.settings import Settings

_log = structlog.get_logger(__name__)

WINDOW_SIZE: Final = "--window-size=1920,1080"
PROFILE_PREFIX: Final = "text-extraction-unblock-"
CHECKBOX_AFTER_SECONDS: Final = 3.0  # of challenge wait before a click, and between clicks
MAX_CLICKS: Final = 2
_POLL_SECONDS: Final = 1.0
_CLOSE_SECONDS: Final = 5.0
_REMOVE_ATTEMPTS: Final = 5
_REMOVE_PAUSE_SECONDS: Final = 0.5
_BOX_WAIT_MS: Final = 500  # for the checkbox inside a widget frame to be found
_MIN_WIDGET_PX: Final = 10  # a frame smaller than this is no visible widget
# Where on a widget frame its checkbox sits when the box itself cannot be found inside it.
_BOX_OFFSET_PX: Final = 30.0
_CHECKBOX_SELECTOR: Final = 'input[type="checkbox"], [role="checkbox"], #checkbox'
_TURNSTILE_HOST: Final = "challenges.cloudflare.com"
_HCAPTCHA_DOMAIN: Final = "hcaptcha.com"


# --- the launcher -------------------------------------------------------------------------------


class PatchrightLauncher:
    """Patchright with Chrome: a persistent context, its own browser, per page (B14)."""

    driver: Driver = "patchright"
    persistent = True

    def __init__(
        self,
        settings: Settings,
        *,
        executable_path: Path | None = None,
        profile_root: Path | None = None,
    ) -> None:
        """``executable_path`` overrides the channel's browser (tests, development);
        ``profile_root`` is where the temporary profiles go (the system's temp dir)."""
        self._settings = settings
        self._executable_path = executable_path
        self._profile_root = profile_root
        self._driver: PatchrightPlaywright | None = None
        self._starting = asyncio.Lock()
        self._profiles: set[Path] = set()  # created and not removed yet
        self._of_context: dict[BrowserContext, Path] = {}
        self.user_agent = ""

    async def open(
        self, browser: Browser | None, proxy: PageProxy, options: RenderOptions
    ) -> tuple[Browser | None, BrowserContext]:
        profile = Path(tempfile.mkdtemp(prefix=PROFILE_PREFIX, dir=self._profile_root))
        self._profiles.add(profile)
        try:
            context = await self._launch(profile, proxy)
        except BaseException:
            await self._remove(profile)
            raise
        self._of_context[context] = profile
        if not self.user_agent:
            try:
                self.user_agent = await _user_agent(context)
            except BaseException:
                with contextlib.suppress(*DRIVER_ERRORS, TimeoutError):
                    async with asyncio.timeout(_CLOSE_SECONDS):
                        await context.close()
                await self.release(context)
                raise
        return None, context

    async def release(self, context: BrowserContext) -> None:
        profile = self._of_context.pop(context, None)
        if profile is not None:
            await self._remove(profile)

    async def stop(self) -> None:
        """Stop the driver (its browsers end with it) and remove the profiles left."""
        driver, self._driver = self._driver, None
        if driver is not None:
            with contextlib.suppress(*DRIVER_ERRORS):
                await driver.stop()
        self._of_context.clear()
        for profile in list(self._profiles):
            await self._remove(profile)

    async def _launch(self, profile: Path, proxy: PageProxy) -> BrowserContext:
        driver = await self._started()
        context = await driver.chromium.launch_persistent_context(
            profile,
            channel=self._settings.browser_unblock_channel,
            executable_path=self._executable_path,
            headless=self._settings.browser_unblock_headless,
            no_viewport=True,
            accept_downloads=False,
            args=[*chromium_args(proxy.server), WINDOW_SIZE],
            proxy={"server": proxy.server, "username": proxy.username, "password": proxy.password},
        )
        # The same API as Playwright's (Patchright is its fork): the shared code takes it as one.
        return cast(BrowserContext, context)

    async def _started(self) -> PatchrightPlaywright:
        async with self._starting:
            if self._driver is None:
                self._driver = await async_patchright().start()
            return self._driver

    async def _remove(self, profile: Path) -> None:
        """Remove ``profile`` (in a thread: a profile holds many small files). A browser that
        went away by itself may still be ending, and write to it once more: then again, a
        little later. What is left is removed by ``stop``."""
        for attempt in range(_REMOVE_ATTEMPTS):
            if attempt:
                await asyncio.sleep(_REMOVE_PAUSE_SECONDS)
            if not await asyncio.to_thread(_removed, profile):
                continue
            self._profiles.discard(profile)
            return


def _removed(profile: Path) -> bool:
    shutil.rmtree(profile, ignore_errors=True)
    return not profile.exists()


async def _user_agent(context: BrowserContext) -> str:
    """Chrome's own user agent (B8 for unblock mode: unmodified), read from the context's
    first tab in Patchright's isolated world."""
    page = context.pages[0] if context.pages else await context.new_page()
    return str(await page.evaluate("() => navigator.userAgent"))


# --- the virtual display ------------------------------------------------------------------------


class Xvfb:
    """An X virtual framebuffer for headful Chrome. ``start`` sets ``DISPLAY``, ``stop`` puts
    it back."""

    def __init__(
        self, binary: str = "Xvfb", *, screen: str = "1920x1080x24", start_seconds: float = 10.0
    ) -> None:
        self._binary = binary
        self._screen = screen
        self._start_seconds = start_seconds
        self._process: asyncio.subprocess.Process | None = None
        self._previous: str | None = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self, display: str = ":99") -> None:
        """Start the server on ``display`` and wait until it accepts clients (Xvfb tells so on
        ``-displayfd``); ``RuntimeError`` when it does not come up in time."""
        loop = asyncio.get_running_loop()
        read_end, write_end = os.pipe()
        reader = asyncio.StreamReader()
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(read_end, "rb", buffering=0)
        )
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    self._binary,
                    display,
                    "-screen",
                    "0",
                    self._screen,
                    "-nolisten",
                    "tcp",
                    "-displayfd",
                    str(write_end),
                    pass_fds=(write_end,),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            finally:
                os.close(write_end)
            try:
                async with asyncio.timeout(self._start_seconds):
                    number = (await reader.readline()).decode("ascii", "replace").strip()
            except BaseException:
                await _ended(process, kill=True)
                raise
        finally:
            transport.close()
        if not number.isdigit():
            await _ended(process, kill=True)
            raise RuntimeError("Xvfb did not start")
        self._process = process
        self._previous = os.environ.get("DISPLAY")
        os.environ["DISPLAY"] = f":{number}"
        _log.info("xvfb_started", display=f":{number}")

    async def stop(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        await _ended(process, kill=False)
        if self._previous is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = self._previous
        self._previous = None


async def _ended(process: asyncio.subprocess.Process, *, kill: bool) -> None:
    """Terminate ``process`` (or kill it at once), and kill it when it does not end in time."""
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill() if kill else process.terminate()
        try:
            async with asyncio.timeout(_CLOSE_SECONDS):
                await process.wait()
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


# --- challenges ---------------------------------------------------------------------------------


def is_checkbox_widget(url: str) -> bool:
    """A frame of a checkbox challenge widget: Cloudflare Turnstile, or hCaptcha's checkbox
    frame (not its image challenge frame)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").rstrip(".")
    if host == _TURNSTILE_HOST:
        return True
    hcaptcha = host == _HCAPTCHA_DOMAIN or host.endswith(f".{_HCAPTCHA_DOMAIN}")
    return hcaptcha and "frame=checkbox" in parts.fragment


def is_challenge(html: str, hosts: frozenset[str]) -> bool:
    """Whether the page is a bot challenge: ``assess`` says so, or it is thin and the render
    contacted a challenge host (B12). CPU-bound."""
    soup = parse_html(html)
    strip_non_content(soup)
    quality = assess(html, soup.get_text(" "))
    return quality.bot_challenge or (
        quality.word_count < THIN_PAGE_BELOW_WORDS and contacted_challenge_host(hosts)
    )


class ChallengeWait:
    """The unblock renderer's step after the load; see the module docstring."""

    def __init__(self, settings: Settings, checkbox_frame: Callable[[str], bool]) -> None:
        self._settings = settings
        self._checkbox_frame = checkbox_frame

    async def __call__(self, loaded: Loaded) -> AfterLoadOutcome:
        loop = asyncio.get_running_loop()
        started = loop.time()
        end = min(started + self._settings.browser_unblock_challenge_wait_seconds, loaded.deadline)
        warnings: list[str] = []
        clicks = 0
        last_click = started
        met = False
        while await asyncio.to_thread(is_challenge, await loaded.read(), loaded.hosts()):
            now = loop.time()
            host = _host(loaded.page)
            if now >= end:
                _log.info("challenge_not_cleared", host=host, clicks=clicks)
                warnings.append(CHALLENGE_NOT_CLEARED)
                return AfterLoadOutcome(tuple(warnings))
            met = True
            if clicks < MAX_CLICKS and now - last_click >= CHECKBOX_AFTER_SECONDS:
                if await self._click_checkbox(loaded.page):
                    clicks += 1
                    _log.info("challenge_checkbox_clicked", host=host, click=clicks)
                    warnings.append("challenge_checkbox_clicked")
                last_click = loop.time()
            await asyncio.sleep(max(min(_POLL_SECONDS, end - loop.time()), 0.0))
            loaded.raise_kept()
        if met:
            _log.info("challenge_cleared", host=_host(loaded.page), clicks=clicks)
            await self._idle(loaded)
        return AfterLoadOutcome(tuple(warnings), challenge_cleared=met)

    async def _idle(self, loaded: Loaded) -> None:
        """Give the page behind the challenge its network-idle wait."""
        left = loaded.deadline - asyncio.get_running_loop().time()
        idle = min(self._settings.browser_idle_wait_seconds, left)
        if idle > 0:
            with contextlib.suppress(*DRIVER_ERRORS):
                await loaded.page.wait_for_load_state("networkidle", timeout=idle * 1000)

    async def _click_checkbox(self, page: Page) -> bool:
        """Click the checkbox of the first visible widget frame; whether one was clicked."""
        for frame in page.frames:
            if frame.is_detached() or frame.parent_frame is None:
                continue
            if not self._checkbox_frame(frame.url):
                continue
            with contextlib.suppress(*DRIVER_ERRORS):
                point = await _checkbox_point(frame)
                if point is None:
                    continue
                x, y = point
                # Towards the box from a little above and left of it, then a press.
                await page.mouse.move(x - 40, y - 20, steps=4)
                await page.mouse.move(x, y, steps=10)
                await asyncio.sleep(0.1)
                await page.mouse.down()
                await asyncio.sleep(0.08)
                await page.mouse.up()
                return True
        return False


async def _checkbox_point(frame: Frame) -> tuple[float, float] | None:
    """Where to click in a widget frame (main frame coordinates): its checkbox's middle, or a
    point near the frame's left edge, where the widgets draw their box; None when the frame
    is not visible."""
    element = await frame.frame_element()
    box = await element.bounding_box()
    if (
        box is None
        or box["width"] < _MIN_WIDGET_PX
        or box["height"] < _MIN_WIDGET_PX
        or not await element.is_visible()
    ):
        return None
    with contextlib.suppress(*DRIVER_ERRORS):
        inner = await frame.locator(_CHECKBOX_SELECTOR).first.bounding_box(timeout=_BOX_WAIT_MS)
        if inner is not None and inner["width"] > 0 and inner["height"] > 0:
            return inner["x"] + inner["width"] / 2, inner["y"] + inner["height"] / 2
    return box["x"] + min(_BOX_OFFSET_PX, box["width"] / 2), box["y"] + box["height"] / 2


def _host(page: Page) -> str:
    try:
        return (urlsplit(page.url).hostname or "").lower()
    except ValueError:
        return ""


# --- the renderer -------------------------------------------------------------------------------


class UnblockRenderer(ChromiumRenderer):
    """The ``Renderer`` of unblock mode: a page from a Patchright pool (``BrowserPool`` with a
    ``PatchrightLauncher``), the unblock options, then the challenge wait."""

    def __init__(
        self,
        pool: BrowserPool,
        settings: Settings,
        *,
        checkbox_frame: Callable[[str], bool] = is_checkbox_widget,
    ) -> None:
        """``checkbox_frame`` tells a challenge widget's frame by its URL (tests stand in
        their own widget)."""
        super().__init__(pool, settings)
        self._challenge = ChallengeWait(settings, checkbox_frame)

    def _options(self, options: RenderOptions) -> RenderOptions:
        return replace(options, unblock=True, blocked_resources=frozenset())

    def _after_load(self) -> AfterLoad | None:
        return self._challenge
