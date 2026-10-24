"""Fair queue with leases: the work list that worker pools claim batch items from.

The queue of one worker pool lives in these Valkey keys. The braces around the pool name are a
literal cluster hash tag, so all keys of a pool share one slot and a Lua script may touch any of
them. No script touches job keys (``job:{<id>}:…``).

- ``q:{<pool>}:clients``              list: clients with queued items, in rotation order
- ``q:{<pool>}:c:<client>:domains``   list: that client's domains with queued items, in rotation
- ``q:{<pool>}:c:<client>:d:<domain>`` list: that client's items of that domain, front first
- ``q:{<pool>}:notbefore``            hash: domain -> epoch seconds before which it is not claimed
- ``q:{<pool>}:running``              hash: domain -> number of leased items (no field: 0)
- ``q:{<pool>}:leases``               sorted set: leased item -> lease expiry (epoch seconds)
- ``q:{<pool>}:leased``               hash: leased item -> ``<client>\\t<domain>``
- ``q:{<pool>}:pending``              counter: items in the domain lists (claimable or deferred)

An item is ``<job_id>:<index>:<attempts>``. In keys and values, ``<client>`` and ``<domain>`` are
escaped (``%`` -> ``%25``, ``:`` -> ``%3A``, tab -> ``%09``): client names are free text and IPv6
hosts contain colons, so unescaped names could make two lists share one key.

Invariants, kept by every script: a domain is in a client's rotation exactly while its list holds
items, and a client is in ``clients`` exactly while it has domains. The keys carry no TTL; the
lists empty themselves as items are claimed. Times are epoch seconds from the injected clock
(every pod needs a synchronised clock), passed into the scripts, which never read Valkey's TIME.
"""

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from redis.asyncio import Redis

from text_extraction.outbound import normalize_host
from text_extraction.settings import Settings

# The domain of a URL without a valid host.
NO_DOMAIN = "-"

# Items per enqueue script call, so one call never carries a whole large batch.
_ENQUEUE_CHUNK = 500
# Expired leases a claim takes back, at most, before it looks for an item.
_RECLAIM_LIMIT = 20

_POOL = re.compile(r"[a-z0-9_-]{1,32}", re.ASCII)
_ESCAPES = {"%": "%25", ":": "%3A", "\t": "%09"}
_ESCAPED = re.compile("|".join(re.escape(code) for code in _ESCAPES.values()))
_UNESCAPES = {code: char for char, code in _ESCAPES.items()}

# Every script gets KEYS = clients, notbefore, running, leases, leased, pending and ARGV[1] = the
# key prefix ``q:{<pool>}:``, from which it builds the per-client and per-domain list keys inside
# the same hash tag. These helpers keep the invariants above.
_PREAMBLE = """
local clients, notbefore, running = KEYS[1], KEYS[2], KEYS[3]
local leases, leased, pending = KEYS[4], KEYS[5], KEYS[6]
local prefix = ARGV[1]

local function domains_key(client)
  return prefix .. 'c:' .. client .. ':domains'
end

local function items_key(client, domain)
  return prefix .. 'c:' .. client .. ':d:' .. domain
end

-- One lease of the domain ends; the count never goes below 0 and a zero count is removed.
local function release(domain)
  if tonumber(redis.call('HGET', running, domain) or '0') <= 1 then
    redis.call('HDEL', running, domain)
  else
    redis.call('HINCRBY', running, domain, -1)
  end
end

-- Ends the lease of an item; returns client and domain, or nil when it holds no lease.
local function unlease(member)
  if redis.call('ZREM', leases, member) == 0 then
    return nil
  end
  local where = redis.call('HGET', leased, member)
  redis.call('HDEL', leased, member)
  if not where then
    return nil
  end
  local client, domain = string.match(where, '^(.*)\\t([^\\t]*)$')
  release(domain)
  return client, domain
end

-- Puts an item back at the front of its list; a list that was empty re-enters the rotations at
-- the front, so the item is next in line.
local function requeue(client, domain, member)
  if redis.call('LPUSH', items_key(client, domain), member) == 1 then
    if redis.call('LPUSH', domains_key(client), domain) == 1 then
      redis.call('LPUSH', clients, client)
    end
  end
  redis.call('INCR', pending)
end
"""

# ARGV = prefix, client, then pairs of domain and item. Appends the items in order; a new domain
# joins the end of the client's rotation and a new client the end of ``clients``.
_ENQUEUE = (
    _PREAMBLE
    + """
local client = ARGV[2]
local count = 0
for i = 3, #ARGV, 2 do
  local domain = ARGV[i]
  if redis.call('RPUSH', items_key(client, domain), ARGV[i + 1]) == 1 then
    if redis.call('RPUSH', domains_key(client), domain) == 1 then
      redis.call('RPUSH', clients, client)
    end
  end
  count = count + 1
end
redis.call('INCRBY', pending, count)
return count
"""
)

# ARGV = prefix, now, lease expiry, max attempts, per-domain cap, reclaim limit.
# 1. Takes back up to `reclaim limit` expired leases: each goes to the front of its list with
#    attempts + 1, or into `exhausted` when that would exceed `max attempts`.
# 2. Rotates `clients` once per client and, for each, its domains once per domain; skips a domain
#    whose not-before lies in the future or that holds `cap` leases.
# 3. Leases the first item of the first claimable domain. A list that becomes empty leaves its
#    client's rotation, and a client without domains leaves `clients`.
# Returns {claimed, retry_in, exhausted}: claimed is {item, client, domain} or empty; retry_in
# (only when nothing was claimed) is the time until the earliest deferred domain frees, or ''; and
# exhausted is a list of {item, client, domain}. Numbers go back as strings: Redis would cut a Lua
# number to an integer.
_CLAIM = (
    _PREAMBLE
    + """
local now = tonumber(ARGV[2])
local expiry = ARGV[3]
local max_attempts = tonumber(ARGV[4])
local cap = tonumber(ARGV[5])

local exhausted = {}
local expired = redis.call('ZRANGEBYSCORE', leases, '-inf', ARGV[2], 'LIMIT', 0, ARGV[6])
for _, member in ipairs(expired) do
  local client, domain = unlease(member)
  if client then
    local job, index, attempts = string.match(member, '^(.*):(%d+):(%d+)$')
    attempts = tonumber(attempts) + 1
    local next_member = job .. ':' .. index .. ':' .. string.format('%d', attempts)
    if attempts > max_attempts then
      table.insert(exhausted, {next_member, client, domain})
    else
      requeue(client, domain, next_member)
    end
  end
end

local earliest = nil
for _ = 1, redis.call('LLEN', clients) do
  local client = redis.call('LMOVE', clients, clients, 'LEFT', 'RIGHT')
  local domains = domains_key(client)
  local count = redis.call('LLEN', domains)
  if count == 0 then
    redis.call('LREM', clients, -1, client)
  end
  for _ = 1, count do
    local domain = redis.call('LMOVE', domains, domains, 'LEFT', 'RIGHT')
    local free_at = tonumber(redis.call('HGET', notbefore, domain) or '0')
    if free_at > now then
      if earliest == nil or free_at < earliest then
        earliest = free_at
      end
    else
      if free_at > 0 then
        redis.call('HDEL', notbefore, domain)
      end
      if tonumber(redis.call('HGET', running, domain) or '0') < cap then
        local items = items_key(client, domain)
        local member = redis.call('LPOP', items)
        if member then
          redis.call('DECR', pending)
          redis.call('ZADD', leases, expiry, member)
          redis.call('HSET', leased, member, client .. '\\t' .. domain)
          redis.call('HINCRBY', running, domain, 1)
          if redis.call('LLEN', items) == 0 then
            redis.call('LREM', domains, -1, domain)
            if redis.call('LLEN', domains) == 0 then
              redis.call('LREM', clients, -1, client)
            end
          end
          return {{member, client, domain}, '', exhausted}
        end
        redis.call('LREM', domains, -1, domain)
      end
    end
  end
end
local retry_in = ''
if earliest then
  retry_in = string.format('%.6f', earliest - now)
end
return {{}, retry_in, exhausted}
"""
)

# ARGV = prefix, item. Ends its lease; returns 1, or 0 when the item held no lease (it expired and
# was taken back, or was completed already), in which case nothing changes.
_COMPLETE = (
    _PREAMBLE
    + """
if unlease(ARGV[2]) then
  return 1
end
return 0
"""
)

# ARGV = prefix, item, the item with attempts + 1, not-before. Ends the lease, moves the domain's
# not-before to the later of its current value and the given one, and puts the item back at the
# front of its list. Returns 0 and changes nothing when the item held no lease.
_DEFER = (
    _PREAMBLE
    + """
local client, domain = unlease(ARGV[2])
if not client then
  return 0
end
if tonumber(ARGV[4]) > tonumber(redis.call('HGET', notbefore, domain) or '0') then
  redis.call('HSET', notbefore, domain, ARGV[4])
end
requeue(client, domain, ARGV[3])
return 1
"""
)

# ARGV = prefix. Returns {pending, leased, clients, distinct domains}; read only.
_COUNTS = (
    _PREAMBLE
    + """
local seen = {}
local domains = 0
local names = redis.call('LRANGE', clients, 0, -1)
for _, client in ipairs(names) do
  for _, domain in ipairs(redis.call('LRANGE', domains_key(client), 0, -1)) do
    if not seen[domain] then
      seen[domain] = true
      domains = domains + 1
    end
  end
end
local queued = tonumber(redis.call('GET', pending) or '0')
return {queued, redis.call('ZCARD', leases), #names, domains}
"""
)


@dataclass(frozen=True)
class QueueItem:
    """One claimed batch item. ``attempts`` counts its lost leases plus its deferrals."""

    job_id: str
    index: int
    client: str
    domain: str
    attempts: int


@dataclass(frozen=True)
class Claim:
    """The answer to one claim.

    ``item`` is the leased item, or None when nothing was claimable. ``exhausted`` holds items
    whose lease was lost more than ``JOB_ITEM_MAX_ATTEMPTS`` times; they left the queue and the
    caller fails them. ``retry_in`` is set only when nothing was claimable: the seconds until the
    earliest deferred domain that still holds items frees, or None when no domain is deferred.
    """

    item: QueueItem | None
    exhausted: list[QueueItem]
    retry_in: float | None


@dataclass(frozen=True)
class QueueCounts:
    """A snapshot of one pool's queue.

    - ``pending``: items waiting in the domain lists, claimable or deferred; leased items are not
      counted. An item is counted again when its lease is taken back or it is deferred.
    - ``leased``: items currently leased by a worker, including leases that expired and were not
      taken back yet.
    - ``clients``: clients with pending items.
    - ``domains``: distinct domains with pending items, over all clients.
    """

    pending: int
    leased: int
    clients: int
    domains: int


def domain_of(url: str) -> str:
    """The queue's domain of ``url``: the host as the ``DomainGate`` keys it, or ``-``."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return NO_DOMAIN
    if not host:
        return NO_DOMAIN
    return normalize_host(host) or NO_DOMAIN


def _escape(name: str) -> str:
    for char, code in _ESCAPES.items():
        name = name.replace(char, code)
    return name


def _unescape(name: str) -> str:
    return _ESCAPED.sub(lambda match: _UNESCAPES[match.group()], name)


def _text(value: bytes | str) -> str:
    return value if isinstance(value, str) else value.decode()


def _member(item: QueueItem, attempts: int) -> str:
    return f"{item.job_id}:{item.index}:{attempts}"


def _item(entry: Sequence[Any]) -> QueueItem:
    """A QueueItem from a script's ``{item, client, domain}``."""
    member, client, domain = (_text(value) for value in entry)
    job_id, index, attempts = member.rsplit(":", 2)
    return QueueItem(
        job_id=job_id,
        index=int(index),
        client=_unescape(client),
        domain=_unescape(domain),
        attempts=int(attempts),
    )


class FairQueue:
    """The fair queue of one worker pool, shared by all pods through Valkey.

    Claims rotate between clients, then between the domains of a client, so a small batch is not
    queued behind a large one and a small domain not behind a large one. A domain is skipped
    while it is deferred or holds ``OUTBOUND_MAX_CONCURRENCY_PER_HOST`` leases. A lease that
    expires (its worker died) puts the item back at the front of its domain.
    """

    def __init__(
        self,
        valkey: Redis,
        settings: Settings,
        pool: str = "extract",
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not _POOL.fullmatch(pool):
            raise ValueError(f"Invalid queue pool name: {pool!r}")
        self._settings = settings
        self._clock = clock
        self._prefix = f"q:{{{pool}}}:"
        self._keys = [
            self._prefix + name
            for name in ("clients", "notbefore", "running", "leases", "leased", "pending")
        ]
        self._enqueue = valkey.register_script(_ENQUEUE)
        self._claim = valkey.register_script(_CLAIM)
        self._complete = valkey.register_script(_COMPLETE)
        self._defer = valkey.register_script(_DEFER)
        self._counts = valkey.register_script(_COUNTS)

    async def enqueue(self, job_id: str, client: str, urls: list[str]) -> None:
        """Queue the items ``0 … len(urls) - 1`` of a job for ``client``, in index order."""
        token = _escape(client)
        for start in range(0, len(urls), _ENQUEUE_CHUNK):
            args: list[str] = [self._prefix, token]
            for index, url in enumerate(urls[start : start + _ENQUEUE_CHUNK], start):
                args += [_escape(domain_of(url)), f"{job_id}:{index}:0"]
            await self._enqueue(keys=self._keys, args=args)

    async def claim(self, lease_seconds: float) -> Claim:
        """Take back expired leases, then lease the next fair item for ``lease_seconds``."""
        now = self._clock()
        settings = self._settings
        claimed, retry_in, exhausted = await self._claim(
            keys=self._keys,
            args=[
                self._prefix,
                repr(now),
                f"{now + lease_seconds:.6f}",
                settings.job_item_max_attempts,
                settings.outbound_max_concurrency_per_host,
                _RECLAIM_LIMIT,
            ],
        )
        retry = _text(retry_in)
        return Claim(
            item=_item(claimed) if claimed else None,
            exhausted=[_item(entry) for entry in exhausted],
            retry_in=float(retry) if retry else None,
        )

    async def complete(self, item: QueueItem) -> bool:
        """End the item's lease. False when the lease had already expired and the item was
        taken back (another worker may run it again); the caller still stores its result."""
        done = await self._complete(
            keys=self._keys, args=[self._prefix, _member(item, item.attempts)]
        )
        return bool(int(done))

    async def defer(self, item: QueueItem, until: float) -> None:
        """Give the item back to the front of its domain with ``attempts + 1`` and keep the
        domain from being claimed before ``until`` (a later not-before already set stays).

        Nothing changes when the lease was lost: the item is queued again already.
        """
        await self._defer(
            keys=self._keys,
            args=[
                self._prefix,
                _member(item, item.attempts),
                _member(item, item.attempts + 1),
                f"{until:.6f}",
            ],
        )

    async def counts(self) -> QueueCounts:
        """How many items are pending and leased, and how many clients and domains wait."""
        pending, leased, clients, domains = await self._counts(keys=self._keys, args=[self._prefix])
        return QueueCounts(
            pending=max(int(pending), 0),
            leased=int(leased),
            clients=int(clients),
            domains=int(domains),
        )
