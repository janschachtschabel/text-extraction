"""A ``DocumentPool`` target for the ``nlp`` tests: the real PII child's functions, with every
attempt to reach the network refused and counted (``connect_attempts``)."""

import socket
from typing import Any, NoReturn

from text_extraction.pii.child import FUNCTIONS as CHILD_FUNCTIONS

_attempts: list[str] = []


def _refuse(kind: str) -> Any:
    def refuse(*_args: Any, **_kwargs: Any) -> NoReturn:
        _attempts.append(kind)
        raise OSError(f"no network in this test ({kind})")

    return refuse


socket.socket.connect = _refuse("connect")  # type: ignore[method-assign]
socket.socket.connect_ex = _refuse("connect_ex")  # type: ignore[method-assign]
socket.getaddrinfo = _refuse("getaddrinfo")


def connect_attempts(_payload: Any) -> list[str]:
    return list(_attempts)


FUNCTIONS = {**CHILD_FUNCTIONS, "connect_attempts": connect_attempts}
