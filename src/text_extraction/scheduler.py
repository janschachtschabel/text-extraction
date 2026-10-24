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
- ``q:{<pool>}:leased``               hash: leased item -> ``<client>\\t<domain>\\t<lease token>``
- ``q:{<pool>}:pending``              counter: items in the domain lists (claimable or deferred)

An item is ``<job_id>:<index>:<attempts>``. In keys and values, ``<client>`` and ``<domain>`` are
escaped (``%`` -> ``%25``, ``:`` -> ``%3A``, tab -> ``%09``): client names are free text and IPv6
hosts contain colons, so unescaped names could make two lists share one key.

Every lease gets a random token. ``complete`` and ``defer`` act only while the caller's token is
the current one, so a worker whose lease was taken back cannot end the lease of the next worker.

Invariants, kept by every script: a domain is in a client's rotation exactly while its list holds
items, and a client is in ``clients`` exactly while it has domains. The keys carry no TTL; the
lists empty themselves as items are claimed. Times are epoch seconds from the injected clock
(every pod needs a synchronised clock), passed into the scripts, which never read Valkey's TIME.

The scripts start with a ``#!lua`` line (Redis 7 / Valkey 7.2 script flags). In cluster mode the
server then refuses any key outside the declared slot, and the read-only counting script runs
under ``noeviction`` memory pressure and on replicas.
"""

import re
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import structlog
from redis.asyncio import Redis

from text_extraction.outbound import normalize_host
from text_extraction.settings import Settings

_log = structlog.get_logger(__name__)

# The domain of a URL without a valid host.
NO_DOMAIN = "-"

# Items per enqueue script call, so one call never carries a whole large batch.
_ENQUEUE_CHUNK = 500
# Expired leases a claim takes back, at most, before it looks for an item.
_RECLAIM_LIMIT = 20
# The retry_in of a claim that spent its scan budget: the next claim goes on where it stopped.
_SCAN_RETRY_SECONDS = 0.05

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

-- Ends the lease of an item when it is held under `token` (nil: under any token). Returns client
-- and domain, or nil when there is no such lease; then nothing changes.
local function unlease(member, token)
  local where = redis.call('HGET', leased, member)
  if not where then
    return nil
  end
  local client, domain, holder = string.match(where, '^([^\\t]*)\\t([^\\t]*)\\t([^\\t]*)$')
  if token and holder ~= token then
    return nil
  end
  redis.call('ZREM', leases, member)
  redis.call('HDEL', leased, member)
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
    "#!lua\n"
    + _PREAMBLE
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

# ARGV = prefix, now, lease expiry, max attempts, per-domain cap, reclaim limit, scan budget,
# lease token, scan retry.
# 1. Takes back up to `reclaim limit` expired leases: each goes to the front of its list with
#    attempts + 1, capped at max attempts + 1 (exhausted: the caller fails it).
# 2. Walks `clients` from the front and each client's domains from the front, reading them with
#    LRANGE and their not-before and lease counts with HMGET. A domain is skipped while its
#    not-before lies in the future or it holds `cap` leases. At most `scan budget` client-domain
#    pairs are visited (a client without domains counts as one). A client whose domains the
#    budget ran out in moves to the end of `clients` with the visited ones.
# 3. Leases the first item of the first claimable domain (ZADD NX: a duplicate of a leased item
#    is dropped). The visited heads of both rotations move to their ends, the claimed domain and
#    client last; a list that became empty leaves its rotation.
# A claim that finds nothing writes only when it ran out of budget (the visited heads move to
# the ends, so the next claim goes on there) or found a stale rotation entry to drop.
# Returns {claimed, retry_in, incomplete}: claimed is {item, client, domain} or empty. retry_in,
# only when nothing was claimed, is `scan retry` when the budget ran out (incomplete = 1), else
# the time until the earliest deferred domain frees, or ''. Numbers go back as strings: Redis
# would cut a Lua number.
_CLAIM = (
    "#!lua\n"
    + _PREAMBLE
    + """
local now = tonumber(ARGV[2])
local expiry = ARGV[3]
local max_attempts = tonumber(ARGV[4])
local cap = tonumber(ARGV[5])
local budget = tonumber(ARGV[7])
local token = ARGV[8]

local expired = redis.call('ZRANGEBYSCORE', leases, '-inf', ARGV[2], 'LIMIT', 0, ARGV[6])
for _, member in ipairs(expired) do
  local client, domain = unlease(member, nil)
  if client then
    local job, index, attempts = string.match(member, '^(.*):(%d+):(%d+)$')
    attempts = math.min(tonumber(attempts) + 1, max_attempts + 1)
    requeue(client, domain, job .. ':' .. index .. ':' .. string.format('%d', attempts))
  else
    redis.call('ZREM', leases, member)
  end
end

-- Moves the first `count` entries of `key` (the values in `head`) to its end, leaving out the
-- positions marked in `drop`.
local function rotate(key, head, count, drop)
  if count == 0 then
    return
  end
  if count == 1 and not drop[1] then
    redis.call('LMOVE', key, key, 'LEFT', 'RIGHT')
    return
  end
  redis.call('LTRIM', key, count, -1)
  local keep = {}
  for i = 1, count do
    if not drop[i] then
      keep[#keep + 1] = head[i]
    end
  end
  if #keep > 0 then
    redis.call('RPUSH', key, unpack(keep))
  end
end

-- Pops items until one can be leased; a duplicate of a leased item is dropped.
local function lease_next(items)
  while true do
    local member = redis.call('LPOP', items)
    if not member then
      return nil
    end
    redis.call('DECR', pending)
    if redis.call('ZADD', leases, 'NX', expiry, member) == 1 then
      return member
    end
  end
end

local names = redis.call('LRANGE', clients, 0, budget - 1)
local total = redis.call('LLEN', clients)
local gone = {}
local spent = 0
local visited = 0
local earliest = nil
for ci, client in ipairs(names) do
  if spent >= budget then
    break
  end
  local domains = domains_key(client)
  local count = redis.call('LLEN', domains)
  if count == 0 then
    gone[ci] = true
    spent = spent + 1
  else
    local take = math.min(count, budget - spent)
    local head = redis.call('LRANGE', domains, 0, take - 1)
    local free_at = redis.call('HMGET', notbefore, unpack(head))
    local held = redis.call('HMGET', running, unpack(head))
    local empty = {}
    for di, domain in ipairs(head) do
      local wait = tonumber(free_at[di]) or 0
      if wait > now then
        if earliest == nil or wait < earliest then
          earliest = wait
        end
      elseif (tonumber(held[di]) or 0) < cap then
        local items = items_key(client, domain)
        local member = lease_next(items)
        if member then
          redis.call('HSET', leased, member, client .. '\\t' .. domain .. '\\t' .. token)
          redis.call('HINCRBY', running, domain, 1)
          if free_at[di] then
            redis.call('HDEL', notbefore, domain)
          end
          if redis.call('LLEN', items) == 0 then
            empty[di] = true
          end
          rotate(domains, head, di, empty)
          if redis.call('LLEN', domains) == 0 then
            gone[ci] = true
          end
          rotate(clients, names, ci, gone)
          return {{member, client, domain}, '', 0}
        end
        empty[di] = true
      end
    end
    spent = spent + take
    if take < count then
      -- The client's domain head has moved on; the client goes to the end of the rotation like
      -- one that was visited completely, so it cannot hold the scan on its own domains.
      rotate(domains, head, take, empty)
      rotate(clients, names, ci, gone)
      return {{}, ARGV[9], 1}
    end
    if next(empty) then
      rotate(domains, head, take, empty)
      if redis.call('LLEN', domains) == 0 then
        gone[ci] = true
      end
    end
  end
  visited = ci
end
if visited < total then
  rotate(clients, names, visited, gone)
  return {{}, ARGV[9], 1}
end
if next(gone) then
  rotate(clients, names, visited, gone)
end
local retry_in = ''
if earliest then
  retry_in = string.format('%.6f', earliest - now)
end
return {{}, retry_in, 0}
"""
)

# ARGV = prefix, item, lease token. Ends that lease; returns 1, or 0 when the item is not leased
# under that token (it expired and was taken back, or was completed already): nothing changes.
_COMPLETE = (
    "#!lua\n"
    + _PREAMBLE
    + """
if unlease(ARGV[2], ARGV[3]) then
  return 1
end
return 0
"""
)

# ARGV = prefix, item, lease token, the item with attempts + 1, not-before. Ends the lease, moves
# the domain's not-before to the later of its current value and the given one, and puts the item
# back at the front of its list. Returns 0 and changes nothing when the lease is not held.
_DEFER = (
    "#!lua\n"
    + _PREAMBLE
    + """
local client, domain = unlease(ARGV[2], ARGV[3])
if not client then
  return 0
end
if tonumber(ARGV[5]) > tonumber(redis.call('HGET', notbefore, domain) or '0') then
  redis.call('HSET', notbefore, domain, ARGV[5])
end
requeue(client, domain, ARGV[4])
return 1
"""
)

# ARGV = prefix. Returns {pending, leased, clients, distinct domains}; writes nothing.
_COUNTS = (
    "#!lua flags=no-writes\n"
    + _PREAMBLE
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
    """One claimed batch item. ``attempts`` counts its lost leases plus its deferrals.

    ``lease`` identifies the claim that handed the item out: ``complete`` and ``defer`` act only
    while that lease is still the item's current one.
    """

    job_id: str
    index: int
    client: str
    domain: str
    attempts: int
    lease: str = ""


@dataclass(frozen=True)
class Claim:
    """The answer to one claim.

    ``item`` is the leased item, or None when nothing was claimable.

    ``exhausted`` is True when the item lost more leases (plus deferrals) than
    ``JOB_ITEM_MAX_ATTEMPTS`` allows. It is leased like any other item: the caller records its
    failure, then calls ``complete``. A caller that dies in between leaves the lease to expire,
    and the item is offered again, still exhausted.

    ``scan_incomplete`` is True when nothing was claimed because the claim ran out of
    ``SCHEDULER_CLAIM_SCAN_BUDGET`` before it had looked at every client and domain. The next
    claim goes on where this one stopped, so ``retry_in`` is short (0.05 s). Many blocked domains
    give this answer on every claim, so a caller backs off exponentially on consecutive
    ``scan_incomplete`` answers: 0.05 s, doubling, capped at its idle sleep (1 s), and back to
    0.05 s once an item is claimed. The queue keeps no scan progress beyond the rotations.

    ``retry_in`` is set only when nothing was claimed. After a full pass it is the seconds until
    the earliest deferred domain that still holds items frees, or None when no domain is deferred.
    """

    item: QueueItem | None
    exhausted: bool = False
    retry_in: float | None = None
    scan_incomplete: bool = False


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


def _item(entry: Sequence[Any], lease: str) -> QueueItem:
    """A QueueItem from a script's ``{item, client, domain}``."""
    member, client, domain = (_text(value) for value in entry)
    job_id, index, attempts = member.rsplit(":", 2)
    return QueueItem(
        job_id=job_id,
        index=int(index),
        client=_unescape(client),
        domain=_unescape(domain),
        attempts=int(attempts),
        lease=lease,
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
        self._pool = pool
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
        lease = uuid.uuid4().hex
        claimed, retry_in, incomplete = await self._claim(
            keys=self._keys,
            args=[
                self._prefix,
                repr(now),
                f"{now + lease_seconds:.6f}",
                settings.job_item_max_attempts,
                settings.outbound_max_concurrency_per_host,
                _RECLAIM_LIMIT,
                settings.scheduler_claim_scan_budget,
                lease,
                f"{_SCAN_RETRY_SECONDS:.6f}",
            ],
        )
        if claimed:
            item = _item(claimed, lease)
            return Claim(item=item, exhausted=item.attempts > settings.job_item_max_attempts)
        retry = _text(retry_in)
        return Claim(
            item=None,
            retry_in=float(retry) if retry else None,
            scan_incomplete=bool(int(incomplete)),
        )

    async def complete(self, item: QueueItem) -> bool:
        """End the item's lease. False when the lease had already expired and the item was
        taken back (another worker may run it again); the caller still stores its result."""
        done = await self._complete(
            keys=self._keys, args=[self._prefix, _member(item, item.attempts), item.lease]
        )
        return bool(int(done))

    async def defer(self, item: QueueItem, until: float) -> None:
        """Give the item back to the front of its domain with ``attempts + 1`` and keep the
        domain from being claimed before ``until`` (a later not-before already set stays).

        Nothing changes when the lease was lost: the item is queued again already. The caller
        fails an item at ``JOB_ITEM_MAX_ATTEMPTS`` instead of deferring it; attempts never go
        beyond that maximum plus one.
        """
        attempts = min(item.attempts + 1, self._settings.job_item_max_attempts + 1)
        await self._defer(
            keys=self._keys,
            args=[
                self._prefix,
                _member(item, item.attempts),
                item.lease,
                _member(item, attempts),
                f"{until:.6f}",
            ],
        )

    async def counts(self) -> QueueCounts:
        """How many items are pending and leased, and how many clients and domains wait."""
        pending, leased, clients, domains = await self._counts(keys=self._keys, args=[self._prefix])
        counts = QueueCounts(
            pending=int(pending), leased=int(leased), clients=int(clients), domains=int(domains)
        )
        if counts.pending < 0:
            _log.warning("queue_counter_negative", pool=self._pool, pending=counts.pending)
        return counts
