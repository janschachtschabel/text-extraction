"""Job records in Valkey: the storage under the job API and the workers.

A job lives in four keys; the braces around the id are a literal cluster hash tag, so one job's
keys share a slot and a Lua script can touch all of them:

- ``job:{<id>}:meta``     hash: kind, owner, owner_role, options (JSON), state, created_at,
                          finished_at, total, done, success, partial, failed, lease_until,
                          trace_id (of the request that created the job, when it had one),
                          secrets (``1`` when the job was created with caller secrets)
- ``job:{<id>}:urls``     list: the URLs in input order
- ``job:{<id>}:results``  hash: item index -> ``Envelope[ExtractResult]`` as JSON
- ``job:{<id>}:done``     bitmap: bit ``index`` is set once that item has its result

All keys expire ``JOB_TTL_SECONDS`` after creation and again after completion. ``results`` and
``done`` do not exist before the first result; they take the remaining TTL of ``meta`` then.

Browser jobs (package 4) may have these as well:

- ``job:{<id>}:secrets``  string: the caller's ``BrowserSecrets`` as JSON (actions, cookies,
                          headers; B6). Kept ``2 * BROWSER_TIMEOUT_SECONDS +
                          BROWSER_QUEUE_WAIT_SECONDS + 60`` seconds at most, and deleted by the
                          worker once it has finished the item. No read of the job shows them.
- ``job:{<id>}:drafts``   hash: item index -> the fast ``Envelope[ExtractResult]`` of an ``auto``
                          item that moved to the browser (B5), with the job's remaining TTL
- ``job:{<id>}:artifact:<name>`` string: a file of the job (``screenshot.png``, B7), kept
                          ``ARTIFACT_TTL_SECONDS``

Times are epoch seconds from the injected clock (every pod needs a synchronised clock), stored
as numbers and handed out as UTC datetimes, except ``lease_until``, which stays a number.
"""

import math
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from redis.asyncio import Redis

from text_extraction.auth import Principal, Role
from text_extraction.browser_options import BrowserSecrets
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractResult
from text_extraction.job_models import (
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

# URLs are pushed in chunks, so one command never carries a whole large batch.
_URL_CHUNK = 1000

# The keys of a job: the four of every job, and those of browser jobs.
_KINDS = ("meta", "urls", "results", "done", "secrets", "drafts")

# The files a job can have; ``delete`` removes them by name.
SCREENSHOT_ARTIFACT = "screenshot.png"
ARTIFACT_NAMES = (SCREENSHOT_ARTIFACT,)
_ARTIFACT_NAME = re.compile(r"[a-z0-9_.-]{1,64}", re.ASCII)

_NOT_FOUND = -1
_OUT_OF_RANGE = -2

# KEYS = meta, results, done, urls; ARGV = index, result JSON, envelope status, now, ttl_ms.
# Returns 1 when this call finished the index, 2 when it also finished the job, 0 when the index
# was finished already (nothing changes), -1 when the job is gone and -2 when the index is outside
# the job. The first write also counts
# the item and, if it was the last, completes the job and renews the TTL of all four keys.
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


# KEYS = meta, drafts; ARGV = index, envelope JSON. Stores the draft with the remaining TTL of
# the job; returns 1, or -1 when the job is gone (nothing is written).
_PUT_DRAFT = """
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

# KEYS = meta, artifact; ARGV = data, ttl in seconds. Returns 1, or -1 when the job is gone.
_PUT_ARTIFACT = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return -1
end
redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[2])
return 1
"""


class JobLost(Exception):  # noqa: N818 - public name fixed by the plan
    """The keys of the job expired (or were deleted) while it was being written."""

    def __init__(self, job_id: str) -> None:
        super().__init__(f"Job {job_id} no longer exists")
        self.job_id = job_id


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
    # The job was created with caller secrets (B6); it stays set after they are gone, so the
    # browser worker never renders the job without them.
    has_secrets: bool = False


def _key(job_id: str, kind: str) -> str:
    """``job:{<id>}:<kind>`` with the id in literal braces: a cluster hash tag."""
    return f"job:{{{job_id}}}:{kind}"


def valid_artifact_name(name: str) -> bool:
    """Whether ``name`` can name a file of a job: ``[a-z0-9_.-]{1,64}``."""
    return _ARTIFACT_NAME.fullmatch(name) is not None


def _artifact_key(job_id: str, name: str) -> str:
    if not valid_artifact_name(name):
        raise ValueError(f"Invalid artifact name: {name!r}")
    return _key(job_id, f"artifact:{name}")


def _epoch(value: float) -> str:
    return repr(float(value))


def _utc(epoch: str) -> datetime:
    return datetime.fromtimestamp(float(epoch), tz=UTC)


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
        # B6: long enough for a sync request's wait in the queue and two renders.
        self._secrets_ttl_seconds = math.ceil(
            2 * settings.browser_timeout_seconds + settings.browser_queue_wait_seconds + 60
        )
        self._artifact_ttl_seconds = settings.artifact_ttl_seconds
        self._store_result = valkey.register_script(_STORE_RESULT)
        self._mark_running = valkey.register_script(_MARK_RUNNING)
        self._put_draft = valkey.register_script(_PUT_DRAFT)
        self._put_artifact = valkey.register_script(_PUT_ARTIFACT)

    async def create(
        self,
        kind: JobKind,
        owner: Principal,
        options: JobOptions,
        urls: list[str],
        *,
        trace_id: str | None = None,
        secrets: BrowserSecrets | None = None,
    ) -> str:
        """Store a new job in state ``queued`` and return its id. ``trace_id`` is the creating
        request's. ``secrets`` (B6) are stored apart from the job, for a short time only."""
        if not urls:
            raise ValueError("A job needs at least one URL")
        job_id = uuid.uuid4().hex
        meta_key = _key(job_id, "meta")
        urls_key = _key(job_id, "urls")
        meta: dict[Any, Any] = {  # redis-py types hash keys as a wide union
            "kind": kind,
            "owner": owner.name,
            "owner_role": owner.role,
            "options": options.model_dump_json(),
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
        stored_secrets = secrets if secrets is not None and not secrets.empty() else None
        if stored_secrets is not None:
            meta["secrets"] = "1"
        async with self._valkey.pipeline(transaction=True) as pipe:
            pipe.hset(meta_key, mapping=meta)
            for start in range(0, len(urls), _URL_CHUNK):
                pipe.rpush(urls_key, *urls[start : start + _URL_CHUNK])
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
        """Remove all keys of the job, its secrets, drafts and files too. A job that is gone
        already is not an error."""
        await self._valkey.delete(
            *(_key(job_id, kind) for kind in _KINDS),
            *(_artifact_key(job_id, name) for name in ARTIFACT_NAMES),
        )

    async def secrets(self, job_id: str) -> BrowserSecrets:
        """The caller's browser secrets of the job; empty when it had none or they are gone."""
        raw = await self._valkey.get(_key(job_id, "secrets"))
        if raw is None:
            return BrowserSecrets()
        return BrowserSecrets.model_validate_json(raw)

    async def drop_secrets(self, job_id: str) -> None:
        """Delete the job's secrets (the worker has finished its item)."""
        await self._valkey.delete(_key(job_id, "secrets"))

    async def put_draft(self, job_id: str, index: int, envelope: Envelope[ExtractResult]) -> None:
        """Keep the fast result of item ``index`` while the browser renders it (B5); a later
        draft of the item replaces it. Over ``JOB_ITEM_MAX_BYTES`` it is kept without its
        content, as a result would be. Raises ``JobLost`` when the job is gone."""
        outcome = await self._put_draft(
            keys=[_key(job_id, "meta"), _key(job_id, "drafts")],
            args=[index, self._encode(envelope)],
        )
        if outcome == _NOT_FOUND:
            raise JobLost(job_id)

    async def draft(self, job_id: str, index: int) -> Envelope[ExtractResult] | None:
        """The draft of item ``index``, or None when there is none."""
        raw = await self._valkey.hget(_key(job_id, "drafts"), str(index))
        if raw is None:
            return None
        return Envelope[ExtractResult].model_validate_json(raw)

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
            has_secrets=meta.get("secrets") == "1",
        )

    async def url(self, job_id: str, index: int) -> str | None:
        """The URL at ``index``, or None when there is none."""
        if index < 0:
            return None
        raw = await self._valkey.lindex(_key(job_id, "urls"), index)
        return reply_text(raw) if raw is not None else None

    async def items(self, job_id: str, offset: int, limit: int) -> list[JobItem]:
        """Up to ``limit`` items from ``offset`` in index order. Indexes past the end are absent."""
        if offset < 0:
            raise ValueError("offset must not be negative")
        if limit <= 0:
            return []
        async with self._valkey.pipeline(transaction=False) as pipe:
            pipe.lrange(_key(job_id, "urls"), offset, offset + limit - 1)
            pipe.hmget(_key(job_id, "results"), [str(n) for n in range(offset, offset + limit)])
            urls, results = await pipe.execute()
        return [
            JobItem(
                index=offset + position,
                url=reply_text(url),
                state="done" if results[position] is not None else "pending",
                result=(
                    Envelope[ExtractResult].model_validate_json(results[position])
                    if results[position] is not None
                    else None
                ),
            )
            for position, url in enumerate(urls)
        ]

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
        stored without its content and with the warning ``content_not_stored_too_large``. Raises
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

    def _encode(self, envelope: Envelope[ExtractResult]) -> str:
        """The envelope as JSON; over the size limit, without the content."""
        payload = envelope.model_dump_json()
        if len(payload.encode()) <= self._item_max_bytes or envelope.data is None:
            return payload
        data = envelope.data.model_copy(
            update={
                "content": None,
                "warnings": [*envelope.data.warnings, CONTENT_NOT_STORED_WARNING],
            }
        )
        return envelope.model_copy(update={"data": data}).model_dump_json()
