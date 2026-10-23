"""Per-domain outbound gate: parallelism, spacing, backoff and a circuit breaker per host.

Every fetch of a target site goes through ``DomainGate.slot(host, crawl_delay)`` and reports its
outcome with ``DomainGate.record(...)``. All state lives in Valkey, so all pods share it:

- ``out:inflight:{host}``  requests in flight: a sorted set of lease tokens scored by expiry time
- ``out:next:{host}``      earliest start time of the next request
- ``out:cooldown:{host}``  end of the pause after 429/503
- ``out:fail:{host}``      consecutive failures (connection error or 5xx)
- ``out:circuit:{host}``   end of an open circuit

The braces around the host are literal: a cluster hash tag, so the keys of one host share a slot.
Times are epoch seconds from the injected clock, not from Valkey, so tests are deterministic.
They also mean every pod needs a synchronised clock. The Valkey TTLs only clean up; the stored
end times and lease expiry times decide.
"""

import asyncio
import math
import random
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC
from email.utils import parsedate_to_datetime

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from text_extraction.settings import Settings

_log = structlog.get_logger(__name__)

# A lease whose holder died frees itself after this long.
_LEASE_TTL_SECONDS = 120
# Failures in a row count only while they are recent.
_FAILURE_TTL_SECONDS = 600
# How often a request that waits for a lease asks again.
_POLL_SECONDS = 0.05
# Cleanup margin on top of the real end time of a slot, pause or open circuit.
_TTL_MARGIN_SECONDS = 60

# KEYS[1] = out:next:{host}; ARGV = now, delay, max_wait (seconds).
# Takes the next free start time if it is at most max_wait away and moves the key on by delay.
# Returns {1, wait} when granted, {0, wait} when the wait would be too long (nothing changes).
# Numbers go back as strings: Redis would cut a Lua number to an integer.
_RESERVE_SLOT = """
local now = tonumber(ARGV[1])
local delay = tonumber(ARGV[2])
local max_wait = tonumber(ARGV[3])
local free_at = tonumber(redis.call('GET', KEYS[1]) or '0')
local start = math.max(now, free_at)
local wait = start - now
if wait > max_wait then
  return {0, string.format('%.6f', wait)}
end
local ttl_ms = math.ceil((wait + delay) * 1000) + tonumber(ARGV[4])
redis.call('SET', KEYS[1], string.format('%.6f', start + delay), 'PX', string.format('%d', ttl_ms))
return {1, string.format('%.6f', wait)}
"""

# KEYS[1] = out:inflight:{host}; ARGV = now, limit, token, expires_at, key_ttl_ms.
# Drops expired leases, then adds the token unless `limit` leases are held. Returns 1 or 0.
# A refusal changes nothing.
_ACQUIRE_LEASE = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then
  return 0
end
redis.call('ZADD', KEYS[1], ARGV[4], ARGV[3])
redis.call('PEXPIRE', KEYS[1], ARGV[5])
return 1
"""

# KEYS[1] = a pause key; ARGV = end time, ttl_seconds. Keeps the later end time: a pause is
# never shortened. Returns 1 when the key was set.
_EXTEND_PAUSE = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current >= tonumber(ARGV[1]) then
  return 0
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""

# At most 10 digits: longer numbers are no sane pause, and the header is under the target's control.
_DELTA_SECONDS = re.compile(r"-?\d{1,10}", re.ASCII)


class OutboundBusy(Exception):  # noqa: N818 - public name fixed by the plan
    """The wait for a free slot at this host would exceed the maximum wait."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Outbound slot is busy, retry after {retry_after:.1f} s")
        self.retry_after = retry_after


class CircuitOpen(Exception):  # noqa: N818 - public name fixed by the plan
    """The host is cooling down after 429/503 or its circuit is open after repeated failures."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Host is paused, retry after {retry_after:.1f} s")
        self.retry_after = retry_after


def parse_retry_after(value: str | None, *, now: float) -> float | None:
    """Seconds to wait from a ``Retry-After`` header: delta-seconds or an HTTP date.

    A date in the past or a negative number gives 0. A missing or unreadable value gives None.
    """
    if value is None:
        return None
    text = value.strip()
    if _DELTA_SECONDS.fullmatch(text):
        return float(max(int(text), 0))
    try:
        date = parsedate_to_datetime(text)
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)  # RFC 9110 dates are GMT
        return max(date.timestamp() - now, 0.0)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _normalize_host(host: str) -> str:
    """Lower-case IDNA form, so every spelling of a host shares one set of keys."""
    name = host.strip().rstrip(".").lower()
    try:
        return name.encode("idna").decode("ascii")
    except UnicodeError:
        return name


def _key(kind: str, host: str) -> str:
    """``out:{kind}:{host}`` with the host in literal braces: a cluster hash tag."""
    return f"out:{kind}:{{{host}}}"


class DomainGate:
    """Limits our outbound traffic per target host, across all pods."""

    def __init__(
        self,
        valkey: Redis,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._valkey = valkey
        self._settings = settings
        self._clock = clock
        self._sleep = sleep
        self._reserve_slot = valkey.register_script(_RESERVE_SLOT)
        self._acquire_lease_script = valkey.register_script(_ACQUIRE_LEASE)
        self._extend_pause = valkey.register_script(_EXTEND_PAUSE)
        # Releases that outlive a cancelled caller; kept so the tasks are not garbage collected.
        self._releases: set[asyncio.Task[None]] = set()

    @asynccontextmanager
    async def slot(self, host: str, crawl_delay: float | None = None) -> AsyncIterator[None]:
        """Hold a turn at ``host`` for the body of the ``async with``.

        Raises ``CircuitOpen`` while the host is paused and ``OutboundBusy`` when the turn is
        more than ``outbound_max_wait_seconds`` away. The lease is released on every exit,
        including cancellation at any point.
        """
        host = _normalize_host(host)
        await self._refuse_while_paused(host)
        deadline = self._clock() + self._settings.outbound_max_wait_seconds
        delay = self._spacing(crawl_delay)
        token = uuid.uuid4().hex
        try:
            await self._acquire_lease(host, token, deadline, delay)
            await self._wait_for_start(host, deadline, delay)
            yield
        finally:
            # Also runs when cancellation hit while the acquire was in flight: the lease may
            # exist in Valkey although the caller never saw the answer. Removing it is harmless
            # when it does not.
            await self._release_lease(host, token)

    async def record(self, host: str, *, status: int | None, retry_after: float | None) -> None:
        """Report how a fetch ended: ``status`` is None for a connection failure.

        ``retry_after`` is the parsed ``Retry-After`` header in seconds (see ``parse_retry_after``).
        """
        host = _normalize_host(host)
        settings = self._settings
        now = self._clock()
        if status in (429, 503):
            if retry_after is None or not math.isfinite(retry_after):
                pause = settings.outbound_backoff_seconds
            else:
                pause = retry_after
            pause = min(max(pause, 0.0), settings.outbound_max_backoff_seconds)
            if pause > 0:
                await self._pause_until(_key("cooldown", host), now + pause, pause)
        if status is None or status >= 500:
            await self._count_failure(host, now)
        elif status != 429:
            await self._valkey.delete(_key("fail", host))

    def _spacing(self, crawl_delay: float | None) -> float:
        """Gap between two request starts: the minimum delay or the capped Crawl-delay."""
        settings = self._settings
        wanted = 0.0 if crawl_delay is None else crawl_delay
        return max(
            settings.outbound_min_delay_ms / 1000,
            min(wanted, settings.outbound_max_crawl_delay_seconds),
        )

    async def _refuse_while_paused(self, host: str) -> None:
        cooldown, circuit = await self._valkey.mget(_key("cooldown", host), _key("circuit", host))
        now = self._clock()
        until = max((float(value) for value in (cooldown, circuit) if value is not None), default=0)
        if until > now:
            raise CircuitOpen(until - now)

    async def _acquire_lease(self, host: str, token: str, deadline: float, delay: float) -> None:
        key = _key("inflight", host)
        limit = self._settings.outbound_max_concurrency_per_host
        while True:
            now = self._clock()
            granted = await self._acquire_lease_script(
                keys=[key],
                args=[
                    now,
                    limit,
                    token,
                    now + _LEASE_TTL_SECONDS,
                    (_LEASE_TTL_SECONDS + _TTL_MARGIN_SECONDS) * 1000,
                ],
            )
            if int(granted):
                return
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise OutboundBusy(max(delay, 1.0))
            # Jitter, so waiting requests of many pods do not ask in lockstep.
            poll = _POLL_SECONDS * random.uniform(0.5, 1.5)  # noqa: S311 - not security related
            await self._sleep(min(poll, remaining))

    async def _release_lease(self, host: str, token: str) -> None:
        """Remove the lease, even when the caller is being cancelled."""
        task = asyncio.create_task(self._remove_lease(host, token))
        self._releases.add(task)
        task.add_done_callback(self._releases.discard)
        await asyncio.shield(task)

    async def _remove_lease(self, host: str, token: str) -> None:
        try:
            await self._valkey.zrem(_key("inflight", host), token)
        except (RedisError, OSError) as error:
            # The work is done; the lease expires by itself. Do not hide the real outcome.
            _log.warning(
                "outbound_lease_release_failed",
                host=host,
                error_type=type(error).__name__,
                error=str(error),
            )

    async def _wait_for_start(self, host: str, deadline: float, delay: float) -> None:
        now = self._clock()
        granted, wait = await self._reserve_slot(
            keys=[_key("next", host)],
            args=[now, delay, max(deadline - now, 0.0), _TTL_MARGIN_SECONDS * 1000],
        )
        if not int(granted):
            raise OutboundBusy(float(wait))
        if float(wait) > 0:
            await self._sleep(float(wait))

    async def _count_failure(self, host: str, now: float) -> None:
        key = _key("fail", host)
        async with self._valkey.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, _FAILURE_TTL_SECONDS)
            count, _ = await pipe.execute()
        if count >= self._settings.circuit_failure_threshold:
            open_for = self._settings.circuit_open_seconds
            await self._pause_until(_key("circuit", host), now + open_for, open_for)
            await self._valkey.delete(key)  # a closed circuit starts counting from zero

    async def _pause_until(self, key: str, until: float, duration: float) -> None:
        """Set the end of a pause; an existing later end stays."""
        await self._extend_pause(
            keys=[key], args=[f"{until:.6f}", math.ceil(duration) + _TTL_MARGIN_SECONDS]
        )
