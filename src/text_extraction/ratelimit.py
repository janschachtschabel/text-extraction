"""Inbound rate limit: a fixed 60 s window per caller, counted in Valkey."""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

import structlog
from fastapi import Depends, Request
from redis.asyncio import Redis
from redis.exceptions import OutOfMemoryError, RedisError
from starlette.datastructures import State

from text_extraction.auth import Principal, authenticate
from text_extraction.envelope import ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.settings import Settings

_log = structlog.get_logger(__name__)

_WINDOW_SECONDS = 60


@dataclass(frozen=True)
class RateDecision:
    """Outcome of one hit: whether it may pass, calls left, seconds until the window ends."""

    allowed: bool
    remaining: int
    retry_after: int


class RateLimiter:
    """Fixed-window counter. The key is ``rl:{name}:{epoch_minute}``, so pods share it."""

    def __init__(
        self,
        client: Redis,
        default_per_minute: int,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self.default_per_minute = default_per_minute
        self._clock = clock

    async def hit(self, principal_name: str, limit: int) -> RateDecision:
        now = self._clock()
        window = int(now // _WINDOW_SECONDS)
        key = f"rl:{principal_name}:{window}"
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, _WINDOW_SECONDS)
            count, _ = await pipe.execute()
        seconds_left = (window + 1) * _WINDOW_SECONDS - now
        return RateDecision(
            allowed=count <= limit,
            remaining=max(limit - count, 0),
            retry_after=max(math.ceil(seconds_left), 1),
        )

    async def peek(self, principal_name: str, limit: int) -> RateDecision:
        """Whether one more hit would pass, without counting one."""
        now = self._clock()
        window = int(now // _WINDOW_SECONDS)
        raw = await self._client.get(f"rl:{principal_name}:{window}")
        count = int(raw) if raw is not None else 0
        seconds_left = (window + 1) * _WINDOW_SECONDS - now
        return RateDecision(
            allowed=count < limit,
            remaining=max(limit - count, 0),
            retry_after=max(math.ceil(seconds_left), 1),
        )


def _unavailable() -> ServiceError:
    return ServiceError(
        ErrorCode.INTERNAL_ERROR,
        "The rate limit backend is unavailable. Try again later.",
        503,
        retryable=True,
    )


async def rate_limited(
    request: Request, principal: Annotated[Principal, Depends(authenticate)]
) -> None:
    """FastAPI dependency: count this call against the caller's bucket, or answer 429/503.

    It depends on `authenticate`, so it always runs after it, and FastAPI runs that once per
    request even when the route also uses `require_role`.
    """
    await check_rate(request.app.state, principal)


async def check_rate(state: State, principal: Principal) -> None:
    """Count one call of ``principal`` against its bucket (the one every door shares: REST, MCP
    and the UI), or raise 429 with ``Retry-After``, or 503 while Valkey is unavailable.
    ``state`` is the public app's state.

    The caller has authenticated ``principal`` (``principal_for``) and checks its role; one call
    of a door (a request, an MCP tool call, a UI view) counts exactly once."""
    await _decide(state, principal, count=True)


async def check_rate_left(state: State, principal: Principal) -> None:
    """As `check_rate`, but without counting: raise 429 when the bucket of ``principal`` is used
    up. For a door that refuses early, before it reads a body, a call that is counted later
    (the UI's single views, whose forwarded REST call counts)."""
    await _decide(state, principal, count=False)


async def _decide(state: State, principal: Principal, *, count: bool) -> None:
    settings: Settings = state.settings
    if not settings.rate_limit_enabled:
        return
    limiter: RateLimiter = state.rate_limiter
    limit = (
        principal.rate_limit_per_minute
        if principal.rate_limit_per_minute is not None
        else limiter.default_per_minute
    )
    try:
        if count:
            decision = await limiter.hit(principal.name, limit)
        else:
            decision = await limiter.peek(principal.name, limit)
    except OutOfMemoryError:
        # Valkey is full (noeviction) and refuses the counter's write: the call goes through
        # uncounted, so job reads and DELETE (which frees memory) still work; the memory guard
        # refuses new jobs meanwhile.
        _log.warning("rate_limit_backend_full")
        return
    except (RedisError, OSError) as error:
        # Type and text only, no traceback per request. Not the URL: it may carry a password;
        # the exception text names host and port only.
        _log.warning(
            "rate_limit_backend_unavailable", error_type=type(error).__name__, error=str(error)
        )
        raise _unavailable() from None
    if not decision.allowed:
        raise ServiceError(
            ErrorCode.RATE_LIMITED,
            "Rate limit exceeded. Retry after the time given in Retry-After.",
            429,
            retryable=True,
            headers={"Retry-After": str(decision.retry_after)},
        )
