"""Optional features kept for admin keys (package 19, P19-D1 b, ruling P19-R4):
``OCR_ALLOW_USERS``, ``PII_ALLOW_USERS``, ``LLM_ALLOW_USERS``, ``ASR_ALLOW_USERS``."""

import hashlib
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from tests.fakes import ARTICLE, PAGE, FakeFetcher, fetched
from text_extraction.app import create_public_app
from text_extraction.auth import Principal
from text_extraction.chunking.types import ChunkingOptions
from text_extraction.errors import ServiceError
from text_extraction.feature_access import (
    allowed,
    check_feature_access,
    forbidden_message,
    settings_for,
)
from text_extraction.fetch import Fetcher
from text_extraction.pii.types import PiiOptions
from text_extraction.pipeline import create_pipeline
from text_extraction.settings import Settings, llm_available
from text_extraction.structured.types import StructuredOptions
from text_extraction.vision.types import DescribeImagesOptions

USER = Principal(name="alice", role="user", rate_limit_per_minute=None)
ADMIN = Principal(name="root", role="admin", rate_limit_per_minute=None)
USER_KEY = "user-key-0123456789-raw-secret"
ADMIN_KEY = "admin-key-0123456789-raw-secret"
AUTH = {"Authorization": f"Bearer {USER_KEY}"}
ADMIN_AUTH = {"Authorization": f"Bearer {ADMIN_KEY}"}
OGG = b"OggS\x00\x02" + b"\x00" * 64
SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}}}
ALL_ON: dict[str, Any] = {
    "docling_enabled": True,
    "pii_enabled": True,
    "llm_enabled": True,
    "asr_enabled": True,
}
NONE_FOR_USERS: dict[str, Any] = {
    "ocr_allow_users": False,
    "pii_allow_users": False,
    "llm_allow_users": False,
    "asr_allow_users": False,
}


def settings(**overrides: Any) -> Settings:
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
        "llm_embedding_model": "text-embedding-3-small",
        **ALL_ON,
    }
    values.update(overrides)
    return Settings(**values)


# --- the switches -------------------------------------------------------------------------------


def test_switches_default_to_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    for name in ("OCR", "PII", "LLM", "ASR"):
        monkeypatch.delenv(f"{name}_ALLOW_USERS", raising=False)
    defaults = Settings()
    assert (
        defaults.ocr_allow_users,
        defaults.pii_allow_users,
        defaults.llm_allow_users,
        defaults.asr_allow_users,
    ) == (True, True, True, True)
    monkeypatch.setenv("PII_ALLOW_USERS", "false")
    assert Settings().pii_allow_users is False


def test_admins_may_always_users_while_allowed() -> None:
    closed = settings(**NONE_FOR_USERS)
    for feature in ("ocr", "pii", "llm", "asr"):
        assert allowed(feature, "admin", closed)
        assert not allowed(feature, "user", closed)
        assert not allowed(feature, None, closed)
        assert allowed(feature, "user", settings())


def test_settings_for_switches_off_what_the_key_may_not_use() -> None:
    open_ = settings()
    assert settings_for(open_, USER) is open_
    closed = settings(llm_allow_users=False)
    assert settings_for(closed, ADMIN) is closed
    seen = settings_for(closed, USER)
    assert not llm_available(seen)
    assert (seen.docling_enabled, seen.pii_enabled, seen.asr_enabled) == (True, True, True)
    assert llm_available(closed)  # the service's own settings are unchanged


# --- the explicit requests ----------------------------------------------------------------------

EXPLICIT: list[tuple[str, dict[str, Any]]] = [
    ("ocr", {"ocr": "always"}),
    ("pii", {"pii": PiiOptions(mode="mask")}),
    ("asr", {"transcribe": True}),
    ("asr", {"media_upload": True}),
    (
        "llm",
        {"structured": StructuredOptions.model_validate({"schema": SCHEMA, "strategy": "llm"})},
    ),
    (
        "llm",
        {
            "structured": StructuredOptions.model_validate(
                {"schema": SCHEMA, "strategy": "auto", "learn_selectors": True}
            )
        },
    ),
    ("llm", {"chunking": ChunkingOptions.model_validate({"strategy": "semantic"})}),
    ("llm", {"describe": DescribeImagesOptions()}),
]


@pytest.mark.parametrize(("feature", "asked"), EXPLICIT)
def test_an_explicit_request_is_403_for_a_user(feature: str, asked: dict[str, Any]) -> None:
    closed = settings(**{f"{feature}_allow_users": False})
    with pytest.raises(ServiceError) as caught:
        check_feature_access(USER, closed, **asked)
    assert caught.value.http_status == 403
    assert caught.value.code == "forbidden"
    assert caught.value.message == forbidden_message(feature)  # type: ignore[arg-type]
    check_feature_access(ADMIN, closed, **asked)  # an admin key may
    check_feature_access(USER, settings(), **asked)  # and so may a user while allowed


@pytest.mark.parametrize(("feature", "asked"), EXPLICIT)
def test_a_switched_off_feature_is_left_to_its_own_check(
    feature: str, asked: dict[str, Any]
) -> None:
    enabled = {"ocr": "docling_enabled", "pii": "pii_enabled", "llm": "llm_enabled"}
    off = settings(**{f"{feature}_allow_users": False, enabled.get(feature, "asr_enabled"): False})
    check_feature_access(USER, off, **asked)  # no 403: the feature's own 400 answers


def test_implicit_uses_pass() -> None:
    closed = settings(**NONE_FOR_USERS)
    auto = StructuredOptions.model_validate({"schema": SCHEMA, "strategy": "auto"})
    check_feature_access(
        USER, closed, ocr="auto", structured=auto, chunking=ChunkingOptions(), transcribe=False
    )


# --- the routes ---------------------------------------------------------------------------------


@pytest.fixture
def client() -> Iterator[TestClient]:
    registry = CollectorRegistry()
    config = settings(**NONE_FOR_USERS)
    app = create_public_app(config, FakeAsyncRedis(), registry)
    fake = FakeFetcher({})
    app.state.pipeline = create_pipeline(config, app.state.valkey, cast(Fetcher, fake), registry)
    with TestClient(app) as test_client:
        yield test_client
    assert fake.calls == []  # every request here was refused before a fetch


def assert_forbidden(response: Any, feature: str) -> None:
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["error"]["code"] == "forbidden"
    assert body["error"]["message"] == forbidden_message(feature)  # type: ignore[arg-type]


ROUTE_BODIES: list[tuple[str, dict[str, Any]]] = [
    ("ocr", {"ocr": "always"}),
    ("pii", {"pii": "mask"}),
    ("asr", {"transcribe": True}),
    ("llm", {"structured": {"schema": SCHEMA, "strategy": "llm"}}),
    ("llm", {"chunking": {"strategy": "semantic"}}),
    ("llm", {"describe_images": {}}),
]


@pytest.mark.parametrize(("feature", "fields"), ROUTE_BODIES)
def test_extract_refuses_a_user(client: TestClient, feature: str, fields: dict[str, Any]) -> None:
    response = client.post("/v1/extract", json={"url": PAGE, **fields}, headers=AUTH)
    assert_forbidden(response, feature)


@pytest.mark.parametrize(("feature", "fields"), ROUTE_BODIES)
def test_batch_refuses_a_user(client: TestClient, feature: str, fields: dict[str, Any]) -> None:
    response = client.post("/v1/batch", json={"urls": [PAGE], **fields}, headers=AUTH)
    assert_forbidden(response, feature)


def test_upload_refuses_a_users_pii_and_media(client: TestClient) -> None:
    text = {"file": ("notes.txt", b"Some words.", "text/plain")}
    response = client.post("/v1/extract/file", files=text, data={"pii": "mask"}, headers=AUTH)
    assert_forbidden(response, "pii")
    audio = {"file": ("talk.ogg", OGG, "audio/ogg")}
    response = client.post("/v1/extract/file", files=audio, headers=AUTH)
    assert_forbidden(response, "asr")


def test_an_admin_is_not_refused(client: TestClient) -> None:
    """The admin's request passes the feature check: it fails later, on the bad URL."""
    body = {"url": "not a url", "pii": "mask", "ocr": "always"}
    response = client.post("/v1/extract", json=body, headers=ADMIN_AUTH)
    assert response.status_code != 403, response.text


# --- the implicit uses --------------------------------------------------------------------------


def serving(config: Settings) -> TestClient:
    registry = CollectorRegistry()
    app = create_public_app(config, FakeAsyncRedis(), registry)
    fake = FakeFetcher({PAGE: fetched(body=ARTICLE)})
    app.state.pipeline = create_pipeline(config, app.state.valkey, cast(Fetcher, fake), registry)
    return TestClient(app)


def test_auto_runs_without_the_llm_for_a_user() -> None:
    """Strategy ``auto`` of a user key without the LLM: its key is never used, the fields the
    page has not are missing with ``llm_unavailable``."""
    wanted = {"type": "object", "properties": {"price_in_euro": {"type": "number"}}}
    body = {"url": PAGE, "structured": {"schema": wanted, "strategy": "auto"}, "wait_seconds": 20}
    headers = {**AUTH, "X-LLM-Api-Key": "sk-caller-key-0123"}
    with serving(settings(llm_allow_users=False, llm_api_key="sk-operator-key")) as client:
        answer = client.post("/v1/extract", json=body, headers=headers)

    assert answer.status_code == 200, answer.text
    data = answer.json()["data"]
    assert "llm_unavailable" in data["warnings"]
    assert data["structured"]["missing"] == ["price_in_euro"]
