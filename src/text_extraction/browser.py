"""Chromium for the browser worker: the page pool, the launcher and the standard renderer.

Only the browser worker imports this module (it needs the extra ``browser``). See
docs/architecture.md, "Browser worker", and the rulings B2-B4 and B8 of package 4. The steps of
a render itself are in ``browser_render.py``.

**Pool.** ``BrowserPool`` keeps one browser per driver per process, behind the egress proxy
(``browser_proxy.py``) with the B4 launch flags. ``start`` launches it, later launches happen on
the next page: after ``BROWSER_RECYCLE_AFTER_PAGES`` pages (the old browser is closed once its
pages end) and after the browser went away (crashed, killed). The pages that were in flight then
fail alone; the renderer turns that into ``internal_error``, retryable. A semaphore lets
``BROWSER_CONCURRENCY`` pages run at once; pools of both drivers can share one (``slots``).
Each page gets its own browser context with its own proxy credentials, so the proxy counts and
caps its bytes, and the context and its proxy context are always closed with the page. Closing
never holds a render past its deadline: a page waits ``_CLOSE_WAIT_SECONDS`` for its context,
then the close goes on in the background (bounded by ``CLOSE_SECONDS``), as does closing a
recycled browser; ``stop`` waits for them.

**Launcher.** What differs between the drivers (Playwright here, Patchright for unblock mode)
is how a browser is launched and how a page's context is opened: a ``Launcher``. The pool, the
recycling and the crash handling are shared. A ``persistent`` launcher (Patchright's) opens
each page in a persistent context, which is its own browser: the pool then launches one for
every page (launch reason ``page``), outside its lock so launches run side by side, and a page
whose browser goes away fails alone as before. Pools of both drivers share one semaphore, so
``BROWSER_CONCURRENCY`` caps the pages of both.

**Renderer.** ``ChromiumRenderer`` takes a page from the pool and runs ``render_page`` in it;
the unblock renderer (``browser_unblock.py``) is one with other options and a step after the
load.

Logs carry hosts and codes only, never a URL, header, cookie or script.
"""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal, Protocol
from weakref import WeakKeyDictionary

import structlog
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from text_extraction.browser_driver import DRIVER_ERRORS
from text_extraction.browser_proxy import EgressProxy, ProxyContext
from text_extraction.browser_render import VIEWPORT, AfterLoad, render_page
from text_extraction.envelope import ErrorCode
from text_extraction.fetch import USER_AGENT, FetchError
from text_extraction.render import (
    CookieData,
    NavigationCheck,
    Rendered,
    RenderOptions,
)
from text_extraction.settings import Settings

if TYPE_CHECKING:
    from playwright._impl._api_structures import SetCookieParam

_log = structlog.get_logger(__name__)

type Driver = Literal["playwright", "patchright"]

_RENDER_ERRORS: Final[tuple[type[Exception], ...]] = (*DRIVER_ERRORS, FetchError)
LOCALE: Final = "de-DE"
CLOSE_SECONDS: Final = 5.0  # closing a context or a browser
# Of a render's deadline: what closing the page may take. A page waits a little less for its
# context to close; a longer close finishes in the background.
_CLOSE_RESERVE_SECONDS: Final = 0.5
_CLOSE_WAIT_SECONDS: Final = 0.4
_RENDER_BUCKETS: Final = (0.5, 1, 2, 3, 5, 8, 10, 15, 20, 30, 45, 60, 90)


def chromium_args(proxy_url: str) -> list[str]:
    """The B4 launch flags: everything through the proxy (loopback too), no QUIC, no WebRTC
    UDP outside the proxy, no /dev/shm, and no DNS prefetching (with a fixed proxy Chromium
    resolves no names itself, as probed; the flag keeps it so)."""
    return [
        f"--proxy-server={proxy_url}",
        "--proxy-bypass-list=<-loopback>",
        "--disable-quic",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-dev-shm-usage",
        "--dns-prefetch-disable",
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
    # Every context is a browser of its own (``open`` always returns None as the browser).
    persistent: bool

    async def open(
        self, browser: Browser | None, proxy: PageProxy, options: RenderOptions
    ) -> tuple[Browser | None, BrowserContext]:
        """A new context for one page, in ``browser``; when it is None, a browser is launched
        first (behind ``proxy.server``) and returned with the context. A launcher whose
        contexts own their browser (a persistent context) returns None: the pool then shares
        nothing and launches for every page."""
        ...

    async def release(self, context: BrowserContext) -> None:
        """Called once the pool closed ``context`` (or gave up closing it): free what the
        launcher keeps for it (a persistent context's profile)."""
        ...

    async def stop(self) -> None:
        """Stop the driver; the pool closed its browsers before."""
        ...


class PlaywrightLauncher:
    """Headless Chromium through Playwright, the standard mode's browser (B3, B4, B8)."""

    driver: Driver = "playwright"
    persistent = False

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._driver: Playwright | None = None
        self.user_agent = ""

    async def open(
        self, browser: Browser | None, proxy: PageProxy, options: RenderOptions
    ) -> tuple[Browser | None, BrowserContext]:
        launched = browser is None
        if browser is None:
            browser = await self._launch(proxy.server)
        try:
            context = await browser.new_context(
                viewport={"width": VIEWPORT["width"], "height": VIEWPORT["height"]},
                service_workers="block",
                accept_downloads=False,
                locale=LOCALE,
                user_agent=self.user_agent,
                proxy={
                    "server": proxy.server,
                    "username": proxy.username,
                    "password": proxy.password,
                },
            )
        except BaseException:
            if launched:  # nobody else holds it
                await _close_quietly(browser)
            raise
        return browser, context

    async def release(self, context: BrowserContext) -> None:
        pass

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
            try:
                # B8: Chromium's own user agent, with our product token and contact added.
                session = await browser.new_browser_cdp_session()
                version = await session.send("Browser.getVersion")
                await session.detach()
            except BaseException:
                await _close_quietly(browser)
                raise
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
            "Browser launches by reason: start, recycle, crash, page (one per page).",
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
        """Whether the page's renderer or browser went away while it was open (a page the
        pool did not close is closed: its browser went away, and the events may still come)."""
        return (
            self._crashed
            or (self._slot is not None and self._slot.dead)
            or (not self._closing and self.page.is_closed())
        )

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
        # Closes that go on alone, and launches whose caller went away (closed when done).
        self._background: set[asyncio.Future[None]] = set()
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
            await self._close_context(context)
        finally:
            traffic.close()

    async def stop(self) -> None:
        """Close every browser and stop the driver."""
        self._stopped = True
        async with self._lock:
            self._current = None
            for slot in list(self._open):
                await self._close_browser(slot)
        await asyncio.gather(*self._background, return_exceptions=True)
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
                try:
                    if context is not None:
                        # A close that takes longer goes on alone (asyncio.wait never cancels).
                        closing = self._in_background(self._close_context(context))
                        await asyncio.wait({closing}, timeout=_CLOSE_WAIT_SECONDS)
                finally:
                    traffic.close()
                    if slot is not None:
                        self._release(slot)
                    self._metrics.in_use.labels(driver=driver).dec()
                    self._metrics.pages.labels(driver=driver, outcome=outcome).inc()
                    self._metrics.seconds.labels(driver=driver).observe(
                        time.perf_counter() - started
                    )

    def _page_proxy(self, traffic: ProxyContext) -> PageProxy:
        return PageProxy(self._proxy.url, traffic.username, traffic.password)

    async def _context(
        self, proxy: PageProxy, options: RenderOptions
    ) -> tuple[_Slot | None, BrowserContext]:
        """A context in the current browser, or in a new one when there is none (yet), when
        it was recycled or when it went away. A persistent launcher's context brings its own
        browser: nothing is shared, so nothing waits for the lock."""
        if self._launcher.persistent:
            return await self._launch(proxy, options, "page")
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
                if new is not None:
                    self._take(new)
                if slot is not None and not slot.dead and slot.in_flight == 0:
                    self._close_later(slot)
                return new, context
            self._take(slot)
        try:
            _, context = await self._launcher.open(slot.browser, proxy, options)
        except BaseException:
            self._release(slot)
            raise
        return slot, context

    async def _launch(
        self, proxy: PageProxy, options: RenderOptions, reason: str
    ) -> tuple[_Slot | None, BrowserContext]:
        # A launch the caller gives up on (its deadline passed) still finishes, and its browser
        # is closed then: a cancelled launch would leave a Chromium nobody holds.
        launch = asyncio.ensure_future(self._launcher.open(None, proxy, options))
        try:
            browser, context = await asyncio.shield(launch)
        except asyncio.CancelledError:
            self._in_background(self._discard_launch(launch))
            raise
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

    def _release(self, slot: _Slot) -> None:
        slot.in_flight -= 1
        if slot.in_flight == 0 and slot is not self._current:
            self._close_later(slot)

    def _in_background(self, work: Awaitable[None]) -> "asyncio.Future[None]":
        """Run ``work`` on its own, held until it is done; ``stop`` waits for it."""
        task = asyncio.ensure_future(work)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def _close_later(self, slot: _Slot) -> None:
        """Close a browser without waiting for it: a page is ending, or a new one launched."""
        if slot.closing:
            return
        slot.closing = True
        self._open.discard(slot)
        self._in_background(_close_quietly(slot.browser))

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
        await _close_quietly(slot.browser)

    async def _close_context(self, context: BrowserContext) -> None:
        """Close a page's context, then let the launcher free what it kept for it."""
        try:
            await _close_context(context)
        finally:
            await self._launcher.release(context)

    async def _discard_launch(
        self, launch: "asyncio.Future[tuple[Browser | None, BrowserContext]]"
    ) -> None:
        """Close what a launch nobody waits for any more opened, once it is done."""
        try:
            browser, context = await launch
        except BaseException:
            return
        await self._close_context(context)
        if browser is not None:
            await _close_quietly(browser)


async def _close_quietly(browser: Browser) -> None:
    with contextlib.suppress(*DRIVER_ERRORS, TimeoutError):
        async with asyncio.timeout(CLOSE_SECONDS):
            await browser.close()


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
    """The standard mode's ``Renderer``: a page from the pool, loaded by ``render_page``.

    A subclass may change the options of every render (``_options``) and add a step after the
    load (``_after_load``)."""

    def __init__(self, pool: BrowserPool, settings: Settings) -> None:
        self._pool = pool
        self._settings = settings

    @property
    def user_agent(self) -> str:
        return self._pool.user_agent

    def _options(self, options: RenderOptions) -> RenderOptions:
        return options

    def _after_load(self) -> AfterLoad | None:
        return None

    async def render(
        self, url: str, check: NavigationCheck, options: RenderOptions, *, deadline: float
    ) -> Rendered:
        options = self._options(options)
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
                        after_load=self._after_load(),
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
