"""``POST /v1/extract/file``: uploads (ruling F17) and ``Pipeline.extract_upload``.

The API tests share one app for the module, whose document pool starts one real MarkItDown child
with the first document (a few seconds); the pipeline tests use a fake pool.
"""

import asyncio
import hashlib
import io
import json
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from starlette.datastructures import UploadFile
from starlette.types import Message
from structlog.testing import capture_logs

from tests.fakes import ARTICLE, PARAGRAPH
from text_extraction.app import create_public_app
from text_extraction.auth import Principal
from text_extraction.conversion_pool import ConversionPool
from text_extraction.documents.pool import DocumentPool
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.extract_models import ExtractResult
from text_extraction.fetch import Fetcher
from text_extraction.file_api import sanitize_file_name
from text_extraction.file_models import FileForm
from text_extraction.jobs import JobStore
from text_extraction.pipeline import create_pipeline
from text_extraction.pipeline_documents import OCR_UNAVAILABLE_WARNING, OcrNeeded
from text_extraction.pipeline_media import (
    TRANSCRIPTION_DISABLED_MESSAGE,
    TRANSCRIPTION_NOT_HANDED_MESSAGE,
    AsrNeeded,
    MediaInputMeta,
)
from text_extraction.settings import Settings

CORPUS = Path(__file__).parent / "documents" / "corpus"
USER_KEY = "user-key-0123456789-raw-secret"
AUTH = {"Authorization": f"Bearer {USER_KEY}"}
USER = Principal(name="alice", role="user", rate_limit_per_minute=None)
GIF = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00"
OGG = b"OggS\x00\x02" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 64
# A file name no log line may carry (ruling F17).
PRIVATE_NAME = "salary-review-kowalski-7f3a.pdf"


def corpus(name: str) -> bytes:
    return (CORPUS / name).read_bytes()


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
        "document_concurrency": 1,
    }
    values.update(overrides)
    return Settings(**values)


def upload(
    client: TestClient,
    body: bytes,
    name: str | None = "report.pdf",
    content_type: str = "application/pdf",
    *,
    headers: dict[str, str] | None = None,
    form: dict[str, str] | None = None,
) -> Any:
    return client.post(
        "/v1/extract/file",
        files={"file": (name, body, content_type)},
        data=form or {},
        headers=AUTH if headers is None else headers,
    )


@pytest.fixture(scope="module")
def registry() -> CollectorRegistry:
    return CollectorRegistry()


@pytest.fixture(scope="module")
def client(registry: CollectorRegistry) -> Iterator[TestClient]:
    app = create_public_app(make_settings(), FakeAsyncRedis(), registry)
    with TestClient(app) as test_client:
        yield test_client


def metric(registry: CollectorRegistry, name: str, **labels: str) -> float:
    return registry.get_sample_value(name, labels) or 0.0


# --- the route ---


def test_upload_pdf(client: TestClient, registry: CollectorRegistry) -> None:
    before = metric(registry, "text_extraction_extract_total", mode="upload", outcome="success")

    response = upload(client, corpus("text.pdf"))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success", body["error"]
    data = body["data"]
    assert data["mode_used"] == "upload"
    assert data["url"] is None
    assert data["final_url"] is None
    assert data["upstream_status"] is None
    assert data["attempts"] == 0
    assert data["content_type"] == "application/pdf"
    assert "Photosynthese" in data["content"]
    assert data["document"]["kind"] == "pdf"
    assert data["document"]["converter"] == "markitdown"
    assert data["document"]["pages"] == 3
    assert data["document"]["file_name"] == "report.pdf"
    assert data["cache"] == "bypass"
    assert data["compliance"]["decision"] == "skipped"
    # Computed, never compared with an earlier one (F17).
    assert data["fingerprint"] is not None
    assert data["fingerprint"]["changed"] is None
    assert data["links"] is None
    assert body["trace_id"]
    after = metric(registry, "text_extraction_extract_total", mode="upload", outcome="success")
    assert after == before + 1


def test_upload_stores_no_result_and_no_fingerprint(client: TestClient) -> None:
    upload(client, corpus("text.pdf"))

    async def keys() -> list[bytes]:
        valkey = cast(FastAPI, client.app).state.valkey
        return [key async for key in valkey.scan_iter("*")]

    stored = client.portal.call(keys)  # type: ignore[union-attr]
    assert [key for key in stored if key.startswith((b"res:", b"fp:"))] == []


def test_upload_options(client: TestClient) -> None:
    response = upload(
        client,
        corpus("lesson.docx"),
        "lesson.docx",
        "application/octet-stream",
        form={"format": "text", "metadata": "false"},
    )

    data = response.json()["data"]
    assert data["format"] == "text"
    assert data["metadata"] is None
    assert data["document"]["kind"] == "docx"
    assert data["content_type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


def test_upload_requires_auth(client: TestClient) -> None:
    response = upload(client, corpus("text.pdf"), headers={})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_upload_rate_limited() -> None:
    app = create_public_app(
        make_settings(rate_limit_per_minute=1), FakeAsyncRedis(), CollectorRegistry()
    )
    with TestClient(app) as test_client:
        first = upload(test_client, GIF, "a.gif", "image/gif")
        second = upload(test_client, GIF, "a.gif", "image/gif")

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limited"


@dataclass(frozen=True)
class Answer:
    status: int
    pulled: int  # chunks of the body the app read
    body: bytes
    headers: dict[str, str]

    def json(self) -> Any:
        return json.loads(self.body)


async def asgi_call(
    app: FastAPI,
    chunks: list[bytes],
    headers: list[tuple[bytes, bytes]],
    path: str = "/v1/extract/file",
) -> Answer:
    """POST ``chunks`` to ``path`` through the whole ASGI stack."""
    pulled = 0
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal pulled
        if pulled < len(chunks):
            pulled += 1
            return {
                "type": "http.request",
                "body": chunks[pulled - 1],
                "more_body": pulled < len(chunks),
            }
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test"), *headers],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }
    await app(scope, receive, send)
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    answer_headers = {name.decode(): value.decode() for name, value in start["headers"]}
    return Answer(start["status"], pulled, body, answer_headers)


def asgi_post(
    app: FastAPI, chunks: list[bytes], headers: list[tuple[bytes, bytes]]
) -> tuple[int, int, bytes]:
    """POST ``chunks`` to the upload route: the answer's status, how many chunks the app read,
    and the answer's body."""
    answer = asyncio.run(asgi_call(app, chunks, headers))
    return answer.status, answer.pulled, answer.body


BOUNDARY = b"upload-boundary"
# What the route's body limit allows beyond the file (R18): 64 KiB for the part headers, the small
# fields and the boundaries, and 64 KiB for each of `structured` and `chunking`.
FORM_OVERHEAD = 192 * 1024
MULTIPART = [(b"content-type", b"multipart/form-data; boundary=" + BOUNDARY)]
BEARER = [(b"authorization", AUTH["Authorization"].encode())]
PART_HEAD = (
    b"--" + BOUNDARY + b"\r\n"
    b'Content-Disposition: form-data; name="file"; filename="a.pdf"\r\n'
    b"Content-Type: application/pdf\r\n\r\n"
)


def test_upload_over_limit_413_before_parse() -> None:
    settings = make_settings(document_max_bytes=2048)
    app = create_public_app(settings, FakeAsyncRedis(), CollectorRegistry())
    limit = 2048 + FORM_OVERHEAD
    declared = [(b"content-length", str(limit + 1).encode())]

    status, pulled, body = asgi_post(
        app, [PART_HEAD, b"x" * limit], [*MULTIPART, *BEARER, *declared]
    )

    assert status == 413
    assert pulled == 0  # nothing was read, so nothing was parsed
    assert json.loads(body)["error"]["code"] == "invalid_request"


def test_upload_at_the_route_limit_is_read() -> None:
    """The route's limit is DOCUMENT_MAX_BYTES + 192 KiB, far above the JSON default."""
    settings = make_settings(document_max_bytes=200_000, request_max_body_bytes=1024)
    app = create_public_app(settings, FakeAsyncRedis(), CollectorRegistry())
    with TestClient(app) as test_client:
        response = upload(test_client, GIF + b"\x00" * 150_000, "a.gif", "image/gif")

    assert response.status_code == 200
    assert response.json()["error"]["code"] == "unsupported_content_type"


def test_chunked_upload_over_limit_413_without_reading_it_all() -> None:
    settings = make_settings(document_max_bytes=2048)
    app = create_public_app(settings, FakeAsyncRedis(), CollectorRegistry())
    chunks = [PART_HEAD, *([b"x" * 65_536] * 40)]  # 2.6 MB, no Content-Length

    status, pulled, body = asgi_post(app, chunks, [*MULTIPART, *BEARER])

    assert status == 413
    assert pulled <= 5  # refused at the chunk that went over 198,656 bytes (2,048 + 192 KiB)
    assert json.loads(body)["error"]["code"] == "invalid_request"


UPLOAD_CHUNKS = [PART_HEAD, *([b"x" * 65_536] * 8)]


def comparable(answer: Answer) -> tuple[int, Any, dict[str, str]]:
    """What must match between two routes' refusals: status, envelope without the trace id,
    and the headers that are not about the body's length."""
    envelope = answer.json()
    envelope.pop("trace_id", None)
    headers = {k: v for k, v in answer.headers.items() if k not in ("content-length",)}
    return answer.status, envelope, headers


def test_upload_without_key_is_401_before_the_body_is_read() -> None:
    app = create_public_app(make_settings(), FakeAsyncRedis(), CollectorRegistry())

    async def scenario() -> tuple[Answer, Answer]:
        refused = await asgi_call(app, UPLOAD_CHUNKS, MULTIPART)
        extract = await asgi_call(
            app,
            [b'{"url": "https://example.org/"}'],
            [(b"content-type", b"application/json")],
            "/v1/extract",
        )
        return refused, extract

    refused, extract = asyncio.run(scenario())

    assert refused.status == 401
    assert refused.pulled == 0  # nothing received, nothing spooled
    assert refused.json()["error"]["code"] == "unauthorized"
    assert comparable(refused) == comparable(extract)


def test_upload_over_rate_limit_is_429_before_the_body_is_read() -> None:
    app = create_public_app(
        make_settings(rate_limit_per_minute=1), FakeAsyncRedis(), CollectorRegistry()
    )

    async def scenario() -> tuple[Answer, Answer]:
        await app.state.rate_limiter.hit("alice", 1)  # the minute's one request is used up
        refused = await asgi_call(app, UPLOAD_CHUNKS, [*MULTIPART, *BEARER])
        extract = await asgi_call(
            app,
            [b'{"url": "https://example.org/"}'],
            [(b"content-type", b"application/json"), *BEARER],
            "/v1/extract",
        )
        return refused, extract

    refused, extract = asyncio.run(scenario())

    assert refused.status == 429
    assert refused.pulled == 0
    assert refused.json()["error"]["code"] == "rate_limited"
    assert "retry-after" in refused.headers
    assert comparable(refused) == comparable(extract)


def test_upload_counts_once_against_the_rate_limit() -> None:
    app = create_public_app(
        make_settings(rate_limit_per_minute=2), FakeAsyncRedis(), CollectorRegistry()
    )
    with TestClient(app) as test_client:
        statuses = [upload(test_client, GIF, "a.gif", "image/gif").status_code for _ in range(3)]

    assert statuses == [200, 200, 429]


def test_file_over_document_limit_fails_too_large() -> None:
    settings = make_settings(document_max_bytes=4096)
    app = create_public_app(settings, FakeAsyncRedis(), CollectorRegistry())
    with TestClient(app) as test_client:
        response = upload(test_client, b"%PDF-1.7\n" + b"x" * 8192)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "too_large"
    assert body["data"]["mode_used"] == "upload"


def test_upload_part_is_closed_before_conversion(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []
    close = UploadFile.close

    async def recording_close(self: UploadFile) -> None:
        closed.append(True)
        await close(self)

    monkeypatch.setattr(UploadFile, "close", recording_close)
    pipeline = cast(FastAPI, client.app).state.pipeline
    convert = pipeline.extract_upload
    seen: list[bool] = []

    async def extract_upload(*args: Any, **kwargs: Any) -> Envelope[ExtractResult]:
        seen.append(bool(closed))
        return cast(Envelope[ExtractResult], await convert(*args, **kwargs))

    monkeypatch.setattr(pipeline, "extract_upload", extract_upload)

    upload(client, GIF, "a.gif", "image/gif")

    assert seen == [True]  # the spooled part was closed (and deleted) before the conversion


def test_two_files_refused(client: TestClient) -> None:
    response = client.post(
        "/v1/extract/file",
        files=[
            ("file", ("a.pdf", corpus("text.pdf"), "application/pdf")),
            ("file", ("b.pdf", corpus("text.pdf"), "application/pdf")),
        ],
        headers=AUTH,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert "Too many files" in response.json()["error"]["message"]


def test_too_many_fields_refused(client: TestClient) -> None:
    fields = {f"field{n}": "x" for n in range(9)}

    response = upload(client, GIF, "a.gif", "image/gif", form=fields)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert "Too many fields" in response.json()["error"]["message"]


# R18: `structured` and `chunking` may be up to 64 KiB each; every other field 1 KiB.
WIDE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        f"field_{n:02d}": {"type": "string", "description": "A field of the worksheet."}
        for n in range(24)
    }
    | {"title": {"type": "string"}},
}
WIDE_STRUCTURED = json.dumps({"schema": WIDE_SCHEMA, "strategy": "metadata"})


def test_structured_form_field_over_1_kib_is_accepted(client: TestClient) -> None:
    assert len(WIDE_STRUCTURED.encode()) > 1024  # 25 fields

    response = upload(
        client,
        ARTICLE.encode(),
        "page.html",
        "text/html",
        form={"structured": WIDE_STRUCTURED, "chunking": '{"strategy": "fixed"}'},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["structured"]["values"]["title"] == "Photosynthesis explained"
    assert len(data["structured"]["values"]) == 25
    assert data["chunks"]


def test_option_form_field_over_64_kib_is_422(client: TestClient) -> None:
    padded = WIDE_STRUCTURED + " " * (65 * 1024)  # still valid JSON, but over 64 KiB

    for name in ("structured", "chunking"):
        response = upload(client, ARTICLE.encode(), "page.html", "text/html", form={name: padded})

        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "invalid_request"
        assert response.json()["error"]["message"] == "Part exceeded maximum size of 64KB."


@pytest.mark.parametrize("name", ["pii", "format", "ocr", "wait_seconds", "metadata"])
def test_other_form_field_over_1_kib_is_422(client: TestClient, name: str) -> None:
    value = '{"mode": "mask"}' + " " * 1024 if name == "pii" else "x" * 1025

    response = upload(client, GIF, "a.gif", "image/gif", form={name: value})

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.json()["error"]["message"] == "Part exceeded maximum size of 1KB."


def test_body_limit_holds_a_full_file_and_both_option_fields_at_their_limit() -> None:
    settings = make_settings(document_max_bytes=2048)
    app = create_public_app(settings, FakeAsyncRedis(), CollectorRegistry())
    structured = WIDE_STRUCTURED.ljust(64 * 1024 - 16)
    chunking = '{"strategy": "fixed"}'.ljust(64 * 1024 - 16)
    page = (b"<html><body><p>" + b"word " * 400)[:2048]  # the file at DOCUMENT_MAX_BYTES

    with TestClient(app) as test_client:
        response = upload(
            test_client,
            page,
            "page.html",
            "text/html",
            form={"structured": structured, "chunking": chunking},
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["chunks"]


@pytest.mark.parametrize(
    "form",
    [
        {"fromat": "text"},
        {"format": "pdf"},
        {"ocr": "maybe"},
        {"wait_seconds": "61"},
        {"pii": "scramble"},
        {"pii": "{not json"},
        {"pii": '{"mode": "mask", "entities": []}'},
    ],
)
def test_invalid_form_is_422(client: TestClient, form: dict[str, str]) -> None:
    response = upload(client, GIF, "a.gif", "image/gif", form=form)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("pii", ["mask", '{"mode": "annotate", "entities": ["PERSON"]}'])
def test_pii_form_field_is_a_mode_or_a_json_object(client: TestClient, pii: str) -> None:
    response = upload(client, GIF, "a.gif", "image/gif", form={"pii": pii})

    # The form parsed (else 422); this service has no PII handling.
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "PII handling is not enabled on this service."


def test_pii_disabled_is_400(client: TestClient) -> None:
    response = upload(client, PARAGRAPH.encode(), "notes.txt", "text/plain", form={"pii": "mask"})

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["message"] == "PII handling is not enabled on this service."


def test_upload_annotate_with_html_is_422() -> None:
    from tests.test_pii_dispatch import PiiService

    service = PiiService()
    with TestClient(service.app) as pii_client:
        response = upload(
            pii_client,
            PARAGRAPH.encode(),
            "notes.txt",
            "text/plain",
            form={"format": "html", "pii": "annotate"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["message"] == "annotate needs format markdown or text."


@pytest.mark.parametrize(
    ("pii", "mode"),
    [
        ("mask", "mask"),
        ('{"mode": "annotate", "entities": ["PERSON", "EMAIL_ADDRESS"]}', "annotate"),
    ],
    ids=["bare", "json"],
)
def test_upload_with_pii_form_field_bare_and_json(pii: str, mode: str) -> None:
    from tests.test_pii_dispatch import EMAIL, NAME, PiiService

    service = PiiService()
    text = f"{PARAGRAPH * 3}\n\nNotes by {NAME}, {EMAIL}.\n"
    with TestClient(service.app) as pii_client, service.nlp():
        response = upload(
            pii_client,
            text.encode(),
            f"notes-{NAME}.txt",
            "text/plain",
            form={"pii": pii},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "success", body["error"]
        data = body["data"]
        assert data["mode_used"] == "upload"
        assert data["pii"]["mode"] == mode
        if mode == "mask":
            assert NAME not in data["content"]
            assert "[PERSON]" in data["content"]
        else:  # the text stays, the entities are listed
            assert NAME in data["content"]
            found = {(item["field"], item["type"]) for item in data["pii"]["items"]}
            assert ("content", "PERSON") in found
            assert ("content", "EMAIL_ADDRESS") in found
        assert service.wait_for_no_jobs() == []  # the nlp job of a sync answer is deleted


def test_upload_with_pii_counts_the_hop_in_its_total_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_pii_dispatch import NAME, PiiService
    from text_extraction.pii_dispatch import PiiHandoff

    hop = PiiHandoff.__call__

    async def slow(self: PiiHandoff, envelope: Any, handover: Any) -> Any:
        await asyncio.sleep(0.3)
        return await hop(self, envelope, handover)

    monkeypatch.setattr(PiiHandoff, "__call__", slow)
    service = PiiService()
    with TestClient(service.app) as pii_client, service.nlp():
        response = upload(
            pii_client,
            f"{PARAGRAPH * 3}\n\nBy {NAME}.\n".encode(),
            "notes.txt",
            "text/plain",
            form={"pii": "mask"},
        )

        body = response.json()
        assert body["status"] == "success", body["error"]
        timings = body["data"]["timings"]
        assert timings["total_ms"] >= 300
        assert timings["pii_ms"] >= 0
        assert body["trace_id"]


def test_missing_file_is_422(client: TestClient) -> None:
    response = client.post("/v1/extract/file", data={"format": "text"}, headers=AUTH)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("a\x00b.pdf", "ab.pdf"),
        ("C:\\Users\\jan\\report.pdf", "report.pdf"),
        ("x" * 300, "x" * 255),
        ("bericht\u202e.pdf", "bericht.pdf"),
        ("  spaced.pdf  ", "spaced.pdf"),
        ("folder/", None),
        ("..", None),
        ("\x01\x02", None),
        ("", None),
        (None, None),
    ],
)
def test_file_name_sanitised(name: str | None, expected: str | None) -> None:
    assert sanitize_file_name(name) == expected


def test_file_name_sanitised_in_the_result(client: TestClient) -> None:
    response = upload(client, corpus("text.pdf"), "../../etc/re\u202eport.pdf")

    assert response.json()["data"]["document"]["file_name"] == "report.pdf"


def test_html_upload_uses_html_converter(client: TestClient) -> None:
    response = upload(client, ARTICLE.encode(), "page.html", "text/html; charset=utf-8")

    body = response.json()
    assert body["status"] == "success", body["error"]
    data = body["data"]
    assert data["content_type"] == "text/html"
    assert data["document"] is None
    assert data["title"] == "Photosynthesis explained"
    assert data["metadata"]["license"]["id"] == "CC-BY-SA-4.0"
    # No base URL: relative links stay as they are.
    assert "(/learn/respiration.html)" in data["content"]
    assert data["mode_used"] == "upload"
    assert data["cache"] == "bypass"


def test_html_named_pdf_upload_follows_the_bytes(client: TestClient) -> None:
    """An upload named report.pdf, sent as application/pdf, that is HTML (Review Focus 2)."""
    response = upload(client, ARTICLE.encode(), "report.pdf", "application/pdf")

    body = response.json()
    assert body["status"] == "success", body["error"]
    data = body["data"]
    assert data["content_type"] == "text/html"
    assert data["document"] is None
    assert data["title"] == "Photosynthesis explained"


def test_plain_text_upload(client: TestClient) -> None:
    response = upload(client, (PARAGRAPH * 3).encode(), "notes.txt", "text/plain")

    data = response.json()["data"]
    assert response.json()["status"] == "success"
    assert data["content_type"] == "text/plain"
    assert data["content"].startswith("Photosynthesis is the process")


MARKDOWN = (
    f"# Photosynthesis notes\n\n{(PARAGRAPH * 3).strip()}\n\n"
    "- **Light** reactions\n- Calvin cycle\n"
)


@pytest.mark.parametrize("declared", ["text/markdown", "text/x-markdown; charset=utf-8"])
def test_markdown_upload_is_converted_as_markdown(client: TestClient, declared: str) -> None:
    """Issue #15: a Markdown upload is read as Markdown, for every output format."""
    answers = {
        fmt: upload(client, MARKDOWN.encode(), "notes.md", declared, form={"format": fmt}).json()
        for fmt in ("markdown", "html", "text")
    }

    for body in answers.values():
        assert body["status"] == "success", body["error"]
        assert body["data"]["content_type"] == declared.split(";", 1)[0]
        assert body["data"]["title"] == "Photosynthesis notes"
        assert body["data"]["document"] is None
    assert answers["markdown"]["data"]["content"] == MARKDOWN.strip()
    crlf = upload(client, MARKDOWN.replace("\n", "\r\n").encode(), "notes.md", declared).json()
    assert crlf["data"]["title"] == "Photosynthesis notes"  # Windows line ends
    assert crlf["data"]["content"] == MARKDOWN.strip()
    html = answers["html"]["data"]["content"]
    assert "<h1>Photosynthesis notes</h1>" in html
    assert "<strong>Light</strong>" in html
    text = answers["text"]["data"]["content"]
    assert text.startswith("Photosynthesis notes\nPhotosynthesis is the process")
    assert "#" not in text
    assert "**" not in text


def test_unsupported_upload(client: TestClient) -> None:
    response = upload(client, GIF, "a.gif", "image/gif")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "unsupported_content_type"
    assert body["data"]["mode_used"] == "upload"
    assert body["data"]["url"] is None


def test_image_upload_without_ocr_is_unsupported(client: TestClient) -> None:
    response = upload(client, PNG, "scan.png", "image/png")

    body = response.json()
    assert body["error"]["code"] == "unsupported_content_type"
    assert body["error"]["message"] == "OCR is not enabled on this service."


def test_ocr_always_while_docling_off_400(client: TestClient) -> None:
    response = upload(client, corpus("text.pdf"), form={"ocr": "always"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.json()["error"]["message"] == "OCR is not enabled on this service."


def test_openapi_documents_multipart(client: TestClient) -> None:
    spec = cast(FastAPI, client.app).openapi()
    operation = spec["paths"]["/v1/extract/file"]["post"]
    content = operation["requestBody"]["content"]

    assert list(content) == ["multipart/form-data"]
    schemas = spec["components"]["schemas"]
    body = schemas[content["multipart/form-data"]["schema"]["$ref"].rsplit("/", 1)[-1]]
    assert body["properties"]["file"]["format"] == "binary"
    assert body["required"] == ["file"]
    form = schemas[body["allOf"][0]["$ref"].rsplit("/", 1)[-1]]
    assert set(form["properties"]) == {
        "format",
        "metadata",
        "ocr",
        "wait_seconds",
        "pii",
        "structured",
        "chunking",
    }
    assert form["properties"]["wait_seconds"]["default"] == 25
    for name in ("pii", "structured", "chunking"):
        assert form["properties"][name]["type"] == "string"
    assert "202" in operation["responses"]


def test_file_name_not_logged(client: TestClient) -> None:
    with capture_logs() as logs:
        upload(client, corpus("text.pdf"), PRIVATE_NAME)
        upload(client, corpus("encrypted.pdf"), PRIVATE_NAME)
        upload(client, GIF, PRIVATE_NAME, "image/gif")
        upload(client, ARTICLE.encode(), PRIVATE_NAME, "text/html")
        upload(client, GIF, PRIVATE_NAME, "image/gif", form={"format": "pdf"})  # 422

    assert any(log["event"] == "extract_done" for log in logs)
    assert any(log["event"] == "request" for log in logs)
    dumped = json.dumps(logs, default=str)
    assert "salary-review" not in dumped
    assert "kowalski" not in dumped


# --- Pipeline.extract_upload ---


class FakePool:
    """A document pool whose child answers every call with ``answer``."""

    def __init__(self, answer: object) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    async def start(self) -> None:
        pass

    async def run(self, function_name: str, payload: dict[str, Any], deadline: float) -> Any:
        self.calls.append(payload)
        return self.answer

    async def shutdown(self) -> None:
        pass


def reply(**fields: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "content": "# Lesson\n\n" + PARAGRAPH * 3,
        "title": "Lesson",
        "warnings": [],
        "pages": 1,
        "pages_converted": 1,
        "needs_ocr": False,
        "metadata": None,
    }
    values.update(fields)
    return values


def run_upload(
    answer: object,
    body: bytes = b"%PDF-1.7\n",
    *,
    handoff: Any = None,
    asr_handoff: Any = None,
    name: str = "lesson.pdf",
    content_type: str = "application/pdf",
    form: FileForm | None = None,
    **overrides: Any,
) -> tuple[Envelope[ExtractResult], FakePool, CollectorRegistry]:
    settings = make_settings(**overrides)
    pool = FakePool(answer)
    registry = CollectorRegistry()

    async def scenario() -> Envelope[ExtractResult]:
        fetcher = Fetcher(settings)
        pipeline = create_pipeline(
            settings,
            FakeAsyncRedis(),
            fetcher,
            registry,
            ConversionPool(1),
            document_pool=cast(DocumentPool, pool),
        )
        try:
            return await pipeline.extract_upload(
                body,
                name,
                form or FileForm(),
                USER,
                content_type=content_type,
                ocr_handoff=handoff,
                asr_handoff=asr_handoff,
            )
        finally:
            await fetcher.aclose()

    return asyncio.run(scenario()), pool, registry


def test_extract_upload_converts_through_the_document_steps() -> None:
    envelope, pool, registry = run_upload(reply(pages=4, pages_converted=4))

    assert envelope.status == "success"
    data = envelope.data
    assert data is not None
    assert data.document is not None
    assert data.document.file_name == "lesson.pdf"
    assert data.document.pages == 4
    assert pool.calls[0]["base"] is None  # no URL to resolve links against
    total = "text_extraction_document_conversions_total"
    labels = {"converter": "markitdown", "kind": "pdf", "outcome": "success"}
    assert registry.get_sample_value(total, labels) == 1


def test_extract_upload_without_handoff_answers_the_draft() -> None:
    envelope, _, _ = run_upload(reply(needs_ocr=True), docling_enabled=True)

    assert envelope.status == "success"
    assert envelope.data is not None
    assert OCR_UNAVAILABLE_WARNING in envelope.data.warnings


def test_extract_upload_image_without_handoff_is_unsupported() -> None:
    envelope, _, _ = run_upload(AssertionError("not converted"), PNG, docling_enabled=True)

    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.UNSUPPORTED_CONTENT_TYPE
    assert "not enabled" not in envelope.error.message  # OCR exists; no handoff was given
    assert "OCR" in envelope.error.message


def test_extract_upload_hands_over_after_the_deadline() -> None:
    handed: list[OcrNeeded] = []

    async def handoff(
        needed: OcrNeeded, without: Envelope[ExtractResult]
    ) -> Envelope[ExtractResult]:
        handed.append(needed)
        await asyncio.sleep(2.5)  # longer than the whole extraction may take
        assert needed.draft is not None
        assert needed.draft.data is not None
        data = needed.draft.data.model_copy(update={"title": "From OCR"})
        return needed.draft.model_copy(update={"data": data})

    started = time.monotonic()
    envelope, _, _ = run_upload(
        reply(needs_ocr=True), handoff=handoff, docling_enabled=True, extract_timeout_seconds=2
    )

    assert time.monotonic() - started >= 2.5
    assert envelope.status == "success"
    assert envelope.data is not None
    assert envelope.data.title == "From OCR"
    assert envelope.data.timings.total_ms >= 2500
    (needed,) = handed
    meta = needed.input.meta
    assert meta.file_name == "lesson.pdf"
    assert meta.final_url is None
    assert meta.upstream_status is None
    assert meta.needs_ocr is True
    assert meta.raw_sha256 == hashlib.sha256(b"%PDF-1.7\n").hexdigest()
    assert meta.compliance is not None
    assert meta.compliance.decision == "skipped"
    assert needed.input.body == b"%PDF-1.7\n"


def test_openapi_documents_the_warnings_and_licence_sources(client: TestClient) -> None:
    schemas = cast(FastAPI, client.app).openapi()["components"]["schemas"]

    warnings = schemas["ExtractResult"]["properties"]["warnings"]["description"]
    for warning in (
        "ocr_unavailable",
        "pages_truncated",
        "pages_partial",
        "document_fetched_directly",
        "browser_unavailable",
        "content_not_stored_too_large",
    ):
        assert f"`{warning}`" in warnings
    source = schemas["License"]["properties"]["source"]
    assert "xmp" in source["enum"]
    assert "`xmp`" in source["description"]


def test_a_file_name_that_is_not_utf8_is_sanitised() -> None:
    """Review Focus 5: a multipart file name in Latin-1 bytes (not UTF-8) converts the upload
    and reports a sanitised name, never a 4xx or 5xx."""
    app = create_public_app(make_settings(), FakeAsyncRedis(), CollectorRegistry())
    head = (
        b"--" + BOUNDARY + b"\r\n"
        b'Content-Disposition: form-data; name="file"; filename="\xc4rger\x85bericht.pdf"\r\n'
        b"Content-Type: application/pdf\r\n\r\n"
    )
    tail = b"\r\n--" + BOUNDARY + b"--\r\n"

    async def scenario() -> Answer:
        async with app.router.lifespan_context(app):
            return await asgi_call(app, [head + corpus("text.pdf") + tail], [*MULTIPART, *BEARER])

    answer = asyncio.run(scenario())

    assert answer.status == 200
    body = answer.json()
    assert body["status"] == "success", body["error"]
    name = body["data"]["document"]["file_name"]
    assert name is not None
    assert name.endswith("bericht.pdf")
    assert name.isprintable()
    assert name == "\u00c4rgerbericht.pdf"  # read as Latin-1, the C1 control removed


# --- audio and video uploads (ruling M14) ---


def test_audio_upload_while_asr_off() -> None:
    settings = make_settings()  # ASR_ENABLED is off by default
    app = create_public_app(settings, FakeAsyncRedis(), CollectorRegistry())
    with TestClient(app) as test_client:
        response = upload(test_client, OGG, "talk.ogg", "audio/ogg")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "unsupported_content_type"
    assert body["error"]["retryable"] is False
    assert body["error"]["message"] == TRANSCRIPTION_DISABLED_MESSAGE
    data = body["data"]
    assert data["mode_used"] == "upload"
    assert data["url"] is None
    assert data["content_type"] == "audio/ogg"
    assert data["media"] == {
        "platform": "upload",
        "transcript_source": None,
        "transcript_language": None,
        "available_languages": [],
        "duration_seconds": None,
        "uploader": None,
        "asr_model": None,
        "embeds": [],
    }


def test_audio_upload_over_media_limit() -> None:
    settings = make_settings(
        asr_enabled=True, media_upload_max_bytes=1_000_000, document_max_bytes=4096
    )
    app = create_public_app(settings, FakeAsyncRedis(), CollectorRegistry())
    with TestClient(app) as test_client:
        response = upload(test_client, OGG + b"\x00" * 1_000_000, "talk.ogg", "audio/ogg")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "too_large"
    assert "1000000" in body["error"]["message"]
    assert body["data"]["media"]["platform"] == "upload"


def test_body_limit_is_max_of_both() -> None:
    settings = make_settings(
        asr_enabled=True, media_upload_max_bytes=1_000_000, document_max_bytes=4096
    )
    app = create_public_app(settings, FakeAsyncRedis(), CollectorRegistry())
    declared = [(b"content-length", str(1_000_000 + 1024).encode())]

    # Not refused by the middleware: it reads the body (the part is cut, so the form is bad).
    status, pulled, body = asgi_post(
        app, [PART_HEAD, b"x" * 2048], [*MULTIPART, *BEARER, *declared]
    )
    assert pulled > 0, status
    assert status == 422  # the form's parse found no complete file part, not the body limit
    assert json.loads(body)["error"]["code"] == "invalid_request"

    with TestClient(app) as test_client:
        response = upload(test_client, b"%PDF-1.7\n" + b"x" * (1_000_000 + 1024))
    assert response.status_code == 200
    assert response.json()["error"]["code"] == "too_large"
    assert "4096" in response.json()["error"]["message"]

    over = [(b"content-length", str(1_000_000 + FORM_OVERHEAD + 1).encode())]
    status, pulled, _ = asgi_post(app, [PART_HEAD, b"x" * 2048], [*MULTIPART, *BEARER, *over])
    assert (status, pulled) == (413, 0)


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"asr_enabled": True, "media_enabled": False},
        {"asr_enabled": True, "jobs_enabled": False},
    ],
    ids=["asr off", "media off", "jobs off"],
)
def test_body_limit_stays_with_documents_when_nothing_transcribes(
    overrides: dict[str, Any],
) -> None:
    """MEDIA_UPLOAD_MAX_BYTES widens the path only while uploads can be transcribed."""
    settings = make_settings(media_upload_max_bytes=1_000_000, document_max_bytes=4096, **overrides)
    app = create_public_app(settings, FakeAsyncRedis(), CollectorRegistry())
    declared = [(b"content-length", str(1_000_000 + 1024).encode())]

    status, pulled, _ = asgi_post(app, [PART_HEAD, b"x" * 2048], [*MULTIPART, *BEARER, *declared])

    assert (status, pulled) == (413, 0)


def test_audio_upload_with_media_off_is_unsupported() -> None:
    settings = make_settings(asr_enabled=True, media_enabled=False)
    app = create_public_app(settings, FakeAsyncRedis(), CollectorRegistry())
    with TestClient(app) as test_client:
        response = upload(test_client, OGG, "talk.ogg", "audio/ogg")

    body = response.json()
    assert body["error"]["code"] == "unsupported_content_type"
    assert body["error"]["message"] == "Content type audio/ogg is not supported."
    assert body["data"]["media"] is None


def test_audio_upload_hands_to_asr() -> None:
    handed: list[AsrNeeded] = []
    stand_ins: list[Envelope[ExtractResult]] = []

    async def handoff(
        needed: AsrNeeded, without: Envelope[ExtractResult]
    ) -> Envelope[ExtractResult]:
        handed.append(needed)
        stand_ins.append(without)
        await asyncio.sleep(2.5)  # longer than the whole extraction may take
        return without.model_copy(update={"status": "success", "error": None})

    started = time.monotonic()
    envelope, pool, registry = run_upload(
        AssertionError("not converted"),
        OGG,
        asr_handoff=handoff,
        name="talk.ogg",
        content_type="audio/ogg",
        asr_enabled=True,
        extract_timeout_seconds=2,
    )

    assert time.monotonic() - started >= 2.5  # handed over once the deadline's steps are done
    assert pool.calls == []  # no document conversion
    assert envelope.status == "success"
    assert envelope.data is not None
    assert envelope.data.timings.total_ms >= 2500
    (needed,) = handed
    assert needed.draft is None
    assert needed.input == MediaInputMeta(
        source="upload",
        platform="upload",
        url=None,
        extractor=None,
        kind="ogg",
        file_name="talk.ogg",
        duration=None,
        languages=[],
        compliance=needed.input.compliance,
        attempts=0,
    )
    assert needed.input.compliance is not None
    assert needed.input.compliance.decision == "skipped"
    (stand_in,) = stand_ins  # what the answer is while the handoff has not taken over
    assert stand_in.status == "failed"
    assert stand_in.error is not None
    assert stand_in.error.code == ErrorCode.NO_TRANSCRIPT_AVAILABLE
    assert stand_in.data is not None
    assert stand_in.data.media is not None
    assert stand_in.data.media.platform == "upload"
    total = "text_extraction_extract_total"
    assert registry.get_sample_value(total, {"mode": "upload", "outcome": "success"}) == 1


def test_audio_upload_without_a_handoff_answers_that_it_cannot_be_handed_on() -> None:
    envelope, _, _ = run_upload(
        AssertionError("not converted"),
        OGG,
        asr_enabled=True,
        name="a.ogg",
        content_type="audio/ogg",
    )

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.NO_TRANSCRIPT_AVAILABLE
    assert envelope.error.message == TRANSCRIPTION_NOT_HANDED_MESSAGE
    assert envelope.data is not None
    assert envelope.data.content_type == "audio/ogg"


def test_asr_off_never_calls_the_handoff() -> None:
    async def handoff(
        needed: AsrNeeded, without: Envelope[ExtractResult]
    ) -> Envelope[ExtractResult]:
        raise AssertionError("ASR is off")

    envelope, _, _ = run_upload(
        AssertionError("not converted"),
        OGG,
        asr_handoff=handoff,
        name="a.ogg",
        content_type="audio/ogg",
    )

    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.UNSUPPORTED_CONTENT_TYPE
    assert envelope.error.message == TRANSCRIPTION_DISABLED_MESSAGE


@pytest.mark.parametrize("declared", ["application/pdf", "text/html", "application/octet-stream"])
def test_the_bytes_decide_that_an_upload_is_media(declared: str) -> None:
    envelope, pool, _ = run_upload(
        AssertionError("not converted"),
        OGG,
        asr_enabled=True,
        name="report.pdf",
        content_type=declared,
    )

    assert pool.calls == []
    assert envelope.data is not None
    assert envelope.data.media is not None
    assert envelope.data.media.platform == "upload"
    assert envelope.data.content_type == "audio/ogg"


@pytest.mark.parametrize("declared", ["audio/mpeg", "video/mp4", "audio/ogg"])
def test_a_declared_audio_type_does_not_make_garbage_media(declared: str) -> None:
    handed: list[AsrNeeded] = []

    async def handoff(
        needed: AsrNeeded, without: Envelope[ExtractResult]
    ) -> Envelope[ExtractResult]:
        handed.append(needed)
        return without

    envelope, _, _ = run_upload(
        AssertionError("not converted"),
        b"this is not audio, whatever the part says" * 4,
        asr_handoff=handoff,
        name="talk.mp3",
        content_type=declared,
        asr_enabled=True,
    )

    assert handed == []
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.UNSUPPORTED_CONTENT_TYPE
    assert envelope.data is not None
    assert envelope.data.media is None


def test_an_audio_upload_logs_no_file_name() -> None:
    settings = make_settings(asr_enabled=True)
    app = create_public_app(settings, FakeAsyncRedis(), CollectorRegistry())
    with TestClient(app) as test_client, capture_logs() as logs:
        upload(test_client, OGG, PRIVATE_NAME.replace(".pdf", ".ogg"), "audio/ogg")

    done = [log for log in logs if log["event"] == "extract_done"]
    assert done
    assert done[0]["kind"] == "ogg"
    dumped = json.dumps(logs, default=str)
    assert "salary-review" not in dumped
    assert "kowalski" not in dumped


# --- with the ASR dispatch on (Task 8) ---


def asr_app(**overrides: Any) -> tuple[FastAPI, FakePool, FakeAsyncRedis]:
    """The public app with ASR on and a fake document pool (no MarkItDown child)."""
    settings = make_settings(asr_enabled=True, **overrides)
    registry = CollectorRegistry()
    valkey = FakeAsyncRedis()
    app = create_public_app(settings, valkey, registry)
    pool = FakePool(reply())
    app.state.pipeline = create_pipeline(
        settings,
        valkey,
        app.state.fetcher,
        registry,
        ConversionPool(1),
        memory_guard=app.state.memory_guard,
        document_pool=cast(DocumentPool, pool),
    )
    return app, pool, valkey


def keys_of(valkey: FakeAsyncRedis, pattern: str) -> list[bytes]:
    async def scan() -> list[bytes]:
        return [key async for key in valkey.scan_iter(pattern)]

    return asyncio.run(scan())


HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64
ID3_TEXT = b"ID3 tags hold the title and the artist of an MP3 file. " * 8


@pytest.mark.parametrize(
    ("body", "name", "declared"),
    [
        (corpus("lesson.docx"), "lesson.docx", "application/octet-stream"),
        (HEIC, "photo.heic", "image/heic"),
        (ID3_TEXT, "notes.txt", "text/plain"),
    ],
    ids=["ooxml zip", "heic image", "text starting with ID3"],
)
def test_documents_and_images_keep_their_path_with_asr_on(
    body: bytes, name: str, declared: str
) -> None:
    app, pool, valkey = asr_app()
    with TestClient(app) as test_client:
        response = upload(test_client, body, name, declared)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["media"] is None
    assert not (data["content_type"] or "").startswith(("audio/", "video/"))
    assert response.json()["error"] is None or response.json()["error"]["code"] != (
        "no_transcript_available"
    )
    assert keys_of(valkey, "job:*") == []
    assert keys_of(valkey, "q:{asr}:*") == []
    if name == "lesson.docx":
        assert data["document"]["kind"] == "docx"
        assert len(pool.calls) == 1
    if name == "notes.txt":
        assert response.json()["status"] == "success"
        assert "ID3 tags hold the title" in data["content"]


def test_an_audio_upload_of_exactly_the_media_limit_is_handed_over() -> None:
    app, _, valkey = asr_app(media_upload_max_bytes=1_000_000, document_max_bytes=4096)
    exact = OGG + b"\x00" * (1_000_000 - len(OGG))
    with TestClient(app) as test_client:
        handed = upload(test_client, exact, "talk.ogg", "audio/ogg")
        over = upload(test_client, exact + b"\x00", "talk.ogg", "audio/ogg")

    assert handed.status_code == 202, handed.text
    job_id = handed.json()["data"]["id"]
    assert keys_of(valkey, f"job:{{{job_id}}}:input:0") != []
    assert over.status_code == 200
    assert over.json()["error"]["code"] == "too_large"
    assert "1000000" in over.json()["error"]["message"]


def _large_docx(size: int) -> bytes:
    """A DOCX of at least ``size`` bytes: its kind is in its central directory, at the end."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types><Override PartName="/word/document.xml" ContentType="x"/></Types>',
        )
        archive.writestr("word/document.xml", b"x" * size)
    return out.getvalue()


LARGE_DOCX = _large_docx(8192)
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
LARGE_PAGE = b"<!doctype html><html><body>" + b"x" * 8192 + b"</body></html>"
LARGE_PDF = b"%PDF-1.7\n" + b"x" * 8192


PDF_TOO_LARGE = "The file has more than the 4096 bytes that are converted."


def _page_too_large(size: int) -> str:
    return f"The page has {size} bytes, more than the 2048 bytes that are converted."


# With ASR off the route reads DOCUMENT_MAX_BYTES + 1 bytes of the file (4097), with ASR on all of
# it. A DOCX cut there has lost its central directory, so it needs ASR on to be read as one.
@pytest.mark.parametrize(
    ("asr", "body", "name", "declared", "content_type", "message"),
    [
        (False, LARGE_PDF, "big.pdf", "application/pdf", "application/pdf", PDF_TOO_LARGE),
        (True, LARGE_PDF, "big.pdf", "application/pdf", "application/pdf", PDF_TOO_LARGE),
        (True, LARGE_DOCX, "big.docx", "application/octet-stream", DOCX_TYPE, PDF_TOO_LARGE),
        (False, LARGE_PAGE, "big.html", "text/html", "text/html", _page_too_large(4097)),
        (True, LARGE_PAGE, "big.html", "text/html", "text/html", _page_too_large(len(LARGE_PAGE))),
    ],
    ids=["pdf asr off", "pdf asr on", "docx asr on", "page asr off", "page asr on"],
)
def test_a_large_upload_keeps_its_answer_without_hashing_the_body(
    monkeypatch: pytest.MonkeyPatch,
    asr: bool,
    body: bytes,
    name: str,
    declared: str,
    content_type: str,
    message: str,
) -> None:
    """Over its limit (DOCUMENT_MAX_BYTES for a document, CONVERT_MAX_BYTES for a page): too_large
    with the sniffed type and the limit's own message, before the bytes are hashed."""
    from text_extraction import pipeline as pipeline_module

    def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the whole body was hashed")

    monkeypatch.setattr(pipeline_module, "_sha256", never)
    limits: dict[str, Any] = {"document_max_bytes": 4096, "convert_max_bytes": 2048}
    if asr:
        app, pool, _ = asr_app(media_upload_max_bytes=1_000_000, **limits)
    else:
        app = create_public_app(make_settings(**limits), FakeAsyncRedis(), CollectorRegistry())
    with TestClient(app) as test_client:
        response = upload(test_client, body, name, declared)

    assert response.status_code == 200
    answer = response.json()
    assert answer["status"] == "failed"
    assert (answer["error"]["code"], answer["error"]["message"]) == ("too_large", message)
    assert answer["data"]["content_type"] == content_type
    assert answer["data"]["mode_used"] == "upload"
    if asr:
        assert pool.calls == []


def test_xpath_selectors_need_an_admin_key(client: TestClient) -> None:
    """Ruling R4, on the upload route."""
    structured = {
        "schema": {"type": "object", "properties": {"title": {"type": "string"}}},
        "strategy": "selectors",
        "selectors": {"title": {"xpath": "//h1"}},
    }
    response = upload(
        client,
        b"<html><body><h1>Title</h1></body></html>",
        "page.html",
        "text/html",
        form={"structured": json.dumps(structured)},
    )

    assert response.status_code == 422
    assert response.json()["error"]["message"] == "XPath selectors need an admin key; use CSS."


# --- the caller's LLM key and the LLM wait (rulings S9, S12, S13) ------------------------------

LLM_KEY = "sk-llm-caller-key-9c2b"
LLM_STRUCTURED = {
    "schema": {"type": "object", "properties": {"title": {"type": "string"}}},
    "strategy": "llm",
}


def llm_app(**overrides: Any) -> tuple[FastAPI, FakeServer]:
    server = FakeServer()
    settings = make_settings(llm_enabled=True, **overrides)
    return create_public_app(settings, FakeAsyncRedis(server=server), CollectorRegistry()), server


def stored_keys(server: FakeServer, pattern: str) -> list[str]:
    async def scan() -> list[str]:
        valkey = FakeAsyncRedis(server=server)
        return sorted(
            key.decode() if isinstance(key, bytes) else key for key in await valkey.keys(pattern)
        )

    return asyncio.run(scan())


def test_the_llm_key_is_stored_with_the_uploads_job_and_never_shown() -> None:
    """``wait_seconds=0`` turns the upload into a P1 job at once: it carries the caller's key
    for the llm worker."""
    app, server = llm_app()
    with TestClient(app) as test_client, capture_logs() as logs:
        response = upload(
            test_client,
            b"<html><body><h1>Title</h1></body></html>",
            "page.html",
            "text/html",
            headers={**AUTH, "X-LLM-Api-Key": LLM_KEY},
            form={"structured": json.dumps(LLM_STRUCTURED), "wait_seconds": "0"},
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["data"]["id"]
        view = test_client.get(f"/v1/jobs/{job_id}", headers=AUTH)

        async def stored() -> str | None:
            return await JobStore(FakeAsyncRedis(server=server), make_settings()).llm_key(job_id)

        assert asyncio.run(stored()) == LLM_KEY
        assert LLM_KEY not in view.text
    assert not any(LLM_KEY in repr(event) for event in logs)


def test_an_upload_without_llm_work_stores_no_llm_key() -> None:
    app, server = llm_app()
    with TestClient(app) as test_client:
        response = upload(
            test_client,
            b"<html><body><h1>Title</h1></body></html>",
            "page.html",
            "text/html",
            headers={**AUTH, "X-LLM-Api-Key": LLM_KEY},
            form={"wait_seconds": "0"},
        )
        assert response.status_code == 202, response.text

    assert stored_keys(server, "job:*:llm_key") == []


def test_llm_key_checks_on_the_upload_route() -> None:
    app, server = llm_app()  # no LLM_API_KEY
    with TestClient(app) as test_client:
        body = b"<html><body><h1>Title</h1></body></html>"
        form = {"structured": json.dumps(LLM_STRUCTURED)}
        missing = upload(test_client, body, "page.html", "text/html", form=form)
        bad = upload(
            test_client,
            body,
            "page.html",
            "text/html",
            headers={**AUTH, "X-LLM-Api-Key": "has space"},
            form=form,
        )

    assert missing.status_code == 400
    assert missing.json()["error"]["message"] == "No LLM key: send X-LLM-Api-Key."
    assert bad.status_code == 400
    assert "X-LLM-Api-Key" in bad.json()["error"]["message"]
    assert "has space" not in bad.text
    assert stored_keys(server, "job:*") == []


# --- the post stage (rulings S5, S13) ------------------------------------------------------------

NOTES = (
    "# Notes\n\n## Plants\n\n" + PARAGRAPH * 2 + "\n\n## Tables\n\n"
    "| Plant | Height |\n| --- | --- |\n| Oak | 30 m |\n"
)


def test_upload_post_stage_runs_inline() -> None:
    """An upload gets the post stage in the API pod: Markdown chunks with their headings, and
    the tables of a result without a DOM stage from its Markdown."""
    app = create_public_app(make_settings(), FakeAsyncRedis(), CollectorRegistry())
    with TestClient(app) as test_client:
        response = upload(
            test_client,
            NOTES.encode(),
            "notes.md",
            "text/markdown",
            form={
                "structured": json.dumps({"tables": True}),
                "chunking": json.dumps({"strategy": "markdown", "size": 400, "overlap": 0}),
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "success", body["error"]
    data = body["data"]
    assert data["structured"]["stage"] == "done"
    assert data["structured"]["tables"]
    chunks = data["chunks"]
    assert chunks
    assert ["Notes", "Plants"] in [chunk["headings"] for chunk in chunks]
    for chunk in chunks:
        assert data["content"][chunk["start"] : chunk["end"]] == chunk["text"]


def test_upload_p1_job_result_has_post_stage() -> None:
    """An upload that became a P1 job stores its result after the post stage."""
    app, _server = llm_app()
    with TestClient(app) as test_client:
        response = upload(
            test_client,
            NOTES.encode(),
            "notes.md",
            "text/markdown",
            form={"chunking": json.dumps({"strategy": "fixed", "size": 200}), "wait_seconds": "0"},
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["data"]["id"]
        deadline = time.monotonic() + 10
        while True:
            view = test_client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
            if view["data"]["state"] == "completed" or time.monotonic() > deadline:
                break
            time.sleep(0.02)

    result = view["data"]["items"][0]["result"]
    assert result["status"] == "success"
    assert result["data"]["chunks"]
