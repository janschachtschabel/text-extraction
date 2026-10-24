import asyncio
import hashlib
from collections.abc import Iterator
from typing import cast

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from tests.fakes import ARTICLE, PAGE, FakeFetcher, fetched
from text_extraction.app import create_public_app
from text_extraction.fetch import Fetcher
from text_extraction.pipeline import create_pipeline
from text_extraction.settings import Settings

USER_KEY = "user-key-0123456789-raw-secret"
AUTH = {"Authorization": f"Bearer {USER_KEY}"}


def make_settings() -> Settings:
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
