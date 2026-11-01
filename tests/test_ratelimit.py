import asyncio
import hashlib
from collections.abc import Iterator
from typing import Annotated, Any

import pytest
from fakeredis import FakeAsyncRedis
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from redis.exceptions import OutOfMemoryError
from structlog.testing import capture_logs

from text_extraction.auth import Principal, require_role
from text_extraction.errors import install_error_handlers
from text_extraction.ratelimit import (
    RateDecision,
    RateLimiter,
    check_rate_left,
    rate_limited,
)
from text_extraction.settings import Settings
from text_extraction.valkey import create_valkey, valkey_ping

# 20 s into a minute window: 40 s are left.
NOW = 60 * 1_000_000 + 20.0
DOWN_URL = "redis://:s3cret-password@127.0.0.1:1/0"

KEYS = {"alice": "alice-key", "bob": "bob-key", "carol": "carol-key"}


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def make_settings(*, auth_mode: str = "api_key", enabled: bool = True) -> Settings:
    return Settings(
        auth_mode=auth_mode,  # type: ignore[arg-type]
        rate_limit_enabled=enabled,
        api_keys=[
            {"name": "alice", "role": "user", "sha256": sha256_hex(KEYS["alice"])},  # type: ignore[list-item]
            {
                "name": "bob",
                "role": "user",
                "sha256": sha256_hex(KEYS["bob"]),
                "rate_limit_per_minute": 1,
            },  # type: ignore[list-item]
            {
                "name": "carol",
                "role": "admin",
                "sha256": sha256_hex(KEYS["carol"]),
                "rate_limit_per_minute": 5,
            },  # type: ignore[list-item]
        ],
    )


def build_app(settings: Settings, client: Redis, clock: Clock, default_per_minute: int) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    app.state.rate_limiter = RateLimiter(client, default_per_minute, clock=clock)
    install_error_handlers(app)

    @app.get("/limited", dependencies=[Depends(require_role("user")), Depends(rate_limited)])
    async def limited() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/open")
    async def open_route() -> dict[str, bool]:
        return {"ok": True}

    return app


async def _counter_keys(client: Redis) -> list[str]:
    return sorted(
        key.decode() if isinstance(key, bytes) else key for key in await client.keys("rl:*")
    )


def counter_keys(client: Redis) -> list[str]:
    return asyncio.run(_counter_keys(client))


def bearer(name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {KEYS[name]}"}


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def fake() -> Redis:
    return FakeAsyncRedis()


@pytest.fixture
def client(fake: Redis, clock: Clock) -> Iterator[TestClient]:
    with TestClient(build_app(make_settings(), fake, clock, default_per_minute=2)) as test_client:
        yield test_client


def test_allows_up_to_limit(fake: Redis, clock: Clock) -> None:
    limiter = RateLimiter(fake, 3, clock=clock)

    async def scenario() -> list[RateDecision]:
        return [await limiter.hit("alice", 3) for _ in range(3)]

    decisions = asyncio.run(scenario())

    assert [d.allowed for d in decisions] == [True, True, True]
    assert [d.remaining for d in decisions] == [2, 1, 0]


def test_blocks_over_limit_with_retry_after(fake: Redis, clock: Clock) -> None:
    limiter = RateLimiter(fake, 2, clock=clock)

    async def scenario() -> tuple[RateDecision, RateDecision]:
        await limiter.hit("alice", 2)
        await limiter.hit("alice", 2)
        blocked = await limiter.hit("alice", 2)
        clock.now = NOW + 40  # the next window starts
        return blocked, await limiter.hit("alice", 2)

    blocked, fresh = asyncio.run(scenario())

    assert blocked == RateDecision(allowed=False, remaining=0, retry_after=40)
    assert fresh.allowed is True
    assert fresh.remaining == 1


def test_retry_after_is_at_least_one_second(fake: Redis, clock: Clock) -> None:
    clock.now = NOW + 39.999
    limiter = RateLimiter(fake, 1, clock=clock)

    async def scenario() -> RateDecision:
        await limiter.hit("alice", 1)
        return await limiter.hit("alice", 1)

    assert asyncio.run(scenario()).retry_after == 1


async def _count(client: Redis, name: str) -> bytes | str | None:
    value: bytes | str | None = await client.get(f"rl:{name}:{int(NOW // 60)}")
    return value


def test_peek_does_not_count(fake: Redis, clock: Clock) -> None:
    limiter = RateLimiter(fake, 2, clock=clock)

    async def scenario() -> list[RateDecision]:
        fresh = await limiter.peek("alice", 2)
        await limiter.hit("alice", 2)
        one_used = await limiter.peek("alice", 2)
        await limiter.hit("alice", 2)
        return [fresh, one_used, await limiter.peek("alice", 2), await limiter.peek("alice", 2)]

    decisions = asyncio.run(scenario())

    assert decisions[0] == RateDecision(allowed=True, remaining=2, retry_after=40)
    assert decisions[1] == RateDecision(allowed=True, remaining=1, retry_after=40)
    # The bucket is used up: a peek says so, and does not count itself.
    assert decisions[2] == decisions[3] == RateDecision(allowed=False, remaining=0, retry_after=40)
    assert asyncio.run(_count(fake, "alice")) == b"2"


def test_check_rate_left_refuses_a_used_bucket_without_counting(fake: Redis, clock: Clock) -> None:
    app = build_app(make_settings(), fake, clock, default_per_minute=2)
    bob = Principal(name="bob", role="user", rate_limit_per_minute=1)

    @app.get("/peek")
    async def peek(request: Request) -> dict[str, bool]:
        await check_rate_left(request.app.state, bob)
        return {"ok": True}

    with TestClient(app) as client:
        before = [client.get("/peek").status_code for _ in range(3)]
        client.get("/limited", headers=bearer("bob"))
        after = client.get("/peek")

    assert before == [200, 200, 200]
    assert after.status_code == 429
    assert after.json()["error"]["code"] == "rate_limited"
    assert after.headers["retry-after"] == "40"
    assert asyncio.run(_count(fake, "bob")) == b"1"


def test_check_rate_left_off_and_down(clock: Clock) -> None:
    bob = Principal(name="bob", role="user", rate_limit_per_minute=1)
    down = FakeAsyncRedis(connected=False)
    statuses = []
    for enabled in (False, True):
        app = build_app(make_settings(enabled=enabled), down, clock, default_per_minute=1)

        @app.get("/peek")
        async def peek(request: Request) -> dict[str, bool]:
            await check_rate_left(request.app.state, bob)
            return {"ok": True}

        with TestClient(app) as client:
            statuses.append(client.get("/peek").status_code)

    assert statuses == [200, 503]


def test_counter_key_expires_after_the_window(fake: Redis, clock: Clock) -> None:
    limiter = RateLimiter(fake, 1, clock=clock)

    async def scenario() -> int:
        await limiter.hit("alice", 1)
        return await fake.ttl(f"rl:alice:{int(NOW // 60)}")

    ttl = asyncio.run(scenario())

    assert counter_keys(fake) == [f"rl:alice:{int(NOW // 60)}"]
    assert 0 < ttl <= 60


def test_route_blocks_over_limit_with_envelope(client: TestClient) -> None:
    first = client.get("/limited", headers=bearer("alice"))
    second = client.get("/limited", headers=bearer("alice"))
    third = client.get("/limited", headers=bearer("alice"))

    assert first.status_code == second.status_code == 200
    assert third.status_code == 429
    assert third.headers["retry-after"] == "40"
    body = third.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "rate_limited"
    assert body["error"]["retryable"] is True


def test_successful_responses_carry_no_rate_limit_headers(client: TestClient) -> None:
    response = client.get("/limited", headers=bearer("alice"))

    assert response.status_code == 200
    assert "retry-after" not in response.headers
    assert not [name for name in response.headers if name.lower().startswith("x-ratelimit")]


def test_per_key_limit_overrides_default(client: TestClient) -> None:
    # Default is 2, bob has 1, carol has 5.
    bob = [client.get("/limited", headers=bearer("bob")).status_code for _ in range(2)]
    carol = [client.get("/limited", headers=bearer("carol")).status_code for _ in range(6)]
    alice = [client.get("/limited", headers=bearer("alice")).status_code for _ in range(3)]

    assert bob == [200, 429]
    assert carol == [200] * 5 + [429]
    assert alice == [200, 200, 429]


def test_each_principal_has_its_own_bucket(client: TestClient) -> None:
    for _ in range(3):
        client.get("/limited", headers=bearer("alice"))

    assert client.get("/limited", headers=bearer("bob")).status_code == 200


def test_unauthenticated_request_does_not_consume_a_bucket(client: TestClient, fake: Redis) -> None:
    response = client.get("/limited")

    assert response.status_code == 401
    assert counter_keys(fake) == []


def test_auth_none_shares_the_anonymous_bucket(fake: Redis, clock: Clock) -> None:
    settings = make_settings(auth_mode="none")
    with TestClient(build_app(settings, fake, clock, default_per_minute=2)) as client:
        statuses = [client.get("/limited").status_code for _ in range(3)]
        keys = counter_keys(fake)

    assert statuses == [200, 200, 429]
    assert keys == [f"rl:anonymous:{int(NOW // 60)}"]


def test_disabled_never_blocks(fake: Redis, clock: Clock) -> None:
    # The backend is unreachable, so any use of it would answer 503.
    down = FakeAsyncRedis(connected=False)
    settings = make_settings(enabled=False)
    with TestClient(build_app(settings, down, clock, default_per_minute=1)) as client:
        statuses = [client.get("/limited", headers=bearer("bob")).status_code for _ in range(5)]

    assert statuses == [200] * 5
    assert counter_keys(fake) == []


@pytest.mark.parametrize("kind", ["fakeredis_disconnected", "closed_port"])
def test_valkey_down_is_503_retryable(clock: Clock, kind: str) -> None:
    down: Redis = (
        FakeAsyncRedis(connected=False)
        if kind == "fakeredis_disconnected"
        else create_valkey(DOWN_URL)
    )
    app = build_app(make_settings(), down, clock, default_per_minute=2)
    with TestClient(app) as client, capture_logs() as logs:
        response = client.get("/limited", headers=bearer("alice"))

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["retryable"] is True
    warnings = [entry for entry in logs if entry["log_level"] == "warning"]
    (warning,) = warnings
    assert warning["event"] == "rate_limit_backend_unavailable"
    assert warning["error_type"]
    assert "exc_info" not in warning  # one short line per request, no traceback
    # The URL may carry a password: neither the log nor the answer may contain it.
    assert "s3cret-password" not in repr(logs)
    assert "s3cret-password" not in response.text


def test_route_without_the_dependency_is_not_limited(client: TestClient) -> None:
    assert [client.get("/open").status_code for _ in range(5)] == [200] * 5


def test_authentication_runs_once_per_request() -> None:
    # `rate_limited` uses `authenticate`, so a route that also requires a role authenticates once.
    from text_extraction.auth import authenticate

    calls = 0

    async def counting(request: Request) -> Principal:
        nonlocal calls
        calls += 1
        return await authenticate(request)

    app = FastAPI()
    app.state.settings = make_settings()
    app.state.rate_limiter = RateLimiter(FakeAsyncRedis(), 5, clock=Clock())
    install_error_handlers(app)
    app.dependency_overrides[authenticate] = counting

    @app.get("/x", dependencies=[Depends(require_role("user")), Depends(rate_limited)])
    async def route(principal: Annotated[Principal, Depends(authenticate)]) -> dict[str, str]:
        return {"name": principal.name}

    with TestClient(app) as client:
        response = client.get("/x", headers=bearer("alice"))

    assert response.status_code == 200
    assert calls == 1


def test_valkey_ping_true_when_reachable(fake: Redis) -> None:
    assert asyncio.run(valkey_ping(fake)) is True


@pytest.mark.parametrize("kind", ["fakeredis_disconnected", "closed_port"])
def test_valkey_ping_false_on_connection_error(kind: str) -> None:
    down: Redis = (
        FakeAsyncRedis(connected=False)
        if kind == "fakeredis_disconnected"
        else create_valkey(DOWN_URL)
    )

    assert asyncio.run(valkey_ping(down)) is False


def test_create_valkey_uses_short_socket_timeouts() -> None:
    client = create_valkey("redis://localhost:6379/0")
    kwargs = client.connection_pool.connection_kwargs

    assert kwargs["socket_connect_timeout"] == 2
    assert kwargs["socket_timeout"] == 2


def test_per_key_limit_is_not_replaced_by_the_default_when_falsy(fake: Redis, clock: Clock) -> None:
    # `is not None`, not truthiness: a limit of 0 must not fall back to the default of 2.
    app = build_app(make_settings(), fake, clock, default_per_minute=2)
    from text_extraction.auth import authenticate

    app.dependency_overrides[authenticate] = lambda: Principal("zero", "user", 0)
    with TestClient(app) as client:
        response = client.get("/limited")

    assert response.status_code == 429


class _FullValkey(FakeAsyncRedis):
    """A Valkey at maxmemory under noeviction: every write is refused with OOM."""

    def pipeline(self, *args: Any, **kwargs: Any) -> Any:
        raise OutOfMemoryError("OOM command not allowed when used memory > 'maxmemory'.")


def test_a_full_valkey_lets_calls_through_uncounted(clock: Clock) -> None:
    # Valkey at maxmemory refuses the counter's write. Every route answered 503 then, DELETE of
    # a job too, the one call that frees memory; the memory guard refuses new jobs anyway.
    app = build_app(make_settings(), _FullValkey(), clock, default_per_minute=1)
    with TestClient(app) as client, capture_logs() as logs:
        statuses = [client.get("/limited", headers=bearer("alice")).status_code for _ in range(3)]

    assert statuses == [200, 200, 200]
    events = [entry["event"] for entry in logs if entry["log_level"] == "warning"]
    assert events == ["rate_limit_backend_full"] * 3
