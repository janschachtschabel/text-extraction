"""What the browser modules share about the two drivers: error classes, consent modes and
scripts that must run in the page's own JavaScript world.

Playwright and Patchright (its fork) have the same API but separate classes, so code that
runs on either catches both. Patchright evaluates in an isolated world by default (the page
cannot see our scripts there, nor we its globals); ``evaluate_in_main_world`` runs a script
where the page's own scripts run, with either driver.
"""

from typing import Any, Final, Literal

from patchright.async_api import Error as PatchrightError
from patchright.async_api import Frame as PatchrightFrame
from patchright.async_api import Page as PatchrightPage
from patchright.async_api import TimeoutError as PatchrightTimeout
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

type ConsentOpt = Literal["optOut", "optIn"]
# What a consent run found: a banner it handled, no banner, or a banner it could not handle
# (or did not finish in time).
type ConsentResult = Literal["done", "nothing", "failed"]

DRIVER_ERRORS: Final[tuple[type[Exception], ...]] = (PlaywrightError, PatchrightError)
DRIVER_TIMEOUTS: Final[tuple[type[Exception], ...]] = (PlaywrightTimeout, PatchrightTimeout)


async def evaluate_in_main_world(target: Page | Frame, expression: str, arg: Any = None) -> Any:
    """``target.evaluate(expression, arg)`` in the page's main world: the caller's ``evaluate``
    action needs the page's globals, and autoconsent's rules run there (``isMainWorld``)."""
    if isinstance(target, PatchrightPage | PatchrightFrame):
        patchright: Any = target
        return await patchright.evaluate(expression, arg, isolated_context=False)
    return await target.evaluate(expression, arg)
