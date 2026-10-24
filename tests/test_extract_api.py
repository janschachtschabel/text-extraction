import asyncio
import contextlib
import hashlib
import json
import threading
import time
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
import structlog
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from redis.exceptions import ConnectionError as RedisConnectionError
from structlog.testing import capture_logs

from tests.fakes import ARTICLE, PAGE, FakeFetcher, fetched
from text_extraction.app import _wait_for, create_public_app
from text_extraction.auth import Principal
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.extract_api import _store_result, extract
from text_extraction.extract_models import ExtractRequest, ExtractResult, failed_extract
from text_extraction.fetch import Fetcher
from text_extraction.job_models import JobOptions
from text_extraction.jobs import JobStore
from text_extraction.pipeline import Pipeline, create_pipeline
from text_extraction.settings import Settings

USER_KEY = "user-key-0123456789-raw-secret"
USER = Principal(name="user", role="user", rate_limit_per_minute=None)
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


def test_extract_200_schema_is_the_plain_envelope(client: TestClient) -> None:
    """Package 2's contract: the 200 answer is `Envelope[ExtractResult]`; the 202 is declared
    beside it."""
    plain = FastAPI()

    @plain.get("/plain")
    async def plain_route() -> Envelope[ExtractResult]:
        raise NotImplementedError

    expected = plain.openapi()["components"]["schemas"]["Envelope_ExtractResult_"]
    spec = client.get("/openapi.json").json()
    responses = spec["paths"]["/v1/extract"]["post"]["responses"]

    ok = responses["200"]["content"]["application/json"]["schema"]
    assert ok == {"$ref": "#/components/schemas/Envelope_ExtractResult_"}
    assert spec["components"]["schemas"]["Envelope_ExtractResult_"] == expected
    accepted = responses["202"]["content"]["application/json"]["schema"]
    assert accepted == {"$ref": "#/components/schemas/Envelope_JobAccepted_"}


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
#
# The pipeline stub below finishes when the test releases it (a threading.Event, since the app
# runs in the test client's thread), so no test depends on how long a real sleep takes.


class GatedPipeline:
    """The real pipeline once ``release`` is set, or a failure of its own then."""

    def __init__(self, real: Pipeline, fail: Exception | None = None) -> None:
        self.real = real
        self.fail = fail
        self.release = threading.Event()
        self.started = threading.Event()
        self.tasks: list[asyncio.Task[Any]] = []

    async def run(self, req: ExtractRequest, principal: Principal, **kwargs: Any) -> Any:
        task = asyncio.current_task()
        assert task is not None
        self.tasks.append(task)
        self.started.set()
        while not self.release.is_set():  # noqa: ASYNC110 - set by another thread
            await asyncio.sleep(0.005)
        if self.fail is not None:
            raise self.fail
        return await self.real.run(req, principal, **kwargs)


class Handover:
    """A public app on a fakeredis server whose pipeline waits for ``pipeline.release``."""

    def __init__(self, *, fail: Exception | None = None, **overrides: Any) -> None:
        self.settings = make_settings(**overrides)
        self.server = FakeServer()
        self.registry = CollectorRegistry()
        valkey = FakeAsyncRedis(server=self.server)
        self.app = create_public_app(self.settings, valkey, self.registry)
        fake = FakeFetcher({PAGE: fetched(body=ARTICLE)})
        real = create_pipeline(self.settings, valkey, cast(Fetcher, fake), self.registry)
        self.pipeline = GatedPipeline(real, fail)
        self.app.state.pipeline = self.pipeline

    def release_later(self, seconds: float = 0.2) -> None:
        """Release the pipeline from another thread, while the test thread is in a request."""
        threading.Timer(seconds, self.pipeline.release.set).start()

    def release_when(self, ready: Callable[[], bool], after: float = 0.0) -> None:
        """Release the pipeline from another thread once ``ready()`` holds (and ``after``
        seconds later), while the test thread is in a request."""

        def wait_then_release() -> None:
            deadline = time.monotonic() + 30
            while not ready() and time.monotonic() < deadline:
                time.sleep(0.005)
            time.sleep(after)
            self.pipeline.release.set()

        threading.Thread(target=wait_then_release, daemon=True).start()

    async def keys_async(self, pattern: str) -> list[str]:
        valkey = FakeAsyncRedis(server=self.server)
        return sorted(
            key.decode() if isinstance(key, bytes) else key for key in await valkey.keys(pattern)
        )

    def keys(self, pattern: str) -> list[str]:
        return asyncio.run(self.keys_async(pattern))

    def job_keys(self) -> list[str]:
        return [*self.keys("job*"), *self.keys("q:*")]

    def wait_for_job(self, client: TestClient, job_id: str, state: str = "completed") -> Any:
        deadline = time.monotonic() + 30
        while True:
            body = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
            if body["data"]["state"] == state or time.monotonic() > deadline:
                return body
            time.sleep(0.01)

    def wait_for_idle(self) -> None:
        deadline = time.monotonic() + 30
        while self.app.state.background and time.monotonic() < deadline:
            time.sleep(0.01)


@pytest.fixture
def handover() -> Callable[..., Handover]:
    return lambda **kwargs: Handover(**kwargs)


def test_extract_without_wait_seconds_is_unchanged(handover: Callable[..., Handover]) -> None:
    case = handover()
    case.pipeline.release.set()
    with TestClient(case.app) as client:
        response = client.post("/v1/extract", json={"url": PAGE}, headers=AUTH)

        assert response.status_code == 200
        assert response.json()["status"] == "success"
        assert response.json()["data"]["cache"] == "miss"
        assert case.job_keys() == []
        assert case.app.state.background == set()


def test_extract_finishing_within_wait_is_200(handover: Callable[..., Handover]) -> None:
    case = handover()
    case.pipeline.release.set()
    with TestClient(case.app) as client:
        response = client.post("/v1/extract", json={"url": PAGE, "wait_seconds": 30}, headers=AUTH)

        assert response.status_code == 200
        assert response.json()["status"] == "success"
        assert response.json()["data"]["title"]
        assert case.job_keys() == []
        assert case.app.state.background == set()


def test_slow_extract_hands_over_to_a_job(handover: Callable[..., Handover]) -> None:
    case = handover()
    with TestClient(case.app) as client:
        response = client.post(
            "/v1/extract",
            json={"url": PAGE, "wait_seconds": 0.05},
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
        running = client.get(accepted["status_url"], headers=AUTH).json()["data"]
        assert (running["state"], running["items"][0]["state"]) == ("running", "pending")

        case.pipeline.release.set()
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
        case.wait_for_idle()
        assert case.app.state.background == set()


def sample(case: Handover, name: str, kind: str = "extract") -> float | None:
    return case.registry.get_sample_value(f"text_extraction_{name}", {"kind": kind})


def test_handed_over_extract_is_counted_and_timed_once(
    handover: Callable[..., Handover],
) -> None:
    case = handover()
    assert sample(case, "jobs_created_total") == 0
    with TestClient(case.app) as client:
        response = client.post("/v1/extract", json={"url": PAGE, "wait_seconds": 0}, headers=AUTH)
        assert response.status_code == 202
        assert sample(case, "jobs_created_total") == 1
        assert sample(case, "job_duration_seconds_count") is None  # still running

        case.pipeline.release.set()
        case.wait_for_job(client, response.json()["data"]["id"])
        case.wait_for_idle()

        assert sample(case, "job_duration_seconds_count") == 1
        assert 0 <= (sample(case, "job_duration_seconds_sum") or -1) < 30
        assert sample(case, "jobs_created_total", "batch") == 0
        assert sample(case, "job_duration_seconds_count", "batch") is None


def test_extract_that_finishes_within_wait_is_not_a_job_in_the_metrics(
    handover: Callable[..., Handover],
) -> None:
    case = handover()
    case.pipeline.release.set()
    with TestClient(case.app) as client:
        response = client.post("/v1/extract", json={"url": PAGE, "wait_seconds": 30}, headers=AUTH)
        assert response.status_code == 200
    assert sample(case, "jobs_created_total") == 0
    assert sample(case, "job_duration_seconds_count") is None


def test_failed_hand_over_is_not_counted(
    handover: Callable[..., Handover], monkeypatch: pytest.MonkeyPatch
) -> None:
    case = handover()

    async def down(*args: object, **kwargs: object) -> None:
        case.pipeline.release.set()
        raise RedisConnectionError("valkey is down")

    monkeypatch.setattr(case.app.state.job_store, "create", down)
    with TestClient(case.app) as client:
        response = client.post("/v1/extract", json={"url": PAGE, "wait_seconds": 0}, headers=AUTH)
        assert response.status_code == 200
    assert sample(case, "jobs_created_total") == 0
    assert sample(case, "job_duration_seconds_count") is None


def test_only_the_write_that_completes_the_job_is_timed(
    handover: Callable[..., Handover],
) -> None:
    case = handover()
    store: JobStore = case.app.state.job_store
    metrics = case.app.state.job_metrics
    options = JobOptions(mode="fast", compliance="enforce")
    envelope = Envelope[ExtractResult](status="failed")

    async def store_twice() -> None:
        job_id = await store.create("extract", USER, options, [PAGE])
        await store.mark_running(job_id, lease_seconds=60)
        await _store_result(store, metrics, job_id, time.monotonic(), envelope)
        await _store_result(store, metrics, job_id, time.monotonic(), envelope)  # repeat
        await _store_result(store, metrics, "f" * 32, time.monotonic(), envelope)  # job gone

    asyncio.run(store_twice())

    assert sample(case, "job_duration_seconds_count") == 1


def test_wait_seconds_zero_is_immediate_202(handover: Callable[..., Handover]) -> None:
    case = handover()
    with TestClient(case.app) as client:
        response = client.post("/v1/extract", json={"url": PAGE, "wait_seconds": 0}, headers=AUTH)

        # The extraction is still held back by the test: the answer did not wait for it.
        assert response.status_code == 202
        assert not case.pipeline.release.is_set()
        case.pipeline.release.set()
        final = case.wait_for_job(client, response.json()["data"]["id"])
        assert final["data"]["items"][0]["result"]["status"] == "success"


@pytest.mark.parametrize("value", [-1, 61, "soon"])
def test_wait_seconds_is_bounded(client: TestClient, value: object) -> None:
    response = client.post("/v1/extract", json={"url": PAGE, "wait_seconds": value}, headers=AUTH)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_forbidden_compliance_mode_is_403_not_a_job(handover: Callable[..., Handover]) -> None:
    case = handover()
    with TestClient(case.app) as client:
        response = client.post(
            "/v1/extract",
            json={"url": PAGE, "compliance": "ignore", "wait_seconds": 0},
            headers=AUTH,
        )

        assert response.status_code == 403
        assert case.job_keys() == []
        assert case.app.state.background == set()


def test_a_failure_of_the_extraction_is_stored_as_internal_error(
    handover: Callable[..., Handover],
) -> None:
    case = handover(fail=RuntimeError("boom"))
    with TestClient(case.app) as client:
        response = client.post("/v1/extract", json={"url": PAGE, "wait_seconds": 0}, headers=AUTH)
        assert response.status_code == 202
        case.pipeline.release.set()

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
    case = handover(fail=unavailable)
    with TestClient(case.app) as client:
        response = client.post("/v1/extract", json={"url": PAGE, "wait_seconds": 0}, headers=AUTH)
        case.pipeline.release.set()

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
    case = handover()
    store: JobStore = case.app.state.job_store

    async def down(*args: object, **kwargs: object) -> None:
        # The extraction ends only now: the request has to be waiting for it, not answering 503.
        case.pipeline.release.set()
        raise RedisConnectionError("valkey is down")

    monkeypatch.setattr(store, failing, down)
    with TestClient(case.app) as client, capture_logs() as logs:
        response = client.post("/v1/extract", json={"url": PAGE, "wait_seconds": 0}, headers=AUTH)

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
    case = handover(fail=unavailable)

    async def down(*args: object, **kwargs: object) -> None:
        case.pipeline.release.set()
        raise OSError("timed out")

    monkeypatch.setattr(case.app.state.job_store, "create", down)
    with TestClient(case.app) as client:
        response = client.post("/v1/extract", json={"url": PAGE, "wait_seconds": 0}, headers=AUTH)

        assert response.status_code == 503
        assert response.json()["error"]["message"] == "The backend is down."


def test_unstorable_result_is_logged_and_the_lost_view_takes_over(
    handover: Callable[..., Handover], monkeypatch: pytest.MonkeyPatch
) -> None:
    case = handover()
    store: JobStore = case.app.state.job_store

    async def down(*args: object, **kwargs: object) -> None:
        raise RedisConnectionError("valkey is down")

    monkeypatch.setattr(store, "write_result", down)
    with TestClient(case.app) as client, capture_logs() as logs:
        response = client.post("/v1/extract", json={"url": PAGE, "wait_seconds": 0}, headers=AUTH)
        assert response.status_code == 202
        job_id = response.json()["data"]["id"]
        case.pipeline.release.set()
        case.wait_for_idle()

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
    case = handover()
    with TestClient(case.app) as client:
        response = client.post("/v1/extract", json={"url": PAGE, "wait_seconds": 0}, headers=AUTH)
        job_id = response.json()["data"]["id"]
        assert response.status_code == 202
        assert len(case.app.state.background) >= 1
        # The extraction ends only after the shutdown has begun (the app is closed below).
        case.release_later(0.2)
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
    case = handover()  # never released
    monkeypatch.setattr("text_extraction.app._BACKGROUND_GRACE_SECONDS", 0)
    # `extract_timeout_seconds` cannot be set below 2 s, but the shutdown reads it from here.
    monkeypatch.setattr(case.settings, "extract_timeout_seconds", 0.1)
    with TestClient(case.app) as client:
        job_id = client.post(
            "/v1/extract", json={"url": PAGE, "wait_seconds": 0}, headers=AUTH
        ).json()["data"]["id"]
    assert case.app.state.background == set()
    # The cancelled extraction left a retryable error in its job.

    async def read() -> Any:
        return await JobStore(FakeAsyncRedis(server=case.server), case.settings).items(job_id, 0, 1)

    result = asyncio.run(read())[0].result
    assert result is not None
    assert result.error is not None
    assert (result.error.code, result.error.retryable) == ("internal_error", True)


def test_jobs_disabled_ignores_wait_seconds(handover: Callable[..., Handover]) -> None:
    case = handover(jobs_enabled=False)
    # Released only after the extraction started and the wait of 0.05 s has run out: with jobs
    # enabled the request would have become a job (202) by then.
    case.release_when(case.pipeline.started.is_set, after=0.3)
    with TestClient(case.app) as client:
        response = client.post(
            "/v1/extract", json={"url": PAGE, "wait_seconds": 0.05}, headers=AUTH
        )

        assert response.status_code == 200
        assert response.json()["status"] == "success"
        assert response.json()["data"]["title"]
        assert case.job_keys() == []
        assert case.app.state.background == set()
        assert len(case.pipeline.tasks) == 1  # one extraction, run for this request only


def test_background_cap_keeps_the_request_waiting(handover: Callable[..., Handover]) -> None:
    case = handover(extract_max_background=1)
    with TestClient(case.app) as client:
        first = client.post("/v1/extract", json={"url": PAGE, "wait_seconds": 0}, headers=AUTH)
        assert first.status_code == 202
        assert len(case.app.state.background) == 1  # the pod holds one extraction: the cap
        # Released only once the second request's extraction runs, i.e. after its cap check.
        case.release_when(lambda: len(case.pipeline.tasks) == 2)

        second = client.post("/v1/extract", json={"url": PAGE, "wait_seconds": 0}, headers=AUTH)

        assert second.status_code == 200
        assert second.json()["status"] == "success"
        assert len(case.keys("job:*:meta")) == 1  # only the first became a job
        final = case.wait_for_job(client, first.json()["data"]["id"])
        assert final["data"]["items"][0]["result"]["status"] == "success"


@pytest.mark.parametrize("blocked", ["create", "mark_running"])
def test_cancelled_hand_over_leaves_nothing_behind(
    handover: Callable[..., Handover], monkeypatch: pytest.MonkeyPatch, blocked: str
) -> None:
    case = handover()
    app = case.app
    store: JobStore = app.state.job_store
    reached = asyncio.Event

    async def scenario() -> None:
        entered = reached()

        async def hang(*args: object, **kwargs: object) -> None:
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(store, blocked, hang)
        state = SimpleNamespace(
            settings=app.state.settings,
            pipeline=app.state.pipeline,
            job_store=store,
            background=app.state.background,
            memory_guard=app.state.memory_guard,
        )
        request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=state)))
        principal = Principal(name="alice", role="user", rate_limit_per_minute=None)
        body = ExtractRequest(url=PAGE, wait_seconds=0)
        route = asyncio.create_task(extract(body, request, principal))
        await entered.wait()
        assert len(app.state.background) == 1
        route.cancel()
        with pytest.raises(asyncio.CancelledError):
            await route

        assert app.state.background == set()
        # The extraction nobody waits for is cancelled, and no job is left (mark_running: the
        # created job is deleted).
        await asyncio.wait(case.pipeline.tasks, timeout=5)
        assert case.pipeline.tasks[0].cancelled()
        assert await case.keys_async("job*") == []
        # The shutdown has nothing to wait for.
        await asyncio.wait_for(_wait_for(app.state.background, 30), 1)

    asyncio.run(scenario())


def test_wait_for_ignores_finished_tasks() -> None:
    async def scenario() -> float:
        done = asyncio.create_task(asyncio.sleep(0))
        await done
        stale = {done}  # finished, but never removed: it must not make the wait spin
        started = time.monotonic()
        await asyncio.wait_for(_wait_for(stale, 30), 1)
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 1


def test_wait_for_returns_when_the_running_tasks_end() -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        background = {asyncio.create_task(release.wait())}
        waiting = asyncio.create_task(_wait_for(background, 30))
        await asyncio.sleep(0.01)
        assert not waiting.done()
        release.set()
        await asyncio.wait_for(waiting, 1)

    asyncio.run(scenario())


def test_wait_for_stops_at_its_deadline() -> None:
    async def scenario() -> None:
        hang = asyncio.create_task(asyncio.Event().wait())
        await asyncio.wait_for(_wait_for({hang}, 0.05), 1)
        hang.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hang

    asyncio.run(scenario())


def test_failed_extract_carries_the_bound_trace_id() -> None:
    plain = failed_extract(ErrorCode.TIMEOUT, "Too slow.", retryable=True)
    assert plain.status == "failed"
    assert plain.error is not None
    assert (plain.error.code, plain.error.message, plain.error.retryable) == (
        ErrorCode.TIMEOUT,
        "Too slow.",
        True,
    )
    assert plain.trace_id is None

    with structlog.contextvars.bound_contextvars(trace_id="trace-7"):
        traced = failed_extract(ErrorCode.INTERNAL_ERROR, "Broken.", retryable=False)

    assert traced.trace_id == "trace-7"


def test_hand_over_stores_the_trace_id_of_its_request(handover: Callable[..., Handover]) -> None:
    case = handover()
    with TestClient(case.app) as client:
        response = client.post(
            "/v1/extract",
            json={"url": PAGE, "wait_seconds": 0},
            headers={**AUTH, "x-client-trace-id": "trace-h"},
        )
        assert response.status_code == 202
        job_id = response.json()["data"]["id"]

        record = asyncio.run(
            JobStore(FakeAsyncRedis(server=case.server), case.settings).get(job_id)
        )

        case.pipeline.release.set()
        case.wait_for_job(client, job_id)

    assert record is not None
    assert record.trace_id == "trace-h"
