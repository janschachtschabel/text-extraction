"""Chromium for the browser worker: the page pool, the standard renderer and the render steps.

Only the browser worker imports this module (it needs the extra ``browser``). See
docs/architecture.md, "Browser worker", and the rulings B2-B4, B8, B11 and B12 of package 4.

**Pool.** ``BrowserPool`` keeps one browser per driver per process, behind the egress proxy
(``browser_proxy.py``) with the B4 launch flags. ``start`` launches it, later launches happen on
the next page: after ``BROWSER_RECYCLE_AFTER_PAGES`` pages (the old browser is closed once its
pages end) and after the browser went away (crashed, killed). The pages that were in flight then
fail alone; the renderer turns that into ``internal_error``, retryable. A semaphore lets
``BROWSER_CONCURRENCY`` pages run at once; pools of both drivers can share one (``slots``).
Each page gets its own browser context with its own proxy credentials, so the proxy counts and
caps its bytes, and the context and its proxy context are always closed with the page.

**Launcher.** What differs between the drivers (Playwright here, Patchright for unblock mode)
is how a browser is launched and how a page's context is opened: a ``Launcher``. The pool, the
recycling and the crash handling are shared.

**Render.** ``render_page`` loads a page in the steps of B2 within one deadline: ``goto`` until
``load``, up to ``BROWSER_IDLE_WAIT_SECONDS`` for the network to go idle, the consent opt-out
(B11), then hidden elements are removed and the DOM is read; the screenshot comes last. Every
request passes the route handler: a main-frame navigation after the first is sent only after the
pipeline's ``NavigationCheck`` (a non-GET one not at all), blocked resource types are not loaded,
and the caller's headers go only to the request URL's site.

Logs carry hosts and codes only, never a URL, header, cookie or script.
"""

import asyncio
import contextlib
import contextvars
import struct
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal, Protocol
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

import structlog
from patchright.async_api import Error as PatchrightError
from patchright.async_api import TimeoutError as PatchrightTimeout
from playwright.async_api import (
    Browser,
    BrowserContext,
    Frame,
    Page,
    Playwright,
    Request,
    Response,
    Route,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from text_extraction.browser_proxy import EgressProxy, ProxyContext
from text_extraction.browser_scripts import (
    CONSENT_RELAY,
    CONSENT_WAIT,
    PAGE_HEIGHT,
    STRIP_HIDDEN,
    consent_bundle,
    consent_config,
    consent_rules_json,
)
from text_extraction.envelope import ErrorCode
from text_extraction.fetch import USER_AGENT, FetchError
from text_extraction.render import (
    CookieData,
    MainResponse,
    NavigationCheck,
    Rendered,
    RenderOptions,
    site,
)
from text_extraction.settings import Settings

if TYPE_CHECKING:
    from playwright._impl._api_structures import SetCookieParam

_log = structlog.get_logger(__name__)

type Driver = Literal["playwright", "patchright"]
type ConsentOpt = Literal["optOut", "optIn"]

# The errors of both drivers' API (Patchright is a fork with its own classes).
DRIVER_ERRORS: Final[tuple[type[Exception], ...]] = (PlaywrightError, PatchrightError)
DRIVER_TIMEOUTS: Final[tuple[type[Exception], ...]] = (PlaywrightTimeout, PatchrightTimeout)
_RENDER_ERRORS: Final[tuple[type[Exception], ...]] = (*DRIVER_ERRORS, FetchError)
VIEWPORT: Final = {"width": 1920, "height": 1080}
LOCALE: Final = "de-DE"
CONSENT_SECONDS: Final = 3.0  # B11: best effort
CLOSE_SECONDS: Final = 5.0  # closing a context or a browser
# Of a render's deadline: what closing the page may take, and what reading the DOM (and taking
# the screenshot) needs after the waits.
_CLOSE_RESERVE_SECONDS: Final = 0.5
_READ_RESERVE_SECONDS: Final = 1.0
_SCREENSHOT_RESERVE_SECONDS: Final = 3.0
_CONTENT_ATTEMPTS: Final = 3
# The proxy's answers to a plain-HTTP request whose host it refuses outright.
_PROXY_ANSWERS: Final = frozenset({(403, "blocked_address"), (502, "dns_error")})
_RENDER_BUCKETS: Final = (0.5, 1, 2, 3, 5, 8, 10, 15, 20, 30, 45, 60, 90)
# Main-frame load failures that are not the target's: a navigation that a newer one replaced,
# or one the route handler aborted (its reason is kept separately).
_NOT_FAILURES: Final = ("net::ERR_ABORTED", "net::ERR_BLOCKED_BY_CLIENT")


def chromium_args(proxy_url: str) -> list[str]:
    """The B4 launch flags: everything through the proxy (loopback too), no QUIC, no WebRTC
    UDP outside the proxy, no /dev/shm."""
    return [
        f"--proxy-server={proxy_url}",
        "--proxy-bypass-list=<-loopback>",
        "--disable-quic",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-dev-shm-usage",
    ]


@dataclass(frozen=True)
class PageProxy:
    """The egress proxy and the credentials of one page's proxy context."""

    server: str
    username: str
    password: str = field(repr=False)


class Launcher(Protocol):
    """Launches the browser of one driver and opens the context of one page in it."""

    driver: Driver
    # What the pages send as User-Agent; known once a browser was launched.
    user_agent: str

    async def open(
        self, browser: Browser | None, proxy: PageProxy, options: RenderOptions
    ) -> tuple[Browser | None, BrowserContext]:
        """A new context for one page, in ``browser``; when it is None, a browser is launched
        first (behind ``proxy.server``) and returned with the context. A launcher whose
        contexts own their browser (a persistent context) returns None: the pool then shares
        nothing and launches for every page."""
        ...

    async def stop(self) -> None:
        """Stop the driver; the pool closed its browsers before."""
        ...


class PlaywrightLauncher:
    """Headless Chromium through Playwright, the standard mode's browser (B3, B4, B8)."""

    driver: Driver = "playwright"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._driver: Playwright | None = None
        self.user_agent = ""

    async def open(
        self, browser: Browser | None, proxy: PageProxy, options: RenderOptions
    ) -> tuple[Browser | None, BrowserContext]:
        if browser is None:
            browser = await self._launch(proxy.server)
        context = await browser.new_context(
            viewport={"width": VIEWPORT["width"], "height": VIEWPORT["height"]},
            service_workers="block",
            accept_downloads=False,
            locale=LOCALE,
            user_agent=self.user_agent,
            proxy={"server": proxy.server, "username": proxy.username, "password": proxy.password},
        )
        return browser, context

    async def stop(self) -> None:
        driver, self._driver = self._driver, None
        if driver is not None:
            with contextlib.suppress(*DRIVER_ERRORS):
                await driver.stop()

    async def _launch(self, proxy_url: str) -> Browser:
        if self._driver is None:
            self._driver = await async_playwright().start()
        browser = await self._driver.chromium.launch(
            headless=True,
            executable_path=self._settings.browser_executable_path,
            args=chromium_args(proxy_url),
        )
        if not self.user_agent:
            # B8: Chromium's own user agent, with our product token and contact added.
            session = await browser.new_browser_cdp_session()
            version = await session.send("Browser.getVersion")
            await session.detach()
            self.user_agent = f"{version['userAgent']} {USER_AGENT}"
        return browser


# --- the pool -----------------------------------------------------------------------------------


class _Metrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        self.in_use = Gauge(
            "text_extraction_browser_pages_in_use",
            "Browser pages open now.",
            ["driver"],
            registry=registry,
        )
        self.pages = Counter(
            "text_extraction_browser_pages_total",
            "Browser pages by outcome: ok, error (the target or a refusal), crash, timeout.",
            ["driver", "outcome"],
            registry=registry,
        )
        self.seconds = Histogram(
            "text_extraction_browser_render_seconds",
            "Seconds a browser page was open.",
            ["driver"],
            buckets=_RENDER_BUCKETS,
            registry=registry,
        )
        self.launches = Counter(
            "text_extraction_browser_launches_total",
            "Browser launches by reason: start, recycle, crash.",
            ["driver", "reason"],
            registry=registry,
        )


_METRICS: WeakKeyDictionary[CollectorRegistry, _Metrics] = WeakKeyDictionary()


def _metrics_for(registry: CollectorRegistry) -> _Metrics:
    metrics = _METRICS.get(registry)
    if metrics is None:
        metrics = _Metrics(registry)
        _METRICS[registry] = metrics
    return metrics


@dataclass(eq=False)
class _Slot:
    """One launched browser and the pages it serves."""

    browser: Browser
    pages: int = 0  # started in it
    in_flight: int = 0
    retiring: bool = False  # recycled: new pages go to a new browser
    dead: bool = False  # disconnected without being closed by the pool
    closing: bool = False


@dataclass(eq=False)
class PooledPage:
    """A page of the pool and the proxy context that counts its traffic."""

    page: Page
    traffic: ProxyContext
    _slot: _Slot | None = field(default=None, repr=False)
    _crashed: bool = False
    _closing: bool = False

    @property
    def crashed(self) -> bool:
        """Whether the page's renderer or browser went away while it was open."""
        return self._crashed or (self._slot is not None and self._slot.dead)

    def _on_crash(self, _page: Page) -> None:
        self._crashed = True

    def _on_close(self, _context: BrowserContext) -> None:
        if not self._closing:
            self._crashed = True


class BrowserPool:
    """Browser pages for the renderers; see the module docstring."""

    def __init__(
        self,
        settings: Settings,
        proxy: EgressProxy,
        registry: CollectorRegistry,
        *,
        launcher: Launcher,
        slots: asyncio.Semaphore | None = None,
    ) -> None:
        self._settings = settings
        self._proxy = proxy
        self._launcher = launcher
        self._metrics = _metrics_for(registry)
        self._slots = (
            slots if slots is not None else asyncio.Semaphore(settings.browser_concurrency)
        )
        self._lock = asyncio.Lock()
        self._current: _Slot | None = None
        self._open: set[_Slot] = set()  # launched and not closed yet
        self._stopped = False

    @property
    def driver(self) -> Driver:
        return self._launcher.driver

    @property
    def user_agent(self) -> str:
        """The User-Agent the pages send (B8); known after ``start``."""
        return self._launcher.user_agent

    async def start(self) -> None:
        """Launch the browser and open one context in it, so a broken browser shows at once."""
        traffic = self._proxy.open_context()
        try:
            async with self._lock:
                _, context = await self._launch(self._page_proxy(traffic), RenderOptions(), "start")
            await _close_context(context)
        finally:
            traffic.close()

    async def stop(self) -> None:
        """Close every browser and stop the driver."""
        self._stopped = True
        async with self._lock:
            self._current = None
            for slot in list(self._open):
                await self._close_browser(slot)
        await self._launcher.stop()

    @asynccontextmanager
    async def page(self, options: RenderOptions) -> AsyncIterator[PooledPage]:
        """A fresh page in its own context, with ``options.cookies`` set. The context and its
        proxy context are closed when the block ends, whatever happened."""
        async with self._slots:
            if self._stopped:
                raise RuntimeError("the browser pool is stopped")
            driver = self.driver
            traffic = self._proxy.open_context()
            self._metrics.in_use.labels(driver=driver).inc()
            started = time.perf_counter()
            slot: _Slot | None = None
            context: BrowserContext | None = None
            pooled: PooledPage | None = None
            outcome = "error"
            try:
                slot, context = await self._context(self._page_proxy(traffic), options)
                pooled = PooledPage(await context.new_page(), traffic, slot)
                pooled.page.on("crash", pooled._on_crash)
                context.on("close", pooled._on_close)
                context.on("page", _close_other(pooled.page))
                if options.cookies:
                    await context.add_cookies([_cookie_param(cookie) for cookie in options.cookies])
                yield pooled
                outcome = "ok"
            except BaseException as error:
                crashed = pooled.crashed if pooled is not None else bool(slot and slot.dead)
                timed_out = isinstance(error, FetchError) and error.code is ErrorCode.TIMEOUT
                outcome = "crash" if crashed else "timeout" if timed_out else "error"
                raise
            finally:
                if pooled is not None:
                    pooled._closing = True
                if context is not None:
                    await _close_context(context)
                traffic.close()
                if slot is not None:
                    await self._release(slot)
                self._metrics.in_use.labels(driver=driver).dec()
                self._metrics.pages.labels(driver=driver, outcome=outcome).inc()
                self._metrics.seconds.labels(driver=driver).observe(time.perf_counter() - started)

    def _page_proxy(self, traffic: ProxyContext) -> PageProxy:
        return PageProxy(self._proxy.url, traffic.username, traffic.password)

    async def _context(
        self, proxy: PageProxy, options: RenderOptions
    ) -> tuple[_Slot | None, BrowserContext]:
        """A context in the current browser, or in a new one when there is none (yet), when
        it was recycled or when it went away."""
        async with self._lock:
            slot = self._current
            if slot is None or slot.dead or slot.retiring:
                reason = (
                    "crash"
                    if slot is not None and slot.dead
                    else "recycle"
                    if slot is not None
                    else "start"
                )
                new, context = await self._launch(proxy, options, reason)
                if slot is not None and not slot.dead and slot.in_flight == 0:
                    await self._close_browser(slot)
                if new is not None:
                    self._take(new)
                return new, context
            self._take(slot)
        try:
            _, context = await self._launcher.open(slot.browser, proxy, options)
        except BaseException:
            await self._release(slot)
            raise
        return slot, context

    async def _launch(
        self, proxy: PageProxy, options: RenderOptions, reason: str
    ) -> tuple[_Slot | None, BrowserContext]:
        browser, context = await self._launcher.open(None, proxy, options)
        self._metrics.launches.labels(driver=self.driver, reason=reason).inc()
        _log.info("browser_launched", driver=self.driver, reason=reason)
        if browser is None:
            return None, context
        slot = _Slot(browser)
        browser.on("disconnected", lambda _browser: self._disconnected(slot))
        self._current = slot
        self._open.add(slot)
        return slot, context

    def _take(self, slot: _Slot) -> None:
        slot.pages += 1
        slot.in_flight += 1
        if slot.pages >= self._settings.browser_recycle_after_pages:
            slot.retiring = True

    async def _release(self, slot: _Slot) -> None:
        slot.in_flight -= 1
        if slot.in_flight == 0 and slot is not self._current:
            await self._close_browser(slot)

    def _disconnected(self, slot: _Slot) -> None:
        self._open.discard(slot)
        if not slot.closing:
            slot.dead = True
            _log.warning("browser_disconnected", driver=self.driver)

    async def _close_browser(self, slot: _Slot) -> None:
        if slot.closing:
            return
        slot.closing = True
        self._open.discard(slot)
        with contextlib.suppress(*DRIVER_ERRORS, TimeoutError):
            async with asyncio.timeout(CLOSE_SECONDS):
                await slot.browser.close()


async def _close_context(context: BrowserContext) -> None:
    with contextlib.suppress(*DRIVER_ERRORS, TimeoutError):
        async with asyncio.timeout(CLOSE_SECONDS):
            await context.close()


def _close_other(own: Page) -> Callable[[Page], Awaitable[None]]:
    """A handler that closes every new page of a context but ``own`` (a popup, a new tab)."""

    async def close(page: Page) -> None:
        if page is not own:
            with contextlib.suppress(*DRIVER_ERRORS):
                await page.close()

    return close


def _cookie_param(cookie: CookieData) -> "SetCookieParam":
    param: SetCookieParam = {
        "name": cookie.name,
        "value": cookie.value,
        "domain": cookie.domain,
        "path": cookie.path,
        "secure": cookie.secure,
        "httpOnly": cookie.http_only,
    }
    if cookie.expires is not None:
        param["expires"] = cookie.expires
    return param


# --- the renderer -------------------------------------------------------------------------------


class ChromiumRenderer:
    """The standard mode's ``Renderer``: a page from the pool, loaded by ``render_page``."""

    def __init__(self, pool: BrowserPool, settings: Settings) -> None:
        self._pool = pool
        self._settings = settings

    @property
    def user_agent(self) -> str:
        return self._pool.user_agent

    async def render(
        self, url: str, check: NavigationCheck, options: RenderOptions, *, deadline: float
    ) -> Rendered:
        try:
            async with contextlib.AsyncExitStack() as stack:
                # Waiting for a free page and launching count against the deadline; closing
                # the page (outside) has the reserve, and must not be cut short.
                try:
                    async with asyncio.timeout_at(deadline - _CLOSE_RESERVE_SECONDS):
                        pooled = await stack.enter_async_context(self._pool.page(options))
                except TimeoutError:
                    raise FetchError(
                        ErrorCode.TIMEOUT, "No browser page was free in time.", retryable=True
                    ) from None
                try:
                    return await render_page(
                        pooled.page,
                        url,
                        check,
                        options,
                        deadline=deadline - _CLOSE_RESERVE_SECONDS,
                        settings=self._settings,
                        traffic=pooled.traffic,
                    )
                except _RENDER_ERRORS as error:
                    if not pooled.crashed:
                        raise
                    status = error.upstream_status if isinstance(error, FetchError) else None
                    raise FetchError(
                        ErrorCode.INTERNAL_ERROR,
                        "The browser stopped during the render. Try again.",
                        retryable=True,
                        upstream_status=status,
                    ) from None
        except DRIVER_ERRORS as error:
            # Not the target's doing: the browser could not be launched or did not answer.
            _log.warning("browser_failed", driver=self._pool.driver, error=type(error).__name__)
            raise FetchError(
                ErrorCode.INTERNAL_ERROR, "The browser failed. Try again.", retryable=True
            ) from None


# --- the render steps ---------------------------------------------------------------------------


async def render_page(
    page: Page,
    url: str,
    check: NavigationCheck,
    options: RenderOptions,
    *,
    deadline: float,
    settings: Settings,
    traffic: ProxyContext | None = None,
) -> Rendered:
    """Load ``url`` in ``page`` and read it (B2), all before ``deadline`` (``loop.time()``).

    ``traffic`` is the page's proxy context: its hosts become ``Rendered.hosts``, its byte cap
    the warning ``browser_bytes_capped``, and its refusals tell a document the proxy refused
    (``blocked_url``) from one the target failed to deliver. An error of ``check`` ends the
    render unchanged; a failure of the target is a ``FetchError`` that carries the status of
    the last document answer seen."""
    render = _Render(page, url, check, options, settings, traffic)
    await render.install()
    try:
        async with asyncio.timeout_at(deadline):
            return await render.run(deadline)
    except TimeoutError:
        render.raise_kept()
        raise FetchError(
            ErrorCode.TIMEOUT,
            "The page did not finish loading in time.",
            retryable=True,
            upstream_status=render.last_status(),
        ) from None
    finally:
        await render.end_checks()


async def run_consent(page: Page, opt: ConsentOpt, *, seconds: float = CONSENT_SECONDS) -> bool:
    """Run autoconsent with ``opt`` in the page's frames (B11), for at most ``seconds``.
    Best effort: True when a banner was handled in any frame, never an error."""
    if seconds <= 0:
        return False
    frames = [frame for frame in page.frames if not frame.is_detached()][:10]
    args = {"rulesJson": consent_rules_json(), "config": consent_config(opt)}
    # The page's own wait ends a little earlier, so its outcome still comes back in time.
    milliseconds = round(max(seconds - 0.2, 0.0) * 1000)

    async def in_frame(frame: Frame) -> bool:
        try:
            installed = await frame.evaluate(CONSENT_RELAY, args)
            if installed:
                await frame.evaluate(consent_bundle())
            outcome = await frame.evaluate(
                CONSENT_WAIT, {"opt": opt, "timeoutMs": milliseconds, "again": not installed}
            )
        except DRIVER_ERRORS:
            return False
        return bool(outcome == "done")

    try:
        async with asyncio.timeout(seconds):
            results = await asyncio.gather(*(in_frame(frame) for frame in frames))
    except TimeoutError:
        return False
    return any(results)


async def strip_hidden(page: Page) -> None:
    """Remove the elements a reader cannot see (``display: none`` subtrees, hidden leaves),
    so the converter does not take inactive tabs and panels for text. Best effort."""
    with contextlib.suppress(*DRIVER_ERRORS):
        await page.evaluate(STRIP_HIDDEN)


def png_size(png: bytes) -> tuple[int, int]:
    """Width and height from a PNG's header."""
    width, height = struct.unpack(">II", png[16:24])
    return int(width), int(height)


class _Render:
    """The state of one ``render_page`` call: its route handler and what the page did."""

    def __init__(
        self,
        page: Page,
        url: str,
        check: NavigationCheck,
        options: RenderOptions,
        settings: Settings,
        traffic: ProxyContext | None,
    ) -> None:
        self.page = page
        self.url = url
        self.check = check
        self.options = options
        self.settings = settings
        self.traffic = traffic
        self.loop = asyncio.get_running_loop()
        # The navigation check runs in the route handler's task, but belongs to the render: it
        # takes the gate's turns, whose state lives in context variables. So it runs in the
        # render task's own context, as if the render had called it.
        task = asyncio.current_task()
        self.context = task.get_context() if task is not None else contextvars.copy_context()
        self.answers: list[Response] = []  # main-frame document responses, in order
        self.warnings: list[str] = []
        self.refusal: BaseException | None = None  # of the navigation check
        self.failure: tuple[str, str] | None = None  # (URL, error) of a failed document load
        self.download = False
        self.unanswered: set[Request] = set()  # refused navigations answered with 204
        self.first_navigation = True
        self.deadline = 0.0
        self.checks: set[asyncio.Task[None]] = set()

    async def install(self) -> None:
        self.page.on("response", self._on_response)
        self.page.on("requestfailed", self._on_request_failed)
        self.page.on("download", self._on_download)
        await self.page.context.route("**/*", self._route)

    async def end_checks(self) -> None:
        """Cancel the navigation checks still running (their turn waits) and wait for them."""
        for task in self.checks:
            task.cancel()
        await asyncio.gather(*self.checks, return_exceptions=True)

    def raise_kept(self) -> None:
        """End the render with what went wrong in the page meanwhile: the check's refusal of a
        navigation, or a document that did not load."""
        if self.refusal is not None:
            raise self.refusal
        if self.failure is not None:
            raise self._load_error(*self.failure)

    def last_status(self) -> int | None:
        return self.answers[-1].status if self.answers else None

    def left(self, reserve: float = 0.0) -> float:
        return self.deadline - self.loop.time() - reserve

    async def run(self, deadline: float) -> Rendered:
        self.deadline = deadline
        reserve = _SCREENSHOT_RESERVE_SECONDS if self.options.screenshot else _READ_RESERVE_SECONDS
        await self._goto(reserve)
        if self.download:
            return self._rendered("", None, None, ())
        idle = min(self.settings.browser_idle_wait_seconds, self.left(reserve))
        if idle > 0:
            with contextlib.suppress(*DRIVER_ERRORS):
                await self.page.wait_for_load_state("networkidle", timeout=idle * 1000)
        self.raise_kept()
        if self.settings.browser_autoconsent:
            await run_consent(self.page, "optOut", seconds=min(CONSENT_SECONDS, self.left(reserve)))
            self.raise_kept()
        await strip_hidden(self.page)
        html = await self._content()
        screenshot, size = await self._screenshot() if self.options.screenshot else (None, None)
        self.raise_kept()
        return self._rendered(html, screenshot, size, await self._cookies())

    async def _goto(self, reserve: float) -> None:
        timeout = self.left(reserve)
        if timeout <= 0:
            raise FetchError(
                ErrorCode.TIMEOUT, "Too little time was left to render the target.", retryable=True
            )
        try:
            await self.page.goto(self.url, wait_until="load", timeout=timeout * 1000)
        except DRIVER_ERRORS as error:
            self.raise_kept()
            if self.download or "Download is starting" in str(error):
                self.download = True  # its event may come after the error
                return
            if "interrupted by another navigation" not in str(error):
                raise self._goto_error(error) from None
            # A script or meta refresh replaced the document before it loaded.
            with contextlib.suppress(*DRIVER_ERRORS):
                await self.page.wait_for_load_state(
                    "load", timeout=max(self.left(reserve), 0.1) * 1000
                )
        self.raise_kept()

    async def _content(self) -> str:
        for _ in range(_CONTENT_ATTEMPTS):
            try:
                return await self.page.content()
            except DRIVER_ERRORS:
                if self.page.is_closed():
                    raise
                self.raise_kept()
                # The page is navigating: wait for its next document, then read again.
                with contextlib.suppress(*DRIVER_ERRORS):
                    await self.page.wait_for_load_state(
                        "load", timeout=max(min(2.0, self.left()), 0.1) * 1000
                    )
        raise FetchError(
            ErrorCode.CONVERSION_FAILED,
            "The page kept navigating, so its content could not be read.",
            upstream_status=self.last_status(),
        )

    async def _screenshot(self) -> tuple[bytes, tuple[int, int]]:
        """PNG at 1920 wide: the full page up to ``SCREENSHOT_MAX_HEIGHT_PX`` (B7), or the
        viewport."""
        timeout = max(self.left(), 0.1) * 1000
        if not self.options.full_page:
            png = await self.page.screenshot(type="png", timeout=timeout)
            return png, png_size(png)
        limit = self.settings.screenshot_max_height_px
        height = int(await self.page.evaluate(PAGE_HEIGHT) or 0)
        if height > limit:
            self.warnings.append("screenshot_clipped")
        # A clip on a full-page capture is trimmed to the document: 1920 wide, at most `limit`.
        png = await self.page.screenshot(
            type="png",
            full_page=True,
            clip={"x": 0, "y": 0, "width": VIEWPORT["width"], "height": limit},
            timeout=timeout,
        )
        return png, png_size(png)

    async def _cookies(self) -> tuple[CookieData, ...]:
        """The cookies the request URL would get: its host's and its parent domains'."""
        cookies = await self.page.context.cookies([self.url])
        return tuple(
            CookieData(
                name=cookie.get("name", ""),
                value=cookie.get("value", ""),
                domain=cookie.get("domain", "").lstrip("."),
                path=cookie.get("path", "/"),
                secure=cookie.get("secure", False),
                http_only=cookie.get("httpOnly", False),
                expires=expires if (expires := cookie.get("expires", -1)) > 0 else None,
            )
            for cookie in cookies
        )

    def _rendered(
        self,
        html: str,
        screenshot: bytes | None,
        size: tuple[int, int] | None,
        cookies: tuple[CookieData, ...],
    ) -> Rendered:
        responses = self._main_responses()
        self._refused_document(responses)
        warnings = list(self.warnings)
        if self.traffic is not None and self.traffic.capped:
            warnings.append("browser_bytes_capped")
        return Rendered(
            html=html,
            final_url=responses[-1].url if responses else self.url,
            responses=tuple(responses),
            screenshot=screenshot,
            screenshot_size=size,
            warnings=tuple(dict.fromkeys(warnings)),
            hosts=self.traffic.hosts if self.traffic is not None else frozenset(),
            cookies=cookies,
        )

    def _main_responses(self) -> list[MainResponse]:
        """The document answers, without the redirects among them (their URLs become the
        ``redirects`` of the answer they led to)."""
        found: list[MainResponse] = []
        for answer in self.answers:
            request = answer.request
            if request.redirected_to is not None:
                continue
            hops: list[str] = []
            previous = request.redirected_from
            while previous is not None:
                hops.append(previous.url)
                previous = previous.redirected_from
            headers = answer.headers
            media, charset = _media_type(headers.get("content-type"))
            found.append(
                MainResponse(
                    url=answer.url,
                    status=answer.status,
                    headers=dict(headers),
                    content_type=media,
                    charset=charset,
                    redirects=tuple(reversed(hops)),
                )
            )
        return found

    def _refused_document(self, responses: list[MainResponse]) -> None:
        """A plain-HTTP document the proxy refused arrives as the proxy's own answer: 403 for
        a forbidden address, 502 for a name that does not resolve. Both refusals hold for every
        request to the host, so the answer is the refusal, not the target's status."""
        if not responses or self.traffic is None:
            return
        last = responses[-1]
        host = (urlsplit(last.url).hostname or "").lower()
        reasons = {reason for refused, reason in self.traffic.refused if refused == host}
        if (last.status, "blocked_address") in _PROXY_ANSWERS and "blocked_address" in reasons:
            raise _refusal_error({"blocked_address"})
        if (last.status, "dns_error") in _PROXY_ANSWERS and "dns_error" in reasons:
            raise _refusal_error({"dns_error"})

    # --- events

    def _on_response(self, response: Response) -> None:
        if _is_main_document(response.request, self.page) and (
            response.request not in self.unanswered
        ):
            self.answers.append(response)
            self.failure = None  # a later document arrived after all

    def _on_request_failed(self, request: Request) -> None:
        if not _is_main_document(request, self.page):
            return
        error = request.failure or ""
        if not any(error.startswith(code) for code in _NOT_FAILURES):
            self.failure = (request.url, error)

    def _on_download(self, _download: object) -> None:
        self.download = True

    async def _route(self, route: Route, request: Request) -> None:
        with contextlib.suppress(*DRIVER_ERRORS):  # the page may close meanwhile
            await self._decide(route, request)

    async def _decide(self, route: Route, request: Request) -> None:
        options = self.options
        frame = _frame_of(request)
        if (
            frame is not None
            and frame.parent_frame is None
            and request.resource_type == "document"
            and request.is_navigation_request()
        ):
            if frame.page is not self.page:  # a popup or a new tab: never loaded
                await route.abort("blockedbyclient")
                return
            if self.first_navigation:
                self.first_navigation = False
            elif request.method != "GET" and not options.unblock:
                # An empty answer keeps the current document (an abort would replace it with
                # an error page); it is not a document answer of the render.
                self.warnings.append("navigation_refused_non_get")
                self.unanswered.add(request)
                await route.fulfill(status=204)
                return
            elif not await self._checked(request.url):
                await route.abort("blockedbyclient")
                return
        elif request.resource_type in options.blocked_resources:
            await route.abort("blockedbyclient")
            return
        if options.headers and site(request.url) == options.header_host:
            headers = {
                name: value
                for name, value in (await request.all_headers()).items()
                if not name.startswith(":")
            }
            await route.continue_(headers={**headers, **options.headers})
            return
        await route.continue_()

    async def _checked(self, url: str) -> bool:
        """Run the navigation check; False (and the error kept) when it refuses."""

        async def check() -> None:
            await self.check(url)

        task = self.loop.create_task(check(), context=self.context)
        self.checks.add(task)
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            return False  # the render ended meanwhile
        except Exception as error:
            if self.refusal is None:
                self.refusal = error
            return False
        finally:
            self.checks.discard(task)
        return True

    # --- errors

    def _goto_error(self, error: Exception) -> BaseException:
        if isinstance(error, DRIVER_TIMEOUTS):
            return FetchError(
                ErrorCode.TIMEOUT,
                "The page did not finish loading in time.",
                retryable=True,
                upstream_status=self.last_status(),
            )
        url, text = self.failure if self.failure is not None else (self.url, str(error))
        return self._load_error(url, text)

    def _load_error(self, url: str, text: str) -> FetchError:
        """A document that did not load: the proxy's refusal when it refused its host, else
        what the browser's network error says."""
        host = (urlsplit(url).hostname or "").lower()
        status = self.last_status()
        if self.traffic is not None:
            reasons = {reason for refused, reason in self.traffic.refused if refused == host}
            if reasons:
                return _refusal_error(reasons, status)
        if "ERR_NAME_NOT_RESOLVED" in text:
            return FetchError(
                ErrorCode.DNS_ERROR,
                "The host name could not be resolved.",
                retryable=True,
                upstream_status=status,
            )
        if "ERR_TIMED_OUT" in text:
            return FetchError(
                ErrorCode.TIMEOUT,
                "The page did not finish loading in time.",
                retryable=True,
                upstream_status=status,
            )
        if "ERR_TOO_MANY_REDIRECTS" in text:
            return FetchError(
                ErrorCode.TARGET_HTTP_ERROR,
                "The target redirected too many times.",
                upstream_status=status,
            )
        return FetchError(
            ErrorCode.CONNECTION_ERROR,
            "The page could not be loaded.",
            retryable=True,
            upstream_status=status,
        )


def _refusal_error(reasons: set[str], status: int | None = None) -> FetchError:
    """The proxy's refusal of a document's host as the error fast mode would give."""
    if "blocked_address" in reasons:
        return FetchError(
            ErrorCode.BLOCKED_URL, "The target address is not allowed.", upstream_status=status
        )
    if "bytes_cap" in reasons:
        return FetchError(
            ErrorCode.TOO_LARGE,
            "The page needed more bytes than the limit.",
            upstream_status=status,
        )
    if "dns_error" in reasons:
        return FetchError(
            ErrorCode.DNS_ERROR,
            "The host name could not be resolved.",
            retryable=True,
            upstream_status=status,
        )
    return FetchError(
        ErrorCode.CONNECTION_ERROR,
        "The connection to the target failed.",
        retryable=True,
        upstream_status=status,
    )


def _frame_of(request: Request) -> Frame | None:
    try:
        return request.frame
    except DRIVER_ERRORS:  # a service worker's request has no frame
        return None


def _is_main_document(request: Request, page: Page) -> bool:
    frame = _frame_of(request)
    return (
        frame is not None
        and frame is page.main_frame
        and request.resource_type == "document"
        and request.is_navigation_request()
    )


def _media_type(content_type: str | None) -> tuple[str | None, str | None]:
    """``text/html; charset=UTF-8`` to (``text/html``, ``utf-8``)."""
    if not content_type:
        return None, None
    media, *params = content_type.split(";")
    charset = None
    for param in params:
        name, _, value = param.partition("=")
        if name.strip().lower() == "charset":
            charset = value.strip().strip('"').lower() or None
    return media.strip().lower() or None, charset
