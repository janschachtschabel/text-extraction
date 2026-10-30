"""A ``DocumentPool`` target for the selector pool tests: its ``rules`` kills the child."""

import os
from collections.abc import Callable
from typing import Any


def crash(_payload: Any) -> None:
    os._exit(3)


FUNCTIONS: dict[str, Callable[[Any], Any]] = {"rules": crash}
