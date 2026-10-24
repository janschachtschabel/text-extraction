"""The page actions of a render (the request's ``actions``), run after the consent step.

``run_actions`` runs them in order, each bounded by the time left to ``deadline``. The first
action that fails (no such element within ``ELEMENT_WAIT_SECONDS``, the deadline, a script
error, a navigation the route check refused, a cookie banner that could not be handled) stops the
rest and adds the warning ``action_<index>_failed``: the page is extracted as it is then.
A click that navigates is checked by the route handler like any navigation; the action waits
for the new document's ``load``.

Action text, values and scripts are secrets (``browser_options``): nothing here logs them, or
a selector. The module uses only the Playwright API, which Patchright shares.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Final, Protocol

import structlog
from playwright.async_api import Page

from text_extraction.browser_driver import (
    DRIVER_ERRORS,
    ConsentOpt,
    ConsentResult,
    evaluate_in_main_world,
)
from text_extraction.browser_options import (
    AcceptCookiesAction,
    Action,
    ClickAction,
    EvaluateAction,
    ExpandAllAction,
    InfiniteScrollAction,
    PressAction,
    ScrollAction,
    SelectAction,
    TypeAction,
    WaitForAction,
)
from text_extraction.browser_scripts import (
    DOCUMENT_MARKED,
    EXPAND_ALL,
    MARK_DOCUMENT,
    PAGE_HEIGHT,
    SCROLL_TO_END,
    SCROLL_VIEWPORT,
)

_log = structlog.get_logger(__name__)

# How long a locator waits for its element to show up (and to be clickable) before the action
# fails; the time left of the render caps it.
ELEMENT_WAIT_SECONDS: Final = 5.0
CONSENT_SECONDS: Final = 3.0  # as the consent step at the start of a render (B11)
EXPAND_LIMIT: Final = 50  # of elements with aria-expanded="false" that expand_all clicks
_EXPAND_SETTLE_SECONDS: Final = 0.2  # for what the clicks reveal to be drawn


class _Failed(Exception):  # noqa: N818 - an action's failure, not a bug
    """The action could not do what it was asked to (no time left, a banner not handled)."""


# Actions that can start a navigation (the route handler sees it a moment later).
_MAY_NAVIGATE: Final = frozenset(
    {"click", "press", "select", "expand_all", "accept_cookies", "evaluate"}
)
_FAILURES: Final[tuple[type[Exception], ...]] = (TimeoutError, _Failed, *DRIVER_ERRORS)


class ConsentRunner(Protocol):
    """``browser_render.run_consent``: handles the page's cookie banner, if there is one."""

    async def __call__(self, page: Page, opt: ConsentOpt, *, seconds: float) -> ConsentResult: ...


async def _no_navigation(may_navigate: bool) -> None:
    pass


async def run_actions(
    page: Page,
    actions: Sequence[Action],
    *,
    allow_evaluate: bool,
    deadline: float,
    consent: ConsentRunner,
    refused: Callable[[], bool] = lambda: False,
    settle: Callable[[bool], Awaitable[None]] = _no_navigation,
) -> list[str]:
    """Run ``actions`` in ``page`` before ``deadline`` (``loop.time()``) and return the
    warnings (``action_<index>_failed``, the index counting from 0). ``refused`` tells that a
    navigation was refused meanwhile (by the route check, which the render keeps, or because it
    was not a GET); that fails the action that started it. ``settle(may_navigate)`` waits for
    the navigations an action started to reach a document or be refused, so a slow check does
    not end with the action's own time limit.

    Raises ``ValueError`` when an ``evaluate`` action comes without ``allow_evaluate``: the API
    refuses those requests with 403, so this is a guard against a caller's mistake, and
    nothing has run by then."""
    if not allow_evaluate and any(isinstance(action, EvaluateAction) for action in actions):
        raise ValueError("an evaluate action needs allow_evaluate")
    warnings: list[str] = []
    for index, action in enumerate(actions):
        try:
            async with asyncio.timeout_at(deadline):
                # expand_all must not take the reader to another page: it fails then.
                if isinstance(action, ExpandAllAction):
                    await page.evaluate(MARK_DOCUMENT)
                await _run(page, action, deadline, consent)
                await settle(action.type in _MAY_NAVIGATE)
                await _loaded(page, deadline)
                if isinstance(action, ExpandAllAction) and not await page.evaluate(DOCUMENT_MARKED):
                    raise _Failed
            if refused():
                raise _Failed
        except _FAILURES as error:
            # Only the kind of failure is logged: the message could carry a selector or text.
            _log.info(
                "browser_action_failed",
                index=index,
                action=action.type,
                error=type(error).__name__,
            )
            warnings.append(f"action_{index}_failed")
            break
    return warnings


async def _run(page: Page, action: Action, deadline: float, consent: ConsentRunner) -> None:
    loop = asyncio.get_running_loop()

    def wait_ms() -> float:
        """For a driver call: the element wait, or less when the deadline is near. Never 0
        (the drivers read that as no limit)."""
        return max(min(ELEMENT_WAIT_SECONDS, deadline - loop.time()), 0.001) * 1000

    async def pause(milliseconds: int) -> None:
        await asyncio.sleep(milliseconds / 1000)

    match action:
        case ClickAction(selector=selector):
            # Not waiting after the action: the time of a navigation it starts is not the
            # element wait's (``settle`` waits for it, until the deadline).
            await page.locator(selector).first.click(timeout=wait_ms(), no_wait_after=True)
        case TypeAction(selector=selector, text=text):
            field = page.locator(selector).first
            await field.fill("", timeout=wait_ms())
            await field.press_sequentially(text, timeout=wait_ms(), no_wait_after=True)
        case PressAction(key=key, selector=str() as selector):
            await page.locator(selector).first.press(key, timeout=wait_ms(), no_wait_after=True)
        case PressAction(key=key):
            await page.keyboard.press(key)
        case SelectAction(selector=selector, value=value):
            await page.locator(selector).first.select_option(
                value, timeout=wait_ms(), no_wait_after=True
            )
        case WaitForAction(selector=str() as selector):
            await page.locator(selector).first.wait_for(timeout=wait_ms())
        case WaitForAction(ms=int() as milliseconds):
            if milliseconds / 1000 > deadline - loop.time():
                raise _Failed
            await pause(milliseconds)
        case WaitForAction():
            raise _Failed  # neither (the model refuses this)
        case ScrollAction(times=times, pause_ms=pause_ms):
            for _ in range(times):
                await page.evaluate(SCROLL_VIEWPORT)
                await pause(pause_ms)
        case InfiniteScrollAction(max_scrolls=max_scrolls, pause_ms=pause_ms):
            height = int(await page.evaluate(PAGE_HEIGHT) or 0)
            for _ in range(max_scrolls):
                await page.evaluate(SCROLL_TO_END)
                await pause(pause_ms)
                grown = int(await page.evaluate(PAGE_HEIGHT) or 0)
                if grown <= height:
                    break
                height = grown
        case ExpandAllAction():
            if await page.evaluate(EXPAND_ALL, EXPAND_LIMIT):
                await asyncio.sleep(_EXPAND_SETTLE_SECONDS)
        case AcceptCookiesAction():
            seconds = min(CONSENT_SECONDS, deadline - loop.time())
            # No banner is nothing to accept; one that could not be handled is a failure.
            if await consent(page, "optIn", seconds=seconds) == "failed":
                raise _Failed
        case EvaluateAction(script=script):
            # A function body, so the script may hold statements, `await` and `return`. It runs
            # where the page's own scripts run, so it sees their globals.
            await evaluate_in_main_world(page, f"async () => {{\n{script}\n}}")


async def _loaded(page: Page, deadline: float) -> None:
    """Wait for a navigation the action started to reach ``load``; at once when there is none."""
    left = deadline - asyncio.get_running_loop().time()
    await page.wait_for_load_state("load", timeout=max(left, 0.001) * 1000)
