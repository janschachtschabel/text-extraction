"""Job records in Valkey: the storage under the job API and the workers.

A job lives in four keys (a ``site`` job in four more, below); the braces around the id are a
literal cluster hash tag, so one job's keys share a slot and a Lua script can touch all of them:

- ``job:{<id>}:meta``     hash: kind, owner, owner_role, options (JSON; ``ocr``, ``transcribe``
                          and ``languages`` only when not the default), state, created_at,
                          finished_at, total, done, success, partial, failed, lease_until,
                          trace_id (of the request that created the job, when it had one),
                          secrets (``1`` when the job was created with caller secrets)
- ``job:{<id>}:urls``     list: the URLs in input order; an empty string for an uploaded file's
                          item, which has no URL (kind ``file``)
- ``job:{<id>}:results``  hash: item index -> ``Envelope[ExtractResult]`` as JSON
- ``job:{<id>}:done``     bitmap: bit ``index`` is set once that item has its result

All keys expire ``JOB_TTL_SECONDS`` after creation and again after completion. ``results`` and
``done`` do not exist before the first result; they take the remaining TTL of ``meta`` then.

Items that workers claim (D8, ruling P10-S11) have one more key:

- ``job:{<id>}:runs``     hash: item index -> ``<claim number>|<pool>|<epoch ms>``, the item's
                          last claim by a worker (``record_claim``, one script per claim). It
                          lives as long as the job (ruling R-P10-1): each write takes the
                          remaining TTL of ``meta``, and the scripts that renew or align the
                          job's TTL (the completing result write, ``SiteStore.extend``) renew
                          it too. ``delete`` removes it, ``shorten`` cuts it. A P1 job's item,
                          which runs in the API pod, has no entry.

Browser jobs (package 4) may have these as well:

- ``job:{<id>}:secrets``  string: the caller's ``BrowserSecrets`` as JSON (actions, cookies,
                          headers; B6). Kept ``BROWSER_QUEUE_WAIT_SECONDS`` + the browser
                          lease (``browser_lease_seconds``) + 60 seconds at most, and deleted by
                          the worker once it has finished the item. No read of the job shows them.
- ``job:{<id>}:drafts``   hash: item index -> the fast ``Envelope[ExtractResult]`` of an ``auto``
                          item that moved to the browser (B5), with the job's remaining TTL;
                          deleted by the browser worker once the item has its outcome. The nlp
                          worker's items have one too: the unprocessed envelope, deleted once
                          the item has its outcome
- ``job:{<id>}:artifact:<name>`` string: a file of the job (``screenshot.png``, B7), kept
                          ``ARTIFACT_TTL_SECONDS``

Jobs that wait for a transcription (package 6, M11) may have:

- ``job:{<id>}:asr_key``  string: the caller's key of the external transcription endpoint (the
                          request header ``X-ASR-Api-Key``). Kept ``ASR_INPUT_TTL_SECONDS`` + the
                          ASR lease (``asr_lease_seconds``) at most, never longer than the job;
                          a batch's as long as the job (its items move to pool ``asr`` at any
                          time). Deleted with the job, by ``shorten`` and ``drop_secrets``, by
                          the worker of pool ``asr`` once a sync request's item has its outcome,
                          and with a batch's last result. No read of the job shows it, and it is
                          never logged.

Jobs that need LLM work (package 8, ruling S9) may have:

- ``job:{<id>}:llm_key``  string: the caller's key of the LLM endpoint (the request header
                          ``X-LLM-Api-Key``). Kept as long as the job, since the draft the llm
                          worker reads lives that long and the item may wait for the worker
                          until then. Deleted with the job, by ``shorten``, and by whoever
                          writes the item's result (``drop_llm_key``): the worker of any pool
                          (a sync request's job at once, a batch with its last result) and the
                          pod that ran the whole request; a worker that moves the item on to
                          the llm worker keeps it. ``drop_secrets`` leaves it, because the
                          browser, ASR and nlp workers that call it may run before the llm
                          worker. No read of the job shows it, and it is never logged.

Jobs whose items carry bytes (ruling F12: an upload, a document queued for OCR; M9: media queued
for transcription, with an empty body for a URL) have:

- ``job:{<id>}:input:<index>`` hash: ``body`` (the bytes) and ``meta`` (JSON: what the consumer
                          needs to know about them); TTL ``DOCLING_INPUT_TTL_SECONDS`` at most
                          (``ASR_INPUT_TTL_SECONDS`` for media queued for transcription),
                          never longer than the job's. No read of the job shows it; the worker
                          deletes it once the item has its outcome, and ``delete`` and
                          ``shorten`` delete the input of item 0 (the only item of a sync job).

``jobs:{owner:<owner>}:docling`` is a set of an owner's Docling jobs (``add_docling_job``), and
``jobs:{owner:<owner>}:asr`` one of its ASR jobs (``add_asr_job``); a job that is completed or
gone leaves its set when it is counted (``open_docling_jobs``, ``open_asr_jobs``).

All inputs together (documents for OCR, uploads, media for transcription) stay within
``DOCLING_INPUT_BUDGET_BYTES`` (Valkey's memory: a full Valkey under ``noeviction`` refuses the
writes of the job store, the queue and the rate limiter). Three keys of their own hash tag
account for it:

- ``inputs:{budget}:size``   hash: ``<id>:<index>`` -> the input's bytes (body and meta)
- ``inputs:{budget}:expiry`` sorted set: ``<id>:<index>`` scored by when its input expires
                          (Valkey's own clock, in ms)
- ``inputs:{budget}:total``  string: the sum of ``size``

``put_input`` first reserves the input's bytes in one script on these keys: it prunes the
reservations whose expiry has passed (an input that expired by its TTL, which nobody deletes),
then reserves, or refuses with ``InputBudgetExceeded`` when the total would exceed the budget.
Check and reservation are one atomic step, so concurrent writers never pass the budget
together. Only then is the input written; a write that fails gives the reservation back.
Deleting an input (``delete_input``, ``delete``, ``shorten``) gives it back too. The budget's
keys are not in a job's slot, so reservation and write are two scripts: one script on keys of
two hash tags would not run on a Valkey cluster. A pod that dies between them leaves a
reservation that its expiry prunes, never a leak. A reservation lasts the requested TTL even
where the job's shorter life cuts the input's: it then counts a little longer, never shorter.

A ``site`` job (package 9, ruling P9-S3) grows while it runs: its items append new items to
``urls`` (``total`` grows with them). Its crawl lives in four more hashes, written by
``site.store.SiteStore``, each with the remaining TTL of ``meta`` when written (completion renews
only the four keys above and the item runs; nothing reads the crawl keys of a completed job):

- ``job:{<id>}:nodes``    hash: item index -> ``SiteNode`` JSON (role, depth, parent, guessed,
                          part)
- ``job:{<id>}:seen``     hash: seen key (``site.urls.seen_key``) -> item index; sitemap and
                          robots items under ``s:<key>``
- ``job:{<id>}:children`` hash: parent index (``<parent>:<batch>`` for batch 1 and later) ->
                          the stored answer of its ``extend``, so a parent that runs again
                          appends nothing
- ``job:{<id>}:site``     hash: ``scope`` (``SiteScope`` JSON), the counters ``pages``,
                          ``files`` and ``entries``, and ``e:<index>`` (the sitemap entries
                          granted to that item)

A browser job whose result a sync request answered with (``browser_dispatch.py``) is deleted
then; one with a screenshot is kept only as long as the screenshot (``shorten``).

Times are epoch seconds from the injected clock (every pod needs a synchronised clock), stored
as numbers and handed out as UTC datetimes, except ``lease_until``, which stays a number.
"""

import asyncio
import contextlib
import json
import math
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import RedisError

from text_extraction.auth import Principal, Role
from text_extraction.browser_options import BrowserSecrets
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractResult
from text_extraction.job_models import (
    ItemRun,
    JobItem,
    JobKind,
    JobOptions,
    JobProgress,
    JobState,
)
from text_extraction.settings import Settings
from text_extraction.valkey import reply_text

# What one result write did: finished its item (``first``), finished the item and with it the
# job (``last``), or nothing, because the item had its result already (``repeat``).
ResultWrite = Literal["first", "last", "repeat"]
_WRITES: dict[int, ResultWrite] = {0: "repeat", 1: "first", 2: "last"}

# Set in ``data.warnings`` of a result that was stored without its content.
CONTENT_NOT_STORED_WARNING = "content_not_stored_too_large"
CHUNKS_NOT_STORED_WARNING = "chunks_not_stored_too_large"
TABLES_NOT_STORED_WARNING = "tables_not_stored_too_large"

# URLs are pushed in chunks, so one command never carries a whole large batch.
_URL_CHUNK = 1000

# The crawl keys of a site job (P9-S3), which ``delete`` removes and ``shorten`` cuts.
_CRAWL_KINDS = ("nodes", "seen", "children", "site")
# The keys of a job: the four of every job, the item runs, those of browser jobs, the caller's
# ASR and LLM keys and the crawl keys of a site job.
_KINDS = (
    "meta",
    "urls",
    "results",
    "done",
    "runs",
    "secrets",
    "drafts",
    "asr_key",
    "llm_key",
    *_CRAWL_KINDS,
)

# The input of item 0 (the only item of a sync job), which ``delete`` and ``shorten`` remove.
_FIRST_INPUT = "input:0"
# An item without a URL (an upload) is stored with this in the URL list.
_NO_URL = ""
_OWNER_ESCAPES = {"%": "%25", "{": "%7B", "}": "%7D"}

# The files a job can have; ``delete`` removes them by name.
SCREENSHOT_ARTIFACT = "screenshot.png"
ARTIFACT_NAMES = (SCREENSHOT_ARTIFACT,)
_ARTIFACT_NAME = re.compile(r"[a-z0-9_.-]{1,64}", re.ASCII)

_NOT_FOUND = -1
_OUT_OF_RANGE = -2

# KEYS = meta, results, done, urls, runs; ARGV = index, result JSON, envelope status, now, ttl_ms.
# Returns 1 when this call finished the index, 2 when it also finished the job, 0 when the index
# was finished already (nothing changes), -1 when the job is gone and -2 when the index is outside
# the job. The first write also counts the item and, if it was the last, completes the job and
# renews the TTL of all four keys and of the item runs (ruling R-P10-1; a job without ``runs``
# gets none: PEXPIRE of a missing key does nothing).
# Epoch seconds go in and out as strings: Redis would cut a Lua number to an integer.
_STORE_RESULT = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return -1
end
local index = tonumber(ARGV[1])
local total = tonumber(redis.call('HGET', KEYS[1], 'total'))
if index < 0 or index >= total then
  return -2
end
if redis.call('SETBIT', KEYS[3], index, 1) == 1 then
  return 0
end
local remaining = redis.call('PTTL', KEYS[1])
if remaining < 0 then
  remaining = tonumber(ARGV[5])
end
redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
local done = redis.call('HINCRBY', KEYS[1], 'done', 1)
redis.call('HINCRBY', KEYS[1], ARGV[3], 1)
if done >= total then
  redis.call('HSET', KEYS[1], 'state', 'completed', 'finished_at', ARGV[4])
  remaining = tonumber(ARGV[5])
  redis.call('PEXPIRE', KEYS[1], remaining)
  redis.call('PEXPIRE', KEYS[4], remaining)
end
redis.call('PEXPIRE', KEYS[2], remaining)
redis.call('PEXPIRE', KEYS[3], remaining)
redis.call('PEXPIRE', KEYS[5], remaining)
if done >= total then
  return 2
end
return 1
"""

# KEYS[1] = meta; ARGV = lease_until (empty: no lease). Returns 1 when the state was set, 0 when
# the job is completed already (a finished job is never reopened) and -1 when the job is gone.
_MARK_RUNNING = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return -1
end
if redis.call('HGET', KEYS[1], 'state') == 'completed' then
  return 0
end
redis.call('HSET', KEYS[1], 'state', 'running')
if ARGV[1] == '' then
  redis.call('HDEL', KEYS[1], 'lease_until')
else
  redis.call('HSET', KEYS[1], 'lease_until', ARGV[1])
end
return 1
"""


# KEYS = meta, a hash of the job (``drafts``, ``runs``); ARGV = field, value. Stores the field
# with the remaining TTL of the job; returns 1, or -1 when the job is gone (nothing is written).
_PUT_FIELD = """
local remaining = redis.call('PTTL', KEYS[1])
if remaining == -2 then
  return -1
end
redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
if remaining > 0 then
  redis.call('PEXPIRE', KEYS[2], remaining)
end
return 1
"""

# KEYS = meta, input; ARGV = body, meta JSON, ttl in ms. Stores the input for the ttl, but never
# longer than the job lives; returns 1, or -1 when the job is gone (nothing is written).
_PUT_INPUT = """
local remaining = redis.call('PTTL', KEYS[1])
if remaining == -2 then
  return -1
end
local ttl = tonumber(ARGV[3])
if remaining > 0 and remaining < ttl then
  ttl = remaining
end
redis.call('DEL', KEYS[2])
redis.call('HSET', KEYS[2], 'body', ARGV[1], 'meta', ARGV[2])
redis.call('PEXPIRE', KEYS[2], ttl)
return 1
"""

# KEYS = meta, key; ARGV = value, ttl in ms. Stores the value for the ttl, but never longer than
# the job lives; returns 1, or -1 when the job is gone (nothing is written).
_PUT_WITH_JOB = """
local remaining = redis.call('PTTL', KEYS[1])
if remaining == -2 then
  return -1
end
local ttl = tonumber(ARGV[2])
if remaining > 0 and remaining < ttl then
  ttl = remaining
end
redis.call('SET', KEYS[2], ARGV[1], 'PX', ttl)
return 1
"""

# KEYS = the budget's size, expiry and total; ARGV = member (``<id>:<index>``), bytes, ttl in ms,
# budget. Prunes the reservations that have expired, then reserves ``bytes`` for the member (a
# member reserved already counts once, with its new size) and returns 1, or returns 0 when the
# total would exceed the budget (nothing is reserved). Valkey's clock decides the expiry, as for
# the TTL of the input itself.
_RESERVE_INPUT = """
local time = redis.call('TIME')
local now = tonumber(time[1]) * 1000 + math.floor(tonumber(time[2]) / 1000)
local total = tonumber(redis.call('GET', KEYS[3]) or '0')
local expired = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', now)
for _, member in ipairs(expired) do
  total = total - tonumber(redis.call('HGET', KEYS[1], member) or '0')
  redis.call('HDEL', KEYS[1], member)
  redis.call('ZREM', KEYS[2], member)
end
local size = tonumber(ARGV[2])
local previous = tonumber(redis.call('HGET', KEYS[1], ARGV[1]) or '0')
local reserved = 0
if total - previous + size <= tonumber(ARGV[4]) then
  total = total - previous + size
  redis.call('HSET', KEYS[1], ARGV[1], size)
  redis.call('ZADD', KEYS[2], now + tonumber(ARGV[3]), ARGV[1])
  reserved = 1
end
if total > 0 then
  redis.call('SET', KEYS[3], total)
else
  redis.call('DEL', KEYS[3])
end
return reserved
"""

# KEYS = the budget's size, expiry and total; ARGV = members. Gives their reservations back.
_RELEASE_INPUTS = """
local total = tonumber(redis.call('GET', KEYS[3]) or '0')
for _, member in ipairs(ARGV) do
  local size = redis.call('HGET', KEYS[1], member)
  if size then
    total = total - tonumber(size)
    redis.call('HDEL', KEYS[1], member)
    redis.call('ZREM', KEYS[2], member)
  end
end
if total > 0 then
  redis.call('SET', KEYS[3], total)
else
  redis.call('DEL', KEYS[3])
end
return 1
"""

# KEYS = meta, artifact; ARGV = data, ttl in seconds. Returns 1, or -1 when the job is gone.
_PUT_ARTIFACT = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return -1
end
redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[2])
return 1
"""


# The keys of the input budget, in the order the scripts take them.
_BUDGET_KEYS = ("inputs:{budget}:size", "inputs:{budget}:expiry", "inputs:{budget}:total")


class InputBudgetExceeded(Exception):  # noqa: N818 - a refusal, not an error of ours
    """Storing the input would take all inputs over ``DOCLING_INPUT_BUDGET_BYTES``."""


class JobLost(Exception):  # noqa: N818 - public name fixed by the plan
    """The keys of the job expired (or were deleted) while it was being written."""

    def __init__(self, job_id: str) -> None:
        super().__init__(f"Job {job_id} no longer exists")
        self.job_id = job_id


def _trace_fields(raw: str | None) -> dict[str, str]:
    """The stored B3 fields of a job; none when absent or unreadable (a job of an older
    version has none)."""
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}


@dataclass(frozen=True)
class JobRecord:
    """What the meta hash holds. ``owner`` is the principal's name, ``lease_until`` the epoch
    time at which the worker's claim ends (None: no claim); ``get`` reports it unchanged, even
    when it lies in the past. ``trace_id`` is the trace id of the request that created the job;
    workers log its items under it."""

    id: str
    kind: JobKind
    owner: str
    owner_role: Role
    options: JobOptions
    state: JobState
    created_at: datetime
    finished_at: datetime | None
    progress: JobProgress
    lease_until: float | None
    trace_id: str | None = None
    # The creating request's B3 fields, as logged (O4); workers bind them with ``trace_id``.
    trace_fields: dict[str, str] = field(default_factory=dict)
    # The job was created with caller secrets (B6); it stays set after they are gone, so the
    # browser worker never renders the job without them.
    has_secrets: bool = False


def browser_lease_seconds(settings: Settings) -> float:
    """How long the browser worker holds an item: two of the longest render deadline
    (``BROWSER_TIMEOUT_SECONDS`` or ``BROWSER_UNBLOCK_TIMEOUT_SECONDS``) plus 10 s."""
    longest = max(settings.browser_timeout_seconds, settings.browser_unblock_timeout_seconds)
    return 2 * longest + 10


def docling_lease_seconds(settings: Settings) -> float:
    """How long the Docling worker holds an item: two conversions (``DOCLING_TIMEOUT_SECONDS``)
    plus 10 s (ruling F14)."""
    return 2 * settings.docling_timeout_seconds + 10


def asr_lease_seconds(settings: Settings) -> float:
    """How long the ASR worker holds an item: two transcriptions (``ASR_TIMEOUT_SECONDS``) plus
    10 s (ruling M10)."""
    return 2 * settings.asr_timeout_seconds + 10


def pii_lease_seconds(settings: Settings) -> float:
    """How long the nlp worker holds an item: two runs of the child (``PII_TIMEOUT_SECONDS``)
    plus 10 s (ruling N11)."""
    return 2 * settings.pii_timeout_seconds + 10


def llm_lease_seconds(settings: Settings) -> float:
    """How long the llm worker holds an item: its longest overall deadline (three times
    ``LLM_TIMEOUT_SECONDS``, ruling S14, plus half that for the image descriptions of an item
    that owes them, R-V2) plus one more timeout and 10 s (ruling S13). The lease is fixed at
    the claim, so it covers the longest case."""
    return 5.5 * settings.llm_timeout_seconds + 10


def escape_owner(owner: str) -> str:
    """An owner's name for a key's hash tag: ``%``, ``{`` and ``}`` percent-escaped, so a name
    cannot break the hash tag or look like another name."""
    for char, code in _OWNER_ESCAPES.items():
        owner = owner.replace(char, code)
    return owner


def job_principal(job: JobRecord) -> Principal:
    """The job's owner as the principal of its items: its role decides what they may do."""
    return Principal(name=job.owner, role=job.owner_role, rate_limit_per_minute=None)


def owner_key(owner: str, name: str) -> str:
    """``jobs:{owner:<owner>}:<name>``, a key of one owner's jobs; the owner is escaped
    (``escape_owner``), so a name cannot break the hash tag."""
    return f"jobs:{{owner:{escape_owner(owner)}}}:{name}"


def _key(job_id: str, kind: str) -> str:
    """``job:{<id>}:<kind>`` with the id in literal braces: a cluster hash tag."""
    return f"job:{{{job_id}}}:{kind}"


def job_key(job_id: str, kind: str) -> str:
    """``job:{<id>}:<kind>``, a key of the job (for the crawl store, which shares its slot)."""
    return _key(job_id, kind)


def valid_artifact_name(name: str) -> bool:
    """Whether ``name`` can name a file of a job: ``[a-z0-9_.-]{1,64}``."""
    return _ARTIFACT_NAME.fullmatch(name) is not None


def _artifact_key(job_id: str, name: str) -> str:
    if not valid_artifact_name(name):
        raise ValueError(f"Invalid artifact name: {name!r}")
    return _key(job_id, f"artifact:{name}")


def _options_json(options: JobOptions) -> str:
    """The stored form of a job's options. ``ocr`` (``auto``), ``transcribe`` (false),
    ``languages`` (none), ``pii`` (none), ``structured`` (none), ``chunking`` (none),
    ``describe_images`` (none) and ``site`` (none) are left out while they are the default: pods
    of a release before them parse the options strictly, and during a rolling upgrade they read
    the jobs that new pods create."""
    defaults = JobOptions.model_fields
    omit = {
        name
        for name in (
            "ocr",
            "transcribe",
            "languages",
            "pii",
            "structured",
            "chunking",
            "describe_images",
            "site",
        )
        if getattr(options, name) == defaults[name].get_default(call_default_factory=True)
    }
    return options.model_dump_json(exclude=omit or None)


def _budget_member(job_id: str, index: int) -> str:
    """The name of an input in the input budget."""
    return f"{job_id}:{index}"


def _epoch(value: float) -> str:
    return repr(float(value))


def _utc(epoch: str) -> datetime:
    return datetime.fromtimestamp(float(epoch), tz=UTC)


def _item_run(raw: Any) -> ItemRun | None:
    """An entry of ``runs`` as the item's ``ItemRun``; None for none (or one this release cannot
    read)."""
    if raw is None:
        return None
    try:
        attempts, pool, millis = reply_text(raw).split("|")
        return ItemRun(
            pool=pool,
            attempts=int(attempts),
            claimed_at=datetime.fromtimestamp(int(millis) / 1000, tz=UTC),
        )
    except (ValueError, OverflowError, OSError):  # fromtimestamp: OverflowError, OSError
        return None


class JobStore:
    """Creates and reads job records and stores item results, for all pods through Valkey."""

    def __init__(
        self,
        valkey: Redis,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._valkey = valkey
        self._ttl_seconds = settings.job_ttl_seconds
        self._item_max_bytes = settings.job_item_max_bytes
        self._clock = clock
        # B6: long enough for a sync request's wait in the queue, then two renders or a worker
        # that dies at the claim, whose item runs again once the browser lease has ended (M-2).
        # The lease covers two of the longest render.
        self._secrets_ttl_seconds = math.ceil(
            settings.browser_queue_wait_seconds + browser_lease_seconds(settings) + 60
        )
        self._artifact_ttl_seconds = settings.artifact_ttl_seconds
        # M11: as long as the item's input may wait, and one claim of the ASR worker after it.
        self._asr_key_ttl_seconds = math.ceil(
            min(
                settings.job_ttl_seconds,
                settings.asr_input_ttl_seconds + asr_lease_seconds(settings),
            )
        )
        self._store_result = valkey.register_script(_STORE_RESULT)
        self._mark_running = valkey.register_script(_MARK_RUNNING)
        self._put_field = valkey.register_script(_PUT_FIELD)
        self._put_artifact = valkey.register_script(_PUT_ARTIFACT)
        self._put_input = valkey.register_script(_PUT_INPUT)
        self._put_with_job = valkey.register_script(_PUT_WITH_JOB)
        self._reserve_input = valkey.register_script(_RESERVE_INPUT)
        self._release_inputs = valkey.register_script(_RELEASE_INPUTS)
        self._input_budget = settings.docling_input_budget_bytes

    async def create(
        self,
        kind: JobKind,
        owner: Principal,
        options: JobOptions,
        urls: Sequence[str | None],
        *,
        trace_id: str | None = None,
        trace_fields: Mapping[str, str] | None = None,
        secrets: BrowserSecrets | None = None,
    ) -> str:
        """Store a new job in state ``queued`` and return its id. ``urls`` has one entry per
        item, None for an uploaded file. ``trace_id`` is the creating request's, and
        ``trace_fields`` its B3 fields (``tracing.current_trace_fields``, O4). ``secrets``
        (B6) are stored apart from the job, for a short time only."""
        if not urls:
            raise ValueError("A job needs at least one URL (None for an uploaded file)")
        stored_urls = [_NO_URL if url is None else url for url in urls]
        job_id = uuid.uuid4().hex
        meta_key = _key(job_id, "meta")
        urls_key = _key(job_id, "urls")
        meta: dict[Any, Any] = {  # redis-py types hash keys as a wide union
            "kind": kind,
            "owner": owner.name,
            "owner_role": owner.role,
            "options": _options_json(options),
            "state": "queued",
            "created_at": _epoch(self._clock()),
            "total": len(urls),
            "done": 0,
            "success": 0,
            "partial": 0,
            "failed": 0,
        }
        if trace_id is not None:
            meta["trace_id"] = trace_id
        if trace_fields:
            meta["trace_fields"] = json.dumps(dict(trace_fields), separators=(",", ":"))
        stored_secrets = secrets if secrets is not None and not secrets.empty() else None
        if stored_secrets is not None:
            meta["secrets"] = "1"
        async with self._valkey.pipeline(transaction=True) as pipe:
            pipe.hset(meta_key, mapping=meta)
            for start in range(0, len(stored_urls), _URL_CHUNK):
                pipe.rpush(urls_key, *stored_urls[start : start + _URL_CHUNK])
            pipe.expire(meta_key, self._ttl_seconds)
            pipe.expire(urls_key, self._ttl_seconds)
            if stored_secrets is not None:
                pipe.set(
                    _key(job_id, "secrets"),
                    stored_secrets.model_dump_json(),
                    ex=min(self._secrets_ttl_seconds, self._ttl_seconds),
                )
            await pipe.execute()
        return job_id

    async def delete(self, job_id: str) -> None:
        """Remove all keys of the job, its item runs, secrets, drafts, crawl keys, files and the
        input of item 0 too (its bytes leave the input budget). A job that is gone already is
        not an error."""
        await self._valkey.delete(
            *(_key(job_id, kind) for kind in _KINDS),
            *(_artifact_key(job_id, name) for name in ARTIFACT_NAMES),
            _key(job_id, _FIRST_INPUT),
        )
        await self._release(job_id, 0)

    async def shorten(self, job_id: str, seconds: int) -> None:
        """Let the job (its item runs and crawl keys too) and its files expire within
        ``seconds`` (never later than they would) and delete its secrets (the ASR and LLM keys
        too), drafts and the input of item 0: a sync answer's screenshot job, which only the
        artifact route still reads. A job that is gone already is not an error."""
        async with self._valkey.pipeline(transaction=True) as pipe:
            for kind in ("meta", "urls", "results", "done", "runs", *_CRAWL_KINDS):
                pipe.expire(_key(job_id, kind), seconds, lt=True)
            for name in ARTIFACT_NAMES:
                pipe.expire(_artifact_key(job_id, name), seconds, lt=True)
            pipe.delete(
                _key(job_id, "secrets"),
                _key(job_id, "asr_key"),
                _key(job_id, "llm_key"),
                _key(job_id, "drafts"),
                _key(job_id, _FIRST_INPUT),
            )
            await pipe.execute()
        await self._release(job_id, 0)

    async def secrets(self, job_id: str) -> BrowserSecrets:
        """The caller's browser secrets of the job; empty when it had none or they are gone."""
        raw = await self._valkey.get(_key(job_id, "secrets"))
        if raw is None:
            return BrowserSecrets()
        return BrowserSecrets.model_validate_json(raw)

    async def drop_secrets(self, job_id: str) -> None:
        """Delete the job's caller secrets, the browser's and the ASR key (the worker has
        finished its item)."""
        await self._valkey.delete(_key(job_id, "secrets"), _key(job_id, "asr_key"))

    async def put_asr_key(self, job_id: str, key: str, *, whole_job: bool = False) -> None:
        """Keep the caller's key of the external transcription endpoint for the job (M11), for
        ``ASR_INPUT_TTL_SECONDS`` + the ASR lease, or with ``whole_job`` (a batch) as long as the
        job; never longer than the job. Raises ``JobLost`` when the job is gone; nothing is
        stored then."""
        seconds = self._ttl_seconds if whole_job else self._asr_key_ttl_seconds
        outcome = await self._put_with_job(
            keys=[_key(job_id, "meta"), _key(job_id, "asr_key")],
            args=[key, seconds * 1000],
        )
        if outcome == _NOT_FOUND:
            raise JobLost(job_id)

    async def drop_asr_key(self, job_id: str) -> None:
        """Delete the caller's key of the external transcription endpoint only (a hand-over
        that failed after storing it; the job's other secrets stay)."""
        await self._valkey.delete(_key(job_id, "asr_key"))

    async def asr_key(self, job_id: str) -> str | None:
        """The caller's key of the external transcription endpoint, or None when the job had
        none or it is gone."""
        raw = await self._valkey.get(_key(job_id, "asr_key"))
        return reply_text(raw) if raw is not None else None

    async def put_llm_key(self, job_id: str, key: str) -> None:
        """Keep the caller's key of the LLM endpoint for the job (ruling S9, R11) for the job's
        TTL, whatever the job is (a sync request's, a pool job's, a batch's). Unlike the ASR key
        there is no shorter bound: the draft the llm worker reads lives as long as the job, a
        202 job may wait for the worker until then, and a batch's items move to pool ``llm`` at
        any time. The key is not left to expire: whoever writes the item's result drops it
        (``drop_llm_key``: ``Worker._finish`` of any pool, a sync job's at once and a batch's with
        its last result, and ``extract_api._store_result`` when the API pod ran the whole
        request); a worker that moves the item on to the llm worker keeps it. ``delete`` and
        ``shorten`` remove it too. Raises ``JobLost`` when the job is gone; nothing is stored
        then."""
        outcome = await self._put_with_job(
            keys=[_key(job_id, "meta"), _key(job_id, "llm_key")],
            args=[key, self._ttl_seconds * 1000],
        )
        if outcome == _NOT_FOUND:
            raise JobLost(job_id)

    async def drop_llm_key(self, job_id: str) -> None:
        """Delete the caller's key of the LLM endpoint only (the item's result is written, or a
        hand-over failed after storing it)."""
        await self._valkey.delete(_key(job_id, "llm_key"))

    async def llm_key(self, job_id: str) -> str | None:
        """The caller's key of the LLM endpoint, or None when the job had none or it is gone."""
        raw = await self._valkey.get(_key(job_id, "llm_key"))
        return reply_text(raw) if raw is not None else None

    async def put_draft(self, job_id: str, index: int, envelope: Envelope[ExtractResult]) -> None:
        """Keep the fast result of item ``index`` while the browser renders it (B5); a later
        draft of the item replaces it. Over ``JOB_ITEM_MAX_BYTES`` it is kept without its
        content, as a result would be. Raises ``JobLost`` when the job is gone."""
        outcome = await self._put_field(
            keys=[_key(job_id, "meta"), _key(job_id, "drafts")],
            args=[index, self._encode(envelope)],
        )
        if outcome == _NOT_FOUND:
            raise JobLost(job_id)

    async def drop_draft(self, job_id: str, index: int) -> None:
        """Delete the draft of item ``index`` (the item has its outcome)."""
        await self._valkey.hdel(_key(job_id, "drafts"), str(index))

    async def draft(self, job_id: str, index: int) -> Envelope[ExtractResult] | None:
        """The draft of item ``index``, or None when there is none."""
        raw = await self._valkey.hget(_key(job_id, "drafts"), str(index))
        if raw is None:
            return None
        return Envelope[ExtractResult].model_validate_json(raw)

    async def put_input(
        self, job_id: str, index: int, body: bytes, meta: BaseModel, ttl: int
    ) -> None:
        """Keep the bytes of item ``index`` with ``meta`` (stored as JSON) for ``ttl`` seconds,
        but never longer than the job; a later input of the item replaces it. Raises
        ``InputBudgetExceeded`` when all inputs would exceed ``DOCLING_INPUT_BUDGET_BYTES``, and
        ``JobLost`` when the job is gone; nothing is stored then."""
        encoded = meta.model_dump_json()
        member = _budget_member(job_id, index)
        reserved = await self._reserve_input(
            keys=list(_BUDGET_KEYS),
            args=[member, len(body) + len(encoded.encode()), ttl * 1000, self._input_budget],
        )
        if not reserved:
            raise InputBudgetExceeded
        try:
            outcome = await self._put_input(
                keys=[_key(job_id, "meta"), _key(job_id, f"input:{index}")],
                args=[body, encoded, ttl * 1000],
            )
        except BaseException:
            await asyncio.shield(self._release(job_id, index))
            raise
        if outcome == _NOT_FOUND:
            await self._release(job_id, index)
            raise JobLost(job_id)

    async def get_input[M: BaseModel](
        self, job_id: str, index: int, model: type[M]
    ) -> tuple[bytes, M] | None:
        """The bytes of item ``index`` and their meta as ``model``, or None when there are none
        (never stored, deleted or expired)."""
        body, meta = await self._valkey.hmget(_key(job_id, f"input:{index}"), ["body", "meta"])
        if body is None or meta is None:
            return None
        raw = body.encode() if isinstance(body, str) else bytes(body)
        return raw, model.model_validate_json(meta)

    async def delete_input(self, job_id: str, index: int) -> None:
        """Delete the bytes of item ``index`` (it has its outcome); they leave the input
        budget."""
        await self._valkey.delete(_key(job_id, f"input:{index}"))
        await self._release(job_id, index)

    async def _release(self, job_id: str, index: int) -> None:
        """Give the reservation of an input back (after the input is gone). A Valkey failure
        leaves it to its expiry."""
        with contextlib.suppress(RedisError, OSError):
            await self._release_inputs(
                keys=list(_BUDGET_KEYS), args=[_budget_member(job_id, index)]
            )

    async def add_docling_job(self, owner: str, job_id: str) -> int:
        """Count the job as one of the owner's Docling jobs; how many the set holds afterwards,
        finished ones that were not counted out yet included."""
        return await self._add_owner_job(owner_key(owner, "docling"), job_id)

    async def drop_docling_job(self, owner: str, job_id: str) -> None:
        """Count the job out of the owner's Docling jobs."""
        await self._valkey.srem(owner_key(owner, "docling"), job_id)

    async def open_docling_jobs(self, owner: str) -> int:
        """The owner's unfinished Docling jobs. Jobs that are completed or gone are counted
        out."""
        return await self._open_owner_jobs(owner_key(owner, "docling"))

    async def add_asr_job(self, owner: str, job_id: str) -> int:
        """Count the job as one of the owner's ASR jobs, as ``add_docling_job`` does."""
        return await self._add_owner_job(owner_key(owner, "asr"), job_id)

    async def drop_asr_job(self, owner: str, job_id: str) -> None:
        """Count the job out of the owner's ASR jobs."""
        await self._valkey.srem(owner_key(owner, "asr"), job_id)

    async def open_asr_jobs(self, owner: str) -> int:
        """The owner's unfinished ASR jobs, as ``open_docling_jobs`` counts its Docling jobs."""
        return await self._open_owner_jobs(owner_key(owner, "asr"))

    async def _add_owner_job(self, key: str, job_id: str) -> int:
        async with self._valkey.pipeline(transaction=True) as pipe:
            pipe.sadd(key, job_id)
            pipe.expire(key, self._ttl_seconds)
            pipe.scard(key)
            size: int = (await pipe.execute())[-1]
        return size

    async def _open_owner_jobs(self, key: str) -> int:
        ids = [reply_text(raw) for raw in await self._valkey.smembers(key)]
        records = await asyncio.gather(*(self.get(job_id) for job_id in ids))
        finished = [
            job_id
            for job_id, record in zip(ids, records, strict=True)
            if record is None or record.state == "completed"
        ]
        if finished:
            await self._valkey.srem(key, *finished)
        return len(ids) - len(finished)

    async def put_artifact(self, job_id: str, name: str, data: bytes) -> datetime:
        """Store a file of the job for ``ARTIFACT_TTL_SECONDS`` and return when it expires.
        ``name`` matches ``[a-z0-9_.-]{1,64}`` (``ValueError`` otherwise). Raises ``JobLost``
        when the job is gone, so nothing is stored for it."""
        key = _artifact_key(job_id, name)
        expires = self._clock() + self._artifact_ttl_seconds
        outcome = await self._put_artifact(
            keys=[_key(job_id, "meta"), key], args=[data, self._artifact_ttl_seconds]
        )
        if outcome == _NOT_FOUND:
            raise JobLost(job_id)
        return datetime.fromtimestamp(expires, tz=UTC)

    async def artifact(self, job_id: str, name: str) -> bytes | None:
        """The file ``name`` of the job, or None when it is unknown or expired."""
        raw = await self._valkey.get(_artifact_key(job_id, name))
        return raw.encode() if isinstance(raw, str) else raw

    async def get(self, job_id: str) -> JobRecord | None:
        """The job's record, or None when it is unknown or expired."""
        raw = await self._valkey.hgetall(_key(job_id, "meta"))
        if not raw:
            return None
        meta = {reply_text(key): reply_text(value) for key, value in raw.items()}
        finished_at = meta.get("finished_at")
        lease_until = meta.get("lease_until")
        return JobRecord(
            id=job_id,
            kind=meta["kind"],  # type: ignore[arg-type]
            owner=meta["owner"],
            owner_role=meta["owner_role"],  # type: ignore[arg-type]
            options=JobOptions.model_validate_json(meta["options"]),
            state=meta["state"],  # type: ignore[arg-type]
            created_at=_utc(meta["created_at"]),
            finished_at=_utc(finished_at) if finished_at is not None else None,
            progress=JobProgress(
                total=int(meta["total"]),
                done=int(meta["done"]),
                success=int(meta["success"]),
                partial=int(meta["partial"]),
                failed=int(meta["failed"]),
            ),
            lease_until=float(lease_until) if lease_until is not None else None,
            trace_id=meta.get("trace_id"),
            trace_fields=_trace_fields(meta.get("trace_fields")),
            has_secrets=meta.get("secrets") == "1",
        )

    async def url(self, job_id: str, index: int) -> str | None:
        """The URL at ``index``, ``""`` for an uploaded file's item, or None when the job has no
        such item."""
        if index < 0:
            return None
        raw = await self._valkey.lindex(_key(job_id, "urls"), index)
        return reply_text(raw) if raw is not None else None

    async def items(
        self, job_id: str, offset: int, limit: int, *, max_bytes: int | None = None
    ) -> list[JobItem]:
        """Up to ``limit`` items from ``offset`` in index order, each with its last claim
        (``run``). Indexes past the end are absent. With ``max_bytes`` the page stops before
        the item whose stored result would take the results past it (the first item always
        comes): the lengths are read first, then only the results of the page."""
        if offset < 0:
            raise ValueError("offset must not be negative")
        if limit <= 0:
            return []
        if max_bytes is not None:
            limit = await self._fitting(job_id, offset, limit, max_bytes)
        fields = [str(n) for n in range(offset, offset + limit)]
        async with self._valkey.pipeline(transaction=False) as pipe:
            pipe.lrange(_key(job_id, "urls"), offset, offset + limit - 1)
            pipe.hmget(_key(job_id, "results"), fields)
            pipe.hmget(_key(job_id, "runs"), fields)
            urls, results, runs = await pipe.execute()
        return [
            JobItem(
                index=offset + position,
                url=reply_text(url) or None,
                state="done" if results[position] is not None else "pending",
                result=(
                    Envelope[ExtractResult].model_validate_json(results[position])
                    if results[position] is not None
                    else None
                ),
                run=_item_run(runs[position]),
            )
            for position, url in enumerate(urls)
        ]

    async def _fitting(self, job_id: str, offset: int, limit: int, max_bytes: int) -> int:
        """How many items from ``offset`` (1 to ``limit``) fit ``max_bytes`` of results."""
        async with self._valkey.pipeline(transaction=False) as pipe:
            for index in range(offset, offset + limit):
                pipe.hstrlen(_key(job_id, "results"), str(index))
            sizes: list[int] = await pipe.execute()
        total = 0
        for count, size in enumerate(sizes):
            total += size
            if total > max_bytes and count > 0:
                return count
        return limit

    async def record_claim(self, job_id: str, index: int, pool: str, attempts: int) -> None:
        """Record that a worker of ``pool`` claimed item ``index`` (D8, ruling P10-S11):
        ``attempts`` is the claim number (1 on the first claim), the time is the store's clock.
        One script on the job's slot; the entry takes the remaining TTL of the job. Nothing is
        written when the job is gone."""
        millis = int(self._clock() * 1000)
        await self._put_field(
            keys=[_key(job_id, "meta"), _key(job_id, "runs")],
            args=[index, f"{attempts}|{pool}|{millis}"],
        )

    async def has_result(self, job_id: str, index: int) -> bool:
        """Whether item ``index`` has its result already (its ``done`` bit is set)."""
        return bool(await self._valkey.getbit(_key(job_id, "done"), index))

    async def write_result(
        self, job_id: str, index: int, envelope: Envelope[ExtractResult]
    ) -> ResultWrite:
        """Store the result of item ``index`` and tell what the write did: ``first`` (it
        finished the item), ``last`` (it finished the item and with it the job; exactly one write
        of a job does, even when several workers finish items at once) or ``repeat``.

        Only the first write of an index counts and is kept: a repeat (an item that ran again
        after a lost lease) changes nothing. A result over ``JOB_ITEM_MAX_BYTES`` as UTF-8 JSON is
        stored without its content and with the warning ``content_not_stored_too_large``; still
        over it, without its chunks and tables too (``chunks_not_stored_too_large``,
        ``tables_not_stored_too_large``; ``_encode``). Raises
        ``JobLost`` when the job's keys are gone and ``ValueError`` when the index is outside the
        job.
        """
        payload = self._encode(envelope)
        outcome = await self._store_result(
            keys=[
                _key(job_id, "meta"),
                _key(job_id, "results"),
                _key(job_id, "done"),
                _key(job_id, "urls"),
                _key(job_id, "runs"),
            ],
            args=[
                index,
                payload,
                envelope.status,
                _epoch(self._clock()),
                self._ttl_seconds * 1000,
            ],
        )
        if outcome == _NOT_FOUND:
            raise JobLost(job_id)
        if outcome == _OUT_OF_RANGE:
            raise ValueError(f"index {index} is outside job {job_id}")
        return _WRITES[int(outcome)]

    async def mark_running(self, job_id: str, *, lease_seconds: float | None = None) -> None:
        """Set the state ``running`` and claim the job for ``lease_seconds`` (None: no claim).

        A completed job stays completed. Raises ``JobLost`` when the job's keys are gone.
        """
        lease_until = "" if lease_seconds is None else _epoch(self._clock() + lease_seconds)
        outcome: Any = await self._mark_running(keys=[_key(job_id, "meta")], args=[lease_until])
        if outcome == _NOT_FOUND:
            raise JobLost(job_id)

    async def clear_lease(self, job_id: str) -> None:
        """End the job's claim and keep it ``running`` (``mark_running`` without a lease)."""
        await self.mark_running(job_id, lease_seconds=None)

    def encode_result(self, envelope: Envelope[ExtractResult]) -> str:
        """The envelope as ``write_result`` stores it (cut to ``JOB_ITEM_MAX_BYTES``), for a
        script that writes a result itself (the crawl store's finished items)."""
        return self._encode(envelope)

    def _encode(self, envelope: Envelope[ExtractResult]) -> str:
        """The envelope as JSON; over the size limit, without the content, and when that is
        not enough, without the chunks and the tables either (ruling R20), each with its
        warning. What is left (the structured values, metadata, links) is stored as it is."""
        payload = envelope.model_dump_json()
        if len(payload.encode()) <= self._item_max_bytes or envelope.data is None:
            return payload
        data = envelope.data
        data = data.model_copy(
            update={"content": None, "warnings": [*data.warnings, CONTENT_NOT_STORED_WARNING]}
        )
        payload = envelope.model_copy(update={"data": data}).model_dump_json()
        if len(payload.encode()) <= self._item_max_bytes:
            return payload
        warnings = list(data.warnings)
        update: dict[str, Any] = {}
        if data.chunks:
            update["chunks"] = None
            warnings.append(CHUNKS_NOT_STORED_WARNING)
        if data.structured is not None and data.structured.tables:
            update["structured"] = data.structured.model_copy(update={"tables": None})
            warnings.append(TABLES_NOT_STORED_WARNING)
        if not update:
            return payload
        data = data.model_copy(update={**update, "warnings": warnings})
        return envelope.model_copy(update={"data": data}).model_dump_json()
