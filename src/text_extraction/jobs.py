"""Job records in Valkey: the storage under the job API and the workers.

A job lives in four keys; the braces around the id are a literal cluster hash tag, so one job's
keys share a slot and a Lua script can touch all of them:

- ``job:{<id>}:meta``     hash: kind, owner, owner_role, options (JSON), state, created_at,
                          finished_at, total, done, success, partial, failed, lease_until
- ``job:{<id>}:urls``     list: the URLs in input order
- ``job:{<id>}:results``  hash: item index -> ``Envelope[ExtractResult]`` as JSON
- ``job:{<id>}:done``     bitmap: bit ``index`` is set once that item has its result

All keys expire ``JOB_TTL_SECONDS`` after creation and again after completion. ``results`` and
``done`` do not exist before the first result; they take the remaining TTL of ``meta`` then.

Times are epoch seconds from the injected clock (every pod needs a synchronised clock), stored
as numbers and handed out as UTC datetimes, except ``lease_until``, which stays a number.
"""

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from text_extraction.auth import Principal, Role
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

# Set in ``data.warnings`` of a result that was stored without its content.
CONTENT_NOT_STORED_WARNING = "content_not_stored_too_large"

# URLs are pushed in chunks, so one command never carries a whole large batch.
_URL_CHUNK = 1000

_NOT_FOUND = -1
_OUT_OF_RANGE = -2

# KEYS = meta, results, done, urls; ARGV = index, result JSON, envelope status, now, ttl_ms.
# Returns 1 when this call finished the index, 0 when it was finished already (nothing changes),
# -1 when the job is gone and -2 when the index is outside the job. The first write also counts
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


class JobLost(Exception):  # noqa: N818 - public name fixed by the plan
    """The keys of the job expired (or were deleted) while it was being written."""

    def __init__(self, job_id: str) -> None:
        super().__init__(f"Job {job_id} no longer exists")
        self.job_id = job_id


@dataclass(frozen=True)
class JobRecord:
    """What the meta hash holds. ``owner`` is the principal's name, ``lease_until`` the epoch
    time at which the worker's claim ends (None: no claim); ``get`` reports it unchanged, even
    when it lies in the past."""

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


def _key(job_id: str, kind: str) -> str:
    """``job:{<id>}:<kind>`` with the id in literal braces: a cluster hash tag."""
    return f"job:{{{job_id}}}:{kind}"


def _epoch(value: float) -> str:
    return repr(float(value))


def _utc(epoch: str) -> datetime:
    return datetime.fromtimestamp(float(epoch), tz=UTC)


def _text(value: bytes | str) -> str:
    return value if isinstance(value, str) else value.decode()


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
        self._store_result = valkey.register_script(_STORE_RESULT)
        self._mark_running = valkey.register_script(_MARK_RUNNING)

    async def create(
        self, kind: JobKind, owner: Principal, options: JobOptions, urls: list[str]
    ) -> str:
        """Store a new job in state ``queued`` and return its id."""
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
        async with self._valkey.pipeline(transaction=True) as pipe:
            pipe.hset(meta_key, mapping=meta)
            for start in range(0, len(urls), _URL_CHUNK):
                pipe.rpush(urls_key, *urls[start : start + _URL_CHUNK])
            pipe.expire(meta_key, self._ttl_seconds)
            pipe.expire(urls_key, self._ttl_seconds)
            await pipe.execute()
        return job_id

    async def get(self, job_id: str) -> JobRecord | None:
        """The job's record, or None when it is unknown or expired."""
        raw = await self._valkey.hgetall(_key(job_id, "meta"))
        if not raw:
            return None
        meta = {_text(key): _text(value) for key, value in raw.items()}
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
        )

    async def url(self, job_id: str, index: int) -> str | None:
        """The URL at ``index``, or None when there is none."""
        if index < 0:
            return None
        raw = await self._valkey.lindex(_key(job_id, "urls"), index)
        return _text(raw) if raw is not None else None

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
                url=_text(url),
                state="done" if results[position] is not None else "pending",
                result=(
                    Envelope[ExtractResult].model_validate_json(results[position])
                    if results[position] is not None
                    else None
                ),
            )
            for position, url in enumerate(urls)
        ]

    async def store_result(
        self, job_id: str, index: int, envelope: Envelope[ExtractResult]
    ) -> bool:
        """Store the result of item ``index``. True when this call finished the item.

        Only the first write of an index counts and is kept: a repeat (an item that ran again
        after a lost lease) changes nothing and returns False. The last result completes the
        job. A result over ``JOB_ITEM_MAX_BYTES`` as UTF-8 JSON is stored without its content
        and with the warning ``content_not_stored_too_large``. Raises ``JobLost`` when the job's
        keys are gone and ``ValueError`` when the index is outside the job.
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
        return bool(outcome == 1)

    async def mark_running(self, job_id: str, *, lease_seconds: float | None = None) -> None:
        """Set the state ``running`` and claim the job for ``lease_seconds`` (None: no claim).

        A completed job stays completed. Raises ``JobLost`` when the job's keys are gone.
        """
        lease_until = "" if lease_seconds is None else _epoch(self._clock() + lease_seconds)
        outcome: Any = await self._mark_running(keys=[_key(job_id, "meta")], args=[lease_until])
        if outcome == _NOT_FOUND:
            raise JobLost(job_id)

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
