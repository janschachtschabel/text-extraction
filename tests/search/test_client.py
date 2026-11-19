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
async def test_markup_in_snippet_and_title_becomes_plain_text(settings: Settings) -> None:
    # Crossref and OpenAlex hand on JATS XML; Wikipedia marks the match with <span>.
    body = answer(
        [
            hit(
                "https://a.example/",
                title="Learning <i>objects</i> &amp; portals",
                content=(
                    "<jats:title>Abstract</jats:title> <jats:p>For <jats:italic>learning\n"
                    "objects</jats:italic> , see a &lt; b and 3 <  4.</jats:p>"
                ),
            )
        ]
    )

    envelope = await search(SearchRequest(query="q"), settings, serving(body).client(), None)

    assert envelope.data is not None
    [first] = envelope.data.results
    assert first.title == "Learning objects & portals"
    assert first.snippet == "Abstract For learning objects , see a < b and 3 < 4."


@runs_async
async def test_inline_markup_inside_a_word_keeps_the_word(settings: Settings) -> None:
    body = answer(
        [
            hit(
                "https://a.example/",
                content='<span class="searchmatch">Photo</span>synthese ist<br/>ein Prozess.',
            )
        ]
    )

    envelope = await search(SearchRequest(query="q"), settings, serving(body).client(), None)

    assert envelope.data is not None
    assert envelope.data.results[0].snippet == "Photosynthese ist ein Prozess."


@runs_async
async def test_snippet_of_markup_only_is_none(settings: Settings) -> None:
    body = answer([hit("https://a.example/", content="<jats:p> </jats:p>")])

    envelope = await search(SearchRequest(query="q"), settings, serving(body).client(), None)

    assert envelope.data is not None
    assert envelope.data.results[0].snippet is None


@runs_async
async def test_urls_that_are_not_absolute_http_are_dropped(settings: Settings) -> None:
    body = answer(
        [
            hit("magnet:?xt=urn:btih:abc"),
            hit("/relative/path"),
            hit("javascript:alert(1)"),
            hit("https:///nohost"),
            hit("ftp://files.example/x"),
            hit("http://ok.example/a"),
            hit("https://ok.example/b"),
            {"title": "no url"},
        ]
    )

    envelope = await search(SearchRequest(query="q"), settings, serving(body).client(), None)

    assert envelope.data is not None
    assert [h.url for h in envelope.data.results] == ["http://ok.example/a", "https://ok.example/b"]


@runs_async
async def test_results_are_cut_to_max_results(settings: Settings) -> None:
    body = answer([hit(f"https://a.example/{i}") for i in range(8)])

    envelope = await search(
        SearchRequest(query="q", max_results=3), settings, serving(body).client(), None
    )

    assert envelope.data is not None
    assert [h.url for h in envelope.data.results] == [f"https://a.example/{i}" for i in range(3)]


@runs_async
async def test_the_top_hits_of_every_source_come_first(settings: Settings) -> None:
    # SearXNG sorts by its score, which favours hits several engines found; the service lists
    # each source's first hit, then each source's second, and so on (best position of a hit).
    body = answer(
        [
            hit("https://both.example/", engines=["bing", "duckduckgo"], positions=[3, 2]),
            hit("https://bing.example/1", engines=["bing"], positions=[1]),
            hit("https://ddg.example/1", engines=["duckduckgo"], positions=[1]),
            hit("https://wiki.example/", engines=["wikipedia"], positions=[1]),
            hit("https://bing.example/2", engines=["bing"], positions=[2]),
            hit("https://no-position.example/", engines=["mojeek"]),
        ]
    )

    envelope = await search(SearchRequest(query="q"), settings, serving(body).client(), None)

    assert envelope.data is not None
    assert [h.url for h in envelope.data.results] == [
        "https://bing.example/1",
        "https://ddg.example/1",
        "https://wiki.example/",
        "https://both.example/",
        "https://bing.example/2",
        "https://no-position.example/",
    ]


@runs_async
async def test_the_ranking_comes_before_the_cut_to_max_results(settings: Settings) -> None:
    body = answer(
        [
            hit("https://bing.example/2", engines=["bing"], positions=[2]),
            hit("https://ddg.example/1", engines=["duckduckgo"], positions=[1]),
        ]
    )

    envelope = await search(
        SearchRequest(query="q", max_results=1), settings, serving(body).client(), None
    )

    assert envelope.data is not None
    assert [h.url for h in envelope.data.results] == ["https://ddg.example/1"]


@runs_async
async def test_duplicates_are_merged_into_the_best_ranked_hit(settings: Settings) -> None:
    # SearXNG merges only URLs that match without the scheme; www, a trailing slash, the
    # fragment and utm_ parameters still split one page into several hits.
    body = answer(
        [
            hit(
                "https://www.Example.org/page/?utm_source=x&id=1#top",
                engines=["bing"],
                positions=[2],
                title="First",
                publishedDate=None,
                score=1.0,
            ),
            hit(
                "http://example.org/page?id=1",
                engines=["duckduckgo"],
                positions=[1],
                title="Second",
                publishedDate="2026-01-01",
                score=3.0,
            ),
            hit("https://example.org/page?id=2", engines=["mojeek"], positions=[1]),
        ]
    )

    envelope = await search(SearchRequest(query="q"), settings, serving(body).client(), None)

    assert envelope.data is not None
    first, other = envelope.data.results
    # The best ranked hit's title, snippet and place; the https address of its duplicate.
    assert first.url == "https://www.Example.org/page/?utm_source=x&id=1#top"
    assert first.title == "Second"
    assert first.sources == ["duckduckgo", "bing"]
    assert first.published == "2026-01-01"
    assert first.score == 3.0
    assert other.url == "https://example.org/page?id=2"


@pytest.mark.parametrize(
    ("one", "other"),
    [
        ("https://a.example/?UTM_Source=x&b=1&a=2", "https://a.example?a=2&b=1"),
        ("https://a.example:443/p", "https://a.example/p"),
        ("http://a.example:80/p", "https://a.example/p"),
    ],
)
@runs_async
async def test_more_spellings_of_one_page_are_merged(
    settings: Settings, one: str, other: str
) -> None:
    body = answer(
        [
            hit(one, engines=["bing"], positions=[1]),
            hit(other, engines=["mojeek"], positions=[1]),
        ]
    )

    envelope = await search(SearchRequest(query="q"), settings, serving(body).client(), None)

    assert envelope.data is not None
    assert [h.sources for h in envelope.data.results] == [["bing", "mojeek"]]


@pytest.mark.parametrize(
    ("one", "other"),
    [
        ("https://a.example/#/p1", "https://a.example/#/p2"),  # routes of a single-page app
        ("https://a.example/#!p1", "https://a.example/#!p2"),
        ("https://a.example:8443/p", "https://a.example/p"),
    ],
)
@runs_async
async def test_different_pages_stay_apart(settings: Settings, one: str, other: str) -> None:
    body = answer(
        [
            hit(one, engines=["bing"], positions=[1]),
            hit(other, engines=["mojeek"], positions=[1]),
        ]
    )

    envelope = await search(SearchRequest(query="q"), settings, serving(body).client(), None)

    assert envelope.data is not None
    assert [h.url for h in envelope.data.results] == [one, other]


@runs_async
async def test_a_merged_hit_keeps_an_address_without_credentials(settings: Settings) -> None:
    body = answer(
        [
            hit("https://user:pw@a.example/p", engines=["bing"], positions=[1]),
            hit("https://a.example/p", engines=["mojeek"], positions=[2]),
        ]
    )

    envelope = await search(SearchRequest(query="q"), settings, serving(body).client(), None)

    assert envelope.data is not None
    assert [h.url for h in envelope.data.results] == ["https://a.example/p"]


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
    assert envelope.error.message == "No search source answered."
    # A failed answer has no data, so the failed sources travel in the error.
    assert envelope.error.details == [
        {"source": "wikipedia", "reason": "timeout"},
        {"source": "arxiv", "reason": "HTTP error"},
    ]


@runs_async
async def test_no_hits_and_unresponsive_names_in_other_case_is_failed(settings: Settings) -> None:
    body = answer([], unresponsive=[["Wikipedia", "timeout"], ["ArXiv", "HTTP error"]])

    envelope = await search(
        SearchRequest(query="q", sources=["wikipedia", "arxiv"]),
        settings,
        serving(body).client(),
        None,
    )

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == "search_failed"
    assert envelope.error.retryable is True
    assert envelope.error.details == [
        {"source": "Wikipedia", "reason": "timeout"},
        {"source": "ArXiv", "reason": "HTTP error"},
    ]


@runs_async
async def test_every_configured_engine_unresponsive_is_failed(settings: Settings) -> None:
    unresponsive = [[name, "timeout"] for name in settings.search_sources]

    envelope = await search(
        SearchRequest(query="q"), settings, serving(answer([], unresponsive)).client(), None
    )

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.details is not None
    assert [d["source"] for d in envelope.error.details] == settings.search_sources


@runs_async
async def test_the_only_asked_source_blocked_is_failed_and_named(settings: Settings) -> None:
    body = answer([], unresponsive=[["mwmbl", "Suspended: too many requests"]])

    envelope = await search(
        SearchRequest(query="q", sources=["mwmbl"]), settings, serving(body).client(), None
    )

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == "search_failed"
    assert envelope.error.details == [{"source": "mwmbl", "reason": "Suspended: too many requests"}]


@runs_async
async def test_no_hits_and_some_sources_failed_is_an_empty_partial(settings: Settings) -> None:
    # Seven sources answered without a hit, one was suspended: the query has no hits, which is
    # no failure of the search; the blocked source is listed.
    body = answer([], unresponsive=[["mwmbl", "Suspended: too many requests"]])

    envelope = await search(SearchRequest(query="q"), settings, serving(body).client(), None)

    assert envelope.status == "partial"
    assert envelope.error is None
    assert envelope.data is not None
    assert envelope.data.results == []
    assert [(f.source, f.reason) for f in envelope.data.sources_failed] == [
        ("mwmbl", "Suspended: too many requests")
    ]


@runs_async
async def test_no_hits_and_a_requested_engine_in_neither_list_is_partial(
    settings: Settings,
) -> None:
    body = answer([], unresponsive=[["arxiv", "timeout"]])

    envelope = await search(
        SearchRequest(query="q", sources=["wikipedia", "arxiv"]),
        settings,
        serving(body).client(),
        None,
    )

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


@runs_async
async def test_400_is_not_retryable(settings: Settings) -> None:
    # SearXNG refuses a parameter it does not accept: asking again changes nothing.
    envelope = await search(
        SearchRequest(query="q"), settings, serving({}, status=400).client(), None
    )

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == "search_failed"
    assert envelope.error.retryable is False
    assert "400" in envelope.error.message


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
async def test_partial_is_not_cached(settings: Settings) -> None:
    searx = serving(answer([hit("https://a.example/")], unresponsive=[["arxiv", "timeout"]]))
    valkey = FakeAsyncRedis()

    await search(SearchRequest(query="q"), settings, searx.client(), valkey)
    second = await search(SearchRequest(query="q"), settings, searx.client(), valkey)

    assert second.status == "partial"
    assert second.data is not None
    assert second.data.cache == "miss"
    assert len(searx.requests) == 2
    assert await valkey.keys("*") == []


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
        {"query": "q", "language": "german"},
        {"query": "q", "language": "DE"},
        {"query": "q", "language": "de_DE"},
        {"query": "q", "language": "d"},
        {"query": "q", "language": "de-DEU"},
        {"query": "q", "language": "de-"},
    ],
)
def test_request_bounds(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SearchRequest(**bad)


@pytest.mark.parametrize("language", ["de", "en", "deu", "de-DE", "pt-br", "all"])
def test_language_takes_what_searxng_accepts(language: str) -> None:
    # SearXNG's VALID_LANGUAGE_CODE (searx/webutils.py): 2-3 lower-case letters and an optional
    # region of 2 letters. Anything else it answers with HTTP 400.
    assert SearchRequest(query="q", language=language).language == language


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
        "bing",
        "duckduckgo",
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
