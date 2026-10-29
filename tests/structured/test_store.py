"""The store of learned selectors (ruling S8): per owner, host and schema, merged per field,
a TTL renewed on writes only, and a Valkey failure on a read that costs nothing but the rules."""

import asyncio
import json
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis
from prometheus_client import CollectorRegistry
from redis.exceptions import RedisError

from tests.fakes import ARTICLE, PAGE, FakeFetcher, fetched
from text_extraction.auth import Principal
from text_extraction.compliance import ComplianceChecker
from text_extraction.extract_models import ExtractRequest
from text_extraction.fetch import Fetcher
from text_extraction.fingerprint import FingerprintStore
from text_extraction.outbound import DomainGate
from text_extraction.pipeline import Pipeline
from text_extraction.result_cache import ResultCache
from text_extraction.settings import Settings
from text_extraction.structured.store import (
    LEARNED_SELECTORS_UNAVAILABLE_WARNING,
    LearnedSelectorsUnavailableError,
    SelectorStore,
    selector_host,
)
from text_extraction.structured.types import SelectorRule, StructuredOptions

DAY = 86_400
TITLE = SelectorRule(css="h1")
PRICE = SelectorRule(css=".price", attr="content")


def test_store_per_owner_and_ttl() -> None:
    async def scenario() -> None:
        valkey = FakeAsyncRedis()
        store = SelectorStore(valkey, ttl_seconds=30 * DAY)
        await store.put("alice", "example.org", "abc123abc123", {"title": TITLE})

        assert await store.get("alice", "example.org", "abc123abc123") == {"title": TITLE}
        # Another key name, another host or another schema sees nothing (D2 a).
        assert await store.get("bob", "example.org", "abc123abc123") == {}
        assert await store.get("alice", "other.example", "abc123abc123") == {}
        assert await store.get("alice", "example.org", "ffffffffffff") == {}

        key = "sel:{alice}:example.org:abc123abc123"
        raw = await valkey.get(key)
        assert raw is not None
        assert json.loads(raw) == {"title": {"css": "h1"}}
        assert 30 * DAY - 5 <= await valkey.ttl(key) <= 30 * DAY
        # Reads do not renew the TTL, writes do.
        await valkey.expire(key, 100)
        await store.get("alice", "example.org", "abc123abc123")
        assert await valkey.ttl(key) <= 100
        await store.put("alice", "example.org", "abc123abc123", {"title": TITLE})
        assert await valkey.ttl(key) > 100

    asyncio.run(scenario())


def test_store_put_merges_fields() -> None:
    async def scenario() -> None:
        valkey = FakeAsyncRedis()
        store = SelectorStore(valkey, ttl_seconds=DAY)
        await store.put("alice", "Example.ORG", "abc123abc123", {"title": TITLE})
        await store.put("alice", "example.org", "abc123abc123", {"price": PRICE})
        newer = SelectorRule(xpath="//h2")
        await store.put("alice", "example.org", "abc123abc123", {"title": newer})

        assert await store.get("alice", "example.org", "abc123abc123") == {
            "title": newer,
            "price": PRICE,
        }
        # A stored rule this release cannot read is left out; the others stay.
        key = "sel:{alice}:example.org:abc123abc123"
        raw = await valkey.get(key)
        assert raw is not None
        stored = json.loads(raw)
        stored["broken"] = {"css": "a", "xpath": "//a"}
        await valkey.set(key, json.dumps(stored))
        assert set(await store.get("alice", "example.org", "abc123abc123")) == {"title", "price"}
        await valkey.set(key, b"not json")
        assert await store.get("alice", "example.org", "abc123abc123") == {}

    asyncio.run(scenario())


def test_selector_host() -> None:
    assert selector_host("https://WWW.Example.org:8443/a?b") == "www.example.org"
    assert selector_host("") is None


SCHEMA: dict[str, Any] = {"type": "object", "properties": {"headline": {"type": "string"}}}


def _pipeline(valkey: FakeAsyncRedis, store: SelectorStore) -> Pipeline:
    settings = Settings(auth_mode="none", outbound_min_delay_ms=0)
    fetcher = cast(Fetcher, FakeFetcher({PAGE: fetched()}))
    return Pipeline(
        fetcher,
        DomainGate(valkey, settings),
        ComplianceChecker(fetcher, valkey, settings),
        ResultCache(valkey, settings),
        FingerprintStore(valkey, settings),
        settings,
        CollectorRegistry(),
        selector_store=store,
    )


def test_store_error_on_get_is_warning() -> None:
    async def failing_get() -> None:
        store = SelectorStore(FakeAsyncRedis(connected=False), ttl_seconds=DAY)
        with pytest.raises(LearnedSelectorsUnavailableError):
            await store.get("alice", "example.org", "abc123abc123")
        with pytest.raises(RedisError):
            await store.put("alice", "example.org", "abc123abc123", {"title": TITLE})

    asyncio.run(failing_get())

    # In the pipeline: no learned rules and the warning, but the extraction and the caller's
    # own selectors go on.
    pipeline = _pipeline(
        FakeAsyncRedis(), SelectorStore(FakeAsyncRedis(connected=False), ttl_seconds=DAY)
    )
    options = StructuredOptions(
        schema=SCHEMA, strategy="selectors", selectors={"headline": SelectorRule(css="h1")}
    )
    request = ExtractRequest(url=PAGE, structured=options)
    user = Principal(name="alice", role="user", rate_limit_per_minute=None)
    envelope = asyncio.run(pipeline.run(request, user))

    assert envelope.status == "success"
    assert envelope.data is not None
    assert LEARNED_SELECTORS_UNAVAILABLE_WARNING in envelope.data.warnings
    assert envelope.data.structured is not None
    assert envelope.data.structured.values == {"headline": "Photosynthesis explained"}
    assert "Photosynthesis explained" in ARTICLE

    # A request that reads no learned selectors gets no warning.
    plain = asyncio.run(pipeline.run(ExtractRequest(url=PAGE, cache=False), user))
    assert plain.data is not None
    assert LEARNED_SELECTORS_UNAVAILABLE_WARNING not in plain.data.warnings
