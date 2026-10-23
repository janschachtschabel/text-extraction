import json
import logging
from collections.abc import Iterator

import pytest
import structlog

from text_extraction.logging import configure_logging


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """`configure_logging` changes process-wide state; put it back after each test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    uvicorn_state = {
        name: (list(logging.getLogger(name).handlers), logging.getLogger(name).propagate)
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
    }
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, (saved_handlers, propagate) in uvicorn_state.items():
        logger = logging.getLogger(name)
        logger.handlers[:] = saved_handlers
        logger.propagate = propagate
        logger.disabled = False
    structlog.reset_defaults()


def stdout_lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return [line for line in capsys.readouterr().out.splitlines() if line]


def test_json_format_for_structlog_events(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")

    structlog.get_logger("app").info("hello", answer=42)

    (line,) = stdout_lines(capsys)
    entry = json.loads(line)
    assert entry["event"] == "hello"
    assert entry["answer"] == 42
    assert entry["level"] == "info"
    assert entry["logger"] == "app"
    assert entry["timestamp"].endswith("Z")


def test_bound_contextvars_are_in_every_line(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")

    with structlog.contextvars.bound_contextvars(trace_id="t-1"):
        structlog.get_logger("app").info("first")
        logging.getLogger("third.party").warning("second")

    first, second = (json.loads(line) for line in stdout_lines(capsys))
    assert first["trace_id"] == "t-1"
    assert second["trace_id"] == "t-1"


def test_uvicorn_error_traceback_is_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")

    try:
        raise RuntimeError("kaboom")
    except RuntimeError:
        logging.getLogger("uvicorn.error").exception("Exception in ASGI application")

    (line,) = stdout_lines(capsys)  # exactly one line: no plain-text traceback next to it
    entry = json.loads(line)
    assert entry["event"] == "Exception in ASGI application"
    assert entry["level"] == "error"
    assert entry["logger"] == "uvicorn.error"
    assert entry["exception"][0]["exc_type"] == "RuntimeError"
    assert entry["exception"][0]["exc_value"] == "kaboom"


def test_uvicorn_handlers_are_replaced(capsys: pytest.CaptureFixture[str]) -> None:
    plain = logging.StreamHandler()
    plain.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).addHandler(plain)

    configure_logging("INFO", "json")

    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        assert logger.handlers == []
        assert logger.propagate is True
    logging.getLogger("uvicorn").info("Started server process")
    (line,) = stdout_lines(capsys)
    assert json.loads(line)["event"] == "Started server process"


def test_uvicorn_access_log_is_disabled(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")

    logging.getLogger("uvicorn.access").info('127.0.0.1 - "GET / HTTP/1.1" 200')

    assert stdout_lines(capsys) == []


def test_level_filters_events(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("warning", "json")

    log = structlog.get_logger("app")
    log.info("dropped")
    log.warning("kept")
    logging.getLogger("other").info("dropped too")

    (line,) = stdout_lines(capsys)
    assert json.loads(line)["event"] == "kept"


def test_unknown_level_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown log level"):
        configure_logging("LOUD", "json")


def test_console_format_is_not_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "console")

    structlog.get_logger("app").info("hello", answer=42)

    (line,) = stdout_lines(capsys)
    assert "hello" in line
    assert "answer" in line
    with pytest.raises(json.JSONDecodeError):
        json.loads(line)


def test_calling_twice_does_not_duplicate_lines(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    configure_logging("INFO", "json")

    structlog.get_logger("app").info("once")

    assert len(stdout_lines(capsys)) == 1


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def test_json_traceback_has_no_frame_locals(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")

    def connect() -> None:
        auth_args = "hunter2-password"  # noqa: F841 - a local that must not be logged
        raise RuntimeError("boom")

    try:
        connect()
    except RuntimeError:
        structlog.get_logger("app").error("failed", exc_info=True)

    (line,) = stdout_lines(capsys)
    assert "hunter2-password" not in line
    assert not _contains_key(json.loads(line), "locals")
    assert json.loads(line)["exception"][0]["exc_type"] == "RuntimeError"


def test_httpx_info_records_are_not_emitted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("DEBUG", "json")

    logging.getLogger("httpx").info('HTTP Request: GET https://x.test/?token=abc "200 OK"')
    logging.getLogger("httpcore.http11").debug("send_request_headers")
    logging.getLogger("httpx").warning("kept")

    (line,) = stdout_lines(capsys)
    assert json.loads(line)["event"] == "kept"
