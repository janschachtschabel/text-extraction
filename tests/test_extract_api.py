import asyncio
import contextlib
import hashlib
import json
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
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
from text_extraction.browser_dispatch import Handover as BrowserHandover
from text_extraction.browser_options import ArtifactRef, BrowserSecrets, EvaluateAction
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.escalation import BROWSER_UNAVAILABLE_WARNING
from text_extraction.extract_api import (
    _in_browser,
    _store_result,
    default_wait_seconds,
    extract,
    request_wait_seconds,
)
from text_extraction.extract_models import ExtractRequest, ExtractResult, failed_extract
from text_extraction.fetch import Fetcher, FetchResult, Validators
from text_extraction.job_models import JobOptions
from text_extraction.jobs import SCREENSHOT_ARTIFACT, JobRecord, JobStore
from text_extraction.pipeline import Pipeline, create_pipeline
from text_extraction.scheduler import FairQueue
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


def test_ocr_always_while_docling_off_is_400(client: TestClient) -> None:
    response = client.post("/v1/extract", json={"url": PAGE, "ocr": "always"}, headers=AUTH)

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["message"] == "OCR is not enabled on this service."
    # The other options are always fine: a page that is no document ignores them.
    for ocr in ("auto", "never"):
        assert client.post("/v1/extract", json={"url": PAGE, "ocr": ocr}, headers=AUTH).is_success


@pytest.mark.parametrize(("docling", "jobs", "status"), [(True, True, 200), (True, False, 400)])
def test_ocr_always_needs_docling_and_jobs(
    registry: CollectorRegistry, docling: bool, jobs: bool, status: int
) -> None:
    settings = make_settings(docling_enabled=docling, jobs_enabled=jobs)
    app = create_public_app(settings, FakeAsyncRedis(), registry)
    fake = FakeFetcher({PAGE: fetched(body=ARTICLE)})
    app.state.pipeline = create_pipeline(settings, app.state.valkey, cast(Fetcher, fake), registry)
    with TestClient(app) as test_client:
        response = test_client.post(
            "/v1/extract", json={"url": PAGE, "ocr": "always"}, headers=AUTH
        )

    assert response.status_code == status


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
        "screenshot",
        "unblocked",
        "document",
        "media",
        "pii",
        "structured",
        "chunks",
    }
    assert data["structured"] is None
    assert data["chunks"] is None
    assert data["timings"]["llm_ms"] == 0
    assert data["format"] == "text"
    assert data["mode_used"] == "fast"
    assert data["upstream_status"] == 200
    assert set(data["timings"]) == {
        "total_ms",
        "compliance_ms",
        "fetch_ms",
        "convert_ms",
        "render_ms",
        "pii_ms",
        "llm_ms",
    }
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


def test_openapi_pii_takes_a_bare_mode_or_the_object(client: TestClient) -> None:
    """The final review's I2: ``"pii": "mask"`` (the README's form) is valid by the published
    schema, not only by the validator: generated clients and validating gateways read it."""
    spec = client.get("/openapi.json").json()
    body = spec["paths"]["/v1/extract"]["post"]["requestBody"]["content"]["application/json"]
    name = body["schema"]["$ref"].rsplit("/", 1)[1]
    pii = spec["components"]["schemas"][name]["properties"]["pii"]

    assert pii["anyOf"] == [
        {"type": "string", "enum": ["mask", "redact", "hash", "annotate"]},
        {"$ref": "#/components/schemas/PiiOptions"},
        {"type": "null"},
    ]
    assert "PiiOptions" in spec["components"]["schemas"]


def test_openapi_pii_description_names_every_field_it_applies_to(client: TestClient) -> None:
    """The final review's M11: N5 handles the uploaded file's name too."""
    spec = client.get("/openapi.json").json()
    body = spec["paths"]["/v1/extract"]["post"]["requestBody"]["content"]["application/json"]
    name = body["schema"]["$ref"].rsplit("/", 1)[1]
    description = spec["components"]["schemas"][name]["properties"]["pii"]["description"]

    for field in ("content", "title", "metadata", "links", "media uploader", "document.file_name"):
        assert field in description, field


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
            browser_dispatcher=None,
            docling_dispatcher=None,
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


# --- browser dispatch (package 4) -------------------------------------------------------------
#
# The API pod never renders: it queues the page on pool `browser` and waits for the browser
# worker's result. `FakeBrowserWorker` stands in for that worker: a thread with its own event loop
# on the same fakeredis server that claims from `browser`, notes what it saw and writes a result.

ADMIN_KEY = "admin-key-0123456789-raw-secret"
ADMIN_AUTH = {"Authorization": f"Bearer {ADMIN_KEY}"}
SHELL_WITH_WORDS = (
    "<html><head><title>Learning app</title></head><body><div id='root'></div>"
    "<p>The learning app about photosynthesis loads here in a moment, please wait for it.</p>"
    "<script src='/app.js'></script></body></html>"
)
CHALLENGE_PAGE = """<!doctype html><html><head><title>Just a moment...</title></head>
<body><p>Checking your browser before accessing the site.</p></body></html>"""
# Markers, not credentials: the tests look for them where they must never show up.
HEADER_SECRET = "header-secret-4711"  # noqa: S105
COOKIE_SECRET = "cookie-secret-4711"  # noqa: S105
ACTION_SECRET = "typed-secret-4711"  # noqa: S105
SECRET_BODY: dict[str, Any] = {
    "headers": {"Authorization": f"Bearer {HEADER_SECRET}"},
    "cookies": [{"name": "session", "value": COOKIE_SECRET}],
    "actions": [{"type": "type", "selector": "#q", "text": ACTION_SECRET}],
}


def browser_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "auth_mode": "api_key",
        "api_keys": [
            {
                "name": "alice",
                "role": "user",
                "sha256": hashlib.sha256(USER_KEY.encode()).hexdigest(),
            },
            {
                "name": "root",
                "role": "admin",
                "sha256": hashlib.sha256(ADMIN_KEY.encode()).hexdigest(),
            },
        ],
        "outbound_min_delay_ms": 0,
        "browser_enabled": True,
        "browser_queue_wait_seconds": 5,
    }
    values.update(overrides)
    return Settings(**values)


def rendered_envelope(url: str = PAGE, *, words: int = 400) -> Envelope[ExtractResult]:
    """What the browser worker would store for a page it rendered well."""
    return Envelope[ExtractResult].model_validate(
        {
            "status": "success",
            "data": {
                "url": url,
                "final_url": url,
                "upstream_status": 200,
                "content_type": "text/html",
                "mode_used": "browser",
                "attempts": 1,
                "language": "en",
                "word_count": words,
                "title": "Rendered",
                "format": "markdown",
                "content": "Rendered text.",
                "quality": None,
                "timings": {
                    "total_ms": 7,
                    "compliance_ms": 1,
                    "fetch_ms": 0,
                    "convert_ms": 2,
                    "render_ms": 4,
                },
                "compliance": {"mode": "enforce"},
                "metadata": None,
                "links": None,
                "fingerprint": None,
                "cache": "miss",
                "warnings": [],
            },
        }
    )


@dataclass
class Seen:
    """One item the fake browser worker took: its job and what the job carried for it."""

    job: JobRecord
    client: str
    secrets: BrowserSecrets
    draft: Envelope[ExtractResult] | None
    llm_key: str | None = None


class FakeBrowserWorker:
    """Claims items of pool ``browser`` and writes ``respond(seen)`` as their result. While
    ``hold`` is clear it claims but does not write yet."""

    def __init__(
        self,
        server: FakeServer,
        settings: Settings,
        respond: Callable[[Seen], Envelope[ExtractResult]] | None = None,
        *,
        artifact: bytes | None = None,
    ) -> None:
        self.server = server
        self.settings = settings
        self.respond = respond or (lambda seen: rendered_envelope())
        self.artifact = artifact
        self.seen: list[Seen] = []
        self.hold = threading.Event()
        self.hold.set()
        self.claimed = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True)

    def __enter__(self) -> "FakeBrowserWorker":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.hold.set()
        self._stop.set()
        self._thread.join(10)

    async def _run(self) -> None:
        valkey = FakeAsyncRedis(server=self.server)
        store = JobStore(valkey, self.settings)
        queue = FairQueue(valkey, self.settings, "browser")
        while not self._stop.is_set():
            claim = await queue.claim(60)
            if claim.item is None:
                await asyncio.sleep(0.01)
                continue
            item = claim.item
            job = await store.get(item.job_id)
            if job is None:  # abandoned by the API: dropped like the real worker does
                await queue.complete(item)
                continue
            if job.state == "queued":
                await store.mark_running(item.job_id)
            seen = Seen(
                job,
                item.client,
                await store.secrets(job.id),
                await store.draft(job.id, 0),
                await store.llm_key(job.id),
            )
            self.seen.append(seen)
            self.claimed.set()
            while not self.hold.is_set():  # noqa: ASYNC110 - set by another thread
                await asyncio.sleep(0.01)
            envelope = self.respond(seen)
            if self.artifact is not None:
                expires = await store.put_artifact(job.id, SCREENSHOT_ARTIFACT, self.artifact)
                assert envelope.data is not None
                envelope.data.screenshot = ArtifactRef(
                    name=SCREENSHOT_ARTIFACT,
                    url=f"/v1/jobs/{job.id}/artifacts/{SCREENSHOT_ARTIFACT}",
                    content_type="image/png",
                    bytes=len(self.artifact),
                    width=1920,
                    height=1080,
                    expires_at=expires,
                )
            await store.drop_secrets(job.id)
            await store.write_result(job.id, item.index, envelope)
            await queue.complete(item)


class BrowserCase:
    """A public app with browser mode on, on a fakeredis server, with a fake fetcher."""

    def __init__(self, answers: dict[str, Any] | None = None, **overrides: Any) -> None:
        self.settings = browser_settings(**overrides)
        self.server = FakeServer()
        self.registry = CollectorRegistry()
        valkey = FakeAsyncRedis(server=self.server)
        self.app = create_public_app(self.settings, valkey, self.registry)
        self.fetcher = FakeFetcher(answers or {PAGE: fetched(body=ARTICLE)})
        self.real = create_pipeline(
            self.settings, valkey, cast(Fetcher, self.fetcher), self.registry
        )
        self.app.state.pipeline = self.real

    def worker(self, **kwargs: Any) -> FakeBrowserWorker:
        return FakeBrowserWorker(self.server, self.settings, **kwargs)

    def keys(self, pattern: str) -> list[str]:
        async def find() -> list[str]:
            valkey = FakeAsyncRedis(server=self.server)
            return sorted(
                key.decode() if isinstance(key, bytes) else key
                for key in await valkey.keys(pattern)
            )

        return asyncio.run(find())

    def ttl(self, key: str) -> int:
        async def find() -> int:
            return int(await FakeAsyncRedis(server=self.server).ttl(key))

        return asyncio.run(find())

    def record(self, job_id: str) -> JobRecord | None:
        return asyncio.run(JobStore(FakeAsyncRedis(server=self.server), self.settings).get(job_id))

    def wait_for_job(self, client: TestClient, job_id: str, auth: dict[str, str] = AUTH) -> Any:
        deadline = time.monotonic() + 30
        while True:
            body = client.get(f"/v1/jobs/{job_id}", headers=auth).json()
            if body["data"]["state"] == "completed" or time.monotonic() > deadline:
                return body
            time.sleep(0.01)

    def wait_for_idle(self) -> None:
        deadline = time.monotonic() + 30
        while self.app.state.background and time.monotonic() < deadline:
            time.sleep(0.01)


@pytest.mark.parametrize(
    "options",
    [
        {"mode": "browser"},
        {"screenshot": True},
        {"actions": [{"type": "scroll"}]},
        {"headers": {"x-token": "t"}},
    ],
)
def test_browser_mode_disabled_is_400(options: dict[str, Any]) -> None:
    case = BrowserCase(browser_enabled=False)
    with TestClient(case.app) as client:
        response = client.post("/v1/extract", json={"url": PAGE, **options}, headers=AUTH)

        assert response.status_code == 400
        body = response.json()
        assert body["status"] == "failed"
        assert body["error"]["code"] == "invalid_request"
        assert body["error"]["message"] == "Browser mode is not enabled on this service."
        assert case.keys("job*") == []
        assert case.fetcher.calls == []


def test_evaluate_by_user_is_403() -> None:
    case = BrowserCase()
    evaluate = {"actions": [{"type": "evaluate", "script": "document.title = 'x'"}]}
    with TestClient(case.app) as client, case.worker() as worker:
        response = client.post(
            "/v1/extract", json={"url": PAGE, "mode": "browser", **evaluate}, headers=AUTH
        )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"
        assert case.keys("job*") == []

        allowed = client.post(
            "/v1/extract", json={"url": PAGE, "mode": "browser", **evaluate}, headers=ADMIN_AUTH
        )
        assert allowed.status_code == 200
        assert allowed.json()["data"]["mode_used"] == "browser"
        assert worker.seen[0].job.owner_role == "admin"
        assert isinstance(worker.seen[0].secrets.actions[0], EvaluateAction)


def test_browser_mode_returns_the_rendered_result() -> None:
    case = BrowserCase()
    with TestClient(case.app) as client, case.worker() as worker:
        response = client.post(
            "/v1/extract",
            json={"url": PAGE, "mode": "browser", "format": "text"},
            headers={**AUTH, "x-client-trace-id": "trace-b"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "success"
        assert body["trace_id"] == "trace-b"
        assert body["data"]["mode_used"] == "browser"
        assert body["data"]["timings"]["total_ms"] >= 7
        assert case.fetcher.calls == []  # the API pod fetched nothing itself
        seen = worker.seen[0]
        assert (seen.job.kind, seen.job.owner, seen.client) == ("extract", "alice", "alice#live")
        assert (seen.job.options.mode, seen.job.options.format) == ("browser", "text")
        assert seen.job.trace_id == "trace-b"
        assert seen.draft is None


def test_auto_escalates_and_returns_browser_result() -> None:
    case = BrowserCase({PAGE: fetched(body=SHELL_WITH_WORDS)})
    with TestClient(case.app) as client, case.worker() as worker:
        response = client.post("/v1/extract", json={"url": PAGE}, headers=AUTH)

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "success"
        assert body["data"]["mode_used"] == "browser"
        assert BROWSER_UNAVAILABLE_WARNING not in body["data"]["warnings"]
        seen = worker.seen[0]
        assert seen.job.options.mode == "auto"
        assert seen.draft is not None  # the fast result, for the worker to compare (B5)
        assert seen.draft.status == "partial"
        assert seen.draft.data is not None
        assert seen.draft.data.mode_used == "fast"
        assert len(case.fetcher.page_calls()) == 1


@pytest.mark.parametrize("stored", ["hit", "revalidated"])
def test_a_stored_fast_winner_is_answered_without_a_browser_job(stored: str) -> None:
    async def answer(validators: Validators | None) -> FetchResult:
        if validators is not None and validators.etag == '"v1"':
            return fetched(body=b"", status=304, headers={"etag": '"v1"'})
        return fetched(body=SHELL_WITH_WORDS, headers={"etag": '"v1"'})

    ttl = 3600 if stored == "hit" else 0
    case = BrowserCase({PAGE: answer}, result_cache_ttl_seconds=ttl)
    # The entry the browser worker stores when the fast result wins (B5): mode_used fast,
    # browser_recommended true. A pipeline with browser mode off stores the same one.
    seeding = create_pipeline(
        browser_settings(browser_enabled=False, result_cache_ttl_seconds=ttl),
        FakeAsyncRedis(server=case.server),
        cast(Fetcher, case.fetcher),
        CollectorRegistry(),
    )
    seed = asyncio.run(seeding.run(ExtractRequest(url=PAGE), USER))
    assert seed.data is not None
    assert seed.data.quality is not None
    assert seed.data.quality.browser_recommended
    with TestClient(case.app) as client, case.worker() as worker:
        response = client.post("/v1/extract", json={"url": PAGE}, headers=AUTH)

        body = response.json()
        assert body["status"] == "partial"
        assert body["data"]["cache"] == stored
        assert body["data"]["mode_used"] == "fast"
        assert BROWSER_UNAVAILABLE_WARNING not in body["data"]["warnings"]
        assert worker.seen == []
        assert case.keys("job*") == []


def test_auto_with_browser_options_renders_without_a_fast_phase() -> None:
    case = BrowserCase()
    with TestClient(case.app) as client, case.worker() as worker:
        response = client.post(
            "/v1/extract", json={"url": PAGE, "headers": {"x-token": "t"}}, headers=AUTH
        )

        assert response.json()["data"]["mode_used"] == "browser"
        assert case.fetcher.calls == []  # R8: always rendered, like a screenshot
        assert worker.seen[0].draft is None
        assert worker.seen[0].secrets.headers == {"x-token": "t"}


def test_auto_without_worker_returns_fast_with_browser_unavailable() -> None:
    # Review Focus 4: no browser worker runs.
    case = BrowserCase({PAGE: fetched(body=SHELL_WITH_WORDS)}, browser_queue_wait_seconds=0.3)
    with TestClient(case.app) as client:
        started = time.monotonic()
        response = client.post("/v1/extract", json={"url": PAGE}, headers=AUTH)
        took = time.monotonic() - started

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "partial"  # the fast result's own status
        data = body["data"]
        assert data["mode_used"] == "fast"
        assert data["warnings"] == [BROWSER_UNAVAILABLE_WARNING]
        # Never compared or stored, so not shown: the browser may answer the next request.
        assert data["fingerprint"] is None
        assert data["content"]
        assert took >= 0.3
        assert case.keys("job*") == []  # the job is gone
        assert case.keys("fp:*") == []
        assert case.keys("res:*") == []


@pytest.mark.parametrize(
    "options", [{"mode": "browser"}, {"cookies": [{"name": "a", "value": "b"}]}]
)
def test_browser_mode_without_worker_is_timeout(options: dict[str, Any]) -> None:
    case = BrowserCase(browser_queue_wait_seconds=0.2)
    with TestClient(case.app) as client:
        response = client.post("/v1/extract", json={"url": PAGE, **options}, headers=AUTH)

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert body["error"]["code"] == "timeout"
        assert body["error"]["retryable"] is True
        assert body["data"]["mode_used"] == "browser"
        assert body["data"]["url"] == PAGE
        assert case.keys("job*") == []


def test_wait_seconds_during_browser_phase_answers_202_with_browser_job() -> None:
    case = BrowserCase()
    with TestClient(case.app) as client, case.worker() as worker, capture_logs() as logs:
        worker.hold.clear()
        response = client.post(
            "/v1/extract", json={"url": PAGE, "mode": "browser", "wait_seconds": 0.3}, headers=AUTH
        )

        assert response.status_code == 202
        accepted = response.json()["data"]
        assert "browser_job_accepted" in [event["event"] for event in logs]
        assert worker.claimed.wait(10)
        assert accepted["id"] == worker.seen[0].job.id  # the dispatched job, no P1 job
        assert (accepted["kind"], accepted["total"]) == ("extract", 1)
        assert accepted["state"] in {"queued", "running"}
        assert len(case.keys("job:*:meta")) == 1

        worker.hold.set()
        final = case.wait_for_job(client, accepted["id"])
        assert final["status"] == "success"
        assert final["data"]["items"][0]["result"]["data"]["mode_used"] == "browser"
        case.wait_for_idle()
        assert case.app.state.background == set()
        assert sample(cast(Handover, case), "jobs_created_total") == 1


@pytest.mark.parametrize(
    ("body", "answers"),
    [
        ({"mode": "browser"}, None),
        ({"cookies": [{"name": "s", "value": "v"}]}, None),  # secrets
        ({}, {PAGE: fetched(body=SHELL_WITH_WORDS)}),  # escalated, with its draft
        ({"mode": "browser", "wait_seconds": 30}, None),  # answered within wait_seconds
    ],
    ids=["browser", "secrets", "escalated", "within-wait"],
)
def test_a_sync_browser_answer_leaves_no_job_behind(
    body: dict[str, Any], answers: dict[str, Any] | None
) -> None:
    # I-3: nobody can read the job of an answer given to the caller; it must not stay 24 h.
    case = BrowserCase(answers)
    with TestClient(case.app) as client, case.worker() as worker:
        response = client.post("/v1/extract", json={"url": PAGE, **body}, headers=AUTH)

        assert response.status_code == 200
        assert response.json()["data"]["mode_used"] == "browser"
        assert len(worker.seen) == 1
        assert case.keys("job*") == []


def test_a_sync_screenshot_job_lives_only_as_long_as_its_artifact() -> None:
    case = BrowserCase(artifact_ttl_seconds=900)
    with TestClient(case.app) as client, case.worker(artifact=b"\x89PNG") as worker:
        response = client.post("/v1/extract", json={"url": PAGE, "screenshot": True}, headers=AUTH)

        assert response.status_code == 200
        job_id = worker.seen[0].job.id
        assert 0 < case.ttl(f"job:{{{job_id}}}:meta") <= 900
        assert case.keys(f"job:{{{job_id}}}:drafts") == []
        url = response.json()["data"]["screenshot"]["url"]
        assert client.get(url, headers=AUTH).status_code == 200


def test_a_202_browser_job_keeps_the_job_ttl() -> None:
    case = BrowserCase(job_ttl_seconds=86400, artifact_ttl_seconds=900)
    with TestClient(case.app) as client, case.worker() as worker:
        worker.hold.clear()
        response = client.post(
            "/v1/extract", json={"url": PAGE, "mode": "browser", "wait_seconds": 0}, headers=AUTH
        )
        assert response.status_code == 202
        job_id = response.json()["data"]["id"]
        worker.hold.set()
        final = case.wait_for_job(client, job_id)
        assert final["status"] == "success"
        case.wait_for_idle()

        assert case.ttl(f"job:{{{job_id}}}:meta") > 86000


def test_a_result_read_while_the_route_hands_over_keeps_the_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The flow reads the result just as wait_seconds runs out: the route answers 202 with the
    # job, so the flow must not delete it after all.
    case = BrowserCase()
    store: JobStore = case.app.state.job_store
    real_items = store.items

    async def slow_items(job_id: str, offset: int, limit: int) -> Any:
        found = await real_items(job_id, offset, limit)
        await asyncio.sleep(0.6)  # the route detaches meanwhile
        return found

    monkeypatch.setattr(store, "items", slow_items)
    with TestClient(case.app) as client, case.worker():
        response = client.post(
            "/v1/extract", json={"url": PAGE, "mode": "browser", "wait_seconds": 0.3}, headers=AUTH
        )
        assert response.status_code == 202
        job_id = response.json()["data"]["id"]
        case.wait_for_idle()

        assert case.record(job_id) is not None
        final = case.wait_for_job(client, job_id)
        assert final["data"]["items"][0]["result"]["data"]["mode_used"] == "browser"


def test_wait_seconds_zero_for_a_browser_request_answers_202() -> None:
    case = BrowserCase()
    with TestClient(case.app) as client, case.worker() as worker:
        worker.hold.clear()
        response = client.post(
            "/v1/extract", json={"url": PAGE, "mode": "browser", "wait_seconds": 0}, headers=AUTH
        )

        assert response.status_code == 202
        assert worker.claimed.wait(10)
        assert response.json()["data"]["id"] == worker.seen[0].job.id
        assert len(case.keys("job:*:meta")) == 1
        worker.hold.set()
        final = case.wait_for_job(client, response.json()["data"]["id"])
        assert final["data"]["items"][0]["result"]["status"] == "success"


def test_valkey_error_after_the_202_never_deletes_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    # The poll in flight when wait_seconds runs out fails: the job was handed to the caller
    # (202), so the flow must leave it to the worker instead of deleting it.
    case = BrowserCase()
    store: JobStore = case.app.state.job_store
    real_get = store.get
    calls = 0

    async def get_failing_once(job_id: str) -> JobRecord | None:
        nonlocal calls
        calls += 1
        if calls == 2:  # the dispatcher's second poll, still running at the 202
            await asyncio.sleep(0.6)
            raise RedisConnectionError("valkey blinked")
        return await real_get(job_id)

    monkeypatch.setattr(store, "get", get_failing_once)
    with TestClient(case.app) as client, case.worker() as worker:
        worker.hold.clear()
        response = client.post(
            "/v1/extract", json={"url": PAGE, "mode": "browser", "wait_seconds": 0.3}, headers=AUTH
        )
        assert response.status_code == 202
        job_id = response.json()["data"]["id"]
        case.wait_for_idle()  # the flow has seen the error and ended

        assert case.record(job_id) is not None
        worker.hold.set()
        final = case.wait_for_job(client, job_id)
        assert final["status"] == "success"
        assert final["data"]["items"][0]["result"]["data"]["mode_used"] == "browser"


def test_abandon_in_progress_answers_the_fallback_not_a_202(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The flow gave up and is deleting its job when wait_seconds runs out: no 202 with that job.
    case = BrowserCase(browser_queue_wait_seconds=0.1)
    store: JobStore = case.app.state.job_store
    real_delete = store.delete

    async def slow_delete(job_id: str) -> None:
        await asyncio.sleep(0.6)
        await real_delete(job_id)

    monkeypatch.setattr(store, "delete", slow_delete)
    with TestClient(case.app) as client:
        response = client.post(
            "/v1/extract", json={"url": PAGE, "mode": "browser", "wait_seconds": 0.4}, headers=AUTH
        )

        assert response.status_code == 200
        body = response.json()
        assert (body["status"], body["error"]["code"]) == ("failed", "timeout")
        assert case.keys("job*") == []


def test_handover_during_fast_phase_then_escalation_writes_into_p1_job() -> None:
    case = BrowserCase({PAGE: fetched(body=SHELL_WITH_WORDS)})
    gated = GatedPipeline(case.real)
    case.app.state.pipeline = gated
    with TestClient(case.app) as client, case.worker() as worker:
        worker.hold.clear()
        response = client.post("/v1/extract", json={"url": PAGE, "wait_seconds": 0}, headers=AUTH)
        assert response.status_code == 202
        job_id = response.json()["data"]["id"]
        record = case.record(job_id)
        assert record is not None
        assert record.lease_until is not None  # the P1 lease of the fast phase

        gated.release.set()  # the fast result needs the browser: it escalates into this job
        assert worker.claimed.wait(10)
        seen = worker.seen[0]
        assert seen.job.id == job_id
        assert seen.draft is not None
        assert seen.draft.status == "partial"
        assert seen.job.lease_until is None  # cleared: the browser has its own time

        case.wait_for_idle()  # the extraction stored nothing: the worker writes item 0
        view = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()["data"]
        assert (view["state"], view["items"][0]["state"]) == ("running", "pending")

        worker.hold.set()
        final = case.wait_for_job(client, job_id)
        assert final["status"] == "success"
        assert final["data"]["items"][0]["result"]["data"]["mode_used"] == "browser"
        assert len(case.keys("job:*:meta")) == 1


def test_handover_then_escalation_without_worker_keeps_the_job_waiting() -> None:
    case = BrowserCase({PAGE: fetched(body=SHELL_WITH_WORDS)})
    gated = GatedPipeline(case.real)
    case.app.state.pipeline = gated
    with TestClient(case.app) as client:
        response = client.post("/v1/extract", json={"url": PAGE, "wait_seconds": 0}, headers=AUTH)
        job_id = response.json()["data"]["id"]
        gated.release.set()
        case.wait_for_idle()

        async def lease_is_cleared() -> None:  # so it can never run out (P1's lost view)
            valkey = FakeAsyncRedis(server=case.server)
            assert await valkey.hget(f"job:{{{job_id}}}:meta", "lease_until") is None

        asyncio.run(lease_is_cleared())
        view = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
        # The lost view does not apply: the item waits in the browser queue for a worker.
        assert view["data"]["state"] == "running"
        assert view["data"]["items"][0]["state"] == "pending"


@pytest.mark.parametrize(
    ("auth", "compliance", "unblock", "escalates"),
    [
        (ADMIN_AUTH, "ignore", True, True),
        (ADMIN_AUTH, "ignore", False, False),
        (ADMIN_AUTH, "enforce", True, False),
        (AUTH, "enforce", True, False),
    ],
)
def test_bot_wall_escalates_only_for_admin_ignore_with_unblock(
    auth: dict[str, str], compliance: str, unblock: bool, escalates: bool
) -> None:
    # Review Focus 5: a 403 challenge answer goes to the browser only for unblock mode.
    case = BrowserCase(
        {PAGE: fetched(body=CHALLENGE_PAGE, status=403)}, browser_unblock_enabled=unblock
    )
    with TestClient(case.app) as client, case.worker() as worker:
        response = client.post(
            "/v1/extract", json={"url": PAGE, "compliance": compliance}, headers=auth
        )

        data = response.json()["data"]
        if escalates:
            assert data["mode_used"] == "browser"
            assert worker.seen[0].job.options.compliance == "ignore"
            assert worker.seen[0].job.owner_role == "admin"
        else:
            assert response.json()["error"]["code"] == "target_http_error"
            assert data["mode_used"] == "fast"
            assert BROWSER_UNAVAILABLE_WARNING not in data["warnings"]
            assert worker.seen == []
            assert case.keys("job*") == []


def test_secrets_never_in_job_view_or_logs() -> None:
    # Review Focus 5: headers, cookies and action text reach the worker and nothing else.
    case = BrowserCase()
    with TestClient(case.app) as client, case.worker() as worker, capture_logs() as logs:
        worker.hold.clear()
        response = client.post(
            "/v1/extract",
            json={"url": PAGE, "mode": "browser", "wait_seconds": 0, **SECRET_BODY},
            headers=AUTH,
        )
        assert response.status_code == 202
        job_id = response.json()["data"]["id"]
        assert worker.claimed.wait(10)
        running = client.get(f"/v1/jobs/{job_id}", headers=AUTH).text
        worker.hold.set()
        case.wait_for_job(client, job_id)
        done = client.get(f"/v1/jobs/{job_id}", headers=AUTH).text

        secrets = worker.seen[0].secrets
        assert secrets.headers == {"authorization": f"Bearer {HEADER_SECRET}"}
        assert secrets.cookies[0].value == COOKIE_SECRET
        assert any(log["event"] == "browser_job_queued" for log in logs)  # logs were captured
        logged = json.dumps(logs, default=str)
        for text in (response.text, running, done, logged):
            for secret in (HEADER_SECRET, COOKIE_SECRET, ACTION_SECRET):
                assert secret not in text
        assert "secret-token" not in logged  # nor the URL's query (the job view shows the URL)
        assert case.keys("job:*:secrets") == []  # the worker dropped them


def test_fast_mode_and_auto_with_browser_disabled_unchanged() -> None:
    for case, body in [
        (BrowserCase({PAGE: fetched(body=SHELL_WITH_WORDS)}, browser_enabled=False), {}),
        (BrowserCase({PAGE: fetched(body=SHELL_WITH_WORDS)}), {"mode": "fast"}),
    ]:
        with TestClient(case.app) as client, case.worker() as worker:
            response = client.post("/v1/extract", json={"url": PAGE, **body}, headers=AUTH)

            assert response.status_code == 200
            data = response.json()["data"]
            assert response.json()["status"] == "partial"
            assert data["mode_used"] == "fast"
            assert data["warnings"] == []
            assert data["fingerprint"] is not None
            assert worker.seen == []
            assert case.keys("job*") == []
            assert case.keys("q:*") == []


class AbandoningDispatcher:
    """A dispatcher whose wait gives up (None) or fails, and whose abandon takes a while."""

    def __init__(self, wait_error: Exception | None) -> None:
        self.wait_error = wait_error
        self.abandoning = asyncio.Event()
        self.abandoned = False

    async def submit(self, *args: Any, **kwargs: Any) -> str:
        return "f" * 32

    async def wait(self, *args: Any, **kwargs: Any) -> Envelope[ExtractResult] | None:
        if self.wait_error is not None:
            raise self.wait_error
        return None

    async def abandon(self, job_id: str) -> None:
        self.abandoning.set()
        await asyncio.sleep(0.2)
        self.abandoned = True


@pytest.mark.parametrize(
    "wait_error", [None, RedisConnectionError("valkey blinked")], ids=["gave-up", "valkey-error"]
)
def test_a_cancelled_request_still_abandons_its_job(wait_error: Exception | None) -> None:
    # M-8: a client that disconnects while the job is being deleted must not cut the DEL short,
    # or a worker renders and stores a job nobody waits for.
    dispatcher = AbandoningDispatcher(wait_error)
    state = SimpleNamespace(browser_dispatcher=dispatcher, settings=browser_settings())

    async def scenario() -> None:
        flow = asyncio.create_task(
            _in_browser(
                state,
                ExtractRequest(url=PAGE, mode="browser"),
                USER,
                "enforce",
                BrowserHandover(),
                "extract",
                None,
                time.perf_counter(),
            )
        )
        await asyncio.wait_for(dispatcher.abandoning.wait(), 5)
        flow.cancel()
        with pytest.raises(asyncio.CancelledError):
            await flow
        await asyncio.sleep(0.4)

    asyncio.run(scenario())

    assert dispatcher.abandoned


def test_a_cookie_chromium_would_refuse_is_422_without_its_value() -> None:
    case = BrowserCase()
    with TestClient(case.app) as client:
        response = client.post(
            "/v1/extract",
            json={"url": PAGE, "cookies": [{"name": "sid", "value": f"{COOKIE_SECRET};x"}]},
            headers=AUTH,
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"
        assert COOKIE_SECRET not in response.text
        assert case.keys("job*") == []


def test_the_api_exports_its_sync_dispatch_outcomes() -> None:
    case = BrowserCase({PAGE: fetched(body=SHELL_WITH_WORDS)}, browser_queue_wait_seconds=0.3)
    with TestClient(case.app) as client:
        client.post("/v1/extract", json={"url": PAGE}, headers=AUTH)  # no worker: fast answer
        with case.worker():
            client.post("/v1/extract", json={"url": PAGE, "mode": "browser"}, headers=AUTH)

        def outcome(name: str) -> float | None:
            return case.registry.get_sample_value(
                "text_extraction_browser_dispatch_total", {"outcome": name}
            )

        assert outcome("not_claimed") == 1
        assert outcome("rendered") == 1
        assert outcome("no_result") == 0  # exported from the start


# --- PII: the request checks and answers without pii (N4, N11) ----------------------------------

GOLDEN = Path(__file__).parent / "documents" / "golden" / "package4_answers.json"
PII_DISABLED_MESSAGE = "PII handling is not enabled on this service."


@pytest.mark.parametrize(
    ("pii_enabled", "jobs_enabled"), [(False, True), (False, False)], ids=["pii-off", "jobs-off"]
)
def test_pii_disabled_is_400(
    registry: CollectorRegistry, pii_enabled: bool, jobs_enabled: bool
) -> None:
    settings = make_settings(pii_enabled=pii_enabled, jobs_enabled=jobs_enabled)
    app = create_public_app(settings, FakeAsyncRedis(), registry)
    fake = FakeFetcher({PAGE: fetched(body=ARTICLE)})
    app.state.pipeline = create_pipeline(settings, app.state.valkey, cast(Fetcher, fake), registry)
    with TestClient(app) as test_client:
        for pii in ("mask", {"mode": "annotate", "entities": ["PERSON"]}):
            response = test_client.post("/v1/extract", json={"url": PAGE, "pii": pii}, headers=AUTH)

            assert response.status_code == 400
            body = response.json()
            assert body["error"]["code"] == "invalid_request"
            assert body["error"]["message"] == PII_DISABLED_MESSAGE
    assert fake.calls == []  # refused before anything was fetched


def test_pii_with_screenshot_is_422() -> None:
    case = BrowserCase(pii_enabled=True)
    with TestClient(case.app) as client:
        response = client.post(
            "/v1/extract", json={"url": PAGE, "screenshot": True, "pii": "mask"}, headers=AUTH
        )

        assert response.status_code == 422
        body = response.json()
        assert body["error"]["code"] == "invalid_request"
        assert body["error"]["message"] == "A screenshot cannot be masked; ask for it without pii."
        assert case.keys("job*") == []
        assert case.fetcher.calls == []


def test_annotate_with_html_is_422() -> None:
    case = BrowserCase(pii_enabled=True)
    with TestClient(case.app) as client:
        response = client.post(
            "/v1/extract",
            json={"url": PAGE, "format": "html", "pii": {"mode": "annotate"}},
            headers=AUTH,
        )

        assert response.status_code == 422
        body = response.json()
        assert body["error"]["code"] == "invalid_request"
        assert body["error"]["message"] == "annotate needs format markdown or text."
        # The replacing modes take HTML; annotate takes the other formats.
        for allowed in ({"format": "html", "pii": "mask"}, {"format": "text", "pii": "annotate"}):
            assert (
                client.post(
                    "/v1/extract", json={"url": PAGE, "wait_seconds": 0, **allowed}, headers=AUTH
                ).status_code
                == 202  # no nlp worker here: a job at once
            )
        assert case.fetcher.calls  # those were taken


@pytest.mark.parametrize("pii_enabled", [False, True], ids=["pii-off", "pii-on"])
def test_request_without_pii_unchanged(registry: CollectorRegistry, pii_enabled: bool) -> None:
    """The answer of a request without ``pii`` is the package-6 one (the golden recording),
    whether the service has PII or not: the result only gained ``pii: null``, ``structured: null``,
    ``chunks: null``, ``timings.pii_ms: 0`` and ``timings.llm_ms: 0``, and no job or nlp item is
    made."""
    golden = json.loads(GOLDEN.read_text())["article"]
    settings = make_settings(pii_enabled=pii_enabled)
    valkey = FakeAsyncRedis()
    app = create_public_app(settings, valkey, registry)
    fake = FakeFetcher({PAGE: fetched()})
    app.state.pipeline = create_pipeline(settings, valkey, cast(Fetcher, fake), registry)
    with TestClient(app) as test_client:
        response = test_client.post("/v1/extract", json={"url": PAGE, "links": True}, headers=AUTH)

    assert response.status_code == 200
    answer = response.json()
    timings = answer["data"].pop("timings")
    assert timings["pii_ms"] == 0
    assert timings["llm_ms"] == 0
    assert answer.pop("trace_id")
    assert answer["data"]["pii"] is None
    assert answer["data"]["structured"] is None
    assert answer["data"]["chunks"] is None
    assert json.dumps(answer, sort_keys=True) == json.dumps(golden, sort_keys=True)

    async def keys(pattern: str) -> list[Any]:
        return list(await valkey.keys(pattern))

    assert asyncio.run(keys("job*")) == []
    assert asyncio.run(keys("q:{nlp}*")) == []


XPATH_STRUCTURED: dict[str, Any] = {
    "schema": {"type": "object", "properties": {"title": {"type": "string"}}},
    "strategy": "selectors",
    "selectors": {"title": {"xpath": "//h1"}},
}


def test_xpath_selectors_need_an_admin_key(client: TestClient) -> None:
    """Ruling R4: a user's XPath is 422 before anything is fetched; CSS is fine."""
    response = client.post(
        "/v1/extract", json={"url": PAGE, "structured": XPATH_STRUCTURED}, headers=AUTH
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["message"] == "XPath selectors need an admin key; use CSS."
    assert "//h1" not in response.text

    css = {**XPATH_STRUCTURED, "selectors": {"title": {"css": "h1"}}}
    allowed = client.post("/v1/extract", json={"url": PAGE, "structured": css}, headers=AUTH)
    assert allowed.status_code == 200
    assert allowed.json()["data"]["structured"]["values"] == {"title": "Photosynthesis explained"}


# --- structured and chunking: the request checks and the caller's LLM key (rulings S12, S9) -----

LLM_KEY = "sk-llm-caller-key-9c2b"
LLM_KEY_HEADERS = {**AUTH, "X-LLM-Api-Key": LLM_KEY}
SCHEMA_ONE: dict[str, Any] = {"type": "object", "properties": {"title": {"type": "string"}}}
LLM_STRUCTURED: dict[str, Any] = {"schema": SCHEMA_ONE, "strategy": "llm"}
ROUTES = ["extract", "file", "batch"]
LLM_DISABLED = "LLM features are not enabled on this service."
LLM_KEY_MISSING = "No LLM key: send X-LLM-Api-Key."


def without_llm_hop(app: Any) -> None:
    """A pod without the hop to the llm workers: LLM work is answered ``llm_failed`` at once,
    for the tests whose point is elsewhere (the hop is ``tests/test_llm_dispatch.py``'s)."""
    app.state.llm_dispatcher = None


def llm_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "llm_enabled": True,
        "llm_api_key": "sk-operator-key",
        "llm_embedding_model": "embed-1",
    }
    values.update(overrides)
    return make_settings(**values)


class Service:
    """The public app on a fakeredis server with a fake fetcher, for the three routes that take
    ``structured`` and ``chunking``."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or make_settings()
        self.server = FakeServer()
        valkey = FakeAsyncRedis(server=self.server)
        registry = CollectorRegistry()
        self.app = create_public_app(self.settings, valkey, registry)
        self.fetcher = FakeFetcher({PAGE: fetched(body=ARTICLE)})
        self.app.state.pipeline = create_pipeline(
            self.settings, valkey, cast(Fetcher, self.fetcher), registry
        )

    def keys(self, pattern: str = "job*") -> list[str]:
        async def scan() -> list[str]:
            valkey = FakeAsyncRedis(server=self.server)
            return sorted(
                key.decode() if isinstance(key, bytes) else key
                for key in await valkey.keys(pattern)
            )

        return asyncio.run(scan())


def post_route(
    client: TestClient,
    route: str,
    *,
    structured: dict[str, Any] | None = None,
    chunking: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    **options: Any,
) -> Any:
    """The same request on ``POST /v1/extract``, ``/v1/extract/file`` or ``/v1/batch``."""
    given = {
        name: value
        for name, value in (("structured", structured), ("chunking", chunking))
        if value is not None
    }
    headers = AUTH if headers is None else headers
    if route == "extract":
        return client.post("/v1/extract", json={"url": PAGE, **given, **options}, headers=headers)
    if route == "batch":  # a batch is a job anyway: it takes no wait_seconds
        options.pop("wait_seconds", None)
        return client.post("/v1/batch", json={"urls": [PAGE], **given, **options}, headers=headers)
    return client.post(
        "/v1/extract/file",
        files={"file": ("page.html", ARTICLE, "text/html")},
        data={
            name: value if isinstance(value, str) else json.dumps(value)
            for name, value in {**given, **options}.items()
        },
        headers=headers,
    )


def refusal(response: Any, status: int, message: str) -> None:
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["message"] == message


OPTION_CASES = {
    "llm": {"structured": LLM_STRUCTURED},
    "learn": {"structured": {"schema": SCHEMA_ONE, "learn_selectors": True}},
    "semantic": {"chunking": {"strategy": "semantic"}},
}


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("case", list(OPTION_CASES))
def test_llm_features_400_without_llm(route: str, case: str) -> None:
    service = Service()
    with TestClient(service.app) as client:
        response = post_route(client, route, headers=LLM_KEY_HEADERS, **OPTION_CASES[case])

    refusal(response, 400, LLM_DISABLED)
    assert service.fetcher.calls == []  # refused before anything was fetched
    assert service.keys() == []  # and never a job


@pytest.mark.parametrize("route", ROUTES)
def test_semantic_400_without_embedding_model(route: str) -> None:
    service = Service(llm_settings(llm_embedding_model=None))
    with TestClient(service.app) as client:
        response = post_route(client, route, chunking={"strategy": "semantic"})

    refusal(response, 400, "Semantic chunking needs LLM_EMBEDDING_MODEL.")
    assert service.keys() == []


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("strategy", ["llm", "auto"])
def test_llm_work_400_without_any_key(route: str, strategy: str) -> None:
    service = Service(llm_settings(llm_api_key=None))
    with TestClient(service.app) as client:
        response = post_route(client, route, structured={**LLM_STRUCTURED, "strategy": strategy})

    refusal(response, 400, LLM_KEY_MISSING)
    assert service.fetcher.calls == []
    assert service.keys() == []


@pytest.mark.parametrize(("route", "status"), [("extract", 200), ("file", 200), ("batch", 202)])
def test_header_key_accepted_without_env_key(route: str, status: int) -> None:
    service = Service(llm_settings(llm_api_key=None))
    without_llm_hop(service.app)  # the answer comes at once, without an llm worker
    with TestClient(service.app) as client:
        response = post_route(client, route, headers=LLM_KEY_HEADERS, structured=LLM_STRUCTURED)

    assert response.status_code == status, response.text
    assert LLM_KEY not in response.text


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("value", ["has space", "x" * 1025, "tab\there"])
def test_bad_llm_key_header_400(route: str, value: str) -> None:
    service = Service(llm_settings())
    with TestClient(service.app) as client:
        response = post_route(
            client, route, headers={**AUTH, "X-LLM-Api-Key": value}, structured=LLM_STRUCTURED
        )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_request"
    assert "X-LLM-Api-Key" in response.json()["error"]["message"]
    assert value not in response.text
    assert service.keys() == []


@pytest.mark.parametrize("route", ROUTES)
def test_chunking_html_422(route: str) -> None:
    service = Service(llm_settings())
    with TestClient(service.app) as client:
        response = post_route(client, route, chunking={"strategy": "fixed"}, format="html")

    refusal(response, 422, "Chunking needs format markdown or text.")
    assert service.fetcher.calls == []
    assert service.keys() == []


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    ("structured", "what"),
    [
        ({"schema": SCHEMA_ONE, "strategy": "metadata"}, "Strategy metadata"),
        ({"schema": SCHEMA_ONE, "strategy": "selectors"}, "Strategy selectors"),
        ({"tables": True}, "tables"),
        ({"schema": SCHEMA_ONE, "strategy": "llm", "learn_selectors": True}, "learn_selectors"),
        (
            {"schema": SCHEMA_ONE, "strategy": "auto", "selectors": {"title": {"css": "h1"}}},
            "selectors",
        ),
    ],
    ids=["metadata", "selectors", "tables", "learn", "caller-selectors"],
)
def test_pii_with_dom_strategies_422(route: str, structured: dict[str, Any], what: str) -> None:
    service = Service(llm_settings(pii_enabled=True))
    with TestClient(service.app) as client:
        response = post_route(client, route, structured=structured, pii="mask")

    refusal(response, 422, f"{what} reads the raw page and cannot be combined with pii.")
    assert service.fetcher.calls == []
    assert service.keys() == []


@pytest.mark.parametrize("route", ROUTES)
def test_pii_with_patterns_llm_and_chunking_accepted(route: str) -> None:
    service = Service(llm_settings(pii_enabled=True))
    patterns = {
        "schema": SCHEMA_ONE,
        "strategy": "patterns",
        "patterns": {"title": {"regex": r"\w+"}},
    }
    with TestClient(service.app) as client:
        for options in (
            {"structured": patterns},
            {"structured": LLM_STRUCTURED},
            {"structured": {"schema": SCHEMA_ONE}},  # auto
            {"chunking": {"strategy": "markdown"}},
        ):
            # No nlp worker here: a request that goes there is a job at once.
            response = post_route(client, route, pii="mask", wait_seconds=0, **options)
            assert response.status_code in (200, 202), (options, response.text)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("strategy", ["metadata", "selectors", "patterns"])
def test_learn_needs_llm_or_auto_422(route: str, strategy: str) -> None:
    service = Service(llm_settings())
    structured = {"schema": SCHEMA_ONE, "strategy": strategy, "learn_selectors": True}
    with TestClient(service.app) as client:
        response = post_route(client, route, headers=LLM_KEY_HEADERS, structured=structured)

    refusal(response, 422, "learn_selectors needs strategy llm or auto.")
    assert service.keys() == []


def test_requests_without_structured_and_chunking_need_no_llm() -> None:
    service = Service()
    with TestClient(service.app) as client:
        for route in ROUTES:
            assert post_route(client, route).status_code in (200, 202)


@pytest.mark.parametrize("path", ["/v1/extract", "/v1/extract/file", "/v1/batch"])
def test_the_llm_key_header_is_documented(path: str) -> None:
    service = Service(llm_settings())
    with TestClient(service.app) as client:
        spec = client.get("/openapi.json").json()

    parameters = {p["name"]: p for p in spec["paths"][path]["post"]["parameters"]}
    assert "X-LLM-Api-Key" in parameters
    assert parameters["X-LLM-Api-Key"]["in"] == "header"


def wait_body(**changes: Any) -> ExtractRequest:
    return ExtractRequest.model_validate({"url": PAGE, **changes})


def test_default_wait_is_largest_of_transcribe_pii_llm() -> None:
    settings = llm_settings(pii_enabled=True, pii_sync_wait_seconds=40, llm_sync_wait_seconds=50)
    llm = {"structured": LLM_STRUCTURED}

    assert request_wait_seconds(wait_body(), settings) is None
    assert request_wait_seconds(wait_body(transcribe=True), settings) == 20
    assert request_wait_seconds(wait_body(pii="mask"), settings) == 40
    assert request_wait_seconds(wait_body(**llm), settings) == 50
    assert request_wait_seconds(wait_body(pii="mask", **llm), settings) == 50
    assert request_wait_seconds(wait_body(transcribe=True, pii="mask"), settings) == 40
    assert request_wait_seconds(wait_body(transcribe=True, pii="mask", **llm), settings) == 50
    assert request_wait_seconds(wait_body(transcribe=True, **llm), settings) == 50
    assert request_wait_seconds(wait_body(chunking={"strategy": "semantic"}), settings) == 50
    # What does not call the LLM does not wait for it.
    assert request_wait_seconds(wait_body(structured={"tables": True}), settings) is None
    assert request_wait_seconds(wait_body(chunking={"strategy": "fixed"}), settings) is None
    selectors = {"schema": SCHEMA_ONE, "strategy": "selectors"}
    assert request_wait_seconds(wait_body(structured=selectors), settings) is None
    # An explicit wait_seconds always wins, 0 too.
    assert request_wait_seconds(wait_body(wait_seconds=5, pii="mask", **llm), settings) == 5
    assert request_wait_seconds(wait_body(wait_seconds=0, **llm), settings) == 0
    # The default of the LLM is LLM_SYNC_WAIT_SECONDS (30), and the transcription's is 20.
    plain = llm_settings(pii_enabled=True)
    assert request_wait_seconds(wait_body(transcribe=True, **llm), plain) == 30
    assert request_wait_seconds(wait_body(pii="mask"), plain) == 20
    # No LLM on the service: auto has no LLM step to wait for.
    assert (
        request_wait_seconds(wait_body(structured={"schema": SCHEMA_ONE}), make_settings()) is None
    )


def test_default_wait_of_an_upload_is_the_largest_of_pii_and_llm() -> None:
    settings = llm_settings(pii_enabled=True, pii_sync_wait_seconds=40, llm_sync_wait_seconds=50)
    assert default_wait_seconds(settings, pii=False, llm=False) is None
    assert default_wait_seconds(settings, pii=True, llm=False) == 40
    assert default_wait_seconds(settings, pii=False, llm=True) == 50
    assert default_wait_seconds(settings, pii=True, llm=True) == 50
    assert default_wait_seconds(settings, transcribe=True, pii=False, llm=False) == 20


def test_an_llm_request_without_wait_seconds_waits_then_becomes_a_job() -> None:
    """The default wait applies: a request that may need the LLM and whose extraction is slower
    than LLM_SYNC_WAIT_SECONDS answers 202 with its job."""
    case = Handover(llm_enabled=True, llm_api_key="sk-operator-key", llm_sync_wait_seconds=1)
    without_llm_hop(case.app)  # the P1 job is the pod's to write
    case.release_later(4)  # a failing test must not hang
    with TestClient(case.app) as client:
        started = time.monotonic()
        response = client.post(
            "/v1/extract", json={"url": PAGE, "structured": LLM_STRUCTURED}, headers=AUTH
        )
        waited = time.monotonic() - started

        assert response.status_code == 202, response.text
        assert 0.9 <= waited < 5
        case.pipeline.release.set()
        case.wait_for_job(client, response.json()["data"]["id"])


def test_the_llm_key_is_stored_with_the_p1_job() -> None:
    case = Handover(llm_enabled=True, llm_api_key="sk-operator-key")
    without_llm_hop(case.app)  # the P1 job is the pod's to write
    with TestClient(case.app) as client, capture_logs() as logs:
        response = client.post(
            "/v1/extract",
            json={"url": PAGE, "structured": LLM_STRUCTURED, "wait_seconds": 0},
            headers=LLM_KEY_HEADERS,
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["data"]["id"]
        view = client.get(f"/v1/jobs/{job_id}", headers=AUTH)
        stored = asyncio.run(
            JobStore(FakeAsyncRedis(server=case.server), case.settings).llm_key(job_id)
        )
        case.pipeline.release.set()
        case.wait_for_job(client, job_id)
        case.wait_for_idle()
        # The pod wrote the job's result itself: no llm worker will read the key any more.
        left = case.keys("job*llm_key")

    assert stored == LLM_KEY
    assert left == []
    assert LLM_KEY not in view.text
    assert not any(LLM_KEY in repr(event) for event in logs)


def test_no_llm_key_is_stored_for_a_request_without_llm_work() -> None:
    case = Handover(llm_enabled=True, llm_api_key="sk-operator-key")
    with TestClient(case.app) as client:
        plain = client.post(
            "/v1/extract", json={"url": PAGE, "wait_seconds": 0}, headers=LLM_KEY_HEADERS
        )
        patterns = client.post(
            "/v1/extract",
            json={
                "url": PAGE,
                "wait_seconds": 0,
                "structured": {
                    "schema": SCHEMA_ONE,
                    "strategy": "patterns",
                    "patterns": {"title": {"regex": r"\w+"}},
                },
            },
            headers=LLM_KEY_HEADERS,
        )
        assert plain.status_code == patterns.status_code == 202
        keys = case.keys("job*llm_key")
        case.pipeline.release.set()
        case.wait_for_idle()

    assert keys == []


@pytest.mark.parametrize(
    "options", [{"mode": "browser"}, {"mode": "auto"}], ids=["browser", "auto"]
)
def test_a_browser_job_keeps_the_callers_llm_key(options: dict[str, Any]) -> None:
    """The browser job of a request that may need the LLM carries the caller's key, so that
    the llm worker it moves on to can read it; a request without LLM work stores none."""
    case = BrowserCase(
        {PAGE: fetched(body=SHELL_WITH_WORDS)}, llm_enabled=True, llm_api_key="sk-operator-key"
    )
    without_llm_hop(case.app)  # the browser job is the point here, not the llm job after it
    with TestClient(case.app) as client, case.worker() as worker, capture_logs() as logs:
        llm = client.post(
            "/v1/extract",
            json={"url": PAGE, "structured": LLM_STRUCTURED, **options},
            headers=LLM_KEY_HEADERS,
        )
        plain = client.post("/v1/extract", json={"url": PAGE, **options}, headers=LLM_KEY_HEADERS)

        assert llm.status_code == plain.status_code == 200
        assert [seen.llm_key for seen in worker.seen] == [LLM_KEY, None]
        assert LLM_KEY not in llm.text
    assert not any(LLM_KEY in repr(event) for event in logs)


# --- the post stage at every finish point (rulings S5, S13) ---------------------------------------

PATTERNS_STRUCTURED: dict[str, Any] = {
    "schema": {"type": "object", "properties": {"plant": {"type": "string"}}},
    "strategy": "patterns",
    "patterns": {"plant": {"regex": r"green (\w+)", "group": 1}},
}


def assert_chunked(data: dict[str, Any], size: int) -> list[dict[str, Any]]:
    """The chunks of ``data`` cover its content in windows of at most ``size`` characters."""
    chunks = data["chunks"]
    assert isinstance(chunks, list)
    assert chunks
    for chunk in chunks:
        assert len(chunk["text"]) <= size
        assert data["content"][chunk["start"] : chunk["end"]] == chunk["text"]
    return cast(list[dict[str, Any]], chunks)


@pytest.mark.parametrize("route", ["extract", "file"])
def test_sync_chunking_without_llm_runs_inline(route: str) -> None:
    """Chunking and patterns need no LLM: the API pod runs the post stage itself, after the
    DOM stage, and the answer is done (``stage: "done"``, the chunks there)."""
    service = Service(llm_settings())
    with TestClient(service.app) as client:
        response = post_route(
            client,
            route,
            structured=PATTERNS_STRUCTURED,
            chunking={"strategy": "fixed", "size": 300, "overlap": 0},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "success", body["error"]
    data = body["data"]
    assert_chunked(data, 300)
    structured = data["structured"]
    assert structured["stage"] == "done"
    assert structured["values"] == {"plant": "plants"}
    assert structured["sources"] == {"plant": "patterns"}
    assert structured["missing"] == []
    assert "learn_html" not in structured or structured["learn_html"] is None
    assert data["timings"]["llm_ms"] == 0
    assert service.keys() == []  # no job: nothing went to another pool


def test_sync_llm_work_without_the_llm_hop_is_partial_with_content() -> None:
    """A pod without the llm hop (no ``llm_dispatcher``) gives a result that needs the LLM the
    post stage without it: ``partial``/``llm_failed``, retryable, the content kept. With the hop
    the llm worker answers (``tests/test_llm_dispatch.py``)."""
    service = Service(llm_settings())
    without_llm_hop(service.app)
    with TestClient(service.app) as client:
        response = post_route(
            client, "extract", structured={"schema": SCHEMA_ONE, "strategy": "llm"}
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "partial"
    assert body["error"]["code"] == "llm_failed"
    assert body["error"]["retryable"] is True
    assert body["error"]["message"] == "The LLM step did not run; retry later."
    assert body["data"]["content"]
    assert body["data"]["structured"]["stage"] == "done"
    assert body["data"]["structured"]["missing"] == ["title"]


def test_cache_hit_gets_fresh_post_stage() -> None:
    """The cache keeps the DOM stage's result, never the post stage's: a second request with
    other ``chunking`` hits the same entry and gets its own chunks."""
    service = Service(llm_settings())
    with TestClient(service.app) as client:
        first = post_route(client, "extract", chunking={"strategy": "fixed", "size": 1000})
        second = post_route(
            client, "extract", chunking={"strategy": "fixed", "size": 200, "overlap": 0}
        )
        plain = post_route(client, "extract")

    one, two = first.json()["data"], second.json()["data"]
    assert (one["cache"], two["cache"], plain.json()["data"]["cache"]) == ("miss", "hit", "hit")
    assert len(service.fetcher.page_calls()) == 1  # one fetch of the page behind all three
    big, small = assert_chunked(one, 1000), assert_chunked(two, 200)
    assert len(small) > len(big)
    assert plain.json()["data"]["chunks"] is None  # the entry has no chunks of its own


def test_p1_job_result_has_post_stage() -> None:
    """A request that became a P1 job stores its result after the post stage."""
    case = Handover(**llm_settings().model_dump(include={"llm_enabled", "llm_api_key"}))
    with TestClient(case.app) as client:
        response = client.post(
            "/v1/extract",
            json={
                "url": PAGE,
                "wait_seconds": 0,
                "structured": PATTERNS_STRUCTURED,
                "chunking": {"strategy": "sentence", "size": 400, "overlap": 0},
            },
            headers=AUTH,
        )
        assert response.status_code == 202, response.text
        case.pipeline.release.set()
        final = case.wait_for_job(client, response.json()["data"]["id"])
        case.wait_for_idle()

    result = final["data"]["items"][0]["result"]
    assert result["status"] == "success"
    assert_chunked(result["data"], 400)
    assert result["data"]["structured"]["stage"] == "done"
    assert result["data"]["structured"]["values"] == {"plant": "plants"}


def test_stored_p1_result_gets_the_post_stage_it_lacks() -> None:
    """``_store_result`` is a finish point: an envelope whose post stage has not run gets it
    there, before the PII guard, and is stored done."""
    settings = llm_settings()
    store = JobStore(FakeAsyncRedis(), settings)
    req = ExtractRequest.model_validate(
        {"url": PAGE, "structured": PATTERNS_STRUCTURED, "chunking": {"strategy": "fixed"}}
    )
    envelope = rendered_envelope()
    assert envelope.data is not None
    envelope.data.content = "The green plants grow."
    metrics = cast(Any, SimpleNamespace(duration=_NoMetric()))

    async def scenario_() -> Envelope[ExtractResult] | None:
        job_id = await store.create("extract", USER, JobOptions(compliance="enforce"), [PAGE])
        await store.mark_running(job_id, lease_seconds=60)
        await _store_result(
            store,
            metrics,
            job_id,
            time.monotonic(),
            envelope,
            structured=req.structured,
            chunking=req.chunking,
            settings=settings,
        )
        return (await store.items(job_id, 0, 1))[0].result

    stored = asyncio.run(scenario_())

    assert stored is not None
    assert stored.data is not None
    assert stored.data.structured is not None
    assert stored.data.structured.stage == "done"
    assert stored.data.structured.values == {"plant": "plants"}
    assert stored.data.chunks is not None
    assert [chunk.text for chunk in stored.data.chunks] == ["The green plants grow."]


class _NoMetric:
    def labels(self, **labels: str) -> "_NoMetric":
        return self

    def observe(self, value: float) -> None:
        pass


@pytest.mark.parametrize("pii_enabled", [False, True], ids=["pii-off", "pii-on"])
def test_request_without_structured_unchanged(
    registry: CollectorRegistry, pii_enabled: bool
) -> None:
    """On a service with an LLM, the answer of a request without ``structured`` and
    ``chunking`` is the package-7 one (the golden recording) apart from ``structured: null``,
    ``chunks: null`` and ``timings.llm_ms: 0``: the post stage leaves it alone, and no job or
    llm item is made."""
    golden = json.loads(GOLDEN.read_text())["article"]
    settings = llm_settings(pii_enabled=pii_enabled)
    valkey = FakeAsyncRedis()
    app = create_public_app(settings, valkey, registry)
    fake = FakeFetcher({PAGE: fetched()})
    app.state.pipeline = create_pipeline(settings, valkey, cast(Fetcher, fake), registry)
    with TestClient(app) as test_client:
        response = test_client.post(
            "/v1/extract", json={"url": PAGE, "links": True}, headers=LLM_KEY_HEADERS
        )

    assert response.status_code == 200
    answer = response.json()
    timings = answer["data"].pop("timings")
    assert timings["llm_ms"] == 0
    assert answer.pop("trace_id")
    assert answer["data"]["structured"] is None
    assert answer["data"]["chunks"] is None
    assert json.dumps(answer, sort_keys=True) == json.dumps(golden, sort_keys=True)

    async def keys(pattern: str) -> list[Any]:
        return list(await valkey.keys(pattern))

    assert asyncio.run(keys("job*")) == []
    assert asyncio.run(keys("q:*")) == []


def test_sync_patterns_after_pii_on_masked_content() -> None:
    """With ``pii`` the post stage reads the masked content (D3 a): the nlp worker's result
    comes back with values and chunks of the masked text, never the raw one."""
    from tests.test_pii_dispatch import EMAIL, NAME, PiiService, assert_processed

    service = PiiService()
    structured = {
        "schema": {"type": "object", "properties": {"contact": {"type": "string"}}},
        "strategy": "patterns",
        "patterns": {"contact": {"regex": r"write to (\S+)", "group": 1}},
    }
    with TestClient(service.app) as client, service.nlp():
        response = client.post(
            "/v1/extract",
            json={
                "url": PAGE,
                "pii": "mask",
                "structured": structured,
                "chunking": {"strategy": "recursive", "size": 300, "overlap": 0},
            },
            headers=AUTH,
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "success", body["error"]
    data = body["data"]
    assert_processed(data)
    assert data["structured"]["stage"] == "done"
    assert data["structured"]["values"] == {"contact": "[EMAIL_ADDRESS]"}
    texts = [chunk["text"] for chunk in assert_chunked(data, 300)]
    assert "[PERSON]" in "".join(texts)
    assert not any(NAME in text or EMAIL in text for text in texts)


# --- pii with structured: no DOM-stage value (rulings R12, R13; D3 a) ----------------------------

AUTHOR_SCHEMA: dict[str, Any] = {"type": "object", "properties": {"author": {"type": "string"}}}


def author_page() -> str:
    """The PII test page with a JSON-LD author: the ``metadata`` strategy reads it raw."""
    from tests.test_pii_dispatch import NAME, NAMED_ARTICLE

    ld = f'<script type="application/ld+json">{{"@type": "Article", "author": "{NAME}"}}</script>'
    return NAMED_ARTICLE.replace("</head>", f"{ld}</head>")


def pii_author_service() -> Any:
    from tests.test_pii_dispatch import PiiService

    return PiiService({PAGE: fetched(body=author_page())})


def assert_no_author(body: dict[str, Any]) -> None:
    from tests.test_pii_dispatch import NAME

    assert body["status"] == "success", body["error"]
    assert NAME not in json.dumps(body)
    assert "Schmidt" not in json.dumps(body)
    structured = body["data"]["structured"]
    assert structured["stage"] == "done"
    assert structured["values"] == {"author": None}  # neither metadata nor the masked text
    assert structured["sources"] == {}


def test_auto_with_pii_answers_no_dom_value() -> None:
    """``auto`` with ``pii`` runs ``patterns`` and ``llm`` only: the JSON-LD author never
    reaches the answer, though the page states it."""
    service = pii_author_service()
    with TestClient(service.app) as client, service.nlp():
        plain = client.post(
            "/v1/extract",
            json={"url": PAGE, "structured": {"schema": AUTHOR_SCHEMA}, "cache": False},
            headers=AUTH,
        )
        masked = client.post(
            "/v1/extract",
            json={"url": PAGE, "pii": "mask", "structured": {"schema": AUTHOR_SCHEMA}},
            headers=AUTH,
        )

    from tests.test_pii_dispatch import NAME

    assert plain.json()["data"]["structured"]["values"] == {"author": NAME}  # the page has it
    assert masked.json()["data"]["cache"] == "miss"
    assert_no_author(masked.json())


def test_auto_with_pii_after_a_cached_auto_request_answers_no_dom_value() -> None:
    """A non-``pii`` ``auto`` request caches its DOM-stage result; a ``pii`` request for the
    same page does not get it (another key) and answers no DOM value."""
    service = pii_author_service()
    with TestClient(service.app) as client, service.nlp():
        first = client.post(
            "/v1/extract", json={"url": PAGE, "structured": {"schema": AUTHOR_SCHEMA}}, headers=AUTH
        )
        masked = client.post(
            "/v1/extract",
            json={"url": PAGE, "pii": "mask", "structured": {"schema": AUTHOR_SCHEMA}},
            headers=AUTH,
        )

    assert first.json()["data"]["structured"]["sources"] == {"author": "metadata"}
    assert_no_author(masked.json())


def test_a_cache_hit_of_a_pii_request_drops_dom_values() -> None:
    """Defence in depth (R12 b): a cache entry that holds a DOM-stage result under the key of
    a ``pii`` request (planted here) is served without its ``structured``."""
    from text_extraction.result_cache import ResultCache

    service = pii_author_service()
    with TestClient(service.app) as client, service.nlp():
        client.post("/v1/extract", json={"url": PAGE}, headers=AUTH)

        async def plant() -> None:
            valkey = FakeAsyncRedis(server=service.server)
            cache = ResultCache(valkey, service.settings)
            keys = [key async for key in valkey.scan_iter("res:*")]
            assert len(keys) == 1
            key = keys[0].decode()
            found = await cache.get(key)
            assert found is not None
            entry, _fresh = found
            entry.result["structured"] = {
                "values": {"author": "Anna Schmidt"},
                "sources": {"author": "metadata"},
                "stage": "dom",
            }
            await cache.put(key, entry)

        asyncio.run(plant())
        masked = client.post(
            "/v1/extract",
            json={"url": PAGE, "pii": "mask", "structured": {"schema": AUTHOR_SCHEMA}},
            headers=AUTH,
        )

    assert masked.json()["data"]["cache"] == "hit"
    assert_no_author(masked.json())
