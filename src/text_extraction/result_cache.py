"""Cache of extraction results in Valkey.

An entry is a finished ``ExtractResult`` (as JSON-compatible data) with what is needed to
revalidate it: the ``Validators`` (ETag, Last-Modified) of the response it came from and the
SHA-256 of the fetched bytes. Two clocks apply:

* ``RESULT_STORE_TTL_SECONDS`` is the Valkey TTL: how long an entry is kept at all;
* ``RESULT_CACHE_TTL_SECONDS`` is the fresh window, measured from ``stored_at``: a fresh entry
  is served without asking the site. An older (stale) entry is still returned by ``get`` so the
  pipeline can send its validators and reuse it on ``304`` or an equal ``raw_sha256``.

Keys are ``res:{<sha256>}`` (the braces are a literal cluster hash tag); the hash covers the
normalised URL (see ``fingerprint.normalize_url``) and the extraction options, so a different
option set is a different entry. A Valkey problem never fails a request: reads act as a miss,
writes are dropped, both with one log line that carries no URL.
"""

import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from text_extraction import __version__
from text_extraction.fetch import Validators
from text_extraction.fingerprint import normalize_url
from text_extraction.settings import Settings

_log = structlog.get_logger(__name__)

# Entries whose JSON is larger are not cached (the request is simply extracted again).
MAX_ENTRY_BYTES = 2 * 1024 * 1024


@dataclass
class CachedEntry:
    """A stored result. ``result`` is ``ExtractResult.model_dump(mode="json")``."""

    stored_at: float  # Unix time the result was produced
    validators: Validators
    raw_sha256: str
    result: dict[str, Any]


def cache_key(url: str, options: Mapping[str, Any]) -> str:
    """Key of the result for ``url`` extracted with ``options`` (JSON-serialisable values).

    The order of the options does not matter; the spelling of the URL matters only as much as
    ``normalize_url`` leaves it. The package version is part of the hash, so a new release never
    serves (or revalidates) what an older extractor produced.
    """
    canonical = json.dumps(options, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(
        f"{__version__}\0{normalize_url(url)}\0{canonical}".encode()
    ).hexdigest()
    return f"res:{{{digest}}}"


class ResultCache:
    """Read and write ``CachedEntry`` objects; see the module docstring for the two TTLs."""

    def __init__(
        self, valkey: Redis, settings: Settings, clock: Callable[[], float] = time.time
    ) -> None:
        self._valkey = valkey
        self._fresh_seconds = settings.result_cache_ttl_seconds
        self._store_seconds = settings.result_store_ttl_seconds
        self._clock = clock

    async def get(self, key: str) -> tuple[CachedEntry, bool] | None:
        """The entry and whether it is fresh, or ``None`` (missing, unreadable, Valkey down)."""
        try:
            raw = await self._valkey.get(key)
        except (RedisError, OSError) as error:
            _log.warning("result_cache_read_failed", error=type(error).__name__)
            return None
        if raw is None:
            return None
        entry = _decode(raw)
        if entry is None:
            return None
        # A stored_at in the future (another pod's clock is ahead) gives a negative age: stale.
        age = self._clock() - entry.stored_at
        fresh = 0 <= age < self._fresh_seconds
        return entry, fresh

    async def put(self, key: str, entry: CachedEntry) -> None:
        """Store ``entry`` for ``RESULT_STORE_TTL_SECONDS`` (0: store nothing; over 2 MB: skip)."""
        if self._store_seconds <= 0:
            return
        try:
            payload = json.dumps(
                {
                    "stored_at": entry.stored_at,
                    "validators": {
                        "etag": entry.validators.etag,
                        "last_modified": entry.validators.last_modified,
                    },
                    "raw_sha256": entry.raw_sha256,
                    "result": entry.result,
                },
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        except (TypeError, ValueError) as error:  # e.g. a lone surrogate or a non-JSON value
            _log.warning("result_cache_entry_not_serialisable", error=type(error).__name__)
            return
        if len(payload) > MAX_ENTRY_BYTES:
            _log.info("result_cache_entry_too_large", size=len(payload))
            return
        try:
            await self._valkey.set(key, payload, ex=self._store_seconds)
        except (RedisError, OSError) as error:
            _log.warning("result_cache_write_failed", error=type(error).__name__)


def _decode(raw: bytes | str) -> CachedEntry | None:
    """Rebuild an entry; anything that does not have the expected shape is a miss."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    stored_at = data.get("stored_at")
    validators = data.get("validators")
    raw_sha256 = data.get("raw_sha256")
    result = data.get("result")
    if (
        isinstance(stored_at, bool)
        or not isinstance(stored_at, int | float)
        or not math.isfinite(stored_at)
        or not isinstance(validators, dict)
        or not isinstance(raw_sha256, str)
        or not isinstance(result, dict)
    ):
        return None
    etag = validators.get("etag")
    last_modified = validators.get("last_modified")
    if not (etag is None or isinstance(etag, str)) or not (
        last_modified is None or isinstance(last_modified, str)
    ):
        return None
    return CachedEntry(
        stored_at=float(stored_at),
        validators=Validators(etag=etag, last_modified=last_modified),
        raw_sha256=raw_sha256,
        result=result,
    )
