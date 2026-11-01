"""The Host allowlist under AUTH_MODE=none (M3/S4, ``ALLOWED_HOSTS``): a DNS-rebinding page
reaches the API under its own host name, which is not on the list."""

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from pydantic import ValidationError

from text_extraction.app import create_public_app
from text_extraction.host_guard import host_of
from text_extraction.settings import Settings


def client_for(**overrides: object) -> TestClient:
    settings = Settings(**overrides)  # type: ignore[arg-type]
    return TestClient(create_public_app(settings, FakeAsyncRedis(), CollectorRegistry()))


@pytest.mark.parametrize(
    ("header", "host"),
    [
        ("localhost", "localhost"),
        ("localhost:8080", "localhost"),
        ("LocalHost.:8080", "localhost"),
        ("127.0.0.1:8080", "127.0.0.1"),
        ("[::1]:8080", "[::1]"),
        ("[::1]", "[::1]"),
        ("api:8080", "api"),
        ("", ""),
    ],
)
def test_host_of(header: str, host: str) -> None:
    assert host_of(header) == host


@pytest.mark.parametrize("host", ["localhost:8080", "127.0.0.1", "[::1]:8080", "api:8080"])
def test_the_default_hosts_pass_without_keys(host: str) -> None:
    with client_for(auth_mode="none", allowed_hosts="localhost,127.0.0.1,[::1],api") as client:
        assert client.get("/v1/info", headers={"host": host}).status_code == 200


@pytest.mark.parametrize(
    "host", ["attacker.example:8080", "localhost.attacker.example", "10.0.0.5"]
)
def test_another_host_is_refused_without_keys(host: str) -> None:
    with client_for(auth_mode="none", allowed_hosts="localhost,127.0.0.1,[::1],api") as client:
        response = client.post(
            "/v1/extract", json={"url": "https://example.org/"}, headers={"host": host}
        )

    assert response.status_code == 400
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "invalid_request"
    assert "ALLOWED_HOSTS" in body["error"]["message"]


def test_a_star_switches_the_check_off() -> None:
    with client_for(auth_mode="none", allowed_hosts="*") as client:
        assert client.get("/v1/info", headers={"host": "anything.example"}).status_code == 200


def test_with_keys_the_host_is_not_checked() -> None:
    with client_for(
        auth_mode="api_key",
        api_keys=[{"name": "a", "role": "user", "sha256": "0" * 64}],
        allowed_hosts="localhost",
    ) as client:
        assert client.get("/v1/info", headers={"host": "service.example"}).status_code == 401


def test_the_default_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOWED_HOSTS", raising=False)  # tests/conftest.py sets it
    assert Settings(auth_mode="none").allowed_hosts == ["localhost", "127.0.0.1", "[::1]", "api"]


@pytest.mark.parametrize("entry", ["http://localhost", "local host", "host/path", "::1"])
def test_an_invalid_entry_is_refused(entry: str) -> None:
    with pytest.raises(ValidationError):
        Settings(auth_mode="none", allowed_hosts=entry)  # type: ignore[arg-type]


def test_the_ui_reaches_the_api_through_the_guard() -> None:
    # The UI forwards to the API in-process; the inner request carries the checked Host.
    with client_for(auth_mode="none", allowed_hosts="localhost", ui_enabled=True) as client:
        page = client.get("/ui/", headers={"host": "localhost:8080"})
        info = client.post("/ui/run/info", headers={"host": "localhost:8080", "HX-Request": "true"})
        refused = client.get("/ui/", headers={"host": "rebound.example"})

    assert page.status_code == 200
    assert info.status_code == 200, info.text
    assert "ALLOWED_HOSTS" not in info.text
    assert refused.status_code == 400
