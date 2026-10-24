"""Clearance cookies of unblock mode in Valkey (B14): per host, with a TTL from the cookies."""

import asyncio
import json
from typing import Any

from fakeredis import FakeAsyncRedis
from structlog.testing import capture_logs

from text_extraction.clearance import ValkeyClearance, clearance_key
from text_extraction.render import CookieData
from text_extraction.settings import Settings

NOW = 1_000_000.0


def make(valkey: FakeAsyncRedis, **overrides: Any) -> ValkeyClearance:
    return ValkeyClearance(valkey, Settings(auth_mode="none", **overrides), clock=lambda: NOW)


def cookie(
    name: str, value: str, *, expires: float | None = None, domain: str = "example.org"
) -> CookieData:
    return CookieData(
        name=name, value=value, domain=domain, secure=True, http_only=True, expires=expires
    )


def test_round_trip_and_ttl_from_earliest_expiry() -> None:
    async def scenario() -> None:
        valkey = FakeAsyncRedis()
        store = make(valkey, browser_unblock_cookie_ttl_seconds=1800)
        cookies = [
            cookie("cf_clearance", "token-1", expires=NOW + 600),
            cookie("session", "token-2"),  # a session cookie: no expiry of its own
            cookie("later", "token-3", expires=NOW + 7200, domain="www.example.org"),
        ]
        await store.put("Example.ORG", cookies)
        assert await store.get("example.org") == cookies
        assert 590 <= await valkey.ttl(clearance_key("example.org")) <= 600

        # Without an earlier expiry the setting caps the TTL.
        await store.put("example.org", [cookie("session", "token-2")])
        assert 1790 <= await valkey.ttl(clearance_key("example.org")) <= 1800

        # Expired cookies are not stored; with nothing left the key goes.
        await store.put("example.org", [cookie("old", "token-4", expires=NOW - 1)])
        assert await valkey.exists(clearance_key("example.org")) == 0
        assert await store.get("example.org") == []

    asyncio.run(scenario())


def test_ttl_zero_disables_reuse() -> None:
    async def scenario() -> None:
        valkey = FakeAsyncRedis()
        await make(valkey).put("example.org", [cookie("cf_clearance", "token-1")])
        assert await valkey.exists(clearance_key("example.org")) == 1

        disabled = make(valkey, browser_unblock_cookie_ttl_seconds=0)
        assert await disabled.get("example.org") == []  # stored ones are not used either
        await disabled.put("other.example", [cookie("cf_clearance", "token-2")])
        assert await valkey.exists(clearance_key("other.example")) == 0

    asyncio.run(scenario())


def test_hosts_are_kept_apart() -> None:
    async def scenario() -> None:
        valkey = FakeAsyncRedis()
        store = make(valkey)
        first = [cookie("cf_clearance", "for-a", domain="a.example")]
        second = [cookie("cf_clearance", "for-b", domain="b.example")]
        await store.put("a.example", first)
        await store.put("b.example", second)
        assert await store.get("a.example") == first
        assert await store.get("b.example") == second
        assert await store.get("c.example") == []
        assert clearance_key("a.example") == "unblock:cookies:{a.example}"
        stored = json.loads(await valkey.get(clearance_key("a.example")) or b"[]")
        assert [entry["name"] for entry in stored] == ["cf_clearance"]

    asyncio.run(scenario())


def test_unreadable_entries_and_valkey_failures_act_as_none() -> None:
    class Broken(FakeAsyncRedis):
        async def get(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("down")

        async def set(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("down")

    async def scenario() -> None:
        valkey = FakeAsyncRedis()
        await valkey.set(clearance_key("example.org"), b"not json")
        assert await make(valkey).get("example.org") == []

        broken = make(Broken())
        with capture_logs() as logs:
            assert await broken.get("example.org") == []
            await broken.put("example.org", [cookie("cf_clearance", "token-secret")])
        assert [log["event"] for log in logs] == [
            "clearance_read_failed",
            "clearance_write_failed",
        ]
        assert "token-secret" not in repr(logs)

    asyncio.run(scenario())
