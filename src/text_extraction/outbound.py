"""Per-domain outbound gate: parallelism, spacing, backoff and a circuit breaker per host.

Every fetch of a target site goes through ``DomainGate.slot(host, crawl_delay)`` and reports its
outcome with ``DomainGate.record(...)``. All state lives in Valkey, so all pods share it:

- ``out:inflight:{host}``  requests in flight: a sorted set of lease tokens scored by expiry time
- ``out:next:{host}``      earliest start time of the next request (the next turn to book)
- ``out:starts:{host}``    real starts: a sorted set of turn tokens, each scored by the latest
                           real start of a request in that turn (recent turns only)
- ``out:cooldown:{host}``  end of the pause after 429/503
- ``out:fail:{host}``      consecutive failures (connection error or 5xx)
- ``out:circuit:{host}``   end of an open circuit

The spacing lies between the requests of different turns; the requests of one turn (the
origin's compliance files and the page, a same-host redirect hop) go back to back. A turn books
its start (``out:next``) and sleeps until it, and until the delay has passed since the latest
real start of any other turn. Before each of its requests the fetcher asks again
(``before_request``): the request waits out what is left of the delay since the latest start of
the other turns, then records its own start under the turn's token, in one script per host.
Once the request has really gone out, the fetcher reports it (``mark_sent``), which moves the
turn's start on to that moment, never back: a request held up between its check and the send (a
busy event loop) then delays the next turn's request instead of letting it follow closer. Only a
stall longer than the delay itself can still bring two starts closer.

The turn's token travels in a context variable, set while ``slot`` is held: the fetcher's hooks
take only the host. A request to a host whose turn is not held is not checked, and its report is
recorded under a fresh token, so it counts for every turn.

The braces around the host are literal: a cluster hash tag, so the keys of one host share a slot.
Times are epoch seconds from the injected clock, not from Valkey, so tests are deterministic.
They also mean every pod needs a synchronised clock. The Valkey TTLs only clean up; the stored
end times and lease expiry times decide.
"""

import asyncio
import contextvars
import math
import random
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
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

# The latest start of the turns other than ARGV[token]: the two highest scores hold it.
_LATEST_OF_OTHERS = """
local function latest_of_others(key, token)
  local top = redis.call('ZREVRANGE', key, 0, 1, 'WITHSCORES')
  for i = 1, #top, 2 do
    if top[i] ~= token then
      return tonumber(top[i + 1])
    end
  end
  return 0
end
"""

# KEYS[1] = out:next:{host}, KEYS[2] = out:starts:{host}; ARGV = now, delay, max_wait (seconds),
# TTL margin (ms), token.
# Books the next free start time if it is at most max_wait away and moves out:next on by delay.
# Returns {1, wait} when granted, wait being the seconds until the booked start and until delay
# has passed since the latest start of another turn, whichever is later; {0, wait} when the
# booking would be too far away (nothing changes). Nothing is recorded in out:starts: a turn's
# requests do that. Numbers go back as strings: Redis would cut a Lua number to an integer.
_RESERVE_SLOT = (
    _LATEST_OF_OTHERS
    + """
local now = tonumber(ARGV[1])
local delay = tonumber(ARGV[2])
local max_wait = tonumber(ARGV[3])
local margin = tonumber(ARGV[4])
local free_at = tonumber(redis.call('GET', KEYS[1]) or '0')
local start = math.max(now, free_at)
local wait = start - now
if wait > max_wait then
  return {0, string.format('%.6f', wait)}
end
local ttl_ms = math.ceil((wait + delay) * 1000) + margin
redis.call('SET', KEYS[1], string.format('%.6f', start + delay), 'PX', string.format('%d', ttl_ms))
local rest = latest_of_others(KEYS[2], ARGV[5]) + delay - now
return {1, string.format('%.6f', math.max(wait, rest))}
"""
)

# KEYS[1] = out:starts:{host}; ARGV = now, delay (seconds), token, record (1 or 0), trim_before,
# ttl_ms.
# Checks that delay has passed since the latest start of another turn. If so, and record is 1,
# records now as the token's start (never moving it back), drops starts older than trim_before
# and returns {1, 0}. Otherwise returns {0, rest} with the seconds still to wait (nothing
# changes).
_CONFIRM_START = (
    _LATEST_OF_OTHERS
    + """
local now = tonumber(ARGV[1])
local rest = latest_of_others(KEYS[1], ARGV[3]) + tonumber(ARGV[2]) - now
if rest > 0 then
  return {0, string.format('%.6f', rest)}
end
if ARGV[4] == '1' then
  redis.call('ZADD', KEYS[1], 'GT', ARGV[1], ARGV[3])
  redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[5])
  redis.call('PEXPIRE', KEYS[1], ARGV[6])
end
return {1, '0'}
"""
)

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

# KEYS[1] = out:starts:{host}; ARGV = now, token, trim_before, ttl_ms. Moves the token's start on
# to now; it never goes back (a pod may report an earlier send later).
_MARK_SENT = """
redis.call('ZADD', KEYS[1], 'GT', ARGV[1], ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[3])
redis.call('PEXPIRE', KEYS[1], ARGV[4])
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


def normalize_host(host: str) -> str:
    """Lower-case IDNA form, so every spelling of a host shares one set of keys."""
    name = host.strip().rstrip(".").lower()
    try:
        return name.encode("idna").decode("ascii")
    except UnicodeError:
        return name


@dataclass(frozen=True)
class _HeldTurn:
    host: str
    token: str
    delay: float


# The turn this task holds (see ``DomainGate.slot``); a task started meanwhile inherits it.
_held_turn: contextvars.ContextVar[_HeldTurn | None] = contextvars.ContextVar(
    "outbound_turn", default=None
)


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
        self._confirm_start = valkey.register_script(_CONFIRM_START)
        self._mark_sent = valkey.register_script(_MARK_SENT)
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
        host = normalize_host(host)
        await self._refuse_while_paused(host)
        deadline = self._clock() + self._settings.outbound_max_wait_seconds
        delay = self._spacing(crawl_delay)
        token = uuid.uuid4().hex
        try:
            await self._acquire_lease(host, token, deadline, delay)
            await self._wait_for_start(host, token, deadline, delay)
            held = _held_turn.set(_HeldTurn(host, token, delay))
            try:
                yield
            finally:
                _held_turn.reset(held)
        finally:
            # Also runs when cancellation hit while the acquire was in flight: the lease may
            # exist in Valkey although the caller never saw the answer. Removing it is harmless
            # when it does not.
            await self._release_lease(host, token)

    async def before_request(self, host: str) -> None:
        """Called by the fetcher right before each request to ``host``. Inside a turn held at
        ``host``: wait until the turn's delay has passed since the latest start of any other
        turn, and record this request's start under the turn. The turn's own earlier requests do
        not count, so they go back to back. Raises ``OutboundBusy`` when the wait would be
        longer than ``outbound_max_wait_seconds``. Outside such a turn it does nothing."""
        held = _held_turn.get()
        if held is None or held.host != normalize_host(host):
            return
        deadline = self._clock() + self._settings.outbound_max_wait_seconds
        await self._confirm_until(held, deadline, 0.0, record=True)

    async def mark_sent(self, host: str) -> None:
        """Report that a request to ``host`` has just gone out: the other turns' spacing counts
        from now (the fetcher calls this; see the module docstring)."""
        host = normalize_host(host)
        held = _held_turn.get()
        token = held.token if held is not None and held.host == host else uuid.uuid4().hex
        now = self._clock()
        await self._mark_sent(
            keys=[_key("starts", host)],
            args=[f"{now:.6f}", token, f"{self._trim_before(now):.6f}", self._starts_ttl_ms()],
        )

    async def record(self, host: str, *, status: int | None, retry_after: float | None) -> None:
        """Report how a fetch ended: ``status`` is None for a connection failure.

        ``retry_after`` is the parsed ``Retry-After`` header in seconds (see ``parse_retry_after``).
        """
        host = normalize_host(host)
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

    def _longest_spacing(self) -> float:
        settings = self._settings
        return max(settings.outbound_min_delay_ms / 1000, settings.outbound_max_crawl_delay_seconds)

    def _trim_before(self, now: float) -> float:
        """Starts older than this cannot delay any request any more: two longest spacings."""
        return now - 2 * self._longest_spacing()

    def _starts_ttl_ms(self) -> int:
        return math.ceil(self._longest_spacing() * 1000) + _TTL_MARGIN_SECONDS * 1000

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

    async def _wait_for_start(self, host: str, token: str, deadline: float, delay: float) -> None:
        """Book the next turn, then sleep until it and until ``delay`` has passed since the
        latest start of another turn. Nothing is recorded: the turn's requests do that
        (``before_request``).

        The wait after the booked start is at most ``delay`` per request that started meanwhile;
        at most ``OUTBOUND_MAX_CONCURRENCY_PER_HOST`` - 1 turns can send, since each holds a
        lease. Like the booking, it ends by ``deadline`` (``OutboundBusy`` otherwise).
        """
        margin_ms = _TTL_MARGIN_SECONDS * 1000
        now = self._clock()
        granted, wait = await self._reserve_slot(
            keys=[_key("next", host), _key("starts", host)],
            args=[now, delay, max(deadline - now, 0.0), margin_ms, token],
        )
        if not int(granted):
            raise OutboundBusy(float(wait))
        rest = float(wait)
        if rest <= 0:
            return
        if self._clock() + rest > deadline:
            raise OutboundBusy(rest)
        await self._confirm_until(_HeldTurn(host, token, delay), deadline, rest, record=False)

    async def _confirm_until(
        self, turn: _HeldTurn, deadline: float, rest: float, *, record: bool
    ) -> None:
        """Sleep ``rest``, then check that the turn's delay has passed since the latest start of
        another turn, waiting out what is left; with ``record``, the start is recorded under the
        turn's token. Raises ``OutboundBusy`` when a wait would end after ``deadline``."""
        while True:
            if rest > 0:
                await self._sleep(rest)
            now = self._clock()
            recorded, remaining = await self._confirm_start(
                keys=[_key("starts", turn.host)],
                args=[
                    f"{now:.6f}",
                    turn.delay,
                    turn.token,
                    1 if record else 0,
                    f"{self._trim_before(now):.6f}",
                    self._starts_ttl_ms(),
                ],
            )
            if int(recorded):
                return
            rest = float(remaining)
            if self._clock() + rest > deadline:
                raise OutboundBusy(rest)

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
