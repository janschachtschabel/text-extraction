"""Logging setup: structlog and the standard library write one JSON line per event to stdout."""

import logging
import sys
from typing import Literal

import structlog
from structlog.typing import Processor

_HANDLER_MARK = "_text_extraction_handler"
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error")
_UVICORN_ACCESS_LOGGER = "uvicorn.access"


def _level_number(level: str) -> int:
    number = logging.getLevelNamesMapping().get(level.upper())
    if number is None or number == logging.NOTSET:
        raise ValueError(f"Unknown log level: {level!r}")
    return number


def configure_logging(level: str, fmt: Literal["json", "console"]) -> None:
    """Route structlog, the root logger and the uvicorn loggers through one formatter.

    Safe to call more than once. Uvicorn's own access log is disabled because
    `TraceMiddleware` writes the access line.
    """
    level_number = _level_number(level)

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    renderer: list[Processor] = (
        [structlog.processors.dict_tracebacks, structlog.processors.JSONRenderer()]
        if fmt == "json"
        else [structlog.dev.ConsoleRenderer()]
    )

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    handler = logging.StreamHandler(sys.stdout)
    setattr(handler, _HANDLER_MARK, True)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *renderer],
        )
    )

    root = logging.getLogger()
    for existing in [h for h in root.handlers if getattr(h, _HANDLER_MARK, False)]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level_number)

    # Uvicorn installs its own handlers (plain text); drop them and let records reach the root.
    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
        uvicorn_logger.setLevel(logging.NOTSET)
    access_logger = logging.getLogger(_UVICORN_ACCESS_LOGGER)
    access_logger.handlers.clear()
    access_logger.propagate = False
    access_logger.disabled = True
