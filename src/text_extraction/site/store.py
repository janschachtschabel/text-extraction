"""The crawl of a ``site`` job in Valkey (ruling P9-S3): its nodes, seen URLs, children and
counters, next to the job's own keys and under the same hash tag, so one script touches them all.

- ``job:{<id>}:nodes``    hash: item index -> ``SiteNode`` JSON
- ``job:{<id>}:seen``     hash: seen key -> item index; a page under its ``seen_key``, a sitemap
                          or robots item under ``s:<key>`` (a sitemap at a page's URL is another
                          item). The caller passes the plain seen key; the store adds the prefix.
- ``job:{<id>}:children`` hash: parent index -> the stored answer of the parent's ``extend``
                          (batch 0), ``<parent>:<batch>`` -> that of a later batch
- ``job:{<id>}:site``     hash: ``scope`` (``SiteScope`` JSON, set once), ``pages`` (page items,
                          the start page included), ``files`` (sitemap items), ``entries``
                          (sitemap entries granted) and ``e:<index>`` (the grant of one item)

Every write gives the keys the remaining TTL of ``meta``, so they expire with the job; ``delete``
and ``shorten`` of ``JobStore`` remove or cut them.

``extend`` is the one step that grows the job: in one script per batch of at most
``EXTEND_BATCH`` candidates it appends the parent's new items (``urls``, ``total``, node, seen
key, counter) and, with batch 0, its finished items (with result, done bit and the counters
``_STORE_RESULT`` keeps), then stores its answer under the parent and batch. A parent that runs
again (a lost lease) gets each batch's answer back and appends nothing, so its children can be
queued again without new items. Finished and new items both raise ``total``, and the parent
itself is not done yet, so an ``extend`` never completes the job; one for a parent that has its
result already (a worker whose lease was taken back finishing late) appends nothing at all.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractResult
from text_extraction.jobs import JobLost, JobStore, job_key
from text_extraction.settings import Settings
from text_extraction.site.types import SiteNode, SiteScope
from text_extraction.site.urls import seen_key
from text_extraction.valkey import reply_text

__all__ = ["EXTEND_BATCH", "Candidate", "ExtendAnswer", "Finished", "SiteContext", "SiteStore"]

# The most candidates of one ``extend`` call (ruling R-P9-1): a parent with more sends them in
# batches, so one script holds Valkey for tens of milliseconds at most.
EXTEND_BATCH = 1000

# A candidate without an index in a stored answer.
_NO_INDEX = "-"

# The prefix of a sitemap's or the robots item's seen key.
_FILE_PREFIX = "s:"
# Which cap a candidate's role counts against: pages, files (sitemaps) or none (robots.txt).
_CAPS = {"page": "p", "sitemap": "f", "robots": "-"}

_NOT_FOUND = -1
_OUT_OF_RANGE = -2

# Shared by the scripts: the TTL to give a crawl key, the remaining one of meta (KEYS[1]).
_TTL = """
local function remaining_ttl(fallback)
  local remaining = redis.call('PTTL', KEYS[1])
  if remaining < 0 then
    return tonumber(fallback)
  end
  return remaining
end
"""

# KEYS = meta, nodes, seen, site; ARGV = the start URL's seen key, node 0 JSON, ttl_ms (used when
# meta has no TTL). Returns 1 (a job with node 0 already is left as it is), or -1 when the job is
# gone (nothing is written).
_INIT = (
    "#!lua\n"
    + _TTL
    + """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return -1
end
if redis.call('HEXISTS', KEYS[2], '0') == 1 then
  return 1
end
redis.call('HSET', KEYS[2], '0', ARGV[2])
redis.call('HSET', KEYS[3], ARGV[1], '0')
redis.call('HSET', KEYS[4], 'pages', '1')
local ttl = remaining_ttl(ARGV[3])
for i = 2, 4 do
  redis.call('PEXPIRE', KEYS[i], ttl)
end
return 1
"""
)

# KEYS = meta, urls, results, done, nodes, seen, children, site.
# ARGV = parent, max_pages, max_files, ttl_ms, scope JSON ('' for none), candidate count,
# finished count, alias count, the ``children`` field of the call (``<parent>`` for batch 0,
# ``<parent>:<batch>`` after it), then per candidate key (prefixed), url, node JSON, add ('1'/'0'),
# cap ('p', 'f', '-'); per finished item url, node JSON, result JSON, status; per alias its key.
# Returns {code, answer, url...}: code 1 (appended), 0 (repeat: the stored answer of the batch, or
# the parent has its result already) or -1 (job gone) / -2 (parent outside the job), nothing
# written then. The answer is ``capped|indexes|new``: capped '1' or '0', the candidates' indexes
# ('-' for none; '' only when there are no candidates) and the new indexes, each comma-separated;
# the URLs of the new indexes follow it.
_EXTEND = (
    "#!lua\n"
    + _TTL
    + """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return {-1}
end
local parent = ARGV[1]
local total = tonumber(redis.call('HGET', KEYS[1], 'total'))
if tonumber(parent) < 0 or tonumber(parent) >= total then
  return {-2}
end
local ttl = remaining_ttl(ARGV[4])
if ARGV[5] ~= '' then
  redis.call('HSETNX', KEYS[8], 'scope', ARGV[5])
  redis.call('PEXPIRE', KEYS[8], ttl)
end

local function with_urls(code, answer)
  local reply = {code, answer}
  local new = string.match(answer, '|([^|]*)$')
  for index in string.gmatch(new, '%d+') do
    reply[#reply + 1] = redis.call('LINDEX', KEYS[2], tonumber(index))
  end
  return reply
end

local field = ARGV[9]
local stored = redis.call('HGET', KEYS[7], field)
if stored then
  return with_urls(0, stored)
end

local candidates = tonumber(ARGV[6])
local finished = tonumber(ARGV[7])
local aliases = tonumber(ARGV[8])
local first_candidate = 10
local first_finished = first_candidate + 5 * candidates
local first_alias = first_finished + 4 * finished
-- A parent with its result already: its links count no more; answer the look-ups only.
local closed = redis.call('GETBIT', KEYS[4], tonumber(parent)) == 1

local indexes = {}
local new = {}
local capped = '0'
local appended = 0
local pages = tonumber(redis.call('HGET', KEYS[8], 'pages') or '0')
local files = tonumber(redis.call('HGET', KEYS[8], 'files') or '0')
local max_pages = tonumber(ARGV[2])
local max_files = tonumber(ARGV[3])

local function append(url, node)
  local index = total
  redis.call('RPUSH', KEYS[2], url)
  redis.call('HSET', KEYS[5], tostring(index), node)
  total = total + 1
  appended = appended + 1
  return index
end

if not closed then
  for i = 0, finished - 1 do
    local base = first_finished + 4 * i
    local index = append(ARGV[base], ARGV[base + 1])
    redis.call('HSET', KEYS[3], tostring(index), ARGV[base + 2])
    redis.call('SETBIT', KEYS[4], index, 1)
    redis.call('HINCRBY', KEYS[1], 'done', 1)
    redis.call('HINCRBY', KEYS[1], ARGV[base + 3], 1)
  end
  -- Before the candidates: a redirected page's link to its own final URL is the page.
  for i = 0, aliases - 1 do
    redis.call('HSETNX', KEYS[6], ARGV[first_alias + i], parent)
  end
end

for i = 0, candidates - 1 do
  local base = first_candidate + 5 * i
  local key = ARGV[base]
  local index = redis.call('HGET', KEYS[6], key)
  if index then
    indexes[#indexes + 1] = index
  elseif closed or ARGV[base + 3] ~= '1' then
    indexes[#indexes + 1] = '-'
  else
    local cap = ARGV[base + 4]
    if cap == 'p' and pages >= max_pages or cap == 'f' and files >= max_files then
      indexes[#indexes + 1] = '-'
      capped = '1'
    else
      if cap == 'p' then
        pages = pages + 1
      elseif cap == 'f' then
        files = files + 1
      end
      local appended_index = append(ARGV[base + 1], ARGV[base + 2])
      redis.call('HSET', KEYS[6], key, tostring(appended_index))
      indexes[#indexes + 1] = tostring(appended_index)
      new[#new + 1] = tostring(appended_index)
    end
  end
end

local answer = capped .. '|' .. table.concat(indexes, ',') .. '|' .. table.concat(new, ',')
if closed then
  return {0, answer}
end

if appended > 0 then
  redis.call('HSET', KEYS[1], 'total', total)
end
redis.call('HSET', KEYS[8], 'pages', pages, 'files', files)
redis.call('HSET', KEYS[7], field, answer)
for i = 2, 8 do
  if redis.call('EXISTS', KEYS[i]) == 1 then
    redis.call('PEXPIRE', KEYS[i], ttl)
  end
end
return with_urls(1, answer)
"""
)

# KEYS = meta, site; ARGV = index, wanted, max entries, ttl_ms. Grants the item up to ``wanted``
# entries of what is left of the crawl's ``max entries``, once: a later call for the index
# answers the same grant. Returns the grant, or -1 when the job is gone.
_RESERVE_ENTRIES = (
    "#!lua\n"
    + _TTL
    + """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return -1
end
local field = 'e:' .. ARGV[1]
local granted = redis.call('HGET', KEYS[2], field)
if granted then
  return tonumber(granted)
end
local entries = tonumber(redis.call('HGET', KEYS[2], 'entries') or '0')
local grant = math.min(tonumber(ARGV[2]), tonumber(ARGV[3]) - entries)
if grant < 0 then
  grant = 0
end
redis.call('HSET', KEYS[2], field, grant, 'entries', entries + grant)
redis.call('PEXPIRE', KEYS[2], remaining_ttl(ARGV[4]))
return grant
"""
)


@dataclass(frozen=True)
class Candidate:
    """An item a parent may append: ``key`` is the URL's ``seen_key`` (the store files sitemap
    and robots items under ``s:<key>``), ``url`` the URL it is fetched by, ``node`` what it is.
    ``add`` False only looks the key up (deeper than asked, or the memory guard is high)."""

    key: str
    url: str
    node: SiteNode
    add: bool


@dataclass(frozen=True)
class Finished:
    """An item that is appended with its result (a sitemap's part 2 and up): done at once, never
    queued and never seen."""

    url: str
    node: SiteNode
    envelope: Envelope[ExtractResult]


@dataclass(frozen=True)
class ExtendAnswer:
    """What ``extend`` did: per candidate its item index (None: not seen and not appended),
    the new items to queue as ``(index, url)``, whether a cap refused a candidate, and whether
    the answer is a repeat (the parent ran before, or has its result already: queue ``new``
    again, if it holds anything, and append nothing)."""

    indexes: list[int | None]
    new: list[tuple[int, str]]
    capped: bool
    repeat: bool


@dataclass(frozen=True)
class SiteContext:
    """What an item of a site job needs to run: its node (None: unknown) and the crawl's scope
    (None until item 0 sets it)."""

    node: SiteNode | None
    scope: SiteScope | None


def _children_field(parent: int, batch: int) -> str:
    """Where the answer of a parent's batch is stored in ``children``."""
    return str(parent) if batch == 0 else f"{parent}:{batch}"


def _decode_answer(answer: str) -> tuple[bool, list[int | None], list[int]]:
    capped, indexes, new = answer.split("|")
    return (
        capped == "1",
        [None if value == _NO_INDEX else int(value) for value in indexes.split(",")]
        if indexes
        else [],
        [int(value) for value in new.split(",")] if new else [],
    )


class SiteStore:
    """Reads and grows the crawl of ``site`` jobs, for all pods through Valkey."""

    def __init__(self, valkey: Redis, job_store: JobStore, settings: Settings) -> None:
        self._valkey = valkey
        self._jobs = job_store
        self._ttl_ms = settings.job_ttl_seconds * 1000
        self._max_files = settings.site_max_sitemap_files
        self._max_entries = settings.site_max_sitemap_urls
        self._init = valkey.register_script(_INIT)
        self._extend = valkey.register_script(_EXTEND)
        self._reserve_entries = valkey.register_script(_RESERVE_ENTRIES)

    async def init(self, job_id: str, url: str) -> None:
        """Make item 0 the start page (depth 0, no parent), seen under the URL's key, and count
        it as the first page. Raises ``JobLost`` when the job is gone."""
        node = SiteNode(role="page", depth=0, parent=None)
        outcome = await self._init(
            keys=[job_key(job_id, kind) for kind in ("meta", "nodes", "seen", "site")],
            args=[seen_key(url), node.model_dump_json(), self._ttl_ms],
        )
        if outcome == _NOT_FOUND:
            raise JobLost(job_id)

    async def context(self, job_id: str, index: int) -> SiteContext:
        """The node of item ``index`` and the crawl's scope."""
        async with self._valkey.pipeline(transaction=False) as pipe:
            pipe.hget(job_key(job_id, "nodes"), str(index))
            pipe.hget(job_key(job_id, "site"), "scope")
            node, scope = await pipe.execute()
        return SiteContext(
            node=SiteNode.model_validate_json(node) if node is not None else None,
            scope=SiteScope.model_validate_json(scope) if scope is not None else None,
        )

    async def extend(
        self,
        job_id: str,
        parent: int,
        candidates: Sequence[Candidate],
        *,
        finished: Sequence[Finished] = (),
        aliases: Sequence[str] = (),
        max_pages: int,
        scope: SiteScope | None = None,
        batch: int = 0,
    ) -> ExtendAnswer:
        """Grow the job by what item ``parent`` found, in one step per batch (P9-S3, R-P9-1).

        A candidate whose key is seen answers that item's index. An ``add`` candidate under its
        cap (page items fewer than ``max_pages``, sitemap items fewer than
        ``SITE_MAX_SITEMAP_FILES``; the robots item has none) is appended; one over it answers
        None and sets ``capped``. ``finished`` items are appended first, done, with their
        results. ``aliases`` (the parent's other URLs, its ``final_url`` after a redirect) are
        seen as the parent, unless another item has the key. ``scope`` is kept if none is set.
        The answer is stored under the parent and ``batch``: a second call for them answers the
        same and appends nothing (``repeat``).

        A parent with more than ``EXTEND_BATCH`` candidates sends them in batches 0, 1, 2, ...
        of at most that many, in the same order on a rerun; ``finished``, ``aliases`` and
        ``scope`` go with batch 0 only. The batches share the caps, and each one is all or
        nothing. Raises ``JobLost`` when the job is gone and ``ValueError`` when ``parent`` is
        outside the job (nothing is written then), or for a call against these rules."""
        if batch < 0:
            raise ValueError(f"batch {batch} is negative")
        if len(candidates) > EXTEND_BATCH:
            raise ValueError(
                f"{len(candidates)} candidates: one batch takes at most {EXTEND_BATCH}"
            )
        if batch > 0 and (finished or aliases or scope is not None):
            raise ValueError("finished items, aliases and the scope go with batch 0 only")
        args: list[Any] = [
            parent,
            max_pages,
            self._max_files,
            self._ttl_ms,
            scope.model_dump_json() if scope is not None else "",
            len(candidates),
            len(finished),
            len(aliases),
            _children_field(parent, batch),
        ]
        for candidate in candidates:
            cap = _CAPS.get(candidate.node.role)
            if cap is None:
                raise ValueError(f"A {candidate.node.role} item cannot be a candidate")
            key = candidate.key if candidate.node.role == "page" else _FILE_PREFIX + candidate.key
            add = "1" if candidate.add else "0"
            args += [key, candidate.url, candidate.node.model_dump_json(), add, cap]
        for item in finished:
            result = self._jobs.encode_result(item.envelope)
            args += [item.url, item.node.model_dump_json(), result, item.envelope.status]
        args += [seen_key(alias) for alias in aliases]
        reply = await self._extend(
            keys=[
                job_key(job_id, kind)
                for kind in ("meta", "urls", "results", "done", "nodes", "seen", "children", "site")
            ],
            args=args,
        )
        code = int(reply[0])
        if code == _NOT_FOUND:
            raise JobLost(job_id)
        if code == _OUT_OF_RANGE:
            raise ValueError(f"item {parent} is outside job {job_id}")
        capped, indexes, new = _decode_answer(reply_text(reply[1]))
        urls = [reply_text(url) for url in reply[2:]]
        return ExtendAnswer(
            indexes=indexes,
            new=list(zip(new, urls, strict=True)),
            capped=capped,
            repeat=code == 0,
        )

    async def reserve_entries(self, job_id: str, index: int, wanted: int) -> int:
        """Grant item ``index`` up to ``wanted`` sitemap entries of what is left of
        ``SITE_MAX_SITEMAP_URLS``; a later call for the item answers its first grant. Raises
        ``JobLost`` when the job is gone."""
        granted = int(
            await self._reserve_entries(
                keys=[job_key(job_id, "meta"), job_key(job_id, "site")],
                args=[index, max(wanted, 0), self._max_entries, self._ttl_ms],
            )
        )
        if granted == _NOT_FOUND:
            raise JobLost(job_id)
        return granted

    async def stored_children(self, job_id: str, parent: int) -> list[tuple[int, str]]:
        """The items that the stored answers of ``parent`` appended, as ``(index, url)``, its
        batches in order; empty when it has none (it never extended, or the job is gone)."""
        children = job_key(job_id, "children")
        answers: list[tuple[int, str]] = []
        stored = await self._valkey.hget(children, str(parent))
        if stored is not None:
            answers.append((0, reply_text(stored)))
        # Many batches of a large page: 1,000 fields a step, not the default 10.
        scan = self._valkey.hscan_iter(children, match=f"{parent}:*", count=1000)
        async for field, value in scan:
            answers.append((int(reply_text(field).split(":", 1)[1]), reply_text(value)))
        new = [index for _, answer in sorted(answers) for index in _decode_answer(answer)[2]]
        if not new:
            return []
        async with self._valkey.pipeline(transaction=False) as pipe:
            for index in new:
                pipe.lindex(job_key(job_id, "urls"), index)
            urls = await pipe.execute()
        return [
            (index, reply_text(url))
            for index, url in zip(new, urls, strict=True)
            if url is not None
        ]
