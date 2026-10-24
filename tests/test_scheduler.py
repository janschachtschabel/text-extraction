"""Fair queue with leases (``scheduler.py``).

The tests run against fakeredis with Lua. Set ``SCHEDULER_TEST_VALKEY_URL`` (for example
``redis://127.0.0.1:6399/0``) to run them against a real Redis or Valkey instead; that database is
flushed before each test.
"""

import asyncio
import os
import re
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from redis.asyncio import Redis
from structlog.testing import capture_logs

from text_extraction import scheduler
from text_extraction.scheduler import Claim, FairQueue, QueueCounts, QueueItem, domain_of
from text_extraction.settings import Settings

START = 1_000_000.0
LEASE = 60.0
JOB_A = "a" * 32
JOB_B = "b" * 32
JOB_C = "c" * 32


def scenario(test: Callable[[], Coroutine[Any, Any, None]]) -> Callable[[], None]:
    """Run an async test body on its own event loop (the suite has no async plugin)."""

    @wraps(test)
    def run() -> None:
        asyncio.run(test())

    return run


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        return self.now


async def make_valkey() -> Redis:
    url = os.environ.get("SCHEDULER_TEST_VALKEY_URL")
    if not url:
        return FakeAsyncRedis()
    client: Redis = Redis.from_url(url)
    await client.flushdb()
    return client


def make_queue(
    valkey: Redis, clock: Clock, pool: str = "extract", **overrides: object
) -> FairQueue:
    settings = Settings(auth_mode="none", **overrides)  # type: ignore[arg-type]
    return FairQueue(valkey, settings, pool, clock=clock)


def urls(host: str, count: int) -> list[str]:
    return [f"https://{host}/page-{n}" for n in range(count)]


async def claim_item(queue: FairQueue) -> QueueItem:
    claim = await queue.claim(LEASE)
    assert claim.item is not None
    return claim.item


async def running(valkey: Redis, pool: str = "extract") -> dict[str, int]:
    raw = await valkey.hgetall(f"q:{{{pool}}}:running")
    return {os.fsdecode(key): int(value) for key, value in raw.items()}


@scenario
async def test_claims_rotate_between_clients() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock)
    await queue.enqueue(JOB_A, "A", urls("one.example", 100))
    await queue.enqueue(JOB_B, "B", urls("one.example", 10))

    order = []
    for _ in range(25):
        item = await claim_item(queue)
        order.append(item.client)
        assert await queue.complete(item)

    assert order[:20] == ["A", "B"] * 10
    assert order[20:] == ["A"] * 5


@scenario
async def test_claims_rotate_between_domains_of_one_client() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock)
    await queue.enqueue(JOB_A, "A", urls("a.example", 6) + urls("b.example", 2))

    order = []
    for _ in range(8):
        item = await claim_item(queue)
        order.append(item.domain)
        assert await queue.complete(item)

    assert order == ["a.example", "b.example", "a.example", "b.example"] + ["a.example"] * 4
    assert (await queue.claim(LEASE)).item is None


@scenario
async def test_items_of_one_domain_come_in_index_order_across_chunks() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock)
    await queue.enqueue(JOB_A, "A", urls("a.example", 1201))

    indexes = []
    for _ in range(1201):
        item = await claim_item(queue)
        indexes.append(item.index)
        await queue.complete(item)

    assert indexes == list(range(1201))
    assert item.job_id == JOB_A
    assert item.attempts == 0


@scenario
async def test_enqueue_of_nothing_changes_nothing() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock)
    await queue.enqueue(JOB_A, "A", [])

    assert await valkey.keys("*") == []
    assert await queue.counts() == QueueCounts(pending=0, leased=0, clients=0)


@scenario
async def test_domain_at_its_cap_is_skipped() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock, outbound_max_concurrency_per_host=2)
    await queue.enqueue(JOB_A, "A", urls("a.example", 3))
    first = await claim_item(queue)
    await claim_item(queue)
    await queue.enqueue(JOB_B, "A", urls("b.example", 1))

    item = await claim_item(queue)
    assert item.domain == "b.example"
    assert (await queue.claim(LEASE)).item is None
    assert await running(valkey) == {"a.example": 2, "b.example": 1}

    assert await queue.complete(first)
    third = await claim_item(queue)
    assert (third.domain, third.index) == ("a.example", 2)


@scenario
async def test_cap_counts_leases_of_all_clients() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock, outbound_max_concurrency_per_host=1)
    await queue.enqueue(JOB_A, "A", urls("a.example", 2))
    await queue.enqueue(JOB_B, "B", urls("a.example", 2))

    assert (await claim_item(queue)).client == "A"
    claim = await queue.claim(LEASE)
    assert claim == Claim(item=None, exhausted=False, retry_in=None)


@scenario
async def test_deferred_domain_waits_until_not_before() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock)
    await queue.enqueue(JOB_A, "A", urls("a.example", 1) + urls("b.example", 2))

    item = await claim_item(queue)
    assert (item.domain, item.attempts) == ("a.example", 0)
    await queue.defer(item, clock.now + 30)
    assert await running(valkey) == {}

    for _ in range(2):
        other = await claim_item(queue)
        assert other.domain == "b.example"
        assert await queue.complete(other)
    claim = await queue.claim(LEASE)
    assert claim.item is None
    assert claim.retry_in == pytest.approx(30)

    clock.now += 10
    assert (await queue.claim(LEASE)).retry_in == pytest.approx(20)

    clock.now += 20
    again = await claim_item(queue)
    assert (again.job_id, again.index, again.domain) == (JOB_A, 0, "a.example")
    assert again.attempts == 1


@scenario
async def test_defer_keeps_the_later_not_before() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock)
    await queue.enqueue(JOB_A, "A", urls("a.example", 1))
    await queue.enqueue(JOB_B, "B", urls("a.example", 1))

    first = await claim_item(queue)
    second = await claim_item(queue)
    await queue.defer(first, clock.now + 30)
    await queue.defer(second, clock.now + 10)

    assert (await queue.claim(LEASE)).retry_in == pytest.approx(30)
    clock.now += 29
    assert (await queue.claim(LEASE)).item is None
    clock.now += 1
    assert (await claim_item(queue)).attempts == 1


@scenario
async def test_deferred_item_goes_back_to_the_front_of_its_domain() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock)
    await queue.enqueue(JOB_A, "A", urls("a.example", 3))

    item = await claim_item(queue)
    assert await queue.defer(item, clock.now) is True

    again = await claim_item(queue)
    assert (again.index, again.attempts) == (0, 1)


@scenario
async def test_defer_after_a_lost_lease_changes_nothing() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock)
    await queue.enqueue(JOB_A, "A", urls("a.example", 1))
    lost = await claim_item(queue)
    clock.now += LEASE
    rerun = await claim_item(queue)

    assert await queue.defer(lost, clock.now + 30) is False

    assert await queue.counts() == QueueCounts(pending=0, leased=1, clients=0)
    assert await running(valkey) == {"a.example": 1}
    assert await valkey.hlen("q:{extract}:notbefore") == 0
    assert await queue.complete(rerun)


@scenario
async def test_expired_lease_is_reclaimed_first_in_line() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock)
    await queue.enqueue(JOB_A, "A", urls("a.example", 3))

    item = await claim_item(queue)
    clock.now += LEASE - 1
    assert (await claim_item(queue)).index == 1
    clock.now += 1

    again = await claim_item(queue)
    assert (again.job_id, again.index, again.client, again.domain) == (JOB_A, 0, "A", "a.example")
    assert again.attempts == item.attempts + 1
    assert await running(valkey) == {"a.example": 2}


@scenario
async def test_reclaimed_item_reenters_the_rotations() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock)
    await queue.enqueue(JOB_A, "A", urls("a.example", 1))
    await claim_item(queue)
    assert await queue.counts() == QueueCounts(pending=0, leased=1, clients=0)

    clock.now += LEASE
    again = await claim_item(queue)

    assert (again.index, again.attempts) == (0, 1)


@scenario
async def test_lost_leases_beyond_max_attempts_are_exhausted() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock, job_item_max_attempts=2)
    await queue.enqueue(JOB_A, "A", urls("a.example", 1))

    for attempts in range(3):
        item = await claim_item(queue)
        assert item.attempts == attempts
        clock.now += LEASE

    claim = await queue.claim(LEASE)
    assert claim.exhausted is True
    assert claim.item is not None
    item = claim.item
    assert (item.job_id, item.index, item.client, item.domain) == (JOB_A, 0, "A", "a.example")
    assert item.attempts == 3
    assert await queue.counts() == QueueCounts(pending=0, leased=1, clients=0)
    assert await running(valkey) == {"a.example": 1}

    assert await queue.complete(item)
    assert await queue.claim(LEASE) == Claim(item=None, exhausted=False, retry_in=None)
    assert await queue.counts() == QueueCounts(pending=0, leased=0, clients=0)
    assert await running(valkey) == {}


@scenario
async def test_exhausted_item_is_offered_again_until_completed() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock, job_item_max_attempts=1)
    await queue.enqueue(JOB_A, "A", urls("a.example", 1))
    for _ in range(2):
        await claim_item(queue)
        clock.now += LEASE
    first = await queue.claim(LEASE)
    assert first.exhausted
    assert first.item is not None
    assert first.item.attempts == 2

    clock.now += LEASE  # the worker died before it recorded the failure
    second = await queue.claim(LEASE)

    assert second.exhausted
    assert second.item is not None
    assert (second.item.job_id, second.item.index) == (JOB_A, 0)
    assert second.item.attempts == 2  # does not grow while it waits for its failure
    assert second.item.lease != first.item.lease
    assert await queue.complete(first.item) is False  # the old lease is fenced off
    await queue.defer(first.item, clock.now + 30)
    assert await queue.counts() == QueueCounts(pending=0, leased=1, clients=0)
    assert await running(valkey) == {"a.example": 1}
    assert await queue.complete(second.item) is True
    assert await queue.counts() == QueueCounts(pending=0, leased=0, clients=0)
    assert await running(valkey) == {}


@scenario
async def test_deferrals_count_towards_max_attempts() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock, job_item_max_attempts=2)
    await queue.enqueue(JOB_A, "A", urls("a.example", 1))

    await queue.defer(await claim_item(queue), clock.now)
    await queue.defer(await claim_item(queue), clock.now)
    assert (await claim_item(queue)).attempts == 2
    clock.now += LEASE

    claim = await queue.claim(LEASE)
    assert claim.exhausted
    assert claim.item is not None
    assert claim.item.attempts == 3


@scenario
async def test_defer_of_an_exhausted_item_keeps_its_attempts() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock, job_item_max_attempts=1)
    await queue.enqueue(JOB_A, "A", urls("a.example", 1))
    await defer_each(queue, 2, clock.now)
    exhausted = await queue.claim(LEASE)
    assert exhausted.exhausted
    assert exhausted.item is not None

    await queue.defer(exhausted.item, clock.now)

    again = await queue.claim(LEASE)
    assert again.exhausted
    assert again.item is not None
    assert again.item.attempts == 2


@scenario
async def test_reclaim_takes_at_most_twenty_expired_leases_per_claim() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock, outbound_max_concurrency_per_host=50)
    await queue.enqueue(JOB_A, "A", urls("a.example", 25))
    for _ in range(25):
        await claim_item(queue)
    clock.now += LEASE

    await claim_item(queue)
    assert await queue.counts() == QueueCounts(pending=19, leased=6, clients=1)
    await claim_item(queue)
    assert await queue.counts() == QueueCounts(pending=23, leased=2, clients=1)
    assert await running(valkey) == {"a.example": 2}


@scenario
async def test_complete_after_expiry_returns_false() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock)
    await queue.enqueue(JOB_A, "A", urls("a.example", 1))
    lost = await claim_item(queue)
    clock.now += LEASE
    rerun = await claim_item(queue)

    assert await queue.complete(lost) is False
    assert await running(valkey) == {"a.example": 1}
    assert await queue.complete(rerun) is True
    assert await queue.complete(rerun) is False
    assert await running(valkey) == {}
    assert await queue.counts() == QueueCounts(pending=0, leased=0, clients=0)


@scenario
async def test_complete_of_an_expired_lease_not_yet_reclaimed_counts() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock)
    await queue.enqueue(JOB_A, "A", urls("a.example", 1))
    item = await claim_item(queue)
    clock.now += LEASE * 2

    assert await queue.complete(item) is True
    assert await queue.claim(LEASE) == Claim(item=None, exhausted=False, retry_in=None)
    assert await running(valkey) == {}


@scenario
async def test_counts() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock)
    await queue.enqueue(JOB_A, "A", urls("a.example", 3) + urls("b.example", 2))
    await queue.enqueue(JOB_B, "B", urls("a.example", 1))
    assert await queue.counts() == QueueCounts(pending=6, leased=0, clients=2)

    item = await claim_item(queue)
    assert await queue.counts() == QueueCounts(pending=5, leased=1, clients=2)
    await queue.defer(item, clock.now + 30)
    assert await queue.counts() == QueueCounts(pending=6, leased=0, clients=2)

    for _ in range(2):  # a.example is deferred: only A's b.example items are claimable
        other = await claim_item(queue)
        assert other.domain == "b.example"
        await queue.complete(other)
    assert await queue.counts() == QueueCounts(pending=4, leased=0, clients=2)


@scenario
async def test_invalid_url_goes_to_the_dash_domain() -> None:
    assert domain_of("not a url") == "-"
    assert domain_of("https:///no-host") == "-"
    assert domain_of("http://[::1/broken") == "-"
    assert domain_of("https://Example.COM./x?y=1") == "example.com"
    assert domain_of("https://bücher.example/") == "xn--bcher-kva.example"
    assert domain_of("http://[2001:db8::1]:8080/") == "2001:db8::1"

    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock)
    await queue.enqueue(JOB_A, "A", ["not a url", "https://a.example/"])

    item = await claim_item(queue)
    assert (item.index, item.domain) == (0, "-")


@scenario
async def test_client_and_domain_names_with_separators_stay_apart() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock)
    # Without escaping, both lists would be q:{extract}:c:a:d:::d:c
    await queue.enqueue(JOB_A, "a", ["http://[::d:c]/"])
    await queue.enqueue(JOB_B, "a:d::", ["http://c/"])
    await queue.enqueue(JOB_C, "tab\tand %3A", ["https://c.example/"])

    expected = {
        (JOB_A, "a", "::d:c"),
        (JOB_B, "a:d::", "c"),
        (JOB_C, "tab\tand %3A", "c.example"),
    }
    first = {
        (item.job_id, item.client, item.domain)
        for item in [await claim_item(queue) for _ in range(3)]
    }
    assert first == expected
    clock.now += LEASE  # the reclaim reads client and domain back from the lease
    second = {
        (item.job_id, item.client, item.domain)
        for item in [await claim_item(queue) for _ in range(3)]
    }
    assert second == expected
    assert await queue.counts() == QueueCounts(pending=0, leased=3, clients=0)


@scenario
async def test_duplicate_of_a_leased_item_is_dropped() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock, outbound_max_concurrency_per_host=3)
    await queue.enqueue(JOB_A, "A", urls("a.example", 2))
    await queue.enqueue(JOB_A, "A", urls("a.example", 2))  # the same items once more

    assert [(await claim_item(queue)).index for _ in range(2)] == [0, 1]
    clock.now += 10
    assert (await queue.claim(LEASE)).item is None

    for index in range(2):  # the leases of the first copies keep their expiry
        assert await valkey.zscore("q:{extract}:leases", f"{JOB_A}:{index}:0") == START + LEASE
    assert await running(valkey) == {"a.example": 2}
    assert await queue.counts() == QueueCounts(pending=0, leased=2, clients=0)


@scenario
async def test_clients_rotate_while_hosts_are_at_their_cap() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock, outbound_max_concurrency_per_host=2)
    await queue.enqueue(JOB_A, "A", urls("a.example", 10) + urls("c.example", 10))
    await queue.enqueue(JOB_B, "B", urls("b.example", 10))

    order = [(await claim_item(queue)).domain for _ in range(6)]  # nothing is completed

    assert order == ["a.example", "b.example", "c.example", "b.example", "a.example", "c.example"]
    assert await queue.claim(LEASE) == Claim(item=None, exhausted=False, retry_in=None)
    assert await running(valkey) == {"a.example": 2, "b.example": 2, "c.example": 2}


async def defer_each(queue: FairQueue, count: int, until: float) -> None:
    for _ in range(count):
        await queue.defer(await claim_item(queue), until)


@scenario
async def test_claim_visits_at_most_the_scan_budget() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock, scheduler_claim_scan_budget=5)
    await queue.enqueue(JOB_A, "A", [f"https://d{n}.example/" for n in range(8)])
    await defer_each(queue, 8, clock.now + 30)

    for _ in range(3):
        claim = await queue.claim(LEASE)
        assert claim.item is None
        assert claim.retry_in == pytest.approx(0.05)


@scenario
async def test_scan_budget_continues_with_the_next_domains() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock, scheduler_claim_scan_budget=3)
    await queue.enqueue(JOB_A, "A", [f"https://d{n}.example/" for n in range(5)])
    await defer_each(queue, 5, clock.now + 30)
    await queue.enqueue(JOB_B, "A", ["https://free.example/"])

    assert (await queue.claim(LEASE)).item is None
    item = await claim_item(queue)
    assert (item.job_id, item.domain) == (JOB_B, "free.example")


@scenario
async def test_scan_budget_continues_with_the_next_clients() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock, scheduler_claim_scan_budget=2)
    for n in range(4):
        await queue.enqueue(JOB_A, f"client-{n}", [f"https://d{n}.example/"])
    await defer_each(queue, 4, clock.now + 30)
    await queue.enqueue(JOB_B, "late", ["https://free.example/"])

    assert (await queue.claim(LEASE)).item is None
    assert (await queue.claim(LEASE)).item is None
    assert (await claim_item(queue)).client == "late"


@scenario
async def test_client_with_more_domains_than_the_budget_does_not_starve_others() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock, scheduler_claim_scan_budget=3)
    await queue.enqueue(JOB_A, "A", [f"https://d{n}.example/" for n in range(5)])
    await defer_each(queue, 5, clock.now + 600)
    await queue.enqueue(JOB_B, "B", ["https://free.example/"])

    served = None
    for attempt in range(3):
        claim = await queue.claim(LEASE)
        if claim.item is not None:
            served = (attempt, claim.item.client)
            break

    assert served == (1, "B")


@scenario
async def test_scan_incomplete_tells_a_budget_answer_from_a_full_idle_pass() -> None:
    clock = Clock()
    for domains, incomplete, retry_in in ((4, True, 0.05), (3, False, 30)):
        valkey = await make_valkey()
        queue = make_queue(valkey, clock, scheduler_claim_scan_budget=3)
        await queue.enqueue(JOB_A, "A", [f"https://d{n}.example/" for n in range(domains)])
        await defer_each(queue, domains, clock.now + 30)

        claim = await queue.claim(LEASE)

        assert claim.item is None
        assert claim.scan_incomplete is incomplete
        assert claim.retry_in == pytest.approx(retry_in)

    await queue.enqueue(JOB_B, "B", ["https://free.example/"])  # 4 pairs now, budget 3
    assert (await queue.claim(LEASE)).scan_incomplete is True
    claimed = await queue.claim(LEASE)
    assert claimed.item is not None
    assert claimed.scan_incomplete is False


@scenario
async def test_orphan_client_and_empty_domain_are_dropped_on_a_claim() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock)
    await queue.enqueue(JOB_A, "A", urls("a.example", 1))
    await valkey.lpush("q:{extract}:clients", "ghost")
    await valkey.lpush("q:{extract}:c:A:domains", "stray.example")

    item = await claim_item(queue)

    assert item.domain == "a.example"
    assert await valkey.exists("q:{extract}:clients", "q:{extract}:c:A:domains") == 0
    assert await queue.counts() == QueueCounts(pending=0, leased=1, clients=0)


@scenario
async def test_orphan_client_and_empty_domain_are_dropped_on_an_idle_pass() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock)
    await queue.enqueue(JOB_A, "A", urls("a.example", 1))
    await defer_each(queue, 1, clock.now + 30)
    await valkey.rpush("q:{extract}:clients", "ghost")
    await valkey.rpush("q:{extract}:c:A:domains", "stray.example")

    claim = await queue.claim(LEASE)

    assert claim.item is None
    assert claim.retry_in == pytest.approx(30)
    assert await valkey.lrange("q:{extract}:clients", 0, -1) == [b"A"]
    assert await valkey.lrange("q:{extract}:c:A:domains", 0, -1) == [b"a.example"]
    clock.now += 30
    assert (await claim_item(queue)).domain == "a.example"


def test_counts_reads_only_the_totals() -> None:
    """The gauges read the counts every 5 s per API pod: the script reads the size of the client
    list, the pending counter and the size of the lease set, never a client's domain list, so
    its cost does not grow with the queue. It stays read-only."""
    script = scheduler._COUNTS
    assert script.startswith("#!lua flags=no-writes\n")
    body = script.split(scheduler._PREAMBLE, 1)[1]
    calls = sorted(set(re.findall(r"redis\.call\('([A-Z]+)'", body)))
    assert calls == ["GET", "LLEN", "ZCARD"]


@scenario
async def test_counts_report_a_negative_counter_with_a_warning() -> None:
    valkey, clock = await make_valkey(), Clock()
    queue = make_queue(valkey, clock)
    await valkey.set("q:{extract}:pending", -2)

    with capture_logs() as logs:
        counts = await queue.counts()

    assert counts.pending == -2
    assert [entry["event"] for entry in logs] == ["queue_counter_negative"]
    assert logs[0]["log_level"] == "warning"


@scenario
async def test_all_queue_keys_share_one_hash_tag() -> None:
    valkey, clock = await make_valkey(), Clock()
    for pool in ("extract", "browser"):
        queue = make_queue(valkey, clock, pool, outbound_max_concurrency_per_host=1)
        await queue.enqueue(JOB_A, "client:{x}", [*urls("a.example", 2), "http://[::1]/"])
        await queue.enqueue(JOB_B, "B", urls("b.example", 2))
        deferred = await claim_item(queue)
        await queue.defer(deferred, clock.now + 30)
        await claim_item(queue)  # stays leased
        await queue.counts()

    keys = sorted(os.fsdecode(key) for key in await valkey.keys("*"))
    assert keys
    for key in keys:
        assert key.startswith(("q:{extract}:", "q:{browser}:")), key
    assert {"q:{extract}:running", "q:{extract}:leases", "q:{extract}:leased"} <= set(keys)
    assert {"q:{browser}:notbefore", "q:{browser}:pending", "q:{browser}:clients"} <= set(keys)


def test_pool_name_must_be_safe_in_a_key() -> None:
    settings = Settings(auth_mode="none")
    with pytest.raises(ValueError, match="pool"):
        FairQueue(FakeAsyncRedis(), settings, "x}y")


@scenario
async def test_enqueue_items_keeps_given_indexes() -> None:
    valkey = await make_valkey()
    queue = make_queue(valkey, Clock(), "browser")
    await queue.enqueue_items(
        JOB_A, "alice", [(7, "https://example.org/a"), (3, "https://other.example/b")]
    )

    first = await claim_item(queue)
    second = await claim_item(queue)

    assert {(item.job_id, item.index, item.domain, item.attempts) for item in (first, second)} == {
        (JOB_A, 7, "example.org", 0),
        (JOB_A, 3, "other.example", 0),
    }
    assert {first.client, second.client} == {"alice"}
    assert (await queue.claim(LEASE)).item is None
    # The other pool's queue is untouched.
    assert (await make_queue(valkey, Clock()).counts()).pending == 0
