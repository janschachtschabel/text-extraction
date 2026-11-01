"""The MCP job tools (rulings P10-S5 to P10-S7, P10-S10): batch_extract and site_structure start
their route's job, get_job reads (and waits for) a job page cut to MCP_MAX_CONTENT_CHARS,
get_job_screenshot attaches a job's screenshot and cancel_job deletes a job."""

import asyncio
import base64
import contextlib
import hashlib
import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from anyio.from_thread import BlockingPortal
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from structlog.testing import capture_logs

from tests.fakes import ORIGIN, PAGE
from tests.mcp_server.rpc import ACCEPT, call_tool, rpc
from tests.mcp_server.test_tools_extract import (
    AUTH,
    PNG,
    USER_KEY,
    envelope_of,
    hints_of,
    refusal,
)
from tests.test_jobs import success
from text_extraction.app import create_public_app
from text_extraction.jobs import JobStore
from text_extraction.jobs_api import batch_body_limit
from text_extraction.mcp_server.calls import outcome_of
from text_extraction.mcp_server.results import TRUNCATED_WARNING, payload_result
from text_extraction.mcp_server.server import TOOL_ROUTES
from text_extraction.settings import Settings

OTHER_KEY = "other-key-0123456789-raw-secret"
OTHER = {"Authorization": f"Bearer {OTHER_KEY}"}
URLS = [f"{ORIGIN}/a", f"{ORIGIN}/b", f"{ORIGIN}/c"]
JOB_TOOLS = {"batch_extract", "site_structure", "get_job", "get_job_screenshot", "cancel_job"}


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "auth_mode": "api_key",
        "api_keys": [
            {
                "name": "alice",
                "role": "user",
                "sha256": hashlib.sha256(USER_KEY.encode()).hexdigest(),
            },
            {
                "name": "bob",
                "role": "user",
                "sha256": hashlib.sha256(OTHER_KEY.encode()).hexdigest(),
            },
        ],
        "outbound_min_delay_ms": 0,
        "rate_limit_per_minute": 10_000,
        "mcp_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


@contextlib.contextmanager
def running(**overrides: Any) -> Iterator[TestClient]:
    app = create_public_app(make_settings(**overrides), FakeAsyncRedis(), CollectorRegistry())
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def client() -> Iterator[TestClient]:
    with running() as test_client:
        yield test_client


def portal(client: TestClient) -> BlockingPortal:
    """The portal of the running app's event loop (fakeredis is bound to it)."""
    assert client.portal is not None
    return client.portal


def store_of(client: TestClient) -> JobStore:
    app: Any = client.app
    store: JobStore = app.state.job_store
    return store


def start_batch(client: TestClient, urls: list[str] = URLS, **arguments: Any) -> str:
    result = call_tool(client, "batch_extract", {"urls": urls, **arguments}, headers=AUTH)
    assert result["isError"] is False, result
    job_id: str = envelope_of(result)["data"]["id"]
    return job_id


def write(client: TestClient, job_id: str, index: int, content: str = "Hello world") -> None:
    store = store_of(client)
    portal(client).call(store.write_result, job_id, index, success(URLS[index], content))


def test_tools_list_job_tools(client: TestClient) -> None:
    tools = {
        tool["name"]: tool for tool in rpc(client, "tools/list", headers=AUTH)["result"]["tools"]
    }

    assert set(tools) >= JOB_TOOLS
    for name in ("batch_extract", "site_structure"):
        schema = tools[name]["inputSchema"]
        assert schema["properties"]["options"]["type"] == "object", name
        assert "wait_seconds" not in schema["properties"], name
        assert tools[name]["annotations"]["openWorldHint"] is True, name
    for name in ("get_job", "get_job_screenshot", "cancel_job"):
        schema = tools[name]["inputSchema"]
        assert "options" not in schema["properties"], name
        assert schema["required"] == ["job_id"], name
        assert schema.get("additionalProperties") is False, name
    assert tools["get_job"]["annotations"]["readOnlyHint"] is True
    assert tools["get_job_screenshot"]["annotations"]["readOnlyHint"] is True
    assert tools["cancel_job"]["annotations"]["destructiveHint"] is True
    assert tools["cancel_job"]["annotations"]["readOnlyHint"] is False
    limit = tools["get_job"]["inputSchema"]["properties"]["limit"]
    assert (limit["default"], limit["minimum"], limit["maximum"]) == (5, 1, 50)


def test_batch_and_get_job_paging(client: TestClient) -> None:
    result = call_tool(
        client, "batch_extract", {"urls": URLS, "format": "text", "mode": "fast"}, headers=AUTH
    )
    accepted = envelope_of(result)["data"]
    job_id = accepted["id"]
    assert (accepted["kind"], accepted["total"]) == ("batch", 3)
    assert accepted["status_url"] == f"/v1/jobs/{job_id}"
    assert hints_of(result) == [
        f"Continues as job {job_id} (batch): call get_job with job_id={job_id} and "
        "wait_seconds up to 25."
    ]
    store = store_of(client)
    portal(client).call(store.record_claim, job_id, 0, "extract", 2)
    portal(client).call(store.record_claim, job_id, 1, "browser", 1)
    for index in range(3):
        write(client, job_id, index)

    page = call_tool(client, "get_job", {"job_id": job_id, "limit": 2}, headers=AUTH)

    assert page["isError"] is False
    view = envelope_of(page)["data"]
    assert view["state"] == "completed"
    assert [item["index"] for item in view["items"]] == [0, 1]
    assert view["next_offset"] == 2
    rest = client.get(f"/v1/jobs/{job_id}", params={"limit": 2}, headers=AUTH).json()["data"]
    assert [item["run"] for item in view["items"]] == [item["run"] for item in rest["items"]]
    assert view["items"][0]["run"]["pool"] == "extract"
    assert view["items"][0]["run"]["attempts"] == 2
    assert view["items"][1]["run"]["pool"] == "browser"
    assert view["items"] == rest["items"]

    last = envelope_of(call_tool(client, "get_job", {"job_id": job_id, "offset": 2}, headers=AUTH))
    assert [item["index"] for item in last["data"]["items"]] == [2]
    assert last["data"]["next_offset"] is None


def test_batch_options_and_limits(client: TestClient) -> None:
    job_id = start_batch(client, options={"links": True})
    record = portal(client).call(store_of(client).get, job_id)
    assert record is not None
    assert record.options.links is True

    twice = refusal(
        call_tool(client, "batch_extract", {"urls": URLS, "options": {"urls": URLS}}, headers=AUTH)
    )
    assert (twice["code"], twice["message"]) == ("invalid_request", "urls is given twice.")

    unknown = refusal(
        call_tool(
            client, "batch_extract", {"urls": URLS, "options": {"wait_seconds": 5}}, headers=AUTH
        )
    )
    assert unknown["code"] == "invalid_request"
    assert unknown["details"][0]["loc"] == ["wait_seconds"]


def test_batch_over_its_limits() -> None:
    with running(batch_max_urls=2) as client:
        over = refusal(call_tool(client, "batch_extract", {"urls": URLS}, headers=AUTH))
        assert over["code"] == "invalid_request"
        assert over["details"][0]["msg"] == "A batch may have at most 2 URLs."

        # The route's body limit (413 in REST): BATCH_MAX_URLS URLs of the longest length.
        filler = "x" * batch_body_limit(make_settings(batch_max_urls=2))
        large = refusal(
            call_tool(
                client, "batch_extract", {"urls": URLS[:1], "options": {"x": filler}}, headers=AUTH
            )
        )
        assert large["code"] == "invalid_request"
        assert large["message"].startswith("The request body is too large")


def test_get_job_waits_until_completed(client: TestClient) -> None:
    job_id = start_batch(client)
    write(client, job_id, 0)
    write(client, job_id, 1)
    store = store_of(client)

    async def later() -> None:
        await asyncio.sleep(1)
        await store.write_result(job_id, 2, success(URLS[2]))

    portal(client).start_task_soon(later)
    started = time.monotonic()
    result = call_tool(client, "get_job", {"job_id": job_id, "wait_seconds": 5}, headers=AUTH)
    elapsed = time.monotonic() - started

    assert envelope_of(result)["data"]["state"] == "completed"
    assert 0.9 <= elapsed < 3


def test_get_job_wait_bounded(client: TestClient) -> None:
    """Review Focus 5: a job that never completes; the wait ends on time with the page."""
    job_id = start_batch(client)
    portal(client).call(store_of(client).mark_running, job_id)  # as a worker does
    write(client, job_id, 0)

    started = time.monotonic()
    result = call_tool(client, "get_job", {"job_id": job_id, "wait_seconds": 2}, headers=AUTH)
    elapsed = time.monotonic() - started

    assert 2 <= elapsed < 3
    view = envelope_of(result)["data"]
    assert view["state"] == "running"
    assert [item["state"] for item in view["items"]] == ["done", "pending", "pending"]


def test_get_job_wait_cut_to_the_setting() -> None:
    with running(mcp_wait_seconds=1) as client:
        job_id = start_batch(client)
        started = time.monotonic()
        result = call_tool(client, "get_job", {"job_id": job_id, "wait_seconds": 60}, headers=AUTH)
        elapsed = time.monotonic() - started

    assert result["isError"] is False
    assert 1 <= elapsed < 2


def counting_reads(client: TestClient) -> list[float]:
    """The times of the job store's record reads from now on (each poll of a wait is one)."""
    store = store_of(client)
    reads: list[float] = []
    get = store.get

    async def counted(job_id: str) -> Any:
        reads.append(time.monotonic())
        return await get(job_id)

    store.get = counted  # type: ignore[method-assign]
    return reads


async def asgi_call(
    app: Any, message: dict[str, Any], *, disconnect_after: float | None
) -> list[dict[str, Any]]:
    """Drive one POST /mcp through the ASGI app; the client disconnects ``disconnect_after``
    seconds after it sent the body (None: never). Returns what the app sent."""
    body = json.dumps(message).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"accept", ACCEPT.encode()),
            (b"authorization", AUTH["Authorization"].encode()),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    sent_body = False
    sent: list[dict[str, Any]] = []
    # As a server, the disconnect is there from its time on: a receive cancelled before (as
    # ``Request.is_disconnected`` does) finds it later all the same.
    gone_at = None if disconnect_after is None else time.monotonic() + disconnect_after

    async def receive() -> dict[str, Any]:
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": body, "more_body": False}
        if gone_at is None:
            await asyncio.Event().wait()
        elif time.monotonic() < gone_at:  # once gone, answered without a checkpoint
            await asyncio.sleep(gone_at - time.monotonic())
        return {"type": "http.disconnect"}

    async def send(event: dict[str, Any]) -> None:
        sent.append(event)

    await app(scope, receive, send)
    return sent


def get_job_message(job_id: str, wait_seconds: float) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "get_job",
            "arguments": {"job_id": job_id, "wait_seconds": wait_seconds},
        },
    }


def test_get_job_wait_stops_when_the_client_disconnects() -> None:
    """Review of Task 6: an abandoned wait polls no more once the client is gone."""
    with running(mcp_wait_seconds=10) as client:
        job_id = start_batch(client)
        reads = counting_reads(client)
        started = time.monotonic()
        message = get_job_message(job_id, 10)
        sent = portal(client).call(lambda: asgi_call(client.app, message, disconnect_after=0.5))
        ended = time.monotonic()
        portal(client).call(asyncio.sleep, 1.5)

    assert ended - started < 3  # not the 10 s asked for
    assert reads, "the wait polled the job"
    assert all(read <= ended for read in reads)  # no read after the call returned
    assert len(reads) <= 3
    assert sent[0]["type"] == "http.response.start"


def test_get_job_wait_stops_when_cancelled() -> None:
    """A cancelled call (the server cancels its handler) polls no more."""
    with running(mcp_wait_seconds=10) as client:
        job_id = start_batch(client)
        reads = counting_reads(client)
        message = get_job_message(job_id, 10)

        async def cancelled_after(seconds: float) -> float:
            task = asyncio.ensure_future(asgi_call(client.app, message, disconnect_after=None))
            await asyncio.sleep(seconds)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            return time.monotonic()

        cancelled_at = portal(client).call(cancelled_after, 1.5)
        portal(client).call(asyncio.sleep, 1.5)

    assert len(reads) >= 2  # it was polling
    assert all(read <= cancelled_at for read in reads)


def test_get_job_wait_on_a_foreign_job_is_not_found_at_once(client: TestClient) -> None:
    result = call_tool(client, "batch_extract", {"urls": URLS}, headers=OTHER)
    job_id = envelope_of(result)["data"]["id"]

    started = time.monotonic()
    error = refusal(
        call_tool(client, "get_job", {"job_id": job_id, "wait_seconds": 5}, headers=AUTH)
    )

    assert error["code"] == "not_found"
    assert time.monotonic() - started < 1


def test_job_ids_of_get_job_and_cancel_job_are_logged() -> None:
    """P10-S4: the log line of a call that answered a job carries its id."""
    view = {"id": "j1", "kind": "batch", "state": "running", "items": []}
    deleted = {"id": "j2", "kind": "site"}
    shot = {"url": "/v1/jobs/j3/artifacts/screenshot.png", "bytes": 1, "content_type": "image/png"}
    for data, expected in ((view, ("success", "j1")), (deleted, ("success", "j2"))):
        result = payload_result({"status": "success", "data": data}, is_error=False)
        assert outcome_of(result) == expected
    assert outcome_of(payload_result({"status": "success", "data": shot}, is_error=False)) == (
        "success",
        None,
    )


def test_refused_get_job_and_cancel_job_log_their_job_id() -> None:
    """A refused ``get_job`` or ``cancel_job`` (``not_found``, the 503 of jobs off) logs the
    ``job_id`` it was asked for, when it is a job id; any other text is never logged."""
    unknown = "b" * 32
    with running() as client, capture_logs() as logs:
        for tool in ("get_job", "cancel_job"):
            refusal(call_tool(client, tool, {"job_id": unknown}, headers=AUTH))
        refusal(call_tool(client, "get_job", {"job_id": "not-a-job-id-77"}, headers=AUTH))
    with running(jobs_enabled=False) as client, capture_logs() as off_logs:
        refusal(call_tool(client, "cancel_job", {"job_id": unknown}, headers=AUTH))

    lines = [
        {key: line.get(key) for key in ("tool", "outcome", "job_id")}
        for line in [*logs, *off_logs]
        if line["event"] == "mcp_tool_call"
    ]
    assert lines == [
        {"tool": "get_job", "outcome": "not_found", "job_id": unknown},
        {"tool": "cancel_job", "outcome": "not_found", "job_id": unknown},
        {"tool": "get_job", "outcome": "not_found", "job_id": None},
        {"tool": "cancel_job", "outcome": "invalid_request", "job_id": unknown},
    ]
    assert "not-a-job-id-77" not in repr([*logs, *off_logs])


def test_get_job_keeps_a_page_within_the_answer_budget() -> None:
    # M2: a page over MCP_MAX_ANSWER_CHARS drops its trailing items; the next page has them.
    with running(mcp_max_answer_chars=25_000) as client:
        job_id = start_batch(client)
        for index in range(3):
            write(client, job_id, index, "w" * 10_000)

        result = call_tool(client, "get_job", {"job_id": job_id}, headers=AUTH)

    view = envelope_of(result)["data"]
    kept = len(view["items"])
    assert 1 <= kept < 3
    assert view["next_offset"] == kept
    assert hints_of(result) == [
        f"Page cut to {kept} items to fit MCP_MAX_ANSWER_CHARS; call get_job with offset={kept}."
    ]


def test_get_job_slices_each_item(client: TestClient) -> None:
    """Review Focus 2: a page's share of max_chars is split between its items."""
    job_id = start_batch(client)
    contents = [letter * 30_000 for letter in "abc"]
    for index, content in enumerate(contents):
        write(client, job_id, index, content)

    result = call_tool(client, "get_job", {"job_id": job_id, "max_chars": 30_000}, headers=AUTH)

    items = envelope_of(result)["data"]["items"]
    for item, content in zip(items, contents, strict=True):
        data = item["result"]["data"]
        assert data["content"] == content[:10_000]
        assert TRUNCATED_WARNING in data["warnings"]
    assert hints_of(result) == [
        f"Item {index}: Cut at 10000 of 30000 characters; call again with content_offset=10000."
        for index in range(3)
    ]

    tail = call_tool(
        client,
        "get_job",
        {"job_id": job_id, "max_chars": 30_000, "content_offset": 20_000},
        headers=AUTH,
    )
    for item, content in zip(envelope_of(tail)["data"]["items"], contents, strict=True):
        assert item["result"]["data"]["content"] == content[20_000:]
    assert hints_of(tail) == []

    whole = call_tool(client, "get_job", {"job_id": job_id}, headers=AUTH)  # 50,000 // 3 each
    first = envelope_of(whole)["data"]["items"][0]["result"]["data"]
    assert first["content"] == contents[0][:16_666]

    over = refusal(call_tool(client, "get_job", {"job_id": job_id, "limit": 51}, headers=AUTH))
    assert over["code"] == "invalid_request"
    assert over["details"][0]["loc"] == ["limit"]


def test_foreign_job_not_found(client: TestClient) -> None:
    result = call_tool(client, "batch_extract", {"urls": URLS}, headers=OTHER)
    job_id = envelope_of(result)["data"]["id"]

    for tool in ("get_job", "get_job_screenshot", "cancel_job"):
        error = refusal(call_tool(client, tool, {"job_id": job_id}, headers=AUTH))
        assert error["code"] == "not_found", tool
    assert call_tool(client, "get_job", {"job_id": job_id}, headers=OTHER)["isError"] is False
    unknown = refusal(call_tool(client, "get_job", {"job_id": "nope"}, headers=AUTH))
    assert (unknown["code"], unknown["message"]) == ("not_found", "Job not found.")


def test_site_structure_tool(client: TestClient) -> None:
    result = call_tool(
        client, "site_structure", {"url": PAGE, "depth": 1, "max_pages": 10}, headers=AUTH
    )

    assert result["isError"] is False, result
    accepted = envelope_of(result)["data"]
    assert accepted["kind"] == "site"
    assert accepted["total"] == 1
    job_id = accepted["id"]
    assert hints_of(result)[0].startswith(f"Continues as job {job_id} (site)")
    record = portal(client).call(store_of(client).get, job_id)
    assert record is not None
    assert record.options.site is not None
    assert (record.options.site.depth, record.options.site.max_pages) == (1, 10)

    deep = refusal(call_tool(client, "site_structure", {"url": PAGE, "depth": 6}, headers=AUTH))
    assert deep["code"] == "invalid_request"
    assert deep["details"][0]["msg"] == "depth may be at most 5 (SITE_MAX_DEPTH)."

    scoped = call_tool(
        client, "site_structure", {"url": PAGE, "options": {"sitemaps": False}}, headers=AUTH
    )
    assert scoped["isError"] is False, scoped


def test_get_job_screenshot(client: TestClient) -> None:
    job_id = start_batch(client)
    missing = refusal(call_tool(client, "get_job_screenshot", {"job_id": job_id}, headers=AUTH))
    assert (missing["code"], missing["message"]) == ("not_found", "File not found.")

    portal(client).call(store_of(client).put_artifact, job_id, "screenshot.png", PNG)
    result = call_tool(client, "get_job_screenshot", {"job_id": job_id}, headers=AUTH)

    assert result["isError"] is False
    images = [block for block in result["content"] if block["type"] == "image"]
    assert len(images) == 1
    assert images[0]["mimeType"] == "image/png"
    assert base64.b64decode(images[0]["data"]) == PNG
    data = envelope_of(result)["data"]
    assert data == {
        "url": f"/v1/jobs/{job_id}/artifacts/screenshot.png",
        "bytes": len(PNG),
        "content_type": "image/png",
    }


def test_get_job_screenshot_over_the_image_limit() -> None:
    with running(mcp_image_max_bytes=500) as client:
        job_id = start_batch(client)
        portal(client).call(store_of(client).put_artifact, job_id, "screenshot.png", PNG)
        result = call_tool(client, "get_job_screenshot", {"job_id": job_id}, headers=AUTH)

    assert result["isError"] is False
    assert [block for block in result["content"] if block["type"] == "image"] == []
    url = f"/v1/jobs/{job_id}/artifacts/screenshot.png"
    assert hints_of(result) == [
        f"The screenshot ({len(PNG)} bytes) is over MCP_IMAGE_MAX_BYTES; download it from {url}."
    ]


def test_cancel_job_then_not_found(client: TestClient) -> None:
    job_id = start_batch(client)

    result = call_tool(client, "cancel_job", {"job_id": job_id}, headers=AUTH)

    assert result["isError"] is False
    envelope = envelope_of(result)
    assert envelope["status"] == "success"
    assert envelope["data"] == {"id": job_id, "kind": "batch"}
    for tool in ("get_job", "cancel_job"):
        error = refusal(call_tool(client, tool, {"job_id": job_id}, headers=AUTH))
        assert error["code"] == "not_found", tool
    assert client.get(f"/v1/jobs/{job_id}", headers=AUTH).status_code == 404


def test_jobs_disabled_tool_error() -> None:
    """As REST's 400: ``invalid_request`` with REST's message and ``retryable``."""
    with running(jobs_enabled=False) as client:
        rest = client.post("/v1/batch", json={"urls": URLS}, headers=AUTH)
        assert rest.status_code == 400
        calls: list[tuple[str, dict[str, Any]]] = [
            ("batch_extract", {"urls": URLS}),
            ("site_structure", {"url": PAGE}),
            ("get_job", {"job_id": "a" * 32}),
            ("get_job_screenshot", {"job_id": "a" * 32}),
            ("cancel_job", {"job_id": "a" * 32}),
        ]
        for tool, arguments in calls:
            error = refusal(call_tool(client, tool, arguments, headers=AUTH))
            assert error["code"] == "invalid_request", tool
            assert error["message"] == "Jobs are disabled on this deployment.", tool
            assert error["retryable"] is rest.json()["error"]["retryable"], tool


def test_every_public_route_has_a_tool(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    routes = {
        (method.upper(), path)
        for path, operations in spec["paths"].items()
        for method in operations
    }
    tools = {tool["name"] for tool in rpc(client, "tools/list", headers=AUTH)["result"]["tools"]}

    assert routes == set(TOOL_ROUTES)
    assert set(TOOL_ROUTES.values()) == tools
