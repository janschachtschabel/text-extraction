"""The MCP extraction tools (rulings P10-S5 to P10-S8): extract_url, extract_file,
screenshot_url, extract_structured and chunk run their REST route's core with the route's checks,
and answer its envelope, cut to MCP_MAX_CONTENT_CHARS, with hints and the screenshot."""

import asyncio
import base64
import contextlib
import hashlib
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from pydantic import ValidationError
from starlette.datastructures import State

from tests.fakes import ARTICLE, PAGE, FakeFetcher, fetched
from tests.mcp_server.rpc import call_tool, rpc
from tests.test_extract_api import BrowserCase
from text_extraction.app import create_public_app
from text_extraction.auth import Principal
from text_extraction.fetch import Fetcher
from text_extraction.mcp_server.results import TRUNCATED_WARNING, slice_content
from text_extraction.mcp_server.server import mcp_body_limit
from text_extraction.mcp_server.tools_extract import ExtractFileArguments, ScreenshotUrlTool
from text_extraction.pipeline import create_pipeline
from text_extraction.settings import Settings
from text_extraction.structured.checks import XPATH_NEEDS_ADMIN_MESSAGE

USER_KEY = "user-key-0123456789-raw-secret"  # the user key of tests.test_extract_api
AUTH = {"Authorization": f"Bearer {USER_KEY}"}
CORPUS = Path(__file__).parent.parent / "documents" / "corpus"
SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}}}
TEXT = "# Notes\n\nPlants turn light into sugar.\n\n## Roots\n\nRoots take up water.\n"
EXTRACTION_TOOLS = {"extract_url", "extract_file", "screenshot_url", "extract_structured", "chunk"}
LLM_KEY = "sk-llm-caller-key-mcp-41"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * (1024 - 8)


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
        "mcp_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


class SlowFetcher(FakeFetcher):
    """Answers like ``FakeFetcher`` after ``seconds``."""

    def __init__(self, answers: dict[str, Any], seconds: float) -> None:
        super().__init__(answers)
        self.seconds = seconds

    async def fetch(self, url: str, **options: Any) -> Any:
        await asyncio.sleep(self.seconds)
        return await super().fetch(url, **options)


@contextlib.contextmanager
def running(
    fetcher: FakeFetcher | None = None, *, real_fetcher: bool = False, **overrides: Any
) -> Iterator[TestClient]:
    settings = make_settings(**overrides)
    registry = CollectorRegistry()
    app = create_public_app(settings, FakeAsyncRedis(), registry)
    if not real_fetcher:
        fake = fetcher or FakeFetcher({PAGE: fetched(body=ARTICLE)})
        app.state.pipeline = create_pipeline(
            settings, app.state.valkey, cast(Fetcher, fake), registry
        )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def client() -> Iterator[TestClient]:
    with running() as test_client:
        yield test_client


def envelope_of(result: dict[str, Any]) -> dict[str, Any]:
    """The envelope of a tool result; its last text content repeats it as JSON."""
    structured: dict[str, Any] = result["structuredContent"]
    assert json.loads(result["content"][-1]["text"]) == structured
    return structured


def hints_of(result: dict[str, Any]) -> list[str]:
    return [block["text"] for block in result["content"][:-1] if block["type"] == "text"]


def refusal(result: dict[str, Any]) -> dict[str, Any]:
    assert result["isError"] is True, result
    envelope = envelope_of(result)
    assert envelope["status"] == "failed"
    error: dict[str, Any] = envelope["error"]
    return error


def test_tools_list_lean_schemas(client: TestClient) -> None:
    tools = {
        tool["name"]: tool for tool in rpc(client, "tools/list", headers=AUTH)["result"]["tools"]
    }

    assert set(tools) >= EXTRACTION_TOOLS
    for name in EXTRACTION_TOOLS:
        tool = tools[name]
        schema = tool["inputSchema"]
        assert len(json.dumps(schema)) < 4096, name
        assert schema["properties"]["options"]["type"] == "object", name
        assert "/docs" in schema["properties"]["options"]["description"], name
        assert schema.get("additionalProperties") is False, name
        assert tool["annotations"]["openWorldHint"] is True, name
        assert "outputSchema" not in tool, name
    assert tools["extract_structured"]["inputSchema"]["properties"]["structured"]["type"] == (
        "object"
    )
    assert tools["chunk"]["inputSchema"]["properties"]["chunking"]["type"] == "object"
    assert "structured" in tools["extract_structured"]["inputSchema"]["required"]
    assert "chunking" in tools["chunk"]["inputSchema"]["required"]
    assert "content_base64" in tools["extract_file"]["inputSchema"]["required"]


def test_extract_url_matches_rest(client: TestClient) -> None:
    rest = client.post("/v1/extract", json={"url": PAGE}, headers=AUTH).json()

    result = call_tool(client, "extract_url", {"url": PAGE}, headers=AUTH)

    assert result["isError"] is False
    envelope = envelope_of(result)
    assert envelope["status"] == rest["status"] == "success"
    assert envelope["data"]["content"] == rest["data"]["content"]
    assert envelope["data"]["title"] == rest["data"]["title"]
    assert hints_of(result) == []
    assert TRUNCATED_WARNING not in envelope["data"]["warnings"]


def test_options_merge_and_conflict(client: TestClient) -> None:
    linked = envelope_of(
        call_tool(client, "extract_url", {"url": PAGE, "options": {"links": True}}, headers=AUTH)
    )
    assert linked["data"]["links"]

    twice = refusal(
        call_tool(client, "extract_url", {"url": PAGE, "options": {"url": PAGE}}, headers=AUTH)
    )
    assert (twice["code"], twice["message"]) == ("invalid_request", "url is given twice.")

    unknown = refusal(
        call_tool(client, "extract_url", {"url": PAGE, "options": {"colour": "red"}}, headers=AUTH)
    )
    assert unknown["code"] == "invalid_request"
    assert unknown["details"] == [{"loc": ["colour"], "msg": "Extra inputs are not permitted"}]

    argument = refusal(call_tool(client, "extract_url", {"url": PAGE, "links": True}, headers=AUTH))
    assert argument["code"] == "invalid_request"
    assert argument["details"][0]["loc"] == ["links"]


def test_user_options_checked_as_rest() -> None:
    """Review Focus 1: a user key cannot reach an admin option through ``options``."""
    case = BrowserCase(mcp_enabled=True)
    with TestClient(case.app) as client:
        for tool in ("extract_url", "screenshot_url", "extract_structured", "chunk"):
            arguments: dict[str, Any] = {"url": PAGE}
            if tool == "extract_structured":
                arguments["structured"] = {"strategy": "metadata", "schema": SCHEMA}
            if tool == "chunk":
                arguments["chunking"] = {"strategy": "recursive"}
            ignore = refusal(
                call_tool(
                    client, tool, {**arguments, "options": {"compliance": "ignore"}}, headers=AUTH
                )
            )
            assert ignore["code"] == "forbidden", tool
            evaluate = refusal(
                call_tool(
                    client,
                    tool,
                    {**arguments, "options": {"actions": [{"type": "evaluate", "script": "1"}]}},
                    headers=AUTH,
                )
            )
            assert evaluate["code"] == "forbidden", tool
        rule = {"strategy": "selectors", "schema": SCHEMA, "selectors": {"name": {"xpath": "//h1"}}}
        for tool, arguments in (
            ("extract_url", {"url": PAGE, "options": {"structured": rule}}),
            ("extract_structured", {"url": PAGE, "structured": rule}),
            ("chunk", {"url": PAGE, "chunking": {}, "options": {"structured": rule}}),
            ("chunk", {"text": TEXT, "chunking": {}, "options": {"structured": rule}}),
            (
                "extract_file",
                {"content_base64": b64(TEXT.encode()), "options": {"structured": rule}},
            ),
        ):
            xpath = refusal(call_tool(client, tool, arguments, headers=AUTH))
            assert (xpath["code"], xpath["message"]) == (
                "invalid_request",
                XPATH_NEEDS_ADMIN_MESSAGE,
            ), tool

    with running() as client:  # browser mode off
        browser = refusal(
            call_tool(client, "extract_url", {"url": PAGE, "mode": "browser"}, headers=AUTH)
        )
        assert browser["code"] == "invalid_request"
        assert browser["message"] == "Browser mode is not enabled on this service."


def big_schema(size: int) -> dict[str, Any]:
    properties = {f"field_{index:05d}": {"type": "string"} for index in range(size // 30)}
    return {"type": "object", "properties": properties}


def test_body_cap_as_rest() -> None:
    """The merged body of a tool is held to its REST route's body limit, before the core."""
    fetcher = FakeFetcher({PAGE: fetched(body=ARTICLE)})
    with running(fetcher) as client:
        huge = {"strategy": "metadata", "schema": big_schema(100_000)}
        rest = client.post("/v1/structured", json={"url": PAGE, "structured": huge}, headers=AUTH)
        assert rest.status_code == 413
        expected = rest.json()["error"]
        for tool, arguments in (
            ("extract_url", {"url": PAGE, "options": {"structured": huge}}),
            ("screenshot_url", {"url": PAGE, "options": {"headers": {"x": "y" * 70_000}}}),
            ("extract_structured", {"url": PAGE, "structured": huge}),
        ):
            error = refusal(call_tool(client, tool, arguments, headers=AUTH))
            assert (error["code"], error["message"]) == (
                expected["code"],
                expected["message"],
            ), tool
        assert fetcher.calls == []
        # Under the limit the body reaches the REST model, which answers as for REST.
        small = {"strategy": "metadata", "schema": big_schema(30_000)}
        rest = client.post("/v1/structured", json={"url": PAGE, "structured": small}, headers=AUTH)
        assert rest.status_code == 422
        arguments = {"url": PAGE, "structured": small}
        error = refusal(call_tool(client, "extract_structured", arguments, headers=AUTH))
        expected = rest.json()["error"]
        assert error["message"] == expected["message"]
        # REST's locations start with "body"; a tool's name the argument.
        assert error["details"] == [
            {**item, "loc": item["loc"][1:]} for item in expected["details"]
        ]

    with running(fetcher, document_max_bytes=4096) as client:
        # `POST /v1/chunk` is held to the text limit (the upload limit + 64 KiB).
        text = "w " * 35_000  # over the text limit (69,632), under /mcp's (71,000)
        rest = client.post(
            "/v1/chunk", json={"text": text, "chunking": {}, "pii": "mask"}, headers=AUTH
        )
        assert rest.status_code == 413
        error = refusal(
            call_tool(
                client,
                "chunk",
                {"text": text, "chunking": {}, "options": {"pii": "mask"}},
                headers=AUTH,
            )
        )
        assert error["message"] == rest.json()["error"]["message"]

        # `POST /v1/extract/file` holds each form field to its part limit.
        for options, limit in (
            # Just over 64 KiB, under /mcp's body limit (71,000 bytes here).
            ({"structured": {"schema": {"type": "object", "description": "x" * 66_000}}}, "64KB"),
            ({"pii": {"mode": "mask", "entities": ["PERSON"] * 200}}, "1KB"),
        ):
            form = {
                name: value if isinstance(value, str) else json.dumps(value)
                for name, value in options.items()
            }
            rest = client.post(
                "/v1/extract/file",
                files={"file": ("a.txt", b"hello", "text/plain")},
                data=form,
                headers=AUTH,
            )
            assert rest.status_code == 422
            error = refusal(
                call_tool(
                    client,
                    "extract_file",
                    {"content_base64": b64(b"hello"), "options": options},
                    headers=AUTH,
                )
            )
            assert (error["code"], error["message"]) == (
                "invalid_request",
                rest.json()["error"]["message"],
            )
            assert limit in error["message"]


def test_explicit_wait_is_bounded_by_the_setting() -> None:
    """Review Focus 5: an explicit wait_seconds is cut to MCP_WAIT_SECONDS."""
    slow = SlowFetcher({PAGE: fetched(body=ARTICLE)}, 3)
    with running(slow, mcp_wait_seconds=1) as client:
        for arguments in (
            {"url": PAGE, "wait_seconds": 30},
            {"url": PAGE, "options": {"wait_seconds": 30}},
        ):
            started = time.monotonic()
            result = call_tool(client, "extract_url", arguments, headers=AUTH)
            assert time.monotonic() - started < 2.5
            assert "status_url" in envelope_of(result)["data"]


def test_target_failure_is_not_tool_error() -> None:
    missing = "https://example.org/missing"
    with running(FakeFetcher({PAGE: fetched(body=ARTICLE)})) as client:
        result = call_tool(client, "extract_url", {"url": missing}, headers=AUTH)

    assert result["isError"] is False
    envelope = envelope_of(result)
    assert envelope["status"] == "failed"
    assert envelope["error"]["code"] == "target_http_error"
    assert envelope["data"]["upstream_status"] == 404


def test_private_url_blocked() -> None:
    """The SSRF guard of the real fetcher: under ``enforce`` its robots.txt cannot be read
    (``blocked_by_robots``), under ``report`` the page itself is refused (``blocked_url``)."""
    private = "http://169.254.169.254/"
    for mode, code in (("enforce", "blocked_by_robots"), ("report", "blocked_url")):
        with running(real_fetcher=True, compliance_default=mode) as client:
            rest = client.post("/v1/extract", json={"url": private}, headers=AUTH).json()
            result = call_tool(client, "extract_url", {"url": private}, headers=AUTH)

        assert result["isError"] is False
        envelope = envelope_of(result)
        assert envelope["status"] == rest["status"] == "failed"
        assert envelope["error"]["code"] == rest["error"]["code"] == code


def test_default_wait_becomes_job() -> None:
    """Review Focus 5: a call ends within MCP_WAIT_SECONDS with a job."""
    slow = SlowFetcher({PAGE: fetched(body=ARTICLE)}, 3)
    with running(slow, mcp_wait_seconds=1) as client:
        result = call_tool(client, "extract_url", {"url": PAGE}, headers=AUTH)

        assert result["isError"] is False
        envelope = envelope_of(result)
        assert envelope["status"] == "success"
        data = envelope["data"]
        assert set(data) == {"id", "kind", "state", "total", "status_url"}
        assert data["kind"] == "extract"
        assert hints_of(result) == [
            f"Continues as job {data['id']} (extract): call get_job with job_id={data['id']} "
            "and wait_seconds up to 1."
        ]


def test_content_slicing() -> None:
    """Review Focus 2: the content of one answer is at most max_chars."""
    envelope: dict[str, Any] = {
        "status": "success",
        "data": {"content": "a" * 50_000 + "b" * 10_000, "warnings": []},
    }
    first = json.loads(json.dumps(envelope))
    hint = slice_content(first, offset=0, share=50_000)
    assert first["data"]["content"] == "a" * 50_000
    assert first["data"]["warnings"] == [TRUNCATED_WARNING]
    assert hint == "Cut at 50000 of 60000 characters; call again with content_offset=50000."

    rest = json.loads(json.dumps(envelope))
    assert slice_content(rest, offset=50_000, share=50_000) is None
    assert rest["data"]["content"] == "b" * 10_000
    assert rest["data"]["warnings"] == [TRUNCATED_WARNING]

    whole = json.loads(json.dumps(envelope))
    assert slice_content(whole, offset=0, share=60_000) is None
    assert whole == envelope
    for empty in ({"status": "failed", "data": None}, {"status": "success", "data": {}}):
        assert slice_content(empty, offset=0, share=10) is None

    # Offsets count code points: characters outside the BMP are never split.
    wide = "a😀b🎉c" * 1000 + "\u0301e\u0301"
    for offset, share in ((0, 3), (1, 2), (4, 997), (4997, 10)):
        piece: dict[str, Any] = {"status": "success", "data": {"content": wide, "warnings": []}}
        slice_content(piece, offset=offset, share=share)
        sliced = json.loads(json.dumps(piece, ensure_ascii=False))
        assert sliced["data"]["content"] == wide[offset : offset + share]
        assert len(sliced["data"]["content"]) == min(share, len(wide) - offset)

    # End to end: the content of a text upload, cut and read on.
    text = "\n\n".join(f"Paragraph {index:05d} " + "w" * 44 for index in range(1100))
    with running(mcp_max_content_chars=100_000) as client:
        full = envelope_of(
            call_tool(
                client,
                "chunk",
                {"text": text, "chunking": {"strategy": "recursive"}},
                headers=AUTH,
            )
        )["data"]["content"]
        assert len(full) > 60_000
        cut = call_tool(
            client,
            "chunk",
            {"text": text, "chunking": {"strategy": "recursive"}, "max_chars": 50_000},
            headers=AUTH,
        )
        data = envelope_of(cut)["data"]
        assert data["content"] == full[:50_000]
        assert TRUNCATED_WARNING in data["warnings"]
        assert hints_of(cut) == [
            f"Cut at 50000 of {len(full)} characters; call again with content_offset=50000."
        ]
        assert data["chunks"]  # no other field is cut
        tail = envelope_of(
            call_tool(
                client,
                "chunk",
                {
                    "text": text,
                    "chunking": {"strategy": "recursive"},
                    "max_chars": 50_000,
                    "content_offset": 50_000,
                },
                headers=AUTH,
            )
        )["data"]
        assert tail["content"] == full[50_000:100_000]

        over = refusal(
            call_tool(client, "extract_url", {"url": PAGE, "max_chars": 100_001}, headers=AUTH)
        )
        assert over["code"] == "invalid_request"
        assert over["details"][0]["loc"] == ["max_chars"]


def test_llm_key_from_header_only() -> None:
    stored: list[tuple[str, str]] = []
    slow = SlowFetcher({PAGE: fetched(body=ARTICLE)}, 0.3)
    with running(slow, llm_enabled=True, llm_api_key="sk-operator-key") as client:
        app: Any = client.app
        app.state.llm_dispatcher = None  # the P1 job is the pod's to write
        store = app.state.job_store
        put = store.put_llm_key

        async def recording(job_id: str, key: str) -> None:
            stored.append((job_id, key))
            await put(job_id, key)

        store.put_llm_key = recording
        llm = {"strategy": "llm", "schema": SCHEMA}
        result = call_tool(
            client,
            "extract_structured",
            {"url": PAGE, "structured": llm, "wait_seconds": 0},
            headers={**AUTH, "X-LLM-Api-Key": LLM_KEY},
        )
        job_id = envelope_of(result)["data"]["id"]
        assert stored == [(job_id, LLM_KEY)]
        assert LLM_KEY not in json.dumps(result)

        refused = refusal(
            call_tool(
                client,
                "extract_structured",
                {"url": PAGE, "structured": llm, "options": {"llm_key": "x"}},
                headers=AUTH,
            )
        )
        assert refused["code"] == "invalid_request"
        assert refused["details"][0]["loc"] == ["llm_key"]


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_extract_file_base64() -> None:
    """Review Focus 2: base64 with line breaks, a data: prefix, broken, one byte over."""
    pdf = (CORPUS / "text.pdf").read_bytes()
    with running() as client:
        for content in (
            b64(pdf),
            base64.encodebytes(pdf).decode(),  # line breaks every 76 characters
            "data:application/pdf;base64," + b64(pdf),
        ):
            result = call_tool(
                client,
                "extract_file",
                {"content_base64": content, "file_name": "../report.pdf"},
                headers=AUTH,
            )
            assert result["isError"] is False, result
            data = envelope_of(result)["data"]
            assert data["document"]["kind"] == "pdf"
            assert data["document"]["file_name"] == "report.pdf"
            assert data["mode_used"] == "upload"

        for broken in ("not base64!", b64(pdf)[:-1], "data:application/pdf," + b64(pdf)):
            error = refusal(
                call_tool(client, "extract_file", {"content_base64": broken}, headers=AUTH)
            )
            assert (error["code"], error["message"]) == (
                "invalid_request",
                "content_base64 is not valid base64.",
            )

        unknown = refusal(
            call_tool(
                client,
                "extract_file",
                {"content_base64": b64(pdf), "options": {"mode": "browser"}},
                headers=AUTH,
            )
        )
        assert unknown["details"] == [{"loc": ["mode"], "msg": "Extra inputs are not permitted"}]

    with running(document_max_bytes=4096) as client:
        assert (
            call_tool(
                client,
                "extract_file",
                {"content_base64": b64(TEXT.encode().ljust(4096)), "content_type": "text/plain"},
                headers=AUTH,
            )["isError"]
            is False
        )
        over = refusal(
            call_tool(client, "extract_file", {"content_base64": b64(b"x" * 4097)}, headers=AUTH)
        )
        assert (over["code"], over["message"]) == (
            "invalid_request",
            "The file is larger than 4096 bytes.",
        )


def test_file_argument_bounded(client: TestClient) -> None:
    tools = {
        tool["name"]: tool for tool in rpc(client, "tools/list", headers=AUTH)["result"]["tools"]
    }
    app: Any = client.app
    limit = mcp_body_limit(app.state.settings)
    schema = tools["extract_file"]["inputSchema"]["properties"]["content_base64"]
    assert schema["maxLength"] == limit

    # The gate's body limit stops a larger argument first; the model holds it without the gate.
    context = {"settings": app.state.settings}
    ExtractFileArguments.model_validate({"content_base64": "A" * limit}, context=context)
    with pytest.raises(ValidationError) as caught:
        ExtractFileArguments.model_validate({"content_base64": "A" * (limit + 1)}, context=context)
    assert caught.value.errors()[0]["loc"] == ("content_base64",)


def test_structured_and_chunk_tools(client: TestClient) -> None:
    structured = {"strategy": "metadata", "schema": SCHEMA}
    rest = client.post(
        "/v1/structured", json={"url": PAGE, "structured": structured}, headers=AUTH
    ).json()
    result = envelope_of(
        call_tool(
            client, "extract_structured", {"url": PAGE, "structured": structured}, headers=AUTH
        )
    )
    assert result["data"]["structured"]["values"] == rest["data"]["structured"]["values"]
    assert result["data"]["structured"]["values"]  # the page's metadata has a name

    chunking = {"strategy": "recursive", "size": 200}
    rest_chunks = client.post(
        "/v1/chunk", json={"text": TEXT, "chunking": chunking}, headers=AUTH
    ).json()
    chunks = envelope_of(
        call_tool(client, "chunk", {"text": TEXT, "chunking": chunking}, headers=AUTH)
    )
    assert chunks["data"]["chunks"] == rest_chunks["data"]["chunks"]
    assert chunks["data"]["chunks"]
    assert chunks["data"]["mode_used"] == "upload"

    url_chunks = envelope_of(
        call_tool(client, "chunk", {"url": PAGE, "chunking": chunking}, headers=AUTH)
    )
    assert url_chunks["data"]["chunks"]


def test_chunk_text_over_the_limit() -> None:
    with running(document_max_bytes=4096) as client:
        error = refusal(
            call_tool(
                client,
                "chunk",
                {"text": "w " * 2049, "chunking": {"strategy": "recursive"}},
                headers=AUTH,
            )
        )
    assert error["code"] == "invalid_request"
    assert "4096" in error["message"]


def test_screenshot_browser_off_400(client: TestClient) -> None:
    error = refusal(call_tool(client, "screenshot_url", {"url": PAGE}, headers=AUTH))

    assert (error["code"], error["message"]) == (
        "invalid_request",
        "Browser mode is not enabled on this service.",
    )


@pytest.mark.parametrize("limit", [5_000_000, 500])
def test_screenshot_image_attached_and_capped(limit: int) -> None:
    case = BrowserCase(mcp_enabled=True, mcp_image_max_bytes=limit)
    with TestClient(case.app) as client, case.worker(artifact=PNG):
        result = call_tool(client, "screenshot_url", {"url": PAGE}, headers=AUTH)

    assert result["isError"] is False
    envelope = envelope_of(result)
    assert envelope["status"] == "success"
    shot = envelope["data"]["screenshot"]
    assert shot["bytes"] == len(PNG)
    images = [block for block in result["content"] if block["type"] == "image"]
    if limit >= len(PNG):
        assert len(images) == 1
        assert images[0]["mimeType"] == "image/png"
        assert base64.b64decode(images[0]["data"]) == PNG
        assert hints_of(result) == []
    else:
        assert images == []
        assert hints_of(result) == [
            f"The screenshot ({len(PNG)} bytes) is over MCP_IMAGE_MAX_BYTES; download it from "
            f"{shot['url']}."
        ]


def test_screenshot_hint_without_a_size() -> None:
    """A screenshot reference the tool cannot read is linked, never 'None bytes'."""
    state = State({"settings": make_settings()})
    for ref in (
        {"url": "/v1/jobs/j/artifacts/screenshot.png", "bytes": None},
        {"url": "https://elsewhere.example/shot.png", "bytes": 10},
    ):
        hints: list[str] = []
        images: list[Any] = []
        payload = {"status": "success", "data": {"screenshot": ref}}
        asyncio.run(
            ScreenshotUrlTool().attach(
                state, Principal("alice", "user", None), payload, hints, images
            )
        )
        assert images == []
        assert hints == [f"The screenshot is not attached; download it from {ref['url']}."]
