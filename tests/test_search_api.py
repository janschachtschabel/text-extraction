"""``POST /v1/search`` (package 25): the route and its core ``run_search`` in front of the SearXNG
client, with ``httpx.MockTransport`` as SearXNG in ``app.state.search_http``."""

import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import partial
from typing import Any, cast

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from text_extraction.app import create_public_app
from text_extraction.auth import principal_for
from text_extraction.errors import ServiceError
from text_extraction.search.types import SearchRequest
from text_extraction.search_api import SEARCH_DISABLED_MESSAGE, run_search
from text_extraction.settings import Settings

USER_KEY = "search-user-key-0123456789-secret"
ADMIN_KEY = "search-admin-key-0123456789-secret"
USER = {"Authorization": f"Bearer {USER_KEY}"}
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}

Handler = Callable[[httpx.Request], httpx.Response]


def _key(name: str, role: str, raw: str) -> dict[str, Any]:
    return {"name": name, "role": role, "sha256": hashlib.sha256(raw.encode()).hexdigest()}


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "auth_mode": "api_key",
        "api_keys": [_key("alice", "user", USER_KEY), _key("root", "admin", ADMIN_KEY)],
        "rate_limit_per_minute": 10_000,
        "search_enabled": True,
        "search_sources": ["wikipedia", "arxiv", "crossref"],
        "search_max_results": 20,
    }
    values.update(overrides)
    return Settings(**values)


def searx_answer(*urls: str, unresponsive: list[list[str]] | None = None) -> dict[str, Any]:
    return {
        "query": "q",
        "results": [
            {"url": url, "title": f"Title {url}", "content": "Snippet.", "engines": ["wikipedia"]}
            for url in urls
        ],
        "suggestions": ["photosynthesis plants"],
        "unresponsive_engines": unresponsive or [],
    }


class Searx:
    """A fake SearXNG that records the requests it gets."""

    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def client(self) -> httpx.AsyncClient:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self._handler(request)

        return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def serving(body: dict[str, Any]) -> Searx:
    return Searx(lambda _request: httpx.Response(200, json=body))


@contextmanager
def running(searx: Searx | None = None, **overrides: Any) -> Iterator[TestClient]:
    app = create_public_app(make_settings(**overrides), FakeAsyncRedis(), CollectorRegistry())
    if searx is not None:
        app.state.search_http = searx.client()
    with TestClient(app) as client:
        yield client


def search(client: TestClient, body: dict[str, Any], headers: dict[str, str] = USER) -> Any:
    return client.post("/v1/search", json=body, headers=headers)


def test_search_answers_hits_from_searxng() -> None:
    searx = serving(searx_answer("https://en.wikipedia.org/wiki/Photosynthesis"))
    with running(searx) as client:
        response = search(client, {"query": "photosynthesis", "language": "en"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["trace_id"]
    data = body["data"]
    assert data["query"] == "photosynthesis"
    assert [hit["url"] for hit in data["results"]] == [
        "https://en.wikipedia.org/wiki/Photosynthesis"
    ]
    assert data["results"][0]["sources"] == ["wikipedia"]
    assert data["suggestions"] == ["photosynthesis plants"]
    assert data["cache"] == "miss"
    (sent,) = searx.requests
    assert sent.url.params["q"] == "photosynthesis"
    assert sent.url.params["engines"] == "wikipedia,arxiv,crossref"
    assert sent.url.params["language"] == "en"


def test_second_search_is_a_cache_hit() -> None:
    searx = serving(searx_answer("https://example.org/a"))
    with running(searx) as client:
        first = search(client, {"query": "plants"}).json()
        second = search(client, {"query": "plants"}).json()

    assert (first["data"]["cache"], second["data"]["cache"]) == ("miss", "hit")
    assert second["trace_id"]
    assert len(searx.requests) == 1


def test_search_sends_requested_sources_in_lower_case() -> None:
    searx = serving(searx_answer("https://example.org/a"))
    with running(searx) as client:
        response = search(client, {"query": "plants", "sources": ["Wikipedia", "arxiv"]})

    assert response.status_code == 200
    assert searx.requests[0].url.params["engines"] == "wikipedia,arxiv"


def test_searxng_timeout_is_200_failed_and_retryable() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with running(Searx(timeout)) as client:
        response = search(client, {"query": "plants"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "search_failed"
    assert body["error"]["retryable"] is True
    assert body["trace_id"]


def test_some_sources_failed_is_partial() -> None:
    searx = serving(searx_answer("https://example.org/a", unresponsive=[["arxiv", "timeout"]]))
    with running(searx) as client:
        body = search(client, {"query": "plants"}).json()

    assert body["status"] == "partial"
    assert body["data"]["sources_failed"] == [{"source": "arxiv", "reason": "timeout"}]


def test_search_off_is_400_invalid_request() -> None:
    searx = serving(searx_answer())
    with running(searx, search_enabled=False) as client:
        response = search(client, {"query": "plants"}, ADMIN)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["message"] == "Web search is not enabled on this service."
    assert error["retryable"] is False
    assert searx.requests == []


def test_user_key_without_search_allow_users_is_403() -> None:
    searx = serving(searx_answer("https://example.org/a"))
    with running(searx, search_allow_users=False) as client:
        refused = search(client, {"query": "plants"}, USER)
        admin = search(client, {"query": "plants"}, ADMIN)

    assert refused.status_code == 403
    error = refused.json()["error"]
    assert error["code"] == "forbidden"
    assert error["message"] == "Web search is for admin keys only on this service."
    assert admin.status_code == 200
    assert admin.json()["status"] == "success"
    assert len(searx.requests) == 1


def test_without_max_results_a_low_limit_is_the_default() -> None:
    # SEARCH_MAX_RESULTS below 10: a request that leaves max_results out asks for the limit
    # instead of a 422; an explicit value over it stays a 422.
    urls = [f"https://example.org/{i}" for i in range(8)]
    searx = serving(searx_answer(*urls))
    with running(searx, search_max_results=5) as client:
        implicit = search(client, {"query": "plants"})
        explicit = search(client, {"query": "plants", "max_results": 5})
        over = search(client, {"query": "plants", "max_results": 10})

    assert implicit.status_code == 200
    assert len(implicit.json()["data"]["results"]) == 5
    # The default is applied before the cache key: the same search as max_results 5.
    assert explicit.json()["data"]["cache"] == "hit"
    assert len(searx.requests) == 1
    assert over.status_code == 422


def test_an_invalid_language_is_422() -> None:
    searx = serving(searx_answer())
    with running(searx) as client:
        response = search(client, {"query": "plants", "language": "German"})

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["body", "language"]
    assert searx.requests == []


def test_max_results_over_the_setting_is_422() -> None:
    searx = serving(searx_answer("https://example.org/a"))
    with running(searx) as client:
        over = search(client, {"query": "plants", "max_results": 21})
        at_limit = search(client, {"query": "plants", "max_results": 20})

    assert over.status_code == 422
    error = over.json()["error"]
    assert error["code"] == "invalid_request"
    (detail,) = error["details"]
    assert detail["loc"] == ["body", "max_results"]
    assert "20" in detail["msg"]
    assert "SEARCH_MAX_RESULTS" in detail["msg"]
    assert at_limit.status_code == 200
    assert len(searx.requests) == 1


@pytest.mark.parametrize(
    "sources",
    [["google"], ["wikipedia", "bing"], ["wikipedia,arxiv"], [""], ["  "], ["wiki pedia"]],
)
def test_source_outside_search_sources_is_422_with_the_allowed_names(sources: list[str]) -> None:
    searx = serving(searx_answer("https://example.org/a"))
    with running(searx) as client:
        response = search(client, {"query": "plants", "sources": sources})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    (detail,) = error["details"]
    assert detail["loc"] == ["body", "sources"]
    assert "wikipedia, arxiv, crossref" in detail["msg"]
    assert "SEARCH_SOURCES" in detail["msg"]
    assert searx.requests == []


def test_invalid_body_is_422() -> None:
    with running(serving(searx_answer())) as client:
        empty = search(client, {"query": ""})
        unknown = search(client, {"query": "plants", "engines": ["wikipedia"]})

    assert (empty.status_code, unknown.status_code) == (422, 422)


def test_search_counts_one_hit_against_the_rate_limit() -> None:
    searx = serving(searx_answer("https://example.org/a"))
    with running(searx, rate_limit_per_minute=2) as client:
        statuses = [search(client, {"query": "plants"}).status_code for _ in range(3)]

    assert statuses == [200, 200, 429]


def test_search_needs_a_key() -> None:
    with running(serving(searx_answer())) as client:
        response = client.post("/v1/search", json={"query": "plants"})

    assert response.status_code == 401


def test_app_keeps_one_search_client_and_closes_it() -> None:
    app = create_public_app(
        make_settings(search_timeout_seconds=7), FakeAsyncRedis(), CollectorRegistry()
    )
    http: httpx.AsyncClient = app.state.search_http

    assert http.timeout.read == 7
    assert http.follow_redirects is False
    with TestClient(app):
        assert not http.is_closed
    assert http.is_closed


def test_app_has_no_search_client_while_search_is_off() -> None:
    app = create_public_app(
        make_settings(search_enabled=False), FakeAsyncRedis(), CollectorRegistry()
    )
    assert app.state.search_http is None
    with TestClient(app):
        pass


def test_core_answers_like_the_route() -> None:
    searx = serving(searx_answer("https://example.org/a"))
    with running(searx, search_cache_seconds=0) as client:
        rest = search(client, {"query": "plants"}).json()
        state = cast(FastAPI, client.app).state
        principal = principal_for(state.settings, f"Bearer {USER_KEY}")

        portal = client.portal
        assert portal is not None
        envelope = portal.call(partial(run_search, state, principal, SearchRequest(query="plants")))

    answer = envelope.model_dump(mode="json")
    rest.pop("trace_id")
    answer.pop("trace_id")
    assert answer == rest


def test_empty_sources_is_422() -> None:
    """``sources: []`` is refused, not read as every source (Task 2 review, ruling)."""
    searx = serving(searx_answer("https://example.org/a"))
    with running(searx) as client:
        response = search(client, {"query": "plants", "sources": []})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    (detail,) = error["details"]
    assert detail["loc"] == ["body", "sources"]
    assert "at least one source" in detail["msg"]
    assert "leave sources out" in detail["msg"]
    assert searx.requests == []


def test_core_refuses_while_search_is_off() -> None:
    """The core checks the switch itself: a caller that skips ``check_search_enabled`` gets the
    400, not a 500 from the missing client (Task 2 review, ruling)."""
    with running(search_enabled=False) as client:
        state = cast(FastAPI, client.app).state
        principal = principal_for(state.settings, f"Bearer {ADMIN_KEY}")
        portal = client.portal
        assert portal is not None
        with pytest.raises(ServiceError) as raised:
            portal.call(partial(run_search, state, principal, SearchRequest(query="plants")))

    assert (raised.value.code, raised.value.http_status) == ("invalid_request", 400)
    assert raised.value.message == SEARCH_DISABLED_MESSAGE
