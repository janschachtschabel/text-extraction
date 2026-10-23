"""Shared pytest fixtures."""

import pytest


@pytest.fixture(scope="session")
def httpserver_listen_address() -> tuple[str, int]:
    """Bind pytest-httpserver to 127.0.0.1 so its URLs never say ``localhost``.

    On CI runners ``localhost`` also resolves to ``::1``. The SSRF tests allow only
    127.0.0.1, so a mixed answer would be blocked before the request is sent.
    """
    return ("127.0.0.1", 0)
