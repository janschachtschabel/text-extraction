import hashlib
import json
from collections.abc import Iterator
from typing import Annotated, Any

import pytest
import structlog
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from text_extraction.auth import Principal, require_role
from text_extraction.errors import install_error_handlers
from text_extraction.logging import configure_logging
from text_extraction.settings import Settings
from text_extraction.tracing import TraceMiddleware

USER_KEY = "user-key-0123456789-raw-secret"
ADMIN_KEY = "admin-key-9876543210-raw-secret"
OTHER_KEY = "not-configured-key-raw-secret"


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def make_settings(auth_mode: str = "api_key") -> Settings:
    return Settings(
        auth_mode=auth_mode,  # type: ignore[arg-type]
        api_keys=[
            {"name": "alice", "role": "user", "sha256": sha256_hex(USER_KEY)},  # type: ignore[list-item]
            {
                "name": "ops",
                "role": "admin",
                "sha256": sha256_hex(ADMIN_KEY),
                "rate_limit_per_minute": 600,
            },  # type: ignore[list-item]
        ],
    )


UserPrincipal = Annotated[Principal, Depends(require_role("user"))]
AdminPrincipal = Annotated[Principal, Depends(require_role("admin"))]


def build_app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    install_error_handlers(app)
    app.add_middleware(TraceMiddleware)

    @app.get("/user")
    async def user_route(principal: UserPrincipal) -> dict[str, Any]:
        structlog.get_logger("test").info("inside_route")
        return {
            "name": principal.name,
            "role": principal.role,
            "rate_limit_per_minute": principal.rate_limit_per_minute,
            "bound": structlog.contextvars.get_contextvars().get("principal"),
        }

    @app.get("/admin")
    async def admin_route(principal: AdminPrincipal) -> dict[str, Any]:
        return {"name": principal.name, "role": principal.role}

    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(build_app(make_settings())) as test_client:
        yield test_client


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def assert_failed_envelope(response: Any, status: int, code: str) -> None:
    assert response.status_code == status
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == code
    assert body["error"]["retryable"] is False


def test_missing_header_401(client: TestClient) -> None:
    response = client.get("/user")

    assert_failed_envelope(response, 401, "unauthorized")
    assert response.headers["www-authenticate"] == "Bearer"


def test_wrong_scheme_401(client: TestClient) -> None:
    response = client.get("/user", headers={"Authorization": f"Basic {USER_KEY}"})

    assert_failed_envelope(response, 401, "unauthorized")
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("value", ["Bearer", "Bearer ", "Bearer   ", f"Token {USER_KEY}", ""])
def test_malformed_header_401(client: TestClient, value: str) -> None:
    response = client.get("/user", headers={"Authorization": value})

    assert_failed_envelope(response, 401, "unauthorized")


def test_unknown_key_401(client: TestClient) -> None:
    response = client.get("/user", headers=bearer(OTHER_KEY))

    assert_failed_envelope(response, 401, "unauthorized")
    assert response.headers["www-authenticate"] == "Bearer"


def test_non_ascii_key_401(client: TestClient) -> None:
    response = client.get("/user", headers={b"authorization": b"Bearer caf\xe9"})

    assert_failed_envelope(response, 401, "unauthorized")


def test_user_key_ok(client: TestClient) -> None:
    response = client.get("/user", headers=bearer(USER_KEY))

    assert response.status_code == 200
    assert response.json() == {
        "name": "alice",
        "role": "user",
        "rate_limit_per_minute": None,
        "bound": "alice",
    }


def test_scheme_is_case_insensitive(client: TestClient) -> None:
    response = client.get("/user", headers={"Authorization": f"bearer {USER_KEY}"})

    assert response.status_code == 200


def test_user_key_on_admin_route_403(client: TestClient) -> None:
    response = client.get("/admin", headers=bearer(USER_KEY))

    assert_failed_envelope(response, 403, "forbidden")
    assert "www-authenticate" not in response.headers


def test_admin_key_on_admin_route_ok(client: TestClient) -> None:
    response = client.get("/admin", headers=bearer(ADMIN_KEY))

    assert response.status_code == 200
    assert response.json() == {"name": "ops", "role": "admin"}


def test_admin_key_on_user_route_ok(client: TestClient) -> None:
    response = client.get("/user", headers=bearer(ADMIN_KEY))

    assert response.status_code == 200
    assert response.json()["role"] == "admin"
    assert response.json()["rate_limit_per_minute"] == 600


def test_admin_route_without_key_is_401_not_403(client: TestClient) -> None:
    assert_failed_envelope(client.get("/admin"), 401, "unauthorized")


def test_auth_none_allows_all() -> None:
    with TestClient(build_app(make_settings("none"))) as client:
        user = client.get("/user")
        admin = client.get("/admin")
        with_key = client.get("/admin", headers=bearer(OTHER_KEY))

    assert user.status_code == 200
    assert user.json() == {
        "name": "anonymous",
        "role": "admin",
        "rate_limit_per_minute": None,
        "bound": "anonymous",
    }
    assert admin.status_code == 200
    assert with_key.status_code == 200


def test_authenticate_reads_settings_from_app_state() -> None:
    # No cached get_settings(): a second app with other settings behaves differently.
    with (
        TestClient(build_app(make_settings())) as strict,
        TestClient(build_app(make_settings("none"))) as open_,
    ):
        assert strict.get("/user").status_code == 401
        assert open_.get("/user").status_code == 200


def test_principal_is_frozen() -> None:
    principal = Principal(name="a", role="user", rate_limit_per_minute=None)

    with pytest.raises(AttributeError):
        principal.name = "b"  # type: ignore[misc]


def test_error_envelopes_carry_trace_id(client: TestClient) -> None:
    response = client.get("/user", headers={"x-client-trace-id": "trace-401"})

    assert response.json()["trace_id"] == "trace-401"


def access_line(logs: list[Any]) -> Any:
    (line,) = [entry for entry in logs if entry["event"] == "request"]
    return line


def test_principal_name_on_access_line(client: TestClient) -> None:
    # capture_logs bypasses merge_contextvars, so only the explicit field can satisfy this:
    # the middleware reads the name from the ASGI scope state.
    with capture_logs() as logs:
        client.get("/user", headers=bearer(USER_KEY))
        client.get("/admin", headers=bearer(ADMIN_KEY))

    first, second = [entry for entry in logs if entry["event"] == "request"]
    assert first["principal"] == "alice"
    assert second["principal"] == "ops"


def test_failed_request_has_no_principal_on_access_line(client: TestClient) -> None:
    with capture_logs() as logs:
        client.get("/user", headers=bearer(OTHER_KEY))

    assert "principal" not in access_line(logs)


def test_forbidden_request_still_names_the_principal(client: TestClient) -> None:
    with capture_logs() as logs:
        client.get("/admin", headers=bearer(USER_KEY))

    line = access_line(logs)
    assert line["status"] == 403
    assert line["principal"] == "alice"


def test_principal_bound_to_log_context_and_released(client: TestClient) -> None:
    with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as logs:
        client.get("/user", headers=bearer(USER_KEY))

    (inside,) = [entry for entry in logs if entry["event"] == "inside_route"]
    assert inside["principal"] == "alice"
    assert "principal" not in structlog.contextvars.get_contextvars()


def test_principal_on_access_line_with_real_json_logging(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("INFO", "json")
    try:
        client.get("/user", headers=bearer(USER_KEY))
    finally:
        structlog.reset_defaults()

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    inside = next(line for line in lines if line["event"] == "inside_route")
    access = next(line for line in lines if line["event"] == "request")
    assert inside["principal"] == "alice"
    assert access["principal"] == "alice"
    assert access["status"] == 200


def test_key_not_in_logs(client: TestClient, capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("DEBUG", "json")
    try:
        with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as captured:
            ok = client.get("/user", headers=bearer(USER_KEY))
            bad = client.get("/user", headers=bearer(OTHER_KEY))
            denied = client.get("/admin", headers=bearer(USER_KEY))
        client.get("/user", headers=bearer(USER_KEY))
        client.get("/user", headers=bearer(OTHER_KEY))
    finally:
        structlog.reset_defaults()

    assert (ok.status_code, bad.status_code, denied.status_code) == (200, 401, 403)
    output = capsys.readouterr().out
    assert output.strip(), "expected log output to check"
    for secret in (USER_KEY, OTHER_KEY, ADMIN_KEY):
        assert secret not in output
        assert secret not in repr(captured)
        assert secret not in bad.text
        assert secret not in denied.text
        assert secret not in repr(bad.headers)
    # The digest is not a key, but it is just as pointless in a log line.
    assert sha256_hex(USER_KEY) not in output
