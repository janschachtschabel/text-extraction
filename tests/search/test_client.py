"""The SearXNG client (``search/client.py``) against ``httpx.MockTransport`` as SearXNG: the
request it sends, how the answer maps to hits, the failure envelopes and the Valkey cache."""

import asyncio
import functools
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from pydantic import ValidationError
from redis.exceptions import RedisError
from structlog.testing import capture_logs

from text_extraction.search.client import search
from text_extraction.search.types import SearchRequest
from text_extraction.settings import Settings

Handler = Callable[[httpx.Request], httpx.Response]


def runs_async[**P](test: Callable[P, Coroutine[Any, Any, None]]) -> Callable[P, None]:
    """Run an ``async def`` test to completion (the project has no async pytest plugin)."""

    @functools.wraps(test)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        asyncio.run(test(*args, **kwargs))

    return wrapper


def hit(url: str, **extra: Any) -> dict[str, Any]:
    return {
        "url": url,
        "title": f"Title of {url}",
        "content": "A snippet.",
        "engine": "wikipedia",
        "engines": ["wikipedia", "mwmbl"],
        "score": 2.5,
        "category": "general",
        **extra,
    }


def answer(
    results: list[dict[str, Any]] | None = None,
    unresponsive: list[list[str]] | None = None,
    suggestions: list[str] | None = None,
) -> dict[str, Any]:
    results = results if results is not None else []
    return {
        "query": "q",
        "number_of_results": len(results),
        "results": results,
        "answers": [],
        "corrections": [],
        "infoboxes": [],
        "suggestions": suggestions or [],
        "unresponsive_engines": unresponsive or [],
    }


class Searx:
    """A fake SearXNG that records its requests."""

    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def client(self) -> httpx.AsyncClient:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self._handler(request)

        return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def serving(body: dict[str, Any], status: int = 200) -> Searx:
    return Searx(lambda _request: httpx.Response(status, json=body))


@pytest.fixture
def settings() -> Settings:
    return Settings(auth_mode="none", search_enabled=True, search_url="http://searxng:8080")


@runs_async
async def test_request_is_a_get_with_the_documented_parameters(settings: Settings) -> None:
    searx = serving(answer([hit("https://a.example/")]))
    request = SearchRequest(
        query="open data", language="de", page=2, time_range="month", sources=["wikipedia", "arxiv"]
    )

    await search(request, settings, searx.client(), None)

    sent = searx.requests[0]
    assert sent.method == "GET"
    assert sent.url.host == "searxng"
    assert sent.url.path == "/search"
    assert dict(sent.url.params) == {
        "q": "open data",
        "format": "json",
        "pageno": "2",
        "language": "de",
        "engines": "wikipedia,arxiv",
        "time_range": "month",
    }


@runs_async
async def test_without_sources_the_configured_engines_are_used(settings: Settings) -> None:
    searx = serving(answer([hit("https://a.example/")]))

    await search(SearchRequest(query="x"), settings, searx.client(), None)

    params = searx.requests[0].url.params
    assert params["engines"] == ",".join(settings.search_sources)
    assert "language" not in params
    assert "time_range" not in params
    assert params["pageno"] == "1"


@runs_async
async def test_hits_are_mapped(settings: Settings) -> None:
    body = answer(
        [hit("https://a.example/x", publishedDate="2024-05-01T00:00:00")],
        suggestions=["open data portal"],
    )

    envelope = await search(SearchRequest(query="q"), settings, serving(body).client(), None)

    assert envelope.status == "success"
    assert envelope.error is None
    assert envelope.data is not None
    result = envelope.data
    assert result.query == "q"
    assert result.suggestions == ["open data portal"]
    assert result.sources_failed == []
    assert result.cache == "bypass"
    [first] = result.results
    assert first.url == "https://a.example/x"
    assert first.title == "Title of https://a.example/x"
    assert first.snippet == "A snippet."
    assert first.sources == ["wikipedia", "mwmbl"]
    assert first.published == "2024-05-01T00:00:00"
    assert first.score == 2.5


@runs_async
async def test_sparse_hit_gets_defaults(settings: Settings) -> None:
    body = answer([{"url": "https://a.example/", "engine": "arxiv"}])

    envelope = await search(SearchRequest(query="q"), settings, serving(body).client(), None)

    assert envelope.data is not None
    [first] = envelope.data.results
    assert first.title == "https://a.example/"
    assert first.snippet is None
    assert first.sources == ["arxiv"]
    assert first.published is None
    assert first.score is None


@runs_async
async def test_urls_that_are_not_absolute_http_are_dropped(settings: Settings) -> None:
    body = answer(
        [
            hit("magnet:?xt=urn:btih:abc"),
            hit("/relative/path"),
            hit("javascript:alert(1)"),
            hit("https:///nohost"),
            hit("ftp://files.example/x"),
            hit("http://ok.example/"),
            hit("https://ok.example/"),
            {"title": "no url"},
        ]
    )

    envelope = await search(SearchRequest(query="q"), settings, serving(body).client(), None)

    assert envelope.data is not None
    assert [h.url for h in envelope.data.results] == ["http://ok.example/", "https://ok.example/"]


@runs_async
async def test_results_are_cut_to_max_results(settings: Settings) -> None:
    body = answer([hit(f"https://a.example/{i}") for i in range(8)])

    envelope = await search(
        SearchRequest(query="q", max_results=3), settings, serving(body).client(), None
    )

    assert envelope.data is not None
    assert [h.url for h in envelope.data.results] == [f"https://a.example/{i}" for i in range(3)]


@runs_async
async def test_some_sources_failed_is_partial(settings: Settings) -> None:
    body = answer([hit("https://a.example/")], unresponsive=[["arxiv", "timeout"]])

    envelope = await search(SearchRequest(query="q"), settings, serving(body).client(), None)

    assert envelope.status == "partial"
    assert envelope.error is None
    assert envelope.data is not None
    assert [(f.source, f.reason) for f in envelope.data.sources_failed] == [("arxiv", "timeout")]
    assert len(envelope.data.results) == 1


@runs_async
async def test_no_hits_and_every_engine_unresponsive_is_failed(settings: Settings) -> None:
    sources = ["wikipedia", "arxiv"]
    body = answer([], unresponsive=[["wikipedia", "timeout"], ["arxiv", "HTTP error"]])

    envelope = await search(
        SearchRequest(query="q", sources=sources), settings, serving(body).client(), None
    )

    assert envelope.status == "failed"
    assert envelope.data is None
    assert envelope.error is not None
    assert envelope.error.code == "search_failed"
    assert envelope.error.retryable is True


@runs_async
async def test_no_hits_and_one_engine_unresponsive_is_partial_and_empty(
    settings: Settings,
) -> None:
    body = answer([], unresponsive=[["arxiv", "timeout"]])

    envelope = await search(SearchRequest(query="q"), settings, serving(body).client(), None)

    assert envelope.status == "partial"
    assert envelope.data is not None
    assert envelope.data.results == []
    assert [f.source for f in envelope.data.sources_failed] == ["arxiv"]


@runs_async
async def test_no_hits_and_nobody_failed_is_an_empty_success(settings: Settings) -> None:
    envelope = await search(SearchRequest(query="q"), settings, serving(answer([])).client(), None)

    assert envelope.status == "success"
    assert envelope.data is not None
    assert envelope.data.results == []


@pytest.mark.parametrize(
    "error", [httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow")], ids=type
)
@runs_async
async def test_timeout_is_failed_and_retryable(settings: Settings, error: Exception) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise error

    envelope = await search(SearchRequest(query="q"), settings, Searx(handler).client(), None)

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == "search_failed"
    assert envelope.error.retryable is True


@runs_async
async def test_connection_error_is_failed_and_retryable(settings: Settings) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    envelope = await search(SearchRequest(query="q"), settings, Searx(handler).client(), None)

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == "search_failed"
    assert envelope.error.retryable is True


@runs_async
async def test_403_names_the_json_format_setting(settings: Settings) -> None:
    envelope = await search(
        SearchRequest(query="q"), settings, serving({}, status=403).client(), None
    )

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == "search_failed"
    assert "search.formats" in envelope.error.message


@pytest.mark.parametrize("status", [429, 500, 502])
@runs_async
async def test_other_http_errors_are_failed_and_retryable(settings: Settings, status: int) -> None:
    envelope = await search(
        SearchRequest(query="q"), settings, serving({}, status=status).client(), None
    )

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == "search_failed"
    assert envelope.error.retryable is True


@runs_async
async def test_an_answer_that_is_not_json_is_failed(settings: Settings) -> None:
    searx = Searx(lambda _r: httpx.Response(200, text="<html>not json</html>"))

    envelope = await search(SearchRequest(query="q"), settings, searx.client(), None)

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == "search_failed"


@runs_async
async def test_the_error_does_not_echo_the_query_or_the_url(settings: Settings) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused http://searxng:8080/search?q=secret-term")

    envelope = await search(
        SearchRequest(query="secret-term"), settings, Searx(handler).client(), None
    )

    assert envelope.error is not None
    assert "secret-term" not in envelope.error.message
    assert "searxng" not in envelope.error.message


# Cache


@runs_async
async def test_second_identical_search_is_a_cache_hit(settings: Settings) -> None:
    searx = serving(answer([hit("https://a.example/")]))
    valkey = FakeAsyncRedis()
    request = SearchRequest(query="q")

    first = await search(request, settings, searx.client(), valkey)
    second = await search(request, settings, searx.client(), valkey)

    assert first.data is not None
    assert second.data is not None
    assert first.data.cache == "miss"
    assert second.data.cache == "hit"
    assert second.data.results == first.data.results
    assert second.status == "success"
    assert len(searx.requests) == 1


@runs_async
async def test_cache_entry_has_the_ttl(settings: Settings) -> None:
    valkey = FakeAsyncRedis()

    await search(
        SearchRequest(query="q"), settings, serving(answer([hit("https://a/")])).client(), valkey
    )

    [key] = await valkey.keys("*")
    assert 0 < await valkey.ttl(key) <= settings.search_cache_seconds


@pytest.mark.parametrize(
    "other",
    [
        {"page": 2},
        {"language": "de"},
        {"time_range": "year"},
        {"sources": ["arxiv"]},
        {"max_results": 3},
        {"query": "other"},
    ],
    ids=lambda o: next(iter(o)),
)
@runs_async
async def test_any_other_parameter_is_a_cache_miss(
    settings: Settings, other: dict[str, Any]
) -> None:
    searx = serving(answer([hit("https://a.example/")]))
    valkey = FakeAsyncRedis()

    await search(SearchRequest(query="q"), settings, searx.client(), valkey)
    second = await search(
        SearchRequest(**{"query": "q", **other}), settings, searx.client(), valkey
    )

    assert second.data is not None
    assert second.data.cache == "miss"
    assert len(searx.requests) == 2


@runs_async
async def test_source_order_and_query_spacing_do_not_matter_for_the_key(
    settings: Settings,
) -> None:
    searx = serving(answer([hit("https://a.example/")]))
    valkey = FakeAsyncRedis()

    await search(
        SearchRequest(query="open data", sources=["arxiv", "wikipedia"]),
        settings,
        searx.client(),
        valkey,
    )
    second = await search(
        SearchRequest(query="  open   data ", sources=["wikipedia", "arxiv"]),
        settings,
        searx.client(),
        valkey,
    )

    assert second.data is not None
    assert second.data.cache == "hit"


@runs_async
async def test_cache_off_bypasses_valkey() -> None:
    settings = Settings(auth_mode="none", search_enabled=True, search_cache_seconds=0)
    searx = serving(answer([hit("https://a.example/")]))
    valkey = FakeAsyncRedis()

    first = await search(SearchRequest(query="q"), settings, searx.client(), valkey)
    second = await search(SearchRequest(query="q"), settings, searx.client(), valkey)

    assert first.data is not None
    assert second.data is not None
    assert first.data.cache == second.data.cache == "bypass"
    assert len(searx.requests) == 2
    assert await valkey.keys("*") == []


@runs_async
async def test_partial_is_cached_and_stays_partial(settings: Settings) -> None:
    searx = serving(answer([hit("https://a.example/")], unresponsive=[["arxiv", "timeout"]]))
    valkey = FakeAsyncRedis()

    await search(SearchRequest(query="q"), settings, searx.client(), valkey)
    second = await search(SearchRequest(query="q"), settings, searx.client(), valkey)

    assert second.status == "partial"
    assert second.data is not None
    assert second.data.cache == "hit"
    assert [f.source for f in second.data.sources_failed] == ["arxiv"]


@runs_async
async def test_failed_answers_are_not_cached(settings: Settings) -> None:
    valkey = FakeAsyncRedis()

    await search(SearchRequest(query="q"), settings, serving({}, status=502).client(), valkey)

    assert await valkey.keys("*") == []


class BrokenValkey:
    async def get(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RedisError("down")

    async def set(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RedisError("down")


@runs_async
async def test_valkey_errors_are_logged_and_ignored(settings: Settings) -> None:
    searx = serving(answer([hit("https://a.example/")]))

    with capture_logs() as logs:
        envelope = await search(SearchRequest(query="q"), settings, searx.client(), BrokenValkey())  # type: ignore[arg-type]

    assert envelope.status == "success"
    assert envelope.data is not None
    assert envelope.data.cache == "miss"
    events = [entry["event"] for entry in logs]
    assert "search_cache_read_failed" in events
    assert "search_cache_write_failed" in events


@runs_async
async def test_an_unreadable_cache_entry_is_a_miss(settings: Settings) -> None:
    searx = serving(answer([hit("https://a.example/")]))
    valkey = FakeAsyncRedis()
    await search(SearchRequest(query="q"), settings, searx.client(), valkey)
    [key] = await valkey.keys("*")
    await valkey.set(key, b"not json")

    envelope = await search(SearchRequest(query="q"), settings, searx.client(), valkey)

    assert envelope.data is not None
    assert envelope.data.cache == "miss"
    assert len(searx.requests) == 2


# Models and settings


@pytest.mark.parametrize(
    "bad",
    [
        {"query": ""},
        {"query": "x" * 501},
        {"query": "q", "max_results": 0},
        {"query": "q", "page": 0},
        {"query": "q", "page": 11},
        {"query": "q", "time_range": "week"},
        {"query": "q", "unknown": 1},
    ],
)
def test_request_bounds(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SearchRequest(**bad)


def test_request_defaults() -> None:
    request = SearchRequest(query="q")

    assert request.language is None
    assert request.time_range is None
    assert request.sources is None


def test_search_settings_defaults() -> None:
    settings = Settings(auth_mode="none")

    assert settings.search_enabled is False
    assert settings.search_url == "http://searxng:8080"
    assert settings.search_timeout_seconds == 10
    assert settings.search_max_results == 50
    assert settings.search_cache_seconds == 3600
    assert settings.search_allow_users is True
    assert settings.search_sources == [
        "wikipedia",
        "wikibooks",
        "wikiversity",
        "wikinews",
        "mwmbl",
        "openalex",
        "arxiv",
        "crossref",
    ]


def test_search_sources_parse_from_a_comma_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_SOURCES", " Wikipedia, arxiv ,,arxiv")

    assert Settings(auth_mode="none").search_sources == ["wikipedia", "arxiv"]


@pytest.mark.parametrize("value", ["", ",", "bad;name"])
def test_search_sources_reject_nonsense(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(auth_mode="none", search_sources=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("url", ["ftp://searxng", "searxng:8080", "http://", "/search", ""])
def test_search_url_must_be_http_with_a_host(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(auth_mode="none", search_url=url)


def test_search_url_loses_the_trailing_slash() -> None:
    assert (
        Settings(auth_mode="none", search_url="https://s.example/base/").search_url
        == "https://s.example/base"
    )
