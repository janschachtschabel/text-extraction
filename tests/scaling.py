"""How a piece of work grows with its input, for tests that guard against quadratic time.

A wall-clock bound fails on a slow or busy runner. The ratio of two timings of the same work,
at ``n`` and at ``factor * n``, does not: linear work grows by about ``factor``, quadratic
work by about ``factor ** 2``.
"""

import gc
import time
from collections.abc import Callable

GROWTH_LIMIT = 8.0  # for factor 4: linear about 4, quadratic about 16


def growth(prepare: Callable[[int], Callable[[], object]], n: int, factor: int = 4) -> float:
    """The best of three timings at ``factor * n`` divided by the best of three at ``n``.

    ``prepare(size)`` builds a fresh input (not timed) and returns the work to time."""
    return _best(prepare, factor * n) / _best(prepare, n)


def _best(prepare: Callable[[int], Callable[[], object]], size: int) -> float:
    timings = []
    for _ in range(3):
        work = prepare(size)
        gc.disable()  # a collection inside one run would distort the ratio
        try:
            started = time.perf_counter()
            work()
            timings.append(time.perf_counter() - started)
        finally:
            gc.enable()
    return max(min(timings), 1e-6)
