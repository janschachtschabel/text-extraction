"""Package 24, gap 2: ``GET /v1/info`` (and the MCP tool ``service_info``) says which features the
caller's key can use and which limits apply, so a client learns it before a call fails."""

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from text_extraction.app import create_public_app
from text_extraction.info import offered_features
from text_extraction.settings import Settings

USER_KEY = "info-user-key-0123456789-secret"
ADMIN_KEY = "info-admin-key-0123456789-secret"


def _key(name: str, role: str, raw: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "role": role, "sha256": hashlib.sha256(raw.encode()).hexdigest(), **extra}


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "auth_mode": "api_key",
        "api_keys": [_key("alice", "user", USER_KEY), _key("root", "admin", ADMIN_KEY)],
        "rate_limit_per_minute": 60,
    }
    values.update(overrides)
    return Settings(**values)


@contextmanager
def running(**overrides: Any) -> Iterator[TestClient]:
    app = create_public_app(make_settings(**overrides), FakeAsyncRedis(), CollectorRegistry())
    with TestClient(app) as client:
        yield client


def info(client: TestClient, key: str) -> dict[str, Any]:
    response = client.get("/v1/info", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 200
    data: dict[str, Any] = response.json()["data"]
    return data


def test_info_lists_features_for_the_role() -> None:
    with running(docling_enabled=True, ocr_allow_users=False, pii_enabled=False) as client:
        user, admin = info(client, USER_KEY), info(client, ADMIN_KEY)

    assert user["features"]["ocr"] is False
    assert admin["features"]["ocr"] is True
    assert user["features"]["pii"] is admin["features"]["pii"] is False
    assert user["features"]["jobs"] is True
    assert user["features"]["site_structure"] is True
    assert user["features"]["compliance_default"] == "enforce"


def test_info_browser_and_unblock() -> None:
    with running(browser_enabled=True, browser_unblock_enabled=True) as client:
        user, admin = info(client, USER_KEY), info(client, ADMIN_KEY)

    assert user["features"]["browser"] is user["features"]["screenshots"] is True
    assert user["features"]["unblock"] is False
    assert admin["features"]["unblock"] is True
    with running() as client:
        off = info(client, ADMIN_KEY)
    assert (off["features"]["browser"], off["features"]["screenshots"]) == (False, False)
    assert off["features"]["unblock"] is False


def test_info_compliance_choice() -> None:
    with running(compliance_allow_override=False) as client:
        assert info(client, USER_KEY)["features"]["compliance_choice"] is False
        assert info(client, ADMIN_KEY)["features"]["compliance_choice"] is True


def test_info_limits() -> None:
    keys = [
        _key("alice", "user", USER_KEY, rate_limit_per_minute=7),
        _key("root", "admin", ADMIN_KEY),
    ]
    with running(api_keys=keys, batch_max_urls=300, site_max_pages=50, site_max_depth=3) as client:
        user, admin = info(client, USER_KEY), info(client, ADMIN_KEY)

    assert user["limits"]["batch_max_urls"] == 300
    assert user["limits"]["site_max_pages"] == 50
    assert user["limits"]["site_max_depth"] == 3
    assert user["limits"]["rate_limit_per_minute"] == 7
    assert admin["limits"]["rate_limit_per_minute"] == 60
    assert user["limits"]["document_max_bytes"] == 20_000_000
    assert user["limits"]["request_max_body_bytes"] == 65536
    with running(rate_limit_enabled=False) as client:
        assert info(client, USER_KEY)["limits"]["rate_limit_per_minute"] is None


def test_offered_features_match_the_ui() -> None:
    from text_extraction.ui.forms import feature_offer

    settings = make_settings(docling_enabled=True, ocr_allow_users=False, llm_enabled=True)
    for role in ("user", "admin"):
        offered, admin_only = offered_features(role, settings)
        offer = feature_offer(role, settings)
        assert offered == {
            "ocr": offer.ocr,
            "pii": offer.pii,
            "transcribe": offer.transcribe,
            "llm": offer.llm,
            "llm_needs_key": offer.llm_needs_key,
            "semantic": offer.semantic,
        }
        assert admin_only == offer.admin_only


def test_media_upload_limit_only_for_keys_that_transcribe() -> None:
    with running(asr_enabled=True, asr_allow_users=False, media_upload_max_bytes=30_000_000) as c:
        user, admin = info(c, USER_KEY), info(c, ADMIN_KEY)

    assert user["limits"]["media_upload_max_bytes"] is None
    assert admin["limits"]["media_upload_max_bytes"] == 30_000_000
