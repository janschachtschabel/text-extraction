"""A ``DocumentPool`` target for the tests: the functions a test child may call."""

import os
import time
from collections.abc import Callable
from typing import Any

_calls = 0


def echo(payload: Any) -> Any:
    return payload


def sleep_forever(_payload: Any) -> None:
    while True:
        time.sleep(1)


def allocate(payload: dict[str, int]) -> int:
    block = bytearray(payload["megabytes"] * 1024 * 1024)
    return len(block)


def crash(_payload: Any) -> None:
    os._exit(3)


def count_calls(_payload: Any) -> int:
    global _calls
    _calls += 1
    return _calls


def pid(_payload: Any) -> int:
    return os.getpid()


def fail(_payload: Any) -> None:
    raise ValueError("the converter broke")


FUNCTIONS: dict[str, Callable[[Any], Any]] = {
    "echo": echo,
    "sleep_forever": sleep_forever,
    "allocate": allocate,
    "crash": crash,
    "count_calls": count_calls,
    "pid": pid,
    "fail": fail,
}
