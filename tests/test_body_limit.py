"""The inbound body limit (``body_limit.py``): the public app refuses a body over its limit with
413 before it is read whole, parsed or authenticated; the internal app has no limit."""

import asyncio
import hashlib
import json
from collections.abc import Callable, Iterable, Iterator
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from starlette.types import Message, Receive, Scope, Send

from text_extraction.app import create_public_app
from text_extraction.body_limit import BodyLimitMiddleware
from text_extraction.internal import Readiness, create_internal_app
from text_extraction.jobs_api import batch_body_limit
from text_extraction.settings import Settings

USER_KEY = "user-key-0123456789-raw-secret"
USER = {"Authorization": f"Bearer {USER_KEY}"}
JSON = {"content-type": "application/json"}
LIMIT = 2048


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "auth_mode": "api_key",
        "api_keys": [
            {
                "name": "alice",
                "role": "user",
                "sha256": hashlib.sha256(USER_KEY.encode()).hexdigest(),
            }
        ],
        "request_max_body_bytes": LIMIT,
        "batch_max_urls": 3,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture
def public() -> Iterator[TestClient]:
    app = create_public_app(make_settings(), FakeAsyncRedis(), CollectorRegistry())
    with TestClient(app) as client:
        yield client


def body_of(size: int) -> bytes:
    """A JSON object of exactly ``size`` bytes that ``ExtractRequest`` refuses (unknown field)."""
    empty = len(b'{"pad":""}')
    body = json.dumps({"pad": "x" * (size - empty)}, separators=(",", ":")).encode()
    assert len(body) == size
    return body


def chunked(body: bytes, size: int = 500) -> Iterator[bytes]:
    for start in range(0, len(body), size):
        yield body[start : start + size]


def assert_too_large(response: Any, trace_id: str | None = None) -> None:
    assert response.status_code == 413
    envelope = response.json()
    assert envelope["status"] == "failed"
    assert envelope["error"]["code"] == "invalid_request"
    assert envelope["error"]["retryable"] is False
    assert "too large" in envelope["error"]["message"]
    if trace_id is not None:
        assert envelope["trace_id"] == trace_id


def test_oversized_content_length_is_refused_before_authentication(public: TestClient) -> None:
    response = public.post(
        "/v1/extract",
        content=body_of(LIMIT + 1),
        headers={**JSON, "x-client-trace-id": "trace-413"},
    )

    assert_too_large(response, "trace-413")
    assert response.headers["connection"] == "close"


def test_chunked_oversized_body_is_refused(public: TestClient) -> None:
    response = public.post(
        "/v1/extract",
        content=chunked(body_of(LIMIT + 1)),
        headers={**JSON, "x-client-trace-id": "trace-chunked"},
    )

    assert "content-length" not in response.request.headers
    assert_too_large(response, "trace-chunked")


@pytest.mark.parametrize("send", [bytes, chunked], ids=["content-length", "chunked"])
def test_body_at_the_limit_passes(
    public: TestClient, send: Callable[[bytes], Iterable[bytes]]
) -> None:
    body = body_of(LIMIT)

    unauthenticated = public.post("/v1/extract", content=send(body), headers=JSON)
    assert unauthenticated.status_code == 401  # read and passed on: auth answers

    invalid = public.post("/v1/extract", content=send(body), headers={**JSON, **USER})
    assert invalid.status_code == 422  # parsed and validated: the model refuses `pad`


def test_batch_has_the_batch_bound(public: TestClient) -> None:
    bound = batch_body_limit(make_settings())
    assert bound > LIMIT

    under = public.post("/v1/batch", content=body_of(bound), headers={**JSON, **USER})
    assert under.status_code == 422  # passed the limit, refused by the model

    over = public.post("/v1/batch", content=body_of(bound + 1), headers=JSON)
    assert_too_large(over)
    chunked_over = public.post("/v1/batch", content=chunked(body_of(bound + 1)), headers=JSON)
    assert_too_large(chunked_over)


def test_batch_bound_formula() -> None:
    settings = make_settings(batch_max_urls=1000)
    assert batch_body_limit(settings) == 1000 * (2048 + 8) + 4096


def test_internal_app_has_no_body_limit() -> None:
    readiness = Readiness()
    readiness.mark_listening()
    app = create_internal_app(
        make_settings(), FakeAsyncRedis(), CollectorRegistry(), readiness=readiness
    )
    with TestClient(app) as client:
        response = client.post("/health", content=b"x" * (LIMIT * 10))

    assert response.status_code == 405  # routed as usual; no 413


def test_middleware_answers_itself_when_the_app_does_not_handle_the_refusal() -> None:
    """A bare ASGI app that reads the whole body: the refusal raised inside ``receive`` reaches
    the middleware, which answers 413 since no response has started."""

    async def reader(scope: Scope, receive: Receive, send: Send) -> None:
        while (await receive()).get("more_body", False):
            pass
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"read"})

    middleware = BodyLimitMiddleware(reader, default_limit=10)
    chunks: list[Message] = [
        {"type": "http.request", "body": b"123456", "more_body": True},
        {"type": "http.request", "body": b"123456", "more_body": False},
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        return chunks.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {"type": "http", "method": "POST", "path": "/x", "headers": []}
    asyncio.run(middleware(scope, receive, send))

    assert sent[0]["status"] == 413
    assert json.loads(sent[1]["body"])["error"]["code"] == "invalid_request"


def test_middleware_lets_lifespan_and_small_bodies_through() -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])
        if scope["type"] == "http":
            message = await receive()
            seen.append(message["body"].decode())

    middleware = BodyLimitMiddleware(app, default_limit=10, path_limits={"/big": 100})

    async def receive() -> Message:
        return {"type": "http.request", "body": b"x" * 50, "more_body": False}

    async def send(message: Message) -> None:
        raise AssertionError("nothing is sent")

    async def run() -> None:
        await middleware({"type": "lifespan"}, receive, send)
        await middleware({"type": "http", "path": "/big", "headers": []}, receive, send)

    asyncio.run(run())

    assert seen == ["lifespan", "http", "x" * 50]
