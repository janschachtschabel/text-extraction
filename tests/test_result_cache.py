import asyncio
import json
import math
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from redis.asyncio import Redis
from structlog.testing import capture_logs

from text_extraction import result_cache
from text_extraction.fetch import Validators
from text_extraction.result_cache import CachedEntry, ResultCache, cache_key
from text_extraction.settings import Settings

URL = "https://example.org/docs/page.html"
START = 1_000_000.0


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        return self.now


def make_cache(valkey: Redis, clock: Clock | None = None, **overrides: Any) -> ResultCache:
    settings = Settings(auth_mode="none", **overrides)
    return ResultCache(valkey, settings, clock=clock or Clock())


def make_entry(stored_at: float = START, result: dict[str, Any] | None = None) -> CachedEntry:
    return CachedEntry(
        stored_at=stored_at,
        validators=Validators(etag='W/"abc"', last_modified="Wed, 21 Oct 2026 07:28:00 GMT"),
        raw_sha256="c" * 64,
        result=result if result is not None else {"content": "Grüße", "word_count": 1},
    )


def test_cache_fresh_then_stale_then_gone() -> None:
    async def scenario() -> list[Any]:
        valkey = FakeAsyncRedis()
        clock = Clock()
        cache = make_cache(valkey, clock, result_cache_ttl_seconds=60, result_store_ttl_seconds=600)
        key = cache_key(URL, {})
        seen: list[Any] = [await cache.get(key)]  # nothing stored yet

        await cache.put(key, make_entry(stored_at=clock.now))
        ttl = await valkey.ttl(key)
        assert 0 < ttl <= 600

        clock.now += 59
        seen.append(await cache.get(key))
        clock.now += 1  # exactly result_cache_ttl_seconds old
        seen.append(await cache.get(key))

        await valkey.pexpire(key, 20)  # the Valkey TTL (result_store_ttl_seconds) runs out
        await asyncio.sleep(0.1)
        seen.append(await cache.get(key))
        return seen

    missing, fresh, stale, gone = asyncio.run(scenario())
    assert missing is None
    assert fresh is not None
    assert fresh[1] is True
    assert stale is not None
    assert stale[1] is False
    assert gone is None
    entry = fresh[0]
    assert entry == make_entry()  # validators, raw hash and result survive the round trip
    assert isinstance(entry.validators, Validators)


def test_ttl_zero_never_fresh() -> None:
    async def scenario() -> tuple[bool, bool]:
        valkey = FakeAsyncRedis()
        cache = make_cache(valkey, result_cache_ttl_seconds=0)
        key = cache_key(URL, {})
        await cache.put(key, make_entry())
        got = await cache.get(key)
        assert got is not None
        # A zero store TTL stores nothing at all.
        off = make_cache(valkey, result_store_ttl_seconds=0)
        other = cache_key(URL, {"x": 1})
        await off.put(other, make_entry())
        return got[1], await valkey.exists(other) == 1

    fresh, stored = asyncio.run(scenario())
    assert fresh is False  # still usable for revalidation
    assert stored is False


def test_cache_key_order_independent() -> None:
    a = cache_key(URL, {"mode": "auto", "formats": ["markdown"], "nested": {"a": 1, "b": 2}})
    b = cache_key(URL, {"nested": {"b": 2, "a": 1}, "formats": ["markdown"], "mode": "auto"})
    assert a == b
    assert a.startswith("res:{")
    assert a.endswith("}")
    assert len(a) == len("res:{}") + 64


def test_cache_key_differs_by_options_and_url_but_not_by_spelling() -> None:
    base = cache_key(URL, {"mode": "auto"})
    assert cache_key(URL, {"mode": "fast"}) != base
    assert cache_key(URL + "?q=1", {"mode": "auto"}) != base
    assert cache_key("HTTPS://EXAMPLE.org/docs/page.html#x", {"mode": "auto"}) == base
    assert cache_key("https://example.org:443/docs/page.html", {"mode": "auto"}) == base


def test_oversized_entry_not_cached() -> None:
    async def scenario() -> tuple[bool, bool]:
        valkey = FakeAsyncRedis()
        cache = make_cache(valkey)
        big = cache_key(URL, {"size": "big"})
        await cache.put(big, make_entry(result={"content": "x" * (2 * 1024 * 1024)}))
        small = cache_key(URL, {"size": "small"})
        await cache.put(small, make_entry(result={"content": "x" * 100_000}))
        return await valkey.exists(big) == 1, await valkey.exists(small) == 1

    big_stored, small_stored = asyncio.run(scenario())
    assert big_stored is False
    assert small_stored is True


def test_unreadable_entries_count_as_missing() -> None:
    async def scenario() -> list[Any]:
        valkey = FakeAsyncRedis()
        cache = make_cache(valkey)
        key = cache_key(URL, {})
        out = []
        for raw in (
            b"{not json",
            b"[1, 2]",
            json.dumps(
                {"stored_at": "yesterday", "validators": {}, "raw_sha256": "x", "result": {}}
            ),
            json.dumps({"stored_at": 1.0, "validators": [], "raw_sha256": "x", "result": {}}),
            json.dumps({"stored_at": 1.0, "validators": {}, "raw_sha256": "x", "result": []}),
            json.dumps({"stored_at": True, "validators": {}, "raw_sha256": "x", "result": {}}),
        ):
            await valkey.set(key, raw)
            out.append(await cache.get(key))
        return out

    assert asyncio.run(scenario()) == [None] * 6


def test_validators_may_be_empty() -> None:
    async def scenario() -> CachedEntry | None:
        cache = make_cache(FakeAsyncRedis())
        key = cache_key(URL, {})
        entry = CachedEntry(START, Validators(), "d" * 64, {})
        await cache.put(key, entry)
        got = await cache.get(key)
        return got[0] if got else None

    assert asyncio.run(scenario()) == CachedEntry(START, Validators(), "d" * 64, {})


def test_valkey_errors_degrade_to_no_cache() -> None:
    async def scenario() -> Any:
        cache = make_cache(FakeAsyncRedis(connected=False))
        key = cache_key("https://example.org/secret?token=abc", {})
        await cache.put(key, make_entry())  # must not raise
        return await cache.get(key)

    with capture_logs() as logs:
        got = asyncio.run(scenario())
    assert got is None
    events = {entry["event"] for entry in logs if entry["log_level"] == "warning"}
    assert events == {"result_cache_write_failed", "result_cache_read_failed"}
    assert "example.org" not in repr(logs)
    assert "token" not in repr(logs)


def test_cache_key_depends_on_the_package_version(monkeypatch: pytest.MonkeyPatch) -> None:
    before = cache_key(URL, {"mode": "auto"})
    monkeypatch.setattr(result_cache, "__version__", "99.0.0")
    after = cache_key(URL, {"mode": "auto"})
    assert after != before
    assert cache_key(URL, {"mode": "auto"}) == after  # stable within one version


def test_non_finite_stored_at_is_a_miss() -> None:
    async def scenario() -> list[Any]:
        valkey = FakeAsyncRedis()
        cache = make_cache(valkey)
        key = cache_key(URL, {})
        out = []
        for stored_at in ("Infinity", "-Infinity", "NaN"):
            raw = (
                f'{{"stored_at": {stored_at}, "validators": {{}}, '
                '"raw_sha256": "x", "result": {}}'
            )
            await valkey.set(key, raw)
            out.append(await cache.get(key))
        return out

    assert asyncio.run(scenario()) == [None] * 3


def test_future_stored_at_is_stale() -> None:
    async def scenario() -> tuple[CachedEntry, bool] | None:
        clock = Clock()
        cache = make_cache(FakeAsyncRedis(), clock)
        key = cache_key(URL, {})
        await cache.put(key, make_entry(stored_at=clock.now + 30))  # another pod's clock is ahead
        return await cache.get(key)

    got = asyncio.run(scenario())
    assert got is not None
    assert got[1] is False
    assert math.isfinite(got[0].stored_at)


@pytest.mark.parametrize("bad", ["\ud800", object()], ids=["lone_surrogate", "not_json"])
def test_unserialisable_result_is_not_cached(bad: object) -> None:
    async def scenario() -> tuple[bool, list[Any]]:
        valkey = FakeAsyncRedis()
        cache = make_cache(valkey)
        key = cache_key(URL, {})
        with capture_logs() as logs:
            await cache.put(key, make_entry(result={"content": bad}))  # must not raise
        return await valkey.exists(key) == 1, logs

    stored, logs = asyncio.run(scenario())
    assert stored is False
    assert [entry["event"] for entry in logs] == ["result_cache_entry_not_serialisable"]
