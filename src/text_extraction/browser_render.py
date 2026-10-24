"""The steps of one browser render, shared by the standard and the unblock renderer.

``render_page`` loads a page in the steps of B2 within one deadline: ``goto`` until ``load``,
up to ``BROWSER_IDLE_WAIT_SECONDS`` for the network to go idle, the consent opt-out (B11,
``run_consent``; the ``accept_cookies`` action opts in instead), the request's actions
(``browser_actions.run_actions``), then hidden elements are removed (``strip_hidden``) and the
DOM is read; the screenshot comes last. Every request passes the route handler: a main-frame
navigation after the first is sent only after the pipeline's ``NavigationCheck`` (a non-GET one
not at all), blocked resource types are not loaded, and the caller's headers go only to the
request URL's site, also across redirects (``_Render._with_headers``). Speculation rules are
removed or refused (``NO_SPECULATION``), and prefetch requests are refused (``_is_speculative``):
a document they fetch would be used without passing the handler.

The module uses only the Playwright API, which Patchright shares; errors of both drivers are
caught (``DRIVER_ERRORS``). Logs carry hosts and codes only, never a URL, header or cookie.
"""

import asyncio
import contextlib
import contextvars
import json
import re
import struct
from typing import Final
from urllib.parse import urljoin, urlsplit

import structlog
from playwright.async_api import APIResponse, Frame, Page, Request, Response, Route

from text_extraction.browser_actions import run_actions
from text_extraction.browser_driver import (
    DRIVER_ERRORS,
    DRIVER_TIMEOUTS,
    ConsentOpt,
    ConsentResult,
)
from text_extraction.browser_options import AcceptCookiesAction, Action
from text_extraction.browser_proxy import ProxyContext
from text_extraction.browser_scripts import (
    CONSENT_RELAY,
    CONSENT_WAIT,
    NO_SPECULATION,
    PAGE_HEIGHT,
    STRIP_HIDDEN,
    consent_bundle,
    consent_config,
    consent_rules_json,
)
from text_extraction.envelope import ErrorCode
from text_extraction.fetch import FetchError
from text_extraction.render import (
    CookieData,
    MainResponse,
    NavigationCheck,
    Rendered,
    RenderOptions,
    site,
)
from text_extraction.settings import Settings

_log = structlog.get_logger(__name__)

VIEWPORT: Final = {"width": 1920, "height": 1080}
CONSENT_SECONDS: Final = 3.0  # B11: best effort
# Of a render's deadline: what reading the DOM (and taking the screenshot) needs after the waits.
_READ_RESERVE_SECONDS: Final = 1.0
_SCREENSHOT_RESERVE_SECONDS: Final = 3.0
_CONTENT_ATTEMPTS: Final = 3
# The proxy's answers to a plain-HTTP request whose host it refuses outright.
_PROXY_ANSWERS: Final = frozenset({(403, "blocked_address"), (502, "dns_error")})
# Main-frame load failures that are not the target's: a navigation that a newer one replaced,
# or one the route handler aborted (its reason is kept separately).
_NOT_FAILURES: Final = ("net::ERR_ABORTED", "net::ERR_BLOCKED_BY_CLIENT")
# Redirects of a document that carries the caller's headers, followed one by one (as fetch does).
_MAX_HOPS: Final = 10
# Not passed on from a response fetched for the browser: its body arrives decoded and whole.
_NOT_PASSED: Final = frozenset({"content-encoding", "content-length", "transfer-encoding"})
# Values of the ``Sec-Purpose`` request header (``prefetch;anonymous-client-ip`` and the like).
_SPECULATIVE_PURPOSES: Final = ("prefetch", "prerender")
# The strings of a structured-field list, as in ``Speculation-Rules: "/a.json", "/b.json"``.
_SF_STRING: Final = re.compile(r'"((?:[^"\\]|\\.)*)"')


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
    try:
        async with asyncio.timeout_at(deadline):
            await render.install()
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


async def run_consent(
    page: Page, opt: ConsentOpt, *, seconds: float = CONSENT_SECONDS
) -> ConsentResult:
    """Run autoconsent with ``opt`` in the page's frames (B11), for at most ``seconds``.
    Best effort, never an error. ``done``: a banner was handled in any frame; ``nothing``: no
    frame has a banner; ``failed``: a banner was found but not handled, or the time ran out."""
    if seconds <= 0:
        return "failed"
    frames = [frame for frame in page.frames if not frame.is_detached()][:10]
    args = {"rulesJson": consent_rules_json(), "config": consent_config(opt)}
    # The page's own wait ends a little earlier, so its outcome still comes back in time.
    milliseconds = round(max(seconds - 0.2, 0.0) * 1000)

    async def in_frame(frame: Frame) -> ConsentResult:
        try:
            installed = await frame.evaluate(CONSENT_RELAY, args)
            if installed:
                await frame.evaluate(consent_bundle())
            outcome = await frame.evaluate(
                CONSENT_WAIT, {"opt": opt, "timeoutMs": milliseconds, "again": not installed}
            )
        except DRIVER_ERRORS:
            return "nothing"  # the frame went away meanwhile
        if outcome == "done":
            return "done"
        return "nothing" if outcome in ("nothingDetected", "none") else "failed"

    try:
        async with asyncio.timeout(seconds):
            results = await asyncio.gather(*(in_frame(frame) for frame in frames))
    except TimeoutError:
        return "failed"
    if "done" in results:
        return "done"
    return "failed" if "failed" in results else "nothing"


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
        # Main-frame document responses in order, each with the redirects turned into navigations
        # that led to it.
        self.answers: list[tuple[Response, tuple[str, ...]]] = []
        self.warnings: list[str] = []
        self.refusal: BaseException | None = None  # of the navigation check
        self.fault: FetchError | None = None  # the route handler's own failure
        self.failure: tuple[str, str] | None = None  # (URL, error) of a failed document load
        self.download = False
        # Navigations answered by the route handler itself (a refused non-GET one, a redirect
        # step of a document with caller headers, one it aborted): not document answers or
        # load failures of the render.
        self.unanswered: set[Request] = set()
        # Speculation rules files named by documents' ``Speculation-Rules`` headers: refused.
        self.rule_files: set[str] = set()
        # URLs whose redirects were turned into navigations, for the next document answer.
        self.hops: list[str] = []
        self.hop_count = 0
        self.settled = asyncio.Event()  # cleared while such a redirect is under way
        self.settled.set()
        self.first_navigation = True
        self.acting = False  # the request's actions are running
        self.deadline = 0.0
        self.checks: set[asyncio.Task[None]] = set()

    async def install(self) -> None:
        self.page.on("response", self._on_response)
        self.page.on("requestfailed", self._on_request_failed)
        self.page.on("download", self._on_download)
        await self.page.add_init_script(NO_SPECULATION)
        await self.page.context.route("**/*", self._route)

    async def end_checks(self) -> None:
        """Cancel the navigation checks still running (their turn waits) and wait for them."""
        for task in self.checks:
            task.cancel()
        await asyncio.gather(*self.checks, return_exceptions=True)

    def raise_kept(self) -> None:
        """End the render with what went wrong in the page meanwhile: the check's refusal of a
        navigation, a failure of the route handler, or a document that did not load."""
        if self.refusal is not None:
            raise self.refusal
        if self.fault is not None:
            raise self.fault
        if self.failure is not None:
            raise self._load_error(*self.failure)

    def last_status(self) -> int | None:
        return self.answers[-1][0].status if self.answers else None

    def left(self, reserve: float = 0.0) -> float:
        return self.deadline - self.loop.time() - reserve

    async def run(self, deadline: float) -> Rendered:
        self.deadline = deadline
        reserve = _SCREENSHOT_RESERVE_SECONDS if self.options.screenshot else _READ_RESERVE_SECONDS
        await self._goto(reserve)
        if self.download:
            return self._rendered("", None, None, ())
        await self._idle(reserve)
        self.raise_kept()
        actions = self.options.actions
        # The accept_cookies action is the opt-in, which an earlier opt-out would make moot.
        opts_in = any(isinstance(action, AcceptCookiesAction) for action in actions)
        if self.settings.browser_autoconsent and not opts_in:
            await run_consent(self.page, "optOut", seconds=min(CONSENT_SECONDS, self.left(reserve)))
            self.raise_kept()
        if actions:
            await self._run_actions(actions, reserve)
            if self.download:
                return self._rendered("", None, None, ())
        await strip_hidden(self.page)
        html = await self._content()
        screenshot, size = await self._screenshot() if self.options.screenshot else (None, None)
        self.raise_kept()
        return self._rendered(html, screenshot, size, await self._cookies())

    async def _idle(self, reserve: float) -> None:
        """Wait up to ``BROWSER_IDLE_WAIT_SECONDS`` for the network to go idle."""
        idle = min(self.settings.browser_idle_wait_seconds, self.left(reserve))
        if idle > 0:
            with contextlib.suppress(*DRIVER_ERRORS):
                await self.page.wait_for_load_state("networkidle", timeout=idle * 1000)

    async def _run_actions(self, actions: tuple[Action, ...], reserve: float) -> None:
        """Run the request's actions (before the reserve for reading the page), then give what
        they loaded a moment. A navigation the check refused meanwhile failed its action: the
        page stays as it was, so the refusal is no error of the render."""
        self.acting = True
        try:
            self.warnings.extend(
                await run_actions(
                    self.page,
                    actions,
                    allow_evaluate=self.options.allow_evaluate,
                    deadline=self.deadline - reserve,
                    consent=run_consent,
                    refused=lambda: self.refusal is not None,
                )
            )
        finally:
            self.acting = False
        self.refusal = None
        self.settled.set()
        self.raise_kept()
        await self._idle(reserve)
        self.raise_kept()

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
        await self._settle(reserve)
        self.raise_kept()

    async def _settle(self, reserve: float = 0.0) -> None:
        """Wait until a redirect turned into a navigation has reached its document."""
        if self.settled.is_set():
            return
        try:
            async with asyncio.timeout(max(self.left(reserve), 0.0)):
                await self.settled.wait()
        except TimeoutError:
            raise FetchError(
                ErrorCode.TIMEOUT,
                "The page did not finish loading in time.",
                retryable=True,
                upstream_status=self.last_status(),
            ) from None
        with contextlib.suppress(*DRIVER_ERRORS):
            await self.page.wait_for_load_state("load", timeout=max(self.left(reserve), 0.1) * 1000)

    async def _content(self) -> str:
        await self._settle()
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
        for answer, before in self.answers:
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
                    redirects=(*before, *reversed(hops)),
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
        if response.request.resource_type == "document":
            self._note_rule_files(response)
        if _is_main_document(response.request, self.page) and (
            response.request not in self.unanswered
        ):
            self.answers.append((response, tuple(self.hops)))
            self.hops.clear()
            self.failure = None  # a later document arrived after all
            self.settled.set()

    def _note_rule_files(self, response: Response) -> None:
        header = response.headers.get("speculation-rules")
        if header:
            for found in _SF_STRING.finditer(header):
                file = urljoin(response.url, found.group(1)).partition("#")[0]
                self.rule_files.add(file)

    def _on_request_failed(self, request: Request) -> None:
        if request in self.unanswered or not _is_main_document(request, self.page):
            return
        error = request.failure or ""
        if not any(error.startswith(code) for code in _NOT_FAILURES):
            self.failure = (request.url, error)
            self.settled.set()

    def _on_download(self, _download: object) -> None:
        self.download = True

    async def _route(self, route: Route, request: Request) -> None:
        try:
            await self._decide(route, request)
        except DRIVER_ERRORS:  # the page may close meanwhile
            pass
        except Exception as error:
            # A fault of ours: end the request (it would hang until the deadline) and the render.
            _log.error("browser_route_failed", error=type(error).__name__)
            if self.fault is None:
                self.fault = FetchError(
                    ErrorCode.INTERNAL_ERROR,
                    "The browser failed. Try again.",
                    retryable=True,
                    upstream_status=self.last_status(),
                )
            self.unanswered.add(request)
            self.settled.set()
            with contextlib.suppress(*DRIVER_ERRORS):
                await route.abort("failed")

    async def _decide(self, route: Route, request: Request) -> None:
        options = self.options
        if _is_speculative(request):
            await route.abort("blockedbyclient")
            return
        frame = _frame_of(request)
        if (
            frame is not None
            and frame.parent_frame is None
            and request.resource_type == "document"
            and request.is_navigation_request()
        ):
            main = frame.page is self.page
            if not main:  # a popup or a new tab: never loaded
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
                if self.acting:
                    # A navigation of an action: the page stays as it is (an abort would
                    # replace its document with an error page); the action fails.
                    self.unanswered.add(request)
                    await route.fulfill(status=204)
                else:
                    await route.abort("blockedbyclient")
                return
        elif request.resource_type in options.blocked_resources or request.url in self.rule_files:
            await route.abort("blockedbyclient")
            return
        else:
            main = False
        if options.headers and options.header_host and site(request.url) == options.header_host:
            await self._with_headers(route, request, main=main)
            return
        await route.continue_()

    async def _with_headers(self, route: Route, request: Request, *, main: bool) -> None:
        """Send a request on the caller's site with the caller's headers, without letting them
        follow a redirect elsewhere.

        A header override of ``route.continue_`` would go along with every redirect hop of the
        request, whatever its host. So the request is fetched through the page's own proxy
        context (``route.fetch``, redirects not followed) and its answer handed to the browser.
        A redirect of the main document becomes a navigation to its target, which the route
        handler sees like any other: checked first, with the headers only on the caller's site.
        Any other redirect is handed over as it is; the browser follows it without the headers.
        """
        headers = {
            name: value
            for name, value in (await request.all_headers()).items()
            if not name.startswith(":")
        }
        try:
            answer = await route.fetch(
                headers={**headers, **self.options.headers},
                max_redirects=0,
                timeout=max(self.left(), 0.1) * 1000,
            )
        except DRIVER_TIMEOUTS:
            await route.abort("timedout")
            return
        except DRIVER_ERRORS:  # refused by the proxy, or the target did not answer
            await route.abort("failed")
            return
        target = _redirect_target(request.url, answer) if main else None
        if target is None:
            await route.fulfill(
                response=answer,
                headers={k: v for k, v in answer.headers.items() if k not in _NOT_PASSED},
            )
            return
        self.unanswered.add(request)
        if self.hop_count >= _MAX_HOPS:
            self.failure = (request.url, "net::ERR_TOO_MANY_REDIRECTS")
            self.settled.set()
            await route.abort("failed")
            return
        self.hop_count += 1
        self.hops.append(request.url)
        self.settled.clear()
        await route.fulfill(status=200, content_type="text/html", body=_hop_page(target))

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
            self.settled.set()
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


def _redirect_target(url: str, answer: APIResponse) -> str | None:
    """The absolute http(s) URL a 3xx answer redirects to; None for any other answer."""
    location = answer.headers.get("location")
    if not 300 <= answer.status < 400 or not location:
        return None
    target = urljoin(url, location)
    return target if urlsplit(target).scheme in ("http", "https") else None


def _hop_page(target: str) -> str:
    """A document that goes on to ``target`` at once, replacing itself in the history."""
    literal = json.dumps(target).replace("<", "\\u003c").replace(">", "\\u003e")
    return f"<!doctype html><meta charset='utf-8'><script>location.replace({literal});</script>"


def _is_speculative(request: Request) -> bool:
    """A prefetch or prerender, or the fetch of a speculation rules file. Such a document is
    fetched outside the page's navigations, and a later navigation would use it without passing
    the navigation check. ``NO_SPECULATION`` and the refusal of ``Speculation-Rules`` header
    files stop most; this is the net under them (it does not depend on the order of events)."""
    headers = request.headers
    purpose = f"{headers.get('sec-purpose', '')} {headers.get('purpose', '')}".lower()
    return (
        any(part.startswith(_SPECULATIVE_PURPOSES) for part in purpose.split())
        or "application/speculationrules+json" in headers.get("accept", "").lower()
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
