"""What the browser modules share about the two drivers: error classes and consent modes.

Playwright and Patchright (its fork) have the same API but separate classes, so code that
runs on either catches both.
"""

from typing import Final, Literal

from patchright.async_api import Error as PatchrightError
from patchright.async_api import TimeoutError as PatchrightTimeout
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

type ConsentOpt = Literal["optOut", "optIn"]

DRIVER_ERRORS: Final[tuple[type[Exception], ...]] = (PlaywrightError, PatchrightError)
DRIVER_TIMEOUTS: Final[tuple[type[Exception], ...]] = (PlaywrightTimeout, PatchrightTimeout)
