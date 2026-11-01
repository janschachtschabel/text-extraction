import asyncio
import hashlib
from collections.abc import Iterator
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from redis.asyncio import Redis

from text_extraction import __version__
from text_extraction.app import create_public_app
from text_extraction.internal import Readiness, create_internal_app
from text_extraction.server import main
from text_extraction.settings import Settings
from text_extraction.valkey import create_valkey

USER_KEY = "user-key-0123456789-raw-secret"
DOWN_URL = "redis://127.0.0.1:1/0"


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
    )


@pytest.fixture
def registry() -> CollectorRegistry:
    return CollectorRegistry()


@pytest.fixture
def public(registry: CollectorRegistry) -> Iterator[TestClient]:
    app = create_public_app(make_settings(), FakeAsyncRedis(), registry)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def internal(registry: CollectorRegistry) -> Iterator[TestClient]:
    app = create_internal_app(make_settings(), FakeAsyncRedis(), registry, readiness=listening())
    with TestClient(app) as client:
        yield client


def listening() -> Readiness:
    readiness = Readiness()
    readiness.mark_listening()
    return readiness


def internal_client(valkey: Redis, registry: CollectorRegistry) -> TestClient:
    return TestClient(create_internal_app(make_settings(), valkey, registry, readiness=listening()))


def test_info_requires_key(public: TestClient) -> None:
    response = public.get("/v1/info")

    assert response.status_code == 401
    assert response.json()["status"] == "failed"
    assert response.json()["error"]["code"] == "unauthorized"


def test_info_ok_with_user_key(public: TestClient) -> None:
    response = public.get(
        "/v1/info",
        headers={"Authorization": f"Bearer {USER_KEY}", "x-client-trace-id": "trace-42"},
    )

    assert response.status_code == 200
    assert response.json()["trace_id"] == "trace-42"
    assert response.json()["status"] == "success"
    assert response.json()["data"] == {
        "name": "text-extraction",
        "version": __version__,
        "principal": "alice",
        "role": "user",
    }


def test_openapi_has_no_internal_paths(public: TestClient) -> None:
    paths = public.get("/openapi.json").json()["paths"]

    assert "/v1/info" in paths
    for internal_path in ("/health", "/ready", "/metrics"):
        assert internal_path not in paths


def test_openapi_title_and_description(public: TestClient) -> None:
    info = public.get("/openapi.json").json()["info"]

    assert info["title"] == "Text Extraction API"
    assert info["version"] == __version__
    assert "clean text or Markdown" in info["description"]


def _operations(spec: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path, method, operation)
        for path, operations in spec["paths"].items()
        for method, operation in operations.items()
    ]


def test_openapi_declares_the_bearer_key(public: TestClient) -> None:
    spec = public.get("/openapi.json").json()

    assert spec["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    assert spec["security"] == [{"bearerAuth": []}]


def test_openapi_errors_are_the_envelope(public: TestClient) -> None:
    spec = public.get("/openapi.json").json()
    envelope = {"$ref": "#/components/schemas/ErrorEnvelope"}

    assert "HTTPValidationError" not in spec["components"]["schemas"]
    assert spec["components"]["schemas"]["ErrorEnvelope"]["properties"]["status"] == {
        "const": "failed",
        "title": "Status",
    }
    for path, method, operation in _operations(spec):
        responses = operation["responses"]
        for status in ("401", "422", "429", "503"):
            assert responses[status]["content"]["application/json"]["schema"] == envelope, (
                path,
                method,
                status,
            )
        if method == "post":
            assert {"400", "403", "413"} <= set(responses), (path, method)
        if "{job_id}" in path:
            assert "404" in responses, (path, method)
        assert operation["tags"], (path, method)


def test_openapi_without_keys_has_no_security_scheme(registry: CollectorRegistry) -> None:
    app = create_public_app(Settings(auth_mode="none"), FakeAsyncRedis(), registry)

    spec = app.openapi()

    assert "securitySchemes" not in spec["components"]
    assert "security" not in spec
    for path, method, operation in _operations(spec):
        assert "401" not in operation["responses"], (path, method)


def test_metrics_not_on_public_app(public: TestClient) -> None:
    response = public.get("/metrics")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_metrics_on_internal_app_counts_requests(registry: CollectorRegistry) -> None:
    public_app = create_public_app(make_settings(), FakeAsyncRedis(), registry)
    with (
        TestClient(public_app) as public_client,
        internal_client(FakeAsyncRedis(), registry) as internal_app_client,
    ):
        public_client.get("/v1/info", headers={"Authorization": f"Bearer {USER_KEY}"})

        response = internal_app_client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert 'http_requests_total{handler="/v1/info",method="GET",status="2xx"} 1.0' in response.text


def test_internal_app_hides_openapi_and_docs(internal: TestClient) -> None:
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert internal.get(path).status_code == 404


def test_health_and_ready_ok(internal: TestClient) -> None:
    assert internal.get("/health").json() == {"status": "ok"}
    ready = internal.get("/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}


def test_ready_503_when_valkey_down(registry: CollectorRegistry) -> None:
    with internal_client(FakeAsyncRedis(connected=False), registry) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_ready_503_with_real_client_on_closed_port(registry: CollectorRegistry) -> None:
    with internal_client(create_valkey(DOWN_URL), registry) as client:
        assert client.get("/ready").status_code == 503


def test_health_200_when_valkey_down(registry: CollectorRegistry) -> None:
    with internal_client(FakeAsyncRedis(connected=False), registry) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_main_exits_2_on_invalid_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AUTH_MODE", "api_key")
    monkeypatch.delenv("API_KEYS", raising=False)
    monkeypatch.delenv("API_KEYS_FILE", raising=False)

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 2
    assert "AUTH_MODE=api_key requires API_KEYS" in capsys.readouterr().out


def test_info_with_valkey_down_is_503_retryable_envelope(registry: CollectorRegistry) -> None:
    app = create_public_app(make_settings(), FakeAsyncRedis(connected=False), registry)
    with TestClient(app) as client:
        response = client.get(
            "/v1/info",
            headers={"Authorization": f"Bearer {USER_KEY}", "x-client-trace-id": "trace-503"},
        )

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["retryable"] is True
    assert body["trace_id"] == "trace-503"


def test_servers_do_not_send_a_server_header() -> None:
    from fastapi import FastAPI

    from text_extraction.server import _server

    config = _server(FastAPI(), "127.0.0.1", 8080).config

    assert config.server_header is False


@pytest.mark.parametrize(("name", "value"), [("LOG_LEVEL", "verbose"), ("PUBLIC_PORT", "70000")])
def test_main_exits_2_on_invalid_log_level_or_port(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv(name, value)

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 2


def test_servers_wait_30_seconds_for_requests_in_flight() -> None:
    from fastapi import FastAPI

    from text_extraction.server import _server

    config = _server(FastAPI(), "127.0.0.1", 8080).config

    assert config.timeout_graceful_shutdown == 30


def test_graceful_shutdown_outlasts_the_extract_timeout() -> None:
    from text_extraction.server import graceful_shutdown_seconds

    assert graceful_shutdown_seconds(Settings(auth_mode="none")) == 30
    assert graceful_shutdown_seconds(Settings(auth_mode="none", extract_timeout_seconds=60)) == 65
    assert graceful_shutdown_seconds(Settings(auth_mode="none", extract_timeout_seconds=40.2)) == 46


def test_public_app_shuts_its_conversion_pool_down_on_exit(registry: CollectorRegistry) -> None:
    app = create_public_app(make_settings(), FakeAsyncRedis(), registry)
    with TestClient(app):
        pass

    with pytest.raises(RuntimeError, match="shutdown"):
        asyncio.run(app.state.conversion_pool.run(int, "1"))
