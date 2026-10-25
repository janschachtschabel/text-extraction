"""Silenced logging for ``DocumentPool`` children (``child.py``, ``docling_child.py``).

What a converter library or our own ``meta.py`` logs or warns about may quote the document, and
a child's stderr is the pod's log stream (its stdout goes nowhere). Records and warnings are
dropped, structlog's included; the parent logs each outcome by its type. A child target calls
``silence_logging`` before it imports its converters.
"""

import logging
import warnings

import structlog


def silence_logging() -> None:
    root = logging.getLogger()
    root.handlers[:] = [logging.NullHandler()]  # so logging's last-resort handler never writes
    root.setLevel(logging.CRITICAL + 1)
    logging.captureWarnings(True)  # warnings become records of `py.warnings`, dropped above
    warnings.simplefilter("ignore")
    structlog.configure(logger_factory=structlog.ReturnLoggerFactory())
