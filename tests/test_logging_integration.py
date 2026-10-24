"""The real public app with real JSON logging: the lines an operator would see."""

import hashlib
import json

import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from text_extraction.app import create_public_app
from text_extraction.logging import configure_logging
from text_extraction.settings import Settings

USER_KEY = "user-key-0123456789-raw-secret"


def make_app() -> FastAPI:
    settings = Settings(
        auth_mode="api_key",
        api_keys=[
            {  # type: ignore[list-item]
                "name": "alice",
                "role": "user",
                "sha256": hashlib.sha256(USER_KEY.encode()).hexdigest(),
            }
        ],
    )
    return create_public_app(settings, FakeAsyncRedis(), CollectorRegistry())


def json_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]


def test_access_line_is_json_with_trace_path_status_principal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO", "json")

    with TestClient(make_app()) as client:
        response = client.get(
            "/v1/info?secret=in-query",
            headers={"Authorization": f"Bearer {USER_KEY}", "x-client-trace-id": "trace-42"},
        )
    assert response.status_code == 200

    # The test client logs its own request line (with the query string) under "httpx2".
    lines = [line for line in json_lines(capsys) if line["logger"] != "httpx2"]
    (access,) = [line for line in lines if line["event"] == "request"]
    assert access["trace_id"] == "trace-42"
    assert access["path"] == "/v1/info"
    assert access["status"] == 200
    assert access["principal"] == "alice"
    assert access["method"] == "GET"
    assert "in-query" not in json.dumps(lines)
    assert USER_KEY not in json.dumps(lines)


def test_unhandled_error_traceback_has_no_locals(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    app = make_app()

    @app.get("/boom")
    async def boom() -> None:
        db_password = "p4ssw0rd-local"  # noqa: F841, S105 - a local that must not be logged
        raise RuntimeError("kaboom")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom", headers={"x-client-trace-id": "trace-500"})
    assert response.status_code == 500

    output = capsys.readouterr().out
    assert "p4ssw0rd-local" not in output
    lines = [json.loads(line) for line in output.splitlines() if line]
    (error,) = [line for line in lines if line["event"] == "unhandled_exception"]
    assert error["trace_id"] == "trace-500"
    assert '"locals"' not in output
    assert error["exception"][0]["exc_type"] == "RuntimeError"
