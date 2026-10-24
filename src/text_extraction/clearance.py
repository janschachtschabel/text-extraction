"""Clearance cookies of unblock mode in Valkey (B14).

After an unblock render whose last document was 2xx and no challenge, the pipeline stores the
cookies of the target host and its parent domain (``Rendered.cookies``); the next unblock render
of that host sets them before its first navigation, so a cleared challenge is not met again.
Only unblock renders read them, and they are never shown to a caller.

The key is ``unblock:cookies:{<host>}`` (the braces are a literal cluster hash tag), the value a
JSON list. Its TTL is the earliest expiry among the cookies, at most
``BROWSER_UNBLOCK_COOKIE_TTL_SECONDS``; 0 switches the reuse off (nothing is read or stored).
A Valkey problem never fails a render: a read acts as no cookies, a write is dropped, both with
one log line that carries neither the host's cookies nor their values.
"""

import json
import math
import time
from collections.abc import Callable
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from text_extraction.render import CookieData
from text_extraction.settings import Settings

_log = structlog.get_logger(__name__)


def clearance_key(host: str) -> str:
    return f"unblock:cookies:{{{_normal(host)}}}"


class ValkeyClearance:
    """The ``ClearanceStore`` of the browser worker; see the module docstring."""

    def __init__(
        self, valkey: Redis, settings: Settings, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._valkey = valkey
        self._settings = settings
        self._clock = clock

    async def get(self, host: str) -> list[CookieData]:
        if self._ttl_cap <= 0:
            return []
        try:
            raw = await self._valkey.get(clearance_key(host))
        except (RedisError, OSError) as error:
            _log.warning("clearance_read_failed", error=type(error).__name__)
            return []
        if raw is None:
            return []
        try:
            return [_decode(entry) for entry in json.loads(raw)]
        except (ValueError, TypeError, KeyError):
            _log.warning("clearance_entry_unreadable")
            return []

    async def put(self, host: str, cookies: list[CookieData]) -> None:
        cap = self._ttl_cap
        if cap <= 0:
            return
        now = self._clock()
        valid = [cookie for cookie in cookies if cookie.expires is None or cookie.expires > now]
        key = clearance_key(host)
        try:
            if not valid:
                await self._valkey.delete(key)
                return
            expiries = [cookie.expires - now for cookie in valid if cookie.expires is not None]
            ttl = min([float(cap), *expiries])
            payload = json.dumps([_encode(cookie) for cookie in valid], separators=(",", ":"))
            await self._valkey.set(key, payload, ex=max(math.floor(ttl), 1))
        except (RedisError, OSError) as error:
            _log.warning("clearance_write_failed", error=type(error).__name__)

    @property
    def _ttl_cap(self) -> int:
        return self._settings.browser_unblock_cookie_ttl_seconds


def _normal(host: str) -> str:
    return host.lower().rstrip(".")


def _encode(cookie: CookieData) -> dict[str, Any]:
    return {
        "name": cookie.name,
        "value": cookie.value,
        "domain": cookie.domain,
        "path": cookie.path,
        "secure": cookie.secure,
        "http_only": cookie.http_only,
        "expires": cookie.expires,
    }


def _decode(entry: dict[str, Any]) -> CookieData:
    expires = entry.get("expires")
    return CookieData(
        name=str(entry["name"]),
        value=str(entry["value"]),
        domain=str(entry["domain"]),
        path=str(entry.get("path", "/")),
        secure=bool(entry.get("secure", False)),
        http_only=bool(entry.get("http_only", False)),
        expires=float(expires) if expires is not None else None,
    )
