import asyncio
import hashlib
import json
import time
from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from redis.exceptions import ConnectionError as RedisConnectionError
from structlog.testing import capture_logs

from tests.fakes import ARTICLE, PAGE, FakeFetcher, fetched
from text_extraction.app import create_public_app
from text_extraction.auth import Principal
from text_extraction.envelope import ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.extract_models import ExtractRequest
from text_extraction.fetch import Fetcher
from text_extraction.jobs import JobStore
from text_extraction.pipeline import Pipeline, create_pipeline
from text_extraction.settings import Settings

USER_KEY = "user-key-0123456789-raw-secret"
AUTH = {"Authorization": f"Bearer {USER_KEY}"}


def make_settings(**overrides: Any) -> Settings:
    return Settings(
        auth_mode="api_key",
        api_keys=[
            {  # type: ignore[list-item]
                "name": "alice",
                "role": "user",
                "sha256": hashlib.sha256(USER_KEY.encode()).hexdigest(),
            }
        ],
        outbound_min_delay_ms=0,
        **overrides,
    )


@pytest.fixture
def registry() -> CollectorRegistry:
    return CollectorRegistry()


@pytest.fixture
def client(registry: CollectorRegistry) -> Iterator[TestClient]:
    settings = make_settings()
    app = create_public_app(settings, FakeAsyncRedis(), registry)
    fake = FakeFetcher({PAGE: fetched(body=ARTICLE), f"{PAGE}-gone": fetched(status=404)})
    app.state.pipeline = create_pipeline(settings, app.state.valkey, cast(Fetcher, fake), registry)
    with TestClient(app) as test_client:
        yield test_client


def test_requires_key(client: TestClient) -> None:
    response = client.post("/v1/extract", json={"url": PAGE})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_unknown_field_is_422_invalid_request(client: TestClient) -> None:
    response = client.post("/v1/extract", json={"url": PAGE, "wait": 3}, headers=AUTH)

    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "invalid_request"


def test_overlong_url_is_422(client: TestClient) -> None:
    url = "https://example.org/" + "a" * 2048
    response = client.post("/v1/extract", json={"url": url}, headers=AUTH)

    assert response.status_code == 422


def test_user_cannot_ignore_compliance_403(client: TestClient) -> None:
    response = client.post("/v1/extract", json={"url": PAGE, "compliance": "ignore"}, headers=AUTH)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_success_envelope_shape(client: TestClient) -> None:
    response = client.post(
        "/v1/extract",
        json={"url": PAGE, "format": "text", "links": True},
        headers={**AUTH, "x-client-trace-id": "trace-7"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["error"] is None
    assert body["trace_id"] == "trace-7"
    data = body["data"]
    assert set(data) == {
        "url",
        "final_url",
        "upstream_status",
        "content_type",
        "mode_used",
        "attempts",
        "language",
        "word_count",
        "title",
        "format",
        "content",
        "quality",
        "timings",
        "compliance",
        "metadata",
        "links",
        "fingerprint",
        "cache",
        "warnings",
    }
    assert data["format"] == "text"
    assert data["mode_used"] == "fast"
    assert data["upstream_status"] == 200
    assert set(data["timings"]) == {"total_ms", "compliance_ms", "fetch_ms", "convert_ms"}
    assert data["compliance"]["decision"] == "allowed"
    assert {"url", "text", "kind"} <= set(data["links"][0])


def test_target_failure_is_200_failed(client: TestClient) -> None:
    response = client.post("/v1/extract", json={"url": f"{PAGE}-gone"}, headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "target_http_error"
    assert body["data"]["upstream_status"] == 404


def test_metrics_count_outcomes(client: TestClient, registry: CollectorRegistry) -> None:
    client.post("/v1/extract", json={"url": PAGE, "cache": False}, headers=AUTH)
    client.post("/v1/extract", json={"url": PAGE, "cache": False}, headers=AUTH)
    client.post("/v1/extract", json={"url": f"{PAGE}-gone", "mode": "fast"}, headers=AUTH)

    sample = registry.get_sample_value
    assert sample("text_extraction_extract_total", {"mode": "auto", "outcome": "success"}) == 2
    assert (
        sample("text_extraction_extract_total", {"mode": "fast", "outcome": "target_http_error"})
        == 1
    )


def test_extract_is_in_openapi(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert "post" in paths["/v1/extract"]
    assert "wait_seconds" in client.get("/openapi.json").text


def test_lifespan_closes_the_fetcher(registry: CollectorRegistry) -> None:
    class ClosingFetcher:
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    app = create_public_app(make_settings(), FakeAsyncRedis(), registry)
    real = app.state.fetcher
    fake = ClosingFetcher()
    app.state.fetcher = fake
    with TestClient(app):
        assert fake.closed is False

    assert fake.closed is True
    asyncio.run(real.aclose())


# --- wait_seconds: the hand-over of a slow extraction to a job ------------------------------


class SlowPipeline:
    """The real pipeline after a delay, or a failure of its own after it."""

    def __init__(self, real: Pipeline, delay: float, fail: Exception | None = None) -> None:
        self.real = real
        self.delay = delay
        self.fail = fail
        self.started = asyncio.Event()

    async def run(self, req: ExtractRequest, principal: Principal, **kwargs: Any) -> Any:
        self.started.set()
        await asyncio.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        return await self.real.run(req, principal, **kwargs)


class Handover:
    """A public app on a fakeredis server whose pipeline answers after ``delay`` seconds."""

    def __init__(self, *, delay: float, fail: Exception | None = None, **overrides: Any) -> None:
        self.settings = make_settings(**overrides)
        self.server = FakeServer()
        self.registry = CollectorRegistry()
        self.app = create_public_app(
            self.settings, FakeAsyncRedis(server=self.server), self.registry
        )
        fake = FakeFetcher({PAGE: fetched(body=ARTICLE)})
        real = create_pipeline(
            self.settings, self.app.state.valkey, cast(Fetcher, fake), self.registry
        )
        self.pipeline = SlowPipeline(real, delay, fail)
        self.app.state.pipeline = self.pipeline

    def keys(self, pattern: str) -> list[str]:
        async def find() -> list[str]:
            valkey = FakeAsyncRedis(server=self.server)
            return sorted(
                key.decode() if isinstance(key, bytes) else key
                for key in await valkey.keys(pattern)
            )

        return asyncio.run(find())

    def job_keys(self) -> list[str]:
        return [*self.keys("job*"), *self.keys("q:*")]

    def wait_for_job(self, client: TestClient, job_id: str, state: str = "completed") -> Any:
        deadline = time.monotonic() + 10
        while True:
            body = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
            if body["data"]["state"] == state or time.monotonic() > deadline:
                return body
            time.sleep(0.02)


@pytest.fixture
def handover() -> Callable[..., Handover]:
    return lambda delay=0.0, **kwargs: Handover(delay=delay, **kwargs)


def test_extract_without_wait_seconds_is_unchanged(handover: Callable[..., Handover]) -> None:
    case = handover(0.2)
    with TestClient(case.app) as client:
        response = client.post("/v1/extract", json={"url": PAGE}, headers=AUTH)

        assert response.status_code == 200
        assert response.json()["status"] == "success"
        assert response.json()["data"]["cache"] == "miss"
        assert case.job_keys() == []
        assert case.app.state.background == set()


def test_extract_finishing_within_wait_is_200(handover: Callable[..., Handover]) -> None:
    case = handover(0.05)
    with TestClient(case.app) as client:
        response = client.post("/v1/extract", json={"url": PAGE, "wait_seconds": 5}, headers=AUTH)

        assert response.status_code == 200
        assert response.json()["status"] == "success"
        assert response.json()["data"]["title"]
        assert case.job_keys() == []
        assert case.app.state.background == set()


def test_slow_extract_hands_over_to_a_job(handover: Callable[..., Handover]) -> None:
    case = handover(0.5)
    with TestClient(case.app) as client:
        response = client.post(
            "/v1/extract",
            json={"url": PAGE, "wait_seconds": 0.1},
            headers={**AUTH, "x-client-trace-id": "t-9"},
        )

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "success"
        assert body["trace_id"] == "t-9"
        accepted = body["data"]
        assert accepted["kind"] == "extract"
        assert accepted["state"] == "running"
        assert accepted["total"] == 1
        assert accepted["status_url"] == f"/v1/jobs/{accepted['id']}"

        final = case.wait_for_job(client, accepted["id"])
        assert final["status"] == "success"
        assert final["data"]["kind"] == "extract"
        assert final["data"]["progress"] == {
            "total": 1,
            "done": 1,
            "success": 1,
            "partial": 0,
            "failed": 0,
        }
        item = final["data"]["items"][0]
        assert (item["index"], item["url"], item["state"]) == (0, PAGE, "done")
        assert item["result"]["status"] == "success"
        assert item["result"]["data"]["mode_used"] == "fast"
        assert item["result"]["data"]["title"]
        # An extract job is not a batch: it stays out of the owner's open set.
        assert case.keys("jobs:*") == []
        assert case.app.state.background == set()


def test_wait_seconds_zero_is_immediate_202(handover: Callable[..., Handover]) -> None:
    case = handover(1.0)
    with TestClient(case.app) as client:
        started = time.monotonic()
        response = client.post("/v1/extract", json={"url": PAGE, "wait_seconds": 0}, headers=AUTH)

        assert response.status_code == 202
        assert time.monotonic() - started < 0.9  # did not wait for the 1 s extraction
        final = case.wait_for_job(client, response.json()["data"]["id"])
        assert final["data"]["items"][0]["result"]["status"] == "success"


@pytest.mark.parametrize("value", [-1, 61, "soon"])
def test_wait_seconds_is_bounded(client: TestClient, value: object) -> None:
    response = client.post("/v1/extract", json={"url": PAGE, "wait_seconds": value}, headers=AUTH)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_forbidden_compliance_mode_is_403_not_a_job(handover: Callable[..., Handover]) -> None:
    case = handover(0.2)
    with TestClient(case.app) as client:
        response = client.post(
            "/v1/extract",
            json={"url": PAGE, "compliance": "ignore", "wait_seconds": 0},
            headers=AUTH,
        )

        assert response.status_code == 403
        assert case.job_keys() == []


def test_a_failure_of_the_extraction_is_stored_as_internal_error(
    handover: Callable[..., Handover],
) -> None:
    case = handover(0.2, fail=RuntimeError("boom"))
    with TestClient(case.app) as client:
        response = client.post("/v1/extract", json={"url": PAGE, "wait_seconds": 0}, headers=AUTH)
        assert response.status_code == 202

        final = case.wait_for_job(client, response.json()["data"]["id"])

        assert final["data"]["state"] == "completed"
        assert final["status"] == "failed"
        result = final["data"]["items"][0]["result"]
        assert result["status"] == "failed"
        assert result["error"]["code"] == "internal_error"
        assert result["error"]["retryable"] is False
        assert "boom" not in json.dumps(final)


def test_a_service_error_of_the_extraction_keeps_its_code(
    handover: Callable[..., Handover],
) -> None:
    unavailable = ServiceError(
        ErrorCode.INTERNAL_ERROR, "The backend is down.", 503, retryable=True
    )
    case = handover(0.2, fail=unavailable)
    with TestClient(case.app) as client:
        response = client.post("/v1/extract", json={"url": PAGE, "wait_seconds": 0}, headers=AUTH)

        final = case.wait_for_job(client, response.json()["data"]["id"])

        error = final["data"]["items"][0]["result"]["error"]
        assert (error["code"], error["message"], error["retryable"]) == (
            "internal_error",
            "The backend is down.",
            True,
        )


@pytest.mark.parametrize("failing", ["create", "mark_running"])
def test_failed_hand_over_keeps_waiting_and_answers_200(
    handover: Callable[..., Handover], monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    case = handover(0.3)
    store: JobStore = case.app.state.job_store

    async def down(*args: object, **kwargs: object) -> None:
        raise RedisConnectionError("valkey is down")

    monkeypatch.setattr(store, failing, down)
    with TestClient(case.app) as client, capture_logs() as logs:
        response = client.post(
            "/v1/extract", json={"url": PAGE, "wait_seconds": 0.05}, headers=AUTH
        )

        assert response.status_code == 200
        assert response.json()["status"] == "success"
        assert response.json()["data"]["title"]
        assert case.job_keys() == []  # no job is left behind
        assert case.app.state.background == set()
    events = [log for log in logs if log["event"] == "extract_job_handover_failed"]
    assert len(events) == 1
    assert events[0]["error_type"] == "ConnectionError"
    assert PAGE not in json.dumps(events, default=str)


def test_failed_hand_over_still_returns_the_error_of_the_extraction(
    handover: Callable[..., Handover], monkeypatch: pytest.MonkeyPatch
) -> None:
    unavailable = ServiceError(
        ErrorCode.INTERNAL_ERROR, "The backend is down.", 503, retryable=True
    )
    case = handover(0.3, fail=unavailable)

    async def down(*args: object, **kwargs: object) -> None:
        raise OSError("timed out")

    monkeypatch.setattr(case.app.state.job_store, "create", down)
    with TestClient(case.app) as client:
        response = client.post(
            "/v1/extract", json={"url": PAGE, "wait_seconds": 0.05}, headers=AUTH
        )

        assert response.status_code == 503
        assert response.json()["error"]["message"] == "The backend is down."


def test_unstorable_result_is_logged_and_the_lost_view_takes_over(
    handover: Callable[..., Handover], monkeypatch: pytest.MonkeyPatch
) -> None:
    case = handover(0.2)
    store: JobStore = case.app.state.job_store

    async def down(*args: object, **kwargs: object) -> None:
        raise RedisConnectionError("valkey is down")

    monkeypatch.setattr(store, "write_result", down)
    with TestClient(case.app) as client, capture_logs() as logs:
        response = client.post("/v1/extract", json={"url": PAGE, "wait_seconds": 0}, headers=AUTH)
        assert response.status_code == 202
        job_id = response.json()["data"]["id"]
        deadline = time.monotonic() + 10
        while case.app.state.background and time.monotonic() < deadline:
            time.sleep(0.02)

        assert case.app.state.background == set()
        events = [log for log in logs if log["event"] == "extract_job_result_not_stored"]
        assert len(events) == 1
        assert events[0]["job_id"] == job_id
        # The job was not completed. Its lease runs out; then the lost view takes over.
        view = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()["data"]
        assert view["state"] == "running"
        assert view["items"][0]["state"] == "pending"

        async def expire() -> None:
            valkey = FakeAsyncRedis(server=case.server)
            await valkey.hset(f"job:{{{job_id}}}:meta", "lease_until", repr(time.time() - 1))

        asyncio.run(expire())
        lost = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
        assert lost["data"]["items"][0]["result"]["error"]["code"] == "internal_error"
        assert lost["data"]["items"][0]["result"]["error"]["retryable"] is True


def test_shutdown_waits_for_background_extractions(handover: Callable[..., Handover]) -> None:
    case = handover(0.6)
    with TestClient(case.app) as client:
        response = client.post("/v1/extract", json={"url": PAGE, "wait_seconds": 0}, headers=AUTH)
        job_id = response.json()["data"]["id"]
        assert response.status_code == 202
        assert len(case.app.state.background) >= 1
    # The lifespan has ended: the extraction ran to its end and its result is stored.

    async def read() -> Any:
        store = JobStore(FakeAsyncRedis(server=case.server), case.settings)
        return await store.get(job_id), await store.items(job_id, 0, 10)

    record, items = asyncio.run(read())
    assert record is not None
    assert record.state == "completed"
    assert items[0].result is not None
    assert items[0].result.status == "success"
    assert case.app.state.background == set()


def test_shutdown_gives_up_after_the_extraction_deadline(
    handover: Callable[..., Handover], monkeypatch: pytest.MonkeyPatch
) -> None:
    case = handover(60, extract_timeout_seconds=2)
    monkeypatch.setattr("text_extraction.app._BACKGROUND_GRACE_SECONDS", 0.5)
    with TestClient(case.app) as client:
        job_id = client.post(
            "/v1/extract", json={"url": PAGE, "wait_seconds": 0}, headers=AUTH
        ).json()["data"]["id"]
        started = time.monotonic()
    # 2 s + 0.5 s grace instead of the 60 s the stub would take.
    assert time.monotonic() - started < 10
    assert case.app.state.background == set()
    # The cancelled extraction left a retryable error in its job.

    async def read() -> Any:
        return await JobStore(FakeAsyncRedis(server=case.server), case.settings).items(job_id, 0, 1)

    result = asyncio.run(read())[0].result
    assert result is not None
    assert result.error is not None
    assert (result.error.code, result.error.retryable) == ("internal_error", True)


def test_jobs_disabled_ignores_wait_seconds(handover: Callable[..., Handover]) -> None:
    case = handover(0.3, jobs_enabled=False)
    with TestClient(case.app) as client:
        response = client.post(
            "/v1/extract", json={"url": PAGE, "wait_seconds": 0.05}, headers=AUTH
        )

        assert response.status_code == 200
        assert response.json()["status"] == "success"
        assert response.json()["data"]["title"]
        assert case.job_keys() == []
        assert case.app.state.background == set()
