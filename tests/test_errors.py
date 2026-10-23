from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.errors import ServiceError, install_error_handlers


class Payload(BaseModel):
    url: str
    count: int = Field(ge=1)


def build_app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/service-error")
    def service_error() -> None:
        raise ServiceError(ErrorCode.FORBIDDEN, "not allowed here", 403)

    @app.get("/retryable")
    def retryable() -> None:
        raise ServiceError(
            ErrorCode.RATE_LIMITED,
            "slow down",
            429,
            retryable=True,
            headers={"Retry-After": "7"},
        )

    @app.post("/validate")
    def validate(payload: Payload) -> Payload:
        return payload

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret internal detail")

    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(build_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def test_service_error_envelope(client: TestClient) -> None:
    response = client.get("/service-error")

    assert response.status_code == 403
    body = Envelope[None].model_validate(response.json())
    assert body.status == "failed"
    assert body.data is None
    assert body.trace_id is None
    assert body.error is not None
    assert body.error.code == ErrorCode.FORBIDDEN
    assert body.error.message == "not allowed here"
    assert body.error.retryable is False
    assert body.error.details is None


def test_service_error_headers_and_retryable(client: TestClient) -> None:
    response = client.get("/retryable")

    assert response.status_code == 429
    assert response.headers["retry-after"] == "7"
    assert response.json()["error"]["code"] == "rate_limited"
    assert response.json()["error"]["retryable"] is True


def test_validation_error_envelope_has_field_paths(client: TestClient) -> None:
    response = client.post("/validate", json={"count": 0})

    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["retryable"] is False
    details = body["error"]["details"]
    assert {"loc": ["body", "url"], "msg": "Field required"} in details
    assert any(item["loc"] == ["body", "count"] for item in details)
    assert all(set(item) == {"loc", "msg"} for item in details)


def test_unknown_route_is_not_found(client: TestClient) -> None:
    response = client.get("/does-not-exist")

    assert response.status_code == 404
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "not_found"


def test_wrong_method_is_invalid_request(client: TestClient) -> None:
    response = client.post("/service-error")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "invalid_request"
    assert "GET" in response.headers["allow"]


def test_unhandled_exception_is_generic_500(client: TestClient) -> None:
    response = client.get("/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["retryable"] is False
    assert "secret internal detail" not in response.text
    assert "RuntimeError" not in response.text
