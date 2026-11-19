"""The live web search check (`tests/live/run_search.py`, package 25): its assertions, retries,
cache check and summary, against a fake service on `httpx.MockTransport`."""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.live import run_search
from tests.live.run_search import Report, check_engines, check_failure, check_hits, summarize

WIKI = {"url": "https://de.wikipedia.org/wiki/Photosynthese", "sources": ["wikipedia"]}
ARXIV = {"url": "https://arxiv.org/abs/1234.5678", "sources": ["arxiv", "openalex"]}
BING = {"url": "https://www.example.org/bing", "sources": ["bing"]}
DDG = {"url": "https://www.example.org/ddg", "sources": ["duckduckgo", "bing"]}


def envelope(
    *hits: dict[str, Any],
    status: str = "success",
    cache: str = "miss",
    failed: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "data": {
            "query": "Photosynthese",
            "results": [{"title": "t", **hit} for hit in hits],
            "suggestions": [],
            "sources_failed": failed or [],
            "cache": cache,
        },
    }


DOWN: dict[str, Any] = {
    "status": "failed",
    "error": {"code": "search_failed", "message": "not reachable", "retryable": True},
}

Answer = Callable[[dict[str, Any], int], tuple[int, dict[str, Any]]]


class FakeService:
    """`POST /v1/search` and `POST /mcp` (tools/call web_search) answering ``answer(body, n)``,
    where n counts the searches so far; MCP wraps the envelope as structuredContent."""

    def __init__(self, answer: Answer, *, mcp_is_error: bool = False) -> None:
        self.answer = answer
        self.mcp_is_error = mcp_is_error
        self.bodies: list[tuple[str, dict[str, Any]]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        if request.url.path == "/mcp":
            assert sent["method"] == "tools/call"
            assert sent["params"]["name"] == "web_search"
            assert "text/event-stream" in request.headers["accept"]
            body = sent["params"]["arguments"]
            self.bodies.append(("mcp", body))
            status, answer = self.answer(body, len(self.bodies))
            result = {"content": [], "structuredContent": answer, "isError": self.mcp_is_error}
            return httpx.Response(status, json={"jsonrpc": "2.0", "id": 1, "result": result})
        assert request.url.path == "/v1/search"
        self.bodies.append(("rest", sent))
        status, answer = self.answer(sent, len(self.bodies))
        return httpx.Response(status, json=answer)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="http://service", transport=httpx.MockTransport(self.handle)
        )


def cached_after_first(answer: dict[str, Any]) -> Answer:
    """``answer`` as a miss, then as a hit for a body asked before (a success is cached)."""
    seen: list[dict[str, Any]] = []

    def respond(body: dict[str, Any], _n: int) -> tuple[int, dict[str, Any]]:
        hit = body in seen and answer["status"] == "success"
        seen.append(body)
        return 200, {**answer, "data": {**answer["data"], "cache": "hit" if hit else "miss"}}

    return respond


def hits_report(service: FakeService, retries: int = 3) -> tuple[Report, list[float]]:
    pauses: list[float] = []

    async def sleep(seconds: float) -> None:
        pauses.append(seconds)

    async def run() -> Report:
        async with service.client() as client:
            return await check_hits(client, "Photosynthese", "de", retries, 7.0, sleep)

    return asyncio.run(run()), pauses


def failure_report(service: FakeService) -> Report:
    async def run() -> Report:
        async with service.client() as client:
            return await check_failure(client, "Chlorophyll", "de")

    return asyncio.run(run())


def test_hits_from_wikipedia_and_another_source_pass() -> None:
    service = FakeService(cached_after_first(envelope(WIKI, ARXIV)))

    report, pauses = hits_report(service)

    assert report.problems == []
    assert pauses == []
    body = {"query": "Photosynthese", "language": "de", "max_results": 10}
    # REST, then the tool, then the identical REST call that must come from the cache.
    assert service.bodies == [("rest", body), ("mcp", body), ("rest", body)]
    assert [call.label for call in report.calls][-1] == "REST again (cache)"


@pytest.mark.parametrize(
    ("hits", "problem"),
    [
        ((ARXIV,), "no hit from wikipedia on de.wikipedia.org"),
        (({"url": "https://en.wikipedia.org/wiki/P", "sources": ["wikipedia"]}, ARXIV), "no hit"),
        ((WIKI,), "no hit from a source other than wikipedia"),
        ((WIKI, ARXIV, {"url": "magnet:?xt=1", "sources": ["mwmbl"]}), "not http(s)"),
    ],
)
def test_missing_hits_fail_after_the_retries(
    hits: tuple[dict[str, Any], ...], problem: str
) -> None:
    service = FakeService(lambda _body, _n: (200, envelope(*hits)))

    report, pauses = hits_report(service, retries=3)

    assert report.problems == ["no try of 4 had the expected hits"]
    assert problem in " ".join(p for call in report.calls for p in call.problems)
    assert pauses == [7.0, 7.0, 7.0]
    # Each try asks one more hit, so it is never the cached answer of the try before.
    sizes = [body["max_results"] for kind, body in service.bodies if kind == "rest"]
    assert sizes == [10, 11, 12, 13]


def test_a_later_try_with_the_hits_passes() -> None:
    def respond(body: dict[str, Any], _n: int) -> tuple[int, dict[str, Any]]:
        if body["max_results"] == 10:
            return 200, envelope(WIKI)
        return 200, envelope(WIKI, ARXIV, cache="hit" if _n > 4 else "miss")

    report, pauses = hits_report(FakeService(respond))

    assert report.problems == []
    assert pauses == [7.0]


def test_a_failed_search_is_not_accepted() -> None:
    report, _ = hits_report(FakeService(lambda _b, _n: (200, DOWN)), retries=0)

    assert report.problems == ["no try of 1 had the expected hits"]
    assert "status failed (search_failed)" in report.calls[0].problems


def test_a_5xx_is_never_accepted() -> None:
    report, _ = hits_report(FakeService(lambda _b, _n: (502, envelope(WIKI, ARXIV))), retries=0)

    assert report.problems
    assert "HTTP 502, expected 200" in report.calls[0].problems


def test_an_mcp_error_result_fails() -> None:
    service = FakeService(cached_after_first(envelope(WIKI, ARXIV)), mcp_is_error=True)

    report, _ = hits_report(service, retries=0)

    assert report.problems
    assert "isError True, expected false" in report.calls[1].problems


def test_a_partial_answer_checks_the_cache_with_wikipedia_only() -> None:
    failed = [{"source": "mwmbl", "reason": "Suspended: too many requests"}]
    partial = envelope(WIKI, ARXIV, status="partial", failed=failed)

    def respond(body: dict[str, Any], _n: int) -> tuple[int, dict[str, Any]]:
        if body.get("sources") == ["wikipedia"]:
            first = sum(1 for _kind, b in service.bodies if b == body) == 1
            return 200, envelope(WIKI, cache="miss" if first else "hit")
        return 200, partial

    service = FakeService(respond)

    report, _ = hits_report(service)

    assert report.problems == []
    wiki_only = {"query": "Photosynthese", "language": "de", "sources": ["wikipedia"]}
    assert service.bodies[-2:] == [("rest", wiki_only), ("rest", wiki_only)]
    assert "mwmbl: Suspended: too many requests" in summarize("t", report)


def test_no_cache_hit_fails() -> None:
    service = FakeService(lambda _b, _n: (200, envelope(WIKI, ARXIV)))

    report, _ = hits_report(service)

    assert report.problems == ["REST again (cache): data.cache miss, expected hit"]


def test_stopped_searxng_must_answer_a_retryable_search_failed() -> None:
    service = FakeService(lambda _b, _n: (200, DOWN))

    report = failure_report(service)

    assert report.problems == []
    body = {"query": "Chlorophyll", "language": "de"}
    assert service.bodies == [("rest", body), ("mcp", body)]  # once each, never retried


@pytest.mark.parametrize(
    ("status", "answer", "problem"),
    [
        (500, DOWN, "HTTP 500, expected 200"),
        (200, envelope(WIKI), "status success, expected failed"),
        (200, {**DOWN, "error": {**DOWN["error"], "retryable": False}}, "retryable is not"),
        (200, {**DOWN, "error": {**DOWN["error"], "code": "internal_error"}}, "error.code"),
    ],
)
def test_a_wrong_failure_fails(status: int, answer: dict[str, Any], problem: str) -> None:
    report = failure_report(FakeService(lambda _b, _n: (status, answer)))

    assert any(problem in p for p in report.problems)
    assert len(report.calls) == 2


def test_a_failure_with_an_mcp_error_result_fails() -> None:
    report = failure_report(FakeService(lambda _b, _n: (200, DOWN), mcp_is_error=True))

    assert report.problems == ["MCP, SearXNG stopped: isError True, expected false"]


def test_summary_counts_hits_per_source() -> None:
    service = FakeService(cached_after_first(envelope(WIKI, ARXIV)))
    report, _ = hits_report(service)

    summary = summarize("Web search", report)

    assert summary.startswith("#### Web search\n")
    assert (
        "| REST try 1 | 200 | success | miss | 2 | arxiv 1, openalex 1, wikipedia 1 | - | - |"
        in (summary)
    )
    assert "**Result:** ok" in summary


def test_main_writes_the_summary_and_exits_1_on_a_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = FakeService(lambda _b, _n: (200, envelope(WIKI, ARXIV)))  # never a cache hit
    monkeypatch.setattr(run_search, "_transport", lambda: httpx.MockTransport(service.handle))
    out = tmp_path / "summary_web_search.md"

    code = run_search.main(["--expect", "hits", "--retries", "0", "--summary-out", str(out)])

    assert code == 1
    assert "**Result:** FAILED" in out.read_text()


def test_main_failed_mode_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService(lambda _b, _n: (200, DOWN))
    monkeypatch.setattr(run_search, "_transport", lambda: httpx.MockTransport(service.handle))

    code = run_search.main(
        ["--expect", "failed", "--query", "Chlorophyll", "--summary-out", str(tmp_path / "s.md")]
    )

    assert code == 0


def engines_report(
    service: FakeService, engines: list[str], retries: int = 2
) -> tuple[Report, list[float]]:
    pauses: list[float] = []

    async def sleep(seconds: float) -> None:
        pauses.append(seconds)

    async def run() -> Report:
        async with service.client() as client:
            return await check_engines(client, "Photosynthese", "de", engines, retries, 7.0, sleep)

    return asyncio.run(run()), pauses


def by_engine(answers: dict[str, dict[str, Any]]) -> Answer:
    return lambda body, _n: (200, answers[body["sources"][0]])


def test_every_engine_with_a_hit_of_its_own_passes() -> None:
    service = FakeService(by_engine({"bing": envelope(BING), "duckduckgo": envelope(DDG)}))

    report, pauses = engines_report(service, ["bing", "duckduckgo"])

    assert report.problems == []
    assert pauses == []
    assert service.bodies == [
        ("rest", {"query": "Photosynthese", "language": "de", "sources": [name], "max_results": 10})
        for name in ("bing", "duckduckgo")
    ]


def test_a_dead_engine_is_named_and_the_others_still_run() -> None:
    blocked = {
        "status": "failed",
        "error": {
            "code": "search_failed",
            "retryable": True,
            "details": [{"source": "startpage", "reason": "Suspended: CAPTCHA"}],
        },
    }
    service = FakeService(by_engine({"startpage": blocked, "bing": envelope(BING)}))

    report, pauses = engines_report(service, ["startpage", "bing"])

    assert report.problems == ["startpage: no try of 3 had a hit"]
    assert pauses == [7.0, 7.0]
    first = report.calls[0]
    assert "startpage: Suspended: CAPTCHA" in first.problems[0]
    assert "no hit from startpage" in first.problems
    assert [call.label for call in report.calls][-1] == "REST bing try 1"


@pytest.mark.parametrize(
    ("answer", "problem"),
    [
        (envelope(DDG), "no hit from mojeek"),
        (envelope({**BING, "sources": ["mojeek"]}, status="partial"), "status partial"),
    ],
)
def test_an_engine_without_a_clean_hit_fails(answer: dict[str, Any], problem: str) -> None:
    service = FakeService(lambda _b, _n: (200, answer))

    report, _ = engines_report(service, ["mojeek"], retries=0)

    assert report.problems == ["mojeek: no try of 1 had a hit"]
    assert any(problem in p for p in report.calls[0].problems)


def test_main_engines_mode_asks_the_scraping_engines_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hit_of = {name: envelope({**BING, "sources": [name]}) for name in run_search.SCRAPING_ENGINES}
    service = FakeService(by_engine(hit_of))
    monkeypatch.setattr(run_search, "_transport", lambda: httpx.MockTransport(service.handle))

    code = run_search.main(["--expect", "engines", "--summary-out", str(tmp_path / "s.md")])

    assert code == 0
    assert [body["sources"] for _, body in service.bodies] == [
        ["bing"],
        ["duckduckgo"],
        ["mojeek"],
        ["startpage"],
    ]
    assert "Web search, each engine alone" in (tmp_path / "s.md").read_text()
