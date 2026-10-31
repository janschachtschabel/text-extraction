"""An app with the UI on (``UI_ENABLED``), one user key and the ``FakeFetcher`` pipeline, and
``run``, which posts a form to ``/ui/run/<op>`` as htmx does."""

import contextlib
import hashlib
from collections.abc import Iterator, Mapping
from typing import Any, cast

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
HX = {"HX-Request": "true"}


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
        "outbound_min_delay_ms": 0,
        "rate_limit_per_minute": 10_000,
        "ui_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


@contextlib.contextmanager
def running(
    fetcher: FakeFetcher | None = None,
    *,
    registry: CollectorRegistry | None = None,
    **overrides: Any,
) -> Iterator[TestClient]:
    """The public app with ``make_settings(**overrides)`` and a pipeline around ``fetcher``
    (by default one page, ``PAGE``), started."""
    settings = make_settings(**overrides)
    registry = registry or CollectorRegistry()
    app = create_public_app(settings, FakeAsyncRedis(), registry)
    fake = fetcher or FakeFetcher({PAGE: fetched(body=ARTICLE)})
    app.state.pipeline = create_pipeline(settings, app.state.valkey, cast(Fetcher, fake), registry)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def client() -> Iterator[TestClient]:
    with running() as test_client:
        yield test_client


def run(
    client: TestClient,
    op: str,
    *,
    auth: bool = True,
    headers: Mapping[str, str] | None = None,
    files: Any = None,
    **fields: Any,
) -> Any:
    """POST ``fields`` (and ``files``) to ``/ui/run/<op>`` with ``HX-Request: true`` and, with
    ``auth``, the user key, as the page's htmx sends them; ``headers`` add or replace headers."""
    sent = {**HX, **(AUTH if auth else {}), **(headers or {})}
    return client.post(f"/ui/run/{op}", data=fields, files=files, headers=sent)
