"""``POST /v1/extract/file``: uploads (ruling F17) and ``Pipeline.extract_upload``.

The API tests share one app for the module, whose document pool starts one real MarkItDown child
with the first document (a few seconds); the pipeline tests use a fake pool.
"""

import asyncio
import hashlib
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis
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
from text_extraction.pipeline import create_pipeline
from text_extraction.pipeline_documents import OCR_UNAVAILABLE_WARNING, OcrNeeded
from text_extraction.settings import Settings

CORPUS = Path(__file__).parent / "documents" / "corpus"
USER_KEY = "user-key-0123456789-raw-secret"
AUTH = {"Authorization": f"Bearer {USER_KEY}"}
USER = Principal(name="alice", role="user", rate_limit_per_minute=None)
GIF = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00"
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
    limit = 2048 + 64 * 1024
    declared = [(b"content-length", str(limit + 1).encode())]

    status, pulled, body = asgi_post(
        app, [PART_HEAD, b"x" * limit], [*MULTIPART, *BEARER, *declared]
    )

    assert status == 413
    assert pulled == 0  # nothing was read, so nothing was parsed
    assert json.loads(body)["error"]["code"] == "invalid_request"


def test_upload_at_the_route_limit_is_read() -> None:
    """The route's limit is DOCUMENT_MAX_BYTES + 64 KiB, far above the JSON default."""
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
    assert pulled <= 3  # refused at the chunk that went over 66,560 bytes
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


@pytest.mark.parametrize(
    "form",
    [{"fromat": "text"}, {"format": "pdf"}, {"ocr": "maybe"}, {"wait_seconds": "61"}],
)
def test_invalid_form_is_422(client: TestClient, form: dict[str, str]) -> None:
    response = upload(client, GIF, "a.gif", "image/gif", form=form)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


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
    assert set(form["properties"]) == {"format", "metadata", "ocr", "wait_seconds"}
    assert form["properties"]["wait_seconds"]["default"] == 25
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
                "lesson.pdf",
                form or FileForm(),
                USER,
                content_type="application/pdf",
                ocr_handoff=handoff,
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
