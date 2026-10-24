"""Shared pytest fixtures."""

import logging
from collections.abc import Iterator

import pytest
import structlog


@pytest.fixture(scope="session")
def httpserver_listen_address() -> tuple[str, int]:
    """Bind pytest-httpserver to 127.0.0.1 so its URLs never say ``localhost``.

    On CI runners ``localhost`` also resolves to ``::1``. The SSRF tests allow only
    127.0.0.1, so a mixed answer would be blocked before the request is sent.
    """
    return ("127.0.0.1", 0)


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """`configure_logging` changes process-wide state; put it back after each test.

    Its handler writes to the `sys.stdout` of the moment, which under `capsys` is a
    capture stream that pytest closes after the test. Left in place, that handler makes
    every later log record (uvicorn's start and stop lines, for one) a "Logging error".
    """
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    uvicorn_state = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).propagate,
            logging.getLogger(name).level,
        )
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
    }
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, (saved_handlers, propagate, saved_level) in uvicorn_state.items():
        logger = logging.getLogger(name)
        logger.handlers[:] = saved_handlers
        logger.propagate = propagate
        logger.setLevel(saved_level)
        logger.disabled = False
    structlog.reset_defaults()
