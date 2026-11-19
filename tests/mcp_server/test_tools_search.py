"""The MCP tool ``web_search`` (package 25): the core of ``POST /v1/search`` (ruling P10-S1) with
the route's checks, answering the REST envelope; SearXNG is ``httpx.MockTransport``."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from tests.mcp_server.rpc import call_tool, rpc
from tests.mcp_server.test_tools_extract import envelope_of, hints_of, refusal
from tests.test_search_api import (
    ADMIN,
    USER,
    Searx,
    make_settings,
    searx_answer,
    serving,
)
from text_extraction.app import create_public_app

HIT = "https://en.wikipedia.org/wiki/Photosynthesis"


@contextmanager
def running(searx: Searx | None = None, **overrides: Any) -> Iterator[TestClient]:
    settings = make_settings(mcp_enabled=True, **overrides)
    app = create_public_app(settings, FakeAsyncRedis(), CollectorRegistry())
    if searx is not None:
        app.state.search_http = searx.client()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def listed_tool(client: TestClient) -> dict[str, Any]:
    tools = {
        tool["name"]: tool for tool in rpc(client, "tools/list", headers=USER)["result"]["tools"]
    }
    tool: dict[str, Any] = tools["web_search"]
    return tool


def test_tools_list_web_search() -> None:
    with running(serving(searx_answer())) as client:
        tool = listed_tool(client)

    schema = tool["inputSchema"]
    assert schema["required"] == ["query"]
    properties = schema["properties"]
    assert next(iter(properties)) == "query"
    assert {"max_results", "language", "sources", "page", "time_range"} <= set(properties)
    assert "options" not in properties
    # The deployment's bounds, so an agent need not learn them from a refusal.
    assert properties["max_results"]["maximum"] == 20
    assert properties["max_results"]["default"] == 10
    assert "wikipedia, arxiv, crossref" in properties["sources"]["description"]
    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["annotations"]["openWorldHint"] is True
    assert "extract_url" in tool["description"]
    assert "batch_extract" in tool["description"]


def test_max_results_default_follows_a_low_limit() -> None:
    # The schema never shows a default above its own maximum.
    with running(serving(searx_answer()), search_max_results=5) as client:
        properties = listed_tool(client)["inputSchema"]["properties"]

    assert properties["max_results"]["maximum"] == 5
    assert properties["max_results"]["default"] == 5


def test_web_search_is_listed_while_search_is_off() -> None:
    """As the job tools while jobs are off: listed, and a call answers REST's 400."""
    with running(search_enabled=False) as client:
        assert listed_tool(client)["name"] == "web_search"


def test_web_search_answers_like_the_route() -> None:
    searx = serving(searx_answer(HIT, unresponsive=[["arxiv", "timeout"]]))
    body = {
        "query": "Photosynthese",
        "max_results": 5,
        "language": "de",
        "sources": ["Wikipedia", "arxiv"],
        "page": 2,
        "time_range": "year",
    }
    with running(searx, search_cache_seconds=0) as client:
        rest = client.post("/v1/search", json=body, headers=USER).json()
        result = call_tool(client, "web_search", body, headers=USER)

    assert result["isError"] is False
    answer = envelope_of(result)
    assert answer["trace_id"]
    rest.pop("trace_id")
    answer.pop("trace_id")
    assert answer == rest
    assert answer["status"] == "partial"
    assert [hit["url"] for hit in answer["data"]["results"]] == [HIT]
    first, second = (request.url.params for request in searx.requests)
    assert first == second
    assert second["engines"] == "wikipedia,arxiv"
    assert (second["language"], second["pageno"], second["time_range"]) == ("de", "2", "year")


def test_web_search_with_the_query_alone() -> None:
    searx = serving(searx_answer(HIT))
    with running(searx) as client:
        result = call_tool(client, "web_search", {"query": "photosynthesis"}, headers=USER)

    assert result["isError"] is False
    answer = envelope_of(result)
    assert answer["status"] == "success"
    assert answer["data"]["cache"] == "miss"
    assert searx.requests[0].url.params["engines"] == "wikipedia,arxiv,crossref"


def test_a_failed_search_is_an_answer_not_a_tool_error() -> None:
    """REST's 200 with ``status: failed``: the agent reads ``error.code`` and retries."""

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with running(Searx(timeout)) as client:
        result = call_tool(client, "web_search", {"query": "plants"}, headers=USER)

    assert result["isError"] is False
    answer = envelope_of(result)
    assert answer["status"] == "failed"
    assert (answer["error"]["code"], answer["error"]["retryable"]) == ("search_failed", True)


def test_search_off_is_the_tool_error_of_rests_400() -> None:
    searx = serving(searx_answer(HIT))
    with running(searx, search_enabled=False) as client:
        rest = client.post("/v1/search", json={"query": "plants"}, headers=ADMIN)
        error = refusal(call_tool(client, "web_search", {"query": "plants"}, headers=ADMIN))

    assert rest.status_code == 400
    assert error == rest.json()["error"]
    assert error["code"] == "invalid_request"
    assert searx.requests == []


def test_user_key_without_search_allow_users_is_the_tool_error_of_rests_403() -> None:
    searx = serving(searx_answer(HIT))
    with running(searx, search_allow_users=False) as client:
        rest = client.post("/v1/search", json={"query": "plants"}, headers=USER)
        error = refusal(call_tool(client, "web_search", {"query": "plants"}, headers=USER))
        admin = call_tool(client, "web_search", {"query": "plants"}, headers=ADMIN)

    assert rest.status_code == 403
    assert error == rest.json()["error"]
    assert error["code"] == "forbidden"
    assert admin["isError"] is False
    assert len(searx.requests) == 1


def test_the_routes_422s_are_tool_errors_with_the_same_details() -> None:
    searx = serving(searx_answer(HIT))
    bodies: list[dict[str, Any]] = [
        {"query": "plants", "max_results": 21},
        {"query": "plants", "sources": ["google"]},
        {"query": "plants", "sources": []},
    ]
    with running(searx) as client:
        for body in bodies:
            rest = client.post("/v1/search", json=body, headers=USER)
            error = refusal(call_tool(client, "web_search", body, headers=USER))
            assert rest.status_code == 422, body
            assert error == rest.json()["error"], body

    assert searx.requests == []


def test_invalid_arguments_are_refused() -> None:
    with running(serving(searx_answer(HIT))) as client:
        empty = refusal(call_tool(client, "web_search", {"query": ""}, headers=USER))
        unknown = refusal(
            call_tool(client, "web_search", {"query": "plants", "engines": ["x"]}, headers=USER)
        )
        missing = refusal(call_tool(client, "web_search", {}, headers=USER))

    for error in (empty, unknown, missing):
        assert error["code"] == "invalid_request"


def test_web_search_is_held_to_the_routes_body_limit() -> None:
    """The REST route refuses a body over ``REQUEST_MAX_BODY_BYTES`` with 413; so does the tool,
    whose call can carry far more (``MCP_BODY``)."""
    sources = ["wikipedia"] * 2_000
    with running(serving(searx_answer(HIT)), request_max_body_bytes=8_192) as client:
        rest = client.post("/v1/search", json={"query": "q", "sources": sources}, headers=USER)
        error = refusal(
            call_tool(client, "web_search", {"query": "q", "sources": sources}, headers=USER)
        )

    assert rest.status_code == 413
    assert (error["code"], error["message"]) == ("invalid_request", rest.json()["error"]["message"])


def test_web_search_counts_one_hit_against_the_rate_limit() -> None:
    with running(serving(searx_answer(HIT)), rate_limit_per_minute=2) as client:
        results = [
            call_tool(client, "web_search", {"query": "plants"}, headers=USER) for _ in range(3)
        ]

    assert [result["isError"] for result in results] == [False, False, True]
    assert refusal(results[2])["code"] == "rate_limited"


def test_many_hits_are_cut_to_the_answer_budget() -> None:
    """Hits over ``MCP_MAX_ANSWER_CHARS`` are cut from the end with ``mcp_answer_truncated`` and
    a hint; the REST route returns them all."""
    urls = [f"https://example.org/{index}/{'x' * 400}" for index in range(40)]
    with running(
        serving(searx_answer(*urls)),
        search_max_results=50,
        mcp_max_content_chars=1_000,
        mcp_max_answer_chars=10_000,
    ) as client:
        result = call_tool(
            client, "web_search", {"query": "plants", "max_results": 40}, headers=USER
        )

    answer = envelope_of(result)
    kept = answer["data"]["results"]
    assert 0 < len(kept) < 40
    assert len(result["content"][-1]["text"]) <= 10_000
    assert "mcp_answer_truncated" in answer["data"]["warnings"]
    (hint,) = hints_of(result)
    assert f"results cut to {len(kept)} of 40" in hint
