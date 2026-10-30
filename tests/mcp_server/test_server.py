"""The MCP endpoint at /mcp: the gate (origin, key), the call guard (rate limit, logs, metrics,
errors as results) and the service_info tool (rulings P10-S2, P10-S3, P10-S4, P10-S22)."""

import hashlib
import json
import logging
import subprocess
import sys
from collections.abc import Iterator
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from pydantic import BaseModel, ConfigDict
from starlette.datastructures import State

from tests.mcp_server.rpc import ACCEPT, call_tool, post, rpc
from text_extraction.app import create_public_app
from text_extraction.auth import Principal
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.logging import configure_logging
from text_extraction.mcp_server.calls import ModelTool, register
from text_extraction.mcp_server.results import envelope_result, error_result
from text_extraction.mcp_server.server import mcp_body_limit
from text_extraction.settings import Settings

USER_KEY = "user-key-0123456789-raw-secret"
AUTH = {"Authorization": f"Bearer {USER_KEY}"}


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "auth_mode": "api_key",
        "api_keys": [
            {
                "name": "alice",
                "role": "user",
                "sha256": hashlib.sha256(USER_KEY.encode()).hexdigest(),
            }
        ],
        "mcp_enabled": True,
        **overrides,
    }
    return Settings(**values)


def running(
    settings: Settings | None = None, registry: CollectorRegistry | None = None
) -> TestClient:
    app = create_public_app(
        settings or make_settings(), FakeAsyncRedis(), registry or CollectorRegistry()
    )
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with running() as client:
        yield client


def envelope_of(result: dict[str, Any]) -> dict[str, Any]:
    """The envelope of a tool result; its text content repeats it as JSON."""
    structured: dict[str, Any] = result["structuredContent"]
    assert json.loads(result["content"][-1]["text"]) == structured
    return structured


def test_disabled_has_no_mcp_route_and_no_import() -> None:
    with running(make_settings(mcp_enabled=False)) as client:
        assert post(client, "tools/list", headers=AUTH).status_code == 404

    script = (
        "import sys\n"
        "from fakeredis import FakeAsyncRedis\n"
        "from prometheus_client import CollectorRegistry\n"
        "from text_extraction.app import create_public_app\n"
        "from text_extraction.settings import Settings\n"
        "create_public_app(Settings(auth_mode='none'), FakeAsyncRedis(), CollectorRegistry())\n"
        "print(sorted(name for name in sys.modules\n"
        "             if name.split('.')[0] in ('fastmcp', 'mcp', 'mcp_types')))\n"
    )
    found = subprocess.run(  # noqa: S603 - our own interpreter and script
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert found.stdout.strip() == "[]"


def test_initialize_and_list_tools(client: TestClient) -> None:
    answer = rpc(
        client,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
        headers=AUTH,
    )
    assert answer["result"]["serverInfo"]["name"] == "text-extraction"
    assert "get_job" in answer["result"]["instructions"]

    tools = rpc(client, "tools/list", headers=AUTH)["result"]["tools"]
    by_name = {tool["name"]: tool for tool in tools}
    assert "service_info" in by_name
    assert by_name["service_info"]["annotations"]["readOnlyHint"] is True
    assert "outputSchema" not in by_name["service_info"]


def test_service_info_call(client: TestClient) -> None:
    result = call_tool(client, "service_info", headers=AUTH)

    assert result["isError"] is False
    envelope = envelope_of(result)
    assert envelope["status"] == "success"
    assert envelope["data"]["principal"] == "alice"
    assert envelope["data"]["role"] == "user"
    assert envelope["data"]["name"] == "text-extraction"


def test_not_in_openapi(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert not [path for path in paths if path.startswith("/mcp")]


def test_exact_path_no_redirect(client: TestClient) -> None:
    response = post(client, "tools/list", headers=AUTH, path="/mcp/")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_get_mcp_405(client: TestClient) -> None:
    response = client.get("/mcp", headers={**AUTH, "Accept": ACCEPT})

    assert response.status_code == 405


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-key"}])
def test_missing_or_wrong_key_401_envelope(client: TestClient, headers: dict[str, str]) -> None:
    for method, params in (
        ("tools/list", None),
        ("tools/call", {"name": "service_info", "arguments": {}}),
    ):
        response = post(client, method, params, headers=headers)

        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert response.json()["status"] == "failed"
        assert response.json()["error"]["code"] == "unauthorized"
        assert response.json()["error"]["message"] == "A valid API key is required."


def test_auth_none_is_anonymous_admin() -> None:
    with running(make_settings(auth_mode="none", api_keys=[])) as client:
        envelope = envelope_of(call_tool(client, "service_info"))

    assert envelope["data"]["principal"] == "anonymous"
    assert envelope["data"]["role"] == "admin"


@pytest.mark.parametrize("auth_mode", ["api_key", "none"])
def test_foreign_origin_403_allowed_origin_ok(auth_mode: str) -> None:
    settings = make_settings(auth_mode=auth_mode, mcp_allowed_origins=["https://app.example"])
    with running(settings) as client:
        for origin in ("https://evil.example", "http://app.example", "null"):
            # Without a key too: the origin is checked first.
            for headers in ({"Origin": origin}, {**AUTH, "Origin": origin}):
                response = post(client, "tools/list", headers=headers)
                assert response.status_code == 403
                assert response.json()["error"]["code"] == "forbidden"
                assert (
                    response.json()["error"]["message"]
                    == "This origin may not call the MCP endpoint."
                )

        for origin in ("https://app.example", "https://APP.example:443"):
            allowed = call_tool(client, "service_info", headers={**AUTH, "Origin": origin})
            assert allowed["isError"] is False
        # A client without a browser sends no Origin.
        assert call_tool(client, "service_info", headers=AUTH)["isError"] is False


def test_rate_limit_shared_with_rest() -> None:
    settings = make_settings(
        api_keys=[
            {
                "name": "alice",
                "role": "user",
                "sha256": hashlib.sha256(USER_KEY.encode()).hexdigest(),
                "rate_limit_per_minute": 2,
            }
        ]
    )
    with running(settings) as client:
        for _ in range(3):  # free: no tool runs
            rpc(client, "tools/list", headers=AUTH)
        assert client.get("/v1/info", headers=AUTH).status_code == 200
        assert call_tool(client, "service_info", headers=AUTH)["isError"] is False

        refused = call_tool(client, "service_info", headers=AUTH)

        assert refused["isError"] is True
        envelope = envelope_of(refused)
        assert envelope["status"] == "failed"
        assert envelope["error"]["code"] == "rate_limited"
        assert envelope["error"]["retryable"] is True
        assert client.get("/v1/info", headers=AUTH).status_code == 429


def json_lines_of(out: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.splitlines() if line]


def json_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return json_lines_of(capsys.readouterr().out)


def test_trace_id_in_envelope_and_logs(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    with running() as client:
        capsys.readouterr()
        headers = {**AUTH, "x-client-trace-id": "t-1", "x-b3-traceid": "b3-trace"}
        result = call_tool(client, "service_info", headers=headers)
        refused = call_tool(
            client, "service_info", {"secret": "argument-value-42"}, headers=headers
        )

    assert envelope_of(result)["trace_id"] == "t-1"
    assert envelope_of(refused)["trace_id"] == "t-1"
    assert envelope_of(refused)["error"]["code"] == "invalid_request"
    lines = [line for line in json_lines(capsys) if line.get("logger") != "httpx2"]
    calls = [line for line in lines if line["event"] == "mcp_tool_call"]
    assert [(line["tool"], line["outcome"]) for line in calls] == [
        ("service_info", "success"),
        ("service_info", "invalid_request"),
    ]
    for line in calls:
        assert line["principal"] == "alice"
        assert line["trace_id"] == "t-1"
        assert line["x_b3_traceid"] == "b3-trace"
        assert isinstance(line["duration_ms"], float)
    access = [line for line in lines if line["event"] == "request"]
    assert {line["principal"] for line in access} == {"alice"}
    assert {line["trace_id"] for line in access} == {"t-1"}
    text = json.dumps(lines)
    assert "argument-value-42" not in text
    assert USER_KEY not in text


def test_arguments_never_logged_at_debug(capsys: pytest.CaptureFixture[str]) -> None:
    """FastMCP logs a call's arguments at DEBUG: its logger is held at WARNING whatever the
    service's level, by ``configure_logging`` and again by ``build_mcp`` (FastMCP sets its
    level at import, from ``FASTMCP_LOG_LEVEL``)."""
    loggers = [logging.getLogger(name) for name in ("fastmcp", "mcp")]
    levels = [logger.level for logger in loggers]
    try:
        for logger in loggers:
            logger.setLevel(logging.DEBUG)
        configure_logging("DEBUG", "json")
        assert all(logger.getEffectiveLevel() >= logging.WARNING for logger in loggers)

        loggers[0].setLevel(logging.DEBUG)  # as FASTMCP_LOG_LEVEL=DEBUG leaves it
        with running() as client:
            capsys.readouterr()
            call_tool(client, "service_info", {"secret": "argument-value-77"}, headers=AUTH)
            rpc(
                client,
                "tools/call",
                {"name": "no_such_tool", "arguments": {"x": "argument-value-78"}},
                headers=AUTH,
            )
        captured = capsys.readouterr()
    finally:
        configure_logging("INFO", "json")
        for logger, level in zip(loggers, levels, strict=True):
            logger.setLevel(level)

    assert [line for line in json_lines_of(captured.out) if line["event"] == "mcp_tool_call"]
    assert "argument-value-7" not in captured.out
    assert "argument-value-7" not in captured.err


def test_tools_do_not_import_the_app() -> None:
    script = (
        "import sys\n"
        "import text_extraction.mcp_server.tools_info\n"
        "print('text_extraction.app' in sys.modules)\n"
    )
    found = subprocess.run(  # noqa: S603 - our own interpreter and script
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert found.stdout.strip() == "False"


def test_tool_lookup_failure_is_a_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure while the guard looks the tool up is an error result with its log line and
    metric, under the label ``unknown``."""
    registry = CollectorRegistry()
    with running(registry=registry) as client:
        app: Any = client.app

        async def failing(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("lookup failed")

        monkeypatch.setattr(app.state.mcp.server, "get_tool", failing)
        result = call_tool(client, "service_info", headers=AUTH)

    assert result["isError"] is True
    assert envelope_of(result)["error"]["code"] == "internal_error"
    calls = "text_extraction_mcp_tool_calls_total"
    assert sample(registry, calls, tool="unknown", outcome="internal_error") == 1


def sample(registry: CollectorRegistry, name: str, **labels: str) -> float | None:
    return registry.get_sample_value(name, labels)


def test_metrics_counted() -> None:
    registry = CollectorRegistry()
    with running(registry=registry) as client:
        calls = "text_extraction_mcp_tool_calls_total"
        assert sample(registry, calls, tool="service_info", outcome="success") == 0

        call_tool(client, "service_info", headers=AUTH)
        call_tool(client, "service_info", {"unknown": 1}, headers=AUTH)
        call_tool(client, "no_such_tool", headers=AUTH)

    assert sample(registry, calls, tool="service_info", outcome="success") == 1
    assert sample(registry, calls, tool="service_info", outcome="invalid_request") == 1
    # A caller's tool name never becomes a label.
    assert sample(registry, calls, tool="no_such_tool", outcome="not_found") is None
    assert sample(registry, calls, tool="unknown", outcome="not_found") == 1
    seconds = "text_extraction_mcp_tool_seconds_count"
    assert sample(registry, seconds, tool="service_info") == 2
    bucket = "text_extraction_mcp_tool_seconds_bucket"
    assert sample(registry, bucket, tool="service_info", le="0.05") is not None
    assert sample(registry, bucket, tool="service_info", le="120.0") == 2


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Boom(ModelTool):
    args_model = NoArgs

    async def call(self, state: State, principal: Principal, args: BaseModel) -> object:
        raise RuntimeError("secret-detail-of-the-failure")


def test_unexpected_exception_is_internal_error(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    with running() as client:
        app: Any = client.app
        register(
            app.state.mcp.server,
            app.state,
            Boom(name="boom", description="Fails.", parameters=NoArgs.model_json_schema()),
        )
        capsys.readouterr()

        result = call_tool(client, "boom", headers=AUTH)

    assert result["isError"] is True
    envelope = envelope_of(result)
    assert envelope["status"] == "failed"
    assert envelope["error"] == {
        "code": "internal_error",
        "message": "Internal server error.",
        "retryable": False,
        "details": None,
    }
    assert "secret-detail" not in json.dumps(result)
    assert "Traceback" not in json.dumps(result)
    lines = [line for line in json_lines(capsys) if line.get("logger") != "httpx2"]
    (failure,) = [line for line in lines if line["event"] == "mcp_tool_failed"]
    assert failure["tool"] == "boom"
    assert "secret-detail-of-the-failure" in json.dumps(failure["exception"])
    (logged,) = [line for line in lines if line["event"] == "mcp_tool_call"]
    assert logged["outcome"] == "internal_error"
    # FastMCP logs nothing of its own at ERROR for the failure.
    assert [line for line in lines if line["level"] == "error"] == [failure]


def test_body_limit_of_mcp_path() -> None:
    settings = make_settings(document_max_bytes=3000)
    limit = mcp_body_limit(settings)
    assert limit == 1000 * 4 + 65_536
    with running(settings) as client:
        response = client.post(
            "/mcp",
            content=b"x" * (limit + 1),
            headers={"Accept": ACCEPT, "Content-Type": "application/json"},
        )

        assert response.status_code == 413
        assert response.json()["error"]["code"] == "invalid_request"

        def chunks() -> Iterator[bytes]:  # no Content-Length: counted while it is read
            yield b"x" * limit
            yield b"x"

        streamed = client.post(
            "/mcp",
            content=chunks(),
            headers={**AUTH, "Accept": ACCEPT, "Content-Type": "application/json"},
        )
        assert streamed.status_code == 413
        assert streamed.json()["error"]["code"] == "invalid_request"


def test_envelope_result_of_answers() -> None:
    accepted = envelope_result(Envelope(status="success", data={"id": "j1"}), hints=["Hint."])
    assert accepted.is_error is False
    assert accepted.structured_content == {
        "status": "success",
        "data": {"id": "j1"},
        "error": None,
        "trace_id": None,
    }
    texts = [block.text for block in accepted.content]  # type: ignore[union-attr]
    assert texts[0] == "Hint."
    assert json.loads(texts[1]) == accepted.structured_content

    body = {"status": "failed", "error": {"code": "forbidden", "message": "No."}}
    refused = envelope_result(JSONResponse(body, status_code=403))
    assert refused.is_error is True
    assert refused.structured_content == body
    target_failed = {"status": "failed", "error": {"code": "timeout", "message": "Late."}}
    assert envelope_result(JSONResponse(target_failed)).is_error is False

    limited = error_result(ServiceError(ErrorCode.RATE_LIMITED, "Slow down.", 429, retryable=True))
    assert limited.is_error is True
    assert limited.structured_content is not None
    assert limited.structured_content["error"] == {
        "code": "rate_limited",
        "message": "Slow down.",
        "retryable": True,
        "details": None,
    }
