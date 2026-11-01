import re
from collections.abc import Iterator, MutableMapping, Sequence
from typing import Any

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from text_extraction.envelope import ErrorCode
from text_extraction.errors import ServiceError, install_error_handlers
from text_extraction.tracing import TraceMiddleware, current_trace_id, outbound_trace_headers

B3_FIELDS = {
    "b3": "80f198ee56343ba864fe8b2a57d3eff7-e457b5a2e4d86bd1-1",
    "x-b3-traceid": "80f198ee56343ba864fe8b2a57d3eff7",
    "x-b3-spanid": "e457b5a2e4d86bd1",
    "x-b3-parentspanid": "05e3ac9a4f6e3b90",
    "x-b3-sampled": "1",
}


def build_app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.add_middleware(TraceMiddleware)

    @app.get("/ok")
    def ok() -> dict[str, str | None]:
        return {"trace_id": current_trace_id()}

    @app.get("/service-error")
    def service_error() -> None:
        raise ServiceError(ErrorCode.FORBIDDEN, "not allowed here", 403)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret internal detail")

    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(build_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def access_lines(logs: Sequence[MutableMapping[str, Any]]) -> list[MutableMapping[str, Any]]:
    return [line for line in logs if line["event"] == "request"]


def test_client_trace_id_logged_unchanged(client: TestClient) -> None:
    with capture_logs() as logs:
        response = client.get("/ok", headers={"x-client-trace-id": "Client.Trace:42-abc"})

    (line,) = access_lines(logs)
    assert line["trace_id"] == "Client.Trace:42-abc"
    assert line["method"] == "GET"
    assert line["path"] == "/ok"
    assert line["status"] == 200
    assert line["duration_ms"] >= 0
    assert response.json() == {"trace_id": "Client.Trace:42-abc"}


def test_trace_id_generated_when_missing(client: TestClient) -> None:
    with capture_logs() as logs:
        first = client.get("/ok")
        second = client.get("/ok")

    first_line, second_line = access_lines(logs)
    assert re.fullmatch(r"[0-9a-f]{32}", first_line["trace_id"])
    assert first_line["trace_id"] != second_line["trace_id"]
    assert first.json() == {"trace_id": first_line["trace_id"]}
    assert second.json() == {"trace_id": second_line["trace_id"]}


@pytest.mark.parametrize(
    "bad_value",
    ["has space", "semi;colon", "x" * 129, "line\tbreak", "<script>", ""],
)
def test_invalid_trace_id_replaced(client: TestClient, bad_value: str) -> None:
    with capture_logs() as logs:
        client.get("/ok", headers={"x-client-trace-id": bad_value})

    (line,) = access_lines(logs)
    assert line["trace_id"] != bad_value
    assert re.fullmatch(r"[0-9a-f]{32}", line["trace_id"])


def test_b3_headers_logged(client: TestClient) -> None:
    with capture_logs() as logs:
        client.get("/ok", headers=B3_FIELDS)

    (line,) = access_lines(logs)
    assert line["b3"] == B3_FIELDS["b3"]
    assert line["x_b3_traceid"] == B3_FIELDS["x-b3-traceid"]
    assert line["x_b3_spanid"] == B3_FIELDS["x-b3-spanid"]
    assert line["x_b3_parentspanid"] == B3_FIELDS["x-b3-parentspanid"]
    assert line["x_b3_sampled"] == "1"


def test_b3_fields_absent_or_invalid_are_not_logged(client: TestClient) -> None:
    with capture_logs() as logs:
        client.get("/ok", headers={"x-b3-spanid": "e457b5a2e4d86bd1", "x-b3-traceid": "bad value"})

    (line,) = access_lines(logs)
    assert line["x_b3_spanid"] == "e457b5a2e4d86bd1"
    assert not {"b3", "x_b3_traceid", "x_b3_parentspanid", "x_b3_sampled"} & set(line)


def test_trace_fields_are_bound_to_other_log_lines(client: TestClient) -> None:
    app = build_app()

    @app.get("/log")
    def log_something() -> None:
        structlog.get_logger("test").info("inside_request")

    with (
        TestClient(app) as test_client,
        capture_logs(processors=[structlog.contextvars.merge_contextvars]) as logs,
    ):
        test_client.get("/log", headers={"x-client-trace-id": "abc", "x-b3-spanid": "span1"})

    (line,) = [entry for entry in logs if entry["event"] == "inside_request"]
    assert line["trace_id"] == "abc"
    assert line["x_b3_spanid"] == "span1"
    assert current_trace_id() is None


def test_query_string_not_logged(client: TestClient) -> None:
    with capture_logs() as logs:
        client.get("/ok?url=https://example.org/&token=s3cret")

    (line,) = access_lines(logs)
    assert line["path"] == "/ok"
    assert "s3cret" not in repr(logs)
    assert "token" not in repr(logs)


def test_error_envelope_carries_trace_id(client: TestClient) -> None:
    response = client.get("/service-error", headers={"x-client-trace-id": "trace-403"})

    assert response.status_code == 403
    assert response.json()["trace_id"] == "trace-403"


def test_validation_and_not_found_envelopes_carry_trace_id(client: TestClient) -> None:
    response = client.get("/missing", headers={"x-client-trace-id": "trace-404"})

    assert response.status_code == 404
    assert response.json()["trace_id"] == "trace-404"


def test_unhandled_500_carries_trace_id(client: TestClient) -> None:
    # Starlette runs this handler in ServerErrorMiddleware, outside TraceMiddleware, so the
    # contextvar is already reset: the id has to come from the request.
    with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as logs:
        response = client.get(
            "/boom", headers={"x-client-trace-id": "trace-500", "x-b3-spanid": "span500"}
        )

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert body["trace_id"] == "trace-500"
    assert "secret internal detail" not in response.text

    (access,) = access_lines(logs)
    assert access["status"] == 500
    assert access["trace_id"] == "trace-500"
    (failure,) = [entry for entry in logs if entry["event"] == "unhandled_exception"]
    assert failure["trace_id"] == "trace-500"
    assert failure["x_b3_spanid"] == "span500"
    assert current_trace_id() is None


def test_trace_header_not_echoed(client: TestClient) -> None:
    sent = {"x-client-trace-id": "trace-1", **B3_FIELDS}
    responses = [client.get("/ok", headers=sent), client.get("/service-error", headers=sent)]

    for response in responses:
        names = {name.lower() for name in response.headers}
        assert not names & {"x-client-trace-id", *B3_FIELDS}


B3_TRACE = "80f198ee56343ba864fe8b2a57d3eff7"


@pytest.mark.parametrize(
    ("headers", "trace_id"),
    [
        ({"x-b3-traceid": "80f198ee56343ba864fe8b2a57d3eff7"}, "80f198ee56343ba864fe8b2a57d3eff7"),
        (
            {"b3": "80f198ee56343ba864fe8b2a57d3eff7-e457b5a2e4d86bd1-1"},
            "80f198ee56343ba864fe8b2a57d3eff7",
        ),
        (
            {"x-client-trace-id": "client-1", "x-b3-traceid": "80f198ee56343ba864fe8b2a57d3eff7"},
            "client-1",
        ),
    ],
)
def test_b3_trace_id_is_the_fallback(
    client: TestClient, headers: dict[str, str], trace_id: str
) -> None:
    # O4: a mesh that sends only B3 keeps one id across the service's log lines.
    with capture_logs() as logs:
        client.get("/ok", headers=headers)

    (line,) = access_lines(logs)
    assert line["trace_id"] == trace_id


def test_outbound_trace_headers_are_the_bound_ones() -> None:
    assert outbound_trace_headers() == {}
    with structlog.contextvars.bound_contextvars(
        trace_id="t-1", x_b3_traceid="80f198ee56343ba864fe8b2a57d3eff7", x_b3_sampled="1"
    ):
        assert outbound_trace_headers() == {
            "x-client-trace-id": "t-1",
            "x-b3-traceid": "80f198ee56343ba864fe8b2a57d3eff7",
            "x-b3-sampled": "1",
        }
