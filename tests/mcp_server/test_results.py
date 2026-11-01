"""The answer budget of MCP tool results (M2, ``MCP_MAX_ANSWER_CHARS``)."""

import json
from typing import Any

from text_extraction.mcp_server.results import ANSWER_TRUNCATED_WARNING, fit_answer


def size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def result(**data: Any) -> dict[str, Any]:
    return {"status": "success", "data": {"content": "text", "warnings": [], **data}}


def chunks(count: int, length: int = 1_000) -> list[dict[str, Any]]:
    return [{"index": n, "text": "x" * length} for n in range(count)]


def test_an_answer_within_the_budget_is_unchanged() -> None:
    payload = result(chunks=chunks(5))
    before = json.dumps(payload)

    assert fit_answer(payload, max_chars=100_000) == []
    assert json.dumps(payload) == before


def test_lists_are_cut_in_order_until_the_answer_fits() -> None:
    links = [{"url": f"https://example.org/{n}", "text": "y" * 200} for n in range(200)]
    tables = [{"rows": [["z" * 500]]} for _ in range(20)]
    payload = result(chunks=chunks(100), links=links, structured={"values": {}, "tables": tables})

    hints = fit_answer(payload, max_chars=80_000)

    data = payload["data"]
    assert size(payload) <= 80_000
    # chunks go first and are cut to fit; links and tables stay whole once the answer fits
    assert 0 < len(data["chunks"]) < 100
    assert data["chunks"] == chunks(100)[: len(data["chunks"])]  # a prefix, in order
    assert data["links"] == links
    assert data["structured"]["tables"] == tables
    assert data["warnings"] == [ANSWER_TRUNCATED_WARNING]
    assert hints == [
        f"chunks cut to {len(data['chunks'])} of 100 to fit MCP_MAX_ANSWER_CHARS; "
        "the REST route returns them all."
    ]


def test_a_list_is_emptied_before_the_next_is_cut() -> None:
    links = [{"url": f"https://example.org/{n}", "text": "y" * 200} for n in range(200)]
    payload = result(chunks=chunks(30), links=links)

    hints = fit_answer(payload, max_chars=20_000)

    data = payload["data"]
    assert size(payload) <= 20_000
    assert data["chunks"] == []
    assert 0 < len(data["links"]) < 200
    assert len(hints) == 2


def test_a_job_page_drops_trailing_items_first() -> None:
    items: list[dict[str, Any]] = [
        {"index": n, "url": f"https://example.org/{n}", "state": "done", "result": result()}
        for n in range(10)
    ]
    for item in items:
        item["result"]["data"]["chunks"] = chunks(10)
    payload: dict[str, Any] = {
        "status": "success",
        "data": {"id": "a" * 32, "items": items, "next_offset": None},
    }

    hints = fit_answer(payload, max_chars=35_000)

    data = payload["data"]
    kept = len(data["items"])
    assert size(payload) <= 35_000
    assert 1 <= kept < 10
    assert data["next_offset"] == kept
    assert all(len(item["result"]["data"]["chunks"]) == 10 for item in data["items"])
    assert hints == [
        f"Page cut to {kept} items to fit MCP_MAX_ANSWER_CHARS; call get_job with offset={kept}."
    ]


def test_a_single_item_too_large_has_its_lists_cut() -> None:
    item: dict[str, Any] = {
        "index": 4,
        "url": "https://example.org/",
        "state": "done",
        "result": result(),
    }
    item["result"]["data"]["chunks"] = chunks(100)
    payload: dict[str, Any] = {
        "status": "success",
        "data": {"id": "a" * 32, "items": [item], "next_offset": 5},
    }

    hints = fit_answer(payload, max_chars=30_000)

    assert size(payload) <= 30_000
    assert payload["data"]["next_offset"] == 5
    kept = len(item["result"]["data"]["chunks"])
    assert 0 < kept < 100
    assert item["result"]["data"]["warnings"] == [ANSWER_TRUNCATED_WARNING]
    assert hints == [
        f"Item 4: chunks cut to {kept} of 100 to fit MCP_MAX_ANSWER_CHARS; "
        "GET /v1/jobs/{id} returns them all."
    ]


def test_an_answer_without_lists_stays_over_the_budget() -> None:
    payload = result(content="c" * 5_000)

    assert fit_answer(payload, max_chars=1_000) == []
    assert payload["data"]["content"] == "c" * 5_000
