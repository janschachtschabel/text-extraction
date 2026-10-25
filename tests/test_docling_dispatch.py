"""The Docling dispatch of sync requests (ruling F12): ``POST /v1/extract`` and
``POST /v1/extract/file`` with a document that needs OCR, on fakeredis.

The pipeline converts with a fake MarkItDown pool whose reply says the PDF needs OCR. A fake
Docling worker (the real ``Worker`` on pool ``docling`` with a fake runner, in a thread of its own)
claims the item, reads its input and draft from the store and returns an OCR result, which the
``Worker`` writes as the item's result.
"""

import asyncio
import hashlib
import json
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from redis.exceptions import RedisError
from structlog.testing import capture_logs

from tests.fakes import PARAGRAPH, FakeFetcher, fetched
from text_extraction import extract_api
from text_extraction.app import create_public_app
from text_extraction.auth import Principal
from text_extraction.compliance import ComplianceReport
from text_extraction.conversion_pool import ConversionPool
from text_extraction.docling_dispatch import DOCLING_OUTCOMES, docling_queue
from text_extraction.documents.pool import DocumentPool
from text_extraction.documents.types import DocumentInfo
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractResult, Timings
from text_extraction.fetch import Fetcher
from text_extraction.job_models import JobOptions
from text_extraction.jobs import JobRecord, JobStore
from text_extraction.memory_guard import MemoryGuard
from text_extraction.pipeline import create_pipeline
from text_extraction.pipeline_documents import OCR_UNAVAILABLE_WARNING, DocumentInputMeta
from text_extraction.scheduler import FairQueue, QueueItem
from text_extraction.settings import Settings
from text_extraction.worker import Worker

USER_KEY = "user-key-0123456789-raw-secret"
AUTH = {"Authorization": f"Bearer {USER_KEY}"}
ALICE = Principal(name="alice", role="user", rate_limit_per_minute=None)
SCAN = "https://docs.example.org/scan.pdf"
IMAGE = "https://docs.example.org/page.png"
# Bytes no job view or log line may carry (Review Focus 4).
MARKER = b"INPUT-BYTES-7f3a91"
SCAN_BODY = b"%PDF-1.7\n" + MARKER + b"\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + MARKER + b"\x00" * 48
OCR_TEXT = "Text read with OCR. " + PARAGRAPH * 3
TEXT_LAYER = "The PDF's own text layer. " + PARAGRAPH * 3
# Longer than the ``wait_seconds`` of the P1 tests (0.2 s): the route creates the P1 job first.
SLOW_SECONDS = 1.0


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
        "docling_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


class FakePool:
    """MarkItDown's children: every PDF has a text layer too thin to keep (it needs OCR), or,
    with ``needs_ocr=False``, one that is kept. Each conversion takes ``seconds``."""

    def __init__(self, *, seconds: float = 0.0, needs_ocr: bool = True) -> None:
        self._seconds = seconds
        self._needs_ocr = needs_ocr

    async def start(self) -> None:
        pass

    async def run(self, function_name: str, payload: dict[str, Any], deadline: float) -> Any:
        await asyncio.sleep(self._seconds)
        return {
            "content": "Thin text layer." if self._needs_ocr else TEXT_LAYER,
            "title": "Scan",
            "warnings": [],
            "pages": 2,
            "pages_converted": 2,
            "needs_ocr": self._needs_ocr,
            "metadata": None,
        }

    async def shutdown(self) -> None:
        pass


def ocr_result(meta: DocumentInputMeta, url: str | None) -> Envelope[ExtractResult]:
    upload = meta.final_url is None
    result = ExtractResult(
        url=url,
        final_url=meta.final_url,
        upstream_status=meta.upstream_status,
        content_type=meta.content_type,
        mode_used="upload" if upload else "fast",
        attempts=meta.attempts,
        language="en",
        word_count=len(OCR_TEXT.split()),
        title="Scan",
        format="markdown",
        content=OCR_TEXT,
        quality=None,
        timings=Timings(total_ms=1, compliance_ms=0, fetch_ms=0, convert_ms=1),
        compliance=meta.compliance or ComplianceReport(mode="enforce", decision="skipped"),
        metadata=None,
        links=None,
        fingerprint=None,
        cache="bypass" if upload else "miss",
        warnings=[],
        document=DocumentInfo(
            kind=meta.kind,
            converter="docling",
            pages=2,
            pages_converted=2,
            ocr=True,
            file_name=meta.file_name,
        ),
    )
    return Envelope[ExtractResult](status="success", data=result)


class FakeDoclingRunner:
    """Reads the item's input and draft as the Docling worker does; answers once ``release`` is
    set."""

    lease_seconds = 60.0

    def __init__(self, store: JobStore, release: threading.Event) -> None:
        self._store = store
        self._release = release
        self.seen: list[dict[str, Any]] = []

    async def run(
        self,
        job: JobRecord,
        item: QueueItem,
        url: str,
        *,
        gate_refusals: Literal["fail", "raise"],
    ) -> Envelope[ExtractResult] | None:
        stored = await self._store.get_input(job.id, item.index, DocumentInputMeta)
        assert stored is not None
        body, meta = stored
        draft = await self._store.draft(job.id, item.index)
        self.seen.append(
            {"kind": job.kind, "url": url, "body": body, "meta": meta, "draft": draft, "item": item}
        )
        while not self._release.is_set():  # noqa: ASYNC110 - set by another thread
            await asyncio.sleep(0.01)
        await self._store.drop_draft(job.id, item.index)
        return ocr_result(meta, url or None)


class DoclingWorker:
    """The real ``Worker`` on pool ``docling`` with the fake runner, on a loop of its own."""

    def __init__(self, server: FakeServer, settings: Settings) -> None:
        self.release = threading.Event()
        self.release.set()
        self._server = server
        self._settings = settings
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self.runner: FakeDoclingRunner | None = None
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        valkey = FakeAsyncRedis(server=self._server)
        store = JobStore(valkey, self._settings)
        self.runner = FakeDoclingRunner(store, self.release)
        worker = Worker(
            self._settings,
            valkey,
            self.runner,
            store,
            docling_queue(valkey, self._settings),
            CollectorRegistry(),
            concurrency=1,
            random=lambda: 0.0,  # idle sleeps of 0.5 s
        )
        self._ready.set()
        await worker.run(self._stop)

    def __enter__(self) -> "DoclingWorker":
        self._thread.start()
        assert self._ready.wait(10)
        return self

    def __exit__(self, *exc: object) -> None:
        self.release.set()
        assert self._loop is not None
        assert self._stop is not None
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(10)


class Service:
    """The public app with Docling enabled, on a fakeredis server the fake worker shares."""

    def __init__(self, pool: FakePool | None = None, **overrides: Any) -> None:
        self.settings = make_settings(**overrides)
        self.server = FakeServer()
        self.registry = CollectorRegistry()
        valkey = FakeAsyncRedis(server=self.server)
        self.app = create_public_app(self.settings, valkey, self.registry)
        self.fetcher = FakeFetcher(
            {
                SCAN: fetched(SCAN, SCAN_BODY, content_type="application/pdf"),
                IMAGE: fetched(IMAGE, PNG, content_type="image/png"),
            }
        )
        self.app.state.pipeline = create_pipeline(
            self.settings,
            valkey,
            cast(Fetcher, self.fetcher),
            self.registry,
            ConversionPool(1),
            memory_guard=self.app.state.memory_guard,
            document_pool=cast(DocumentPool, pool or FakePool()),
        )

    def worker(self) -> DoclingWorker:
        return DoclingWorker(self.server, self.settings)

    def run(self, work: Callable[[FakeAsyncRedis], Awaitable[Any]]) -> Any:
        async def scenario() -> Any:
            return await work(FakeAsyncRedis(server=self.server))

        return asyncio.run(scenario())

    def stored(self, pattern: str = "job:*") -> list[str]:
        async def scan(valkey: FakeAsyncRedis) -> list[str]:
            return sorted(
                key.decode() if isinstance(key, bytes) else key
                for key in await valkey.keys(pattern)
            )

        return list(self.run(scan))

    def wait_for_no_jobs(self) -> list[str]:
        deadline = time.monotonic() + 5
        while (keys := self.stored()) and time.monotonic() < deadline:
            time.sleep(0.02)
        return keys

    def wait_until_gone(self, key: str) -> list[str]:
        deadline = time.monotonic() + 5
        while key in (keys := self.stored()) and time.monotonic() < deadline:
            time.sleep(0.02)
        return keys

    def ttl(self, key: str) -> int:
        async def read(valkey: FakeAsyncRedis) -> int:
            return int(await valkey.ttl(key))

        return int(self.run(read))

    def pending(self) -> int:
        async def count(valkey: FakeAsyncRedis) -> int:
            return (await docling_queue(valkey, self.settings).counts()).pending

        return int(self.run(count))

    def outcomes(self) -> dict[str, float]:
        counts: dict[str, float] = {}
        for outcome in DOCLING_OUTCOMES:
            value = self.registry.get_sample_value(
                "text_extraction_docling_dispatch_total", {"outcome": outcome}
            )
            assert value is not None  # every outcome is exported from the start
            if value:
                counts[outcome] = value
        return counts

    def wait_for_job(self, client: TestClient, job_id: str) -> Any:
        deadline = time.monotonic() + 10
        while True:
            body = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
            if body["data"]["state"] == "completed" or time.monotonic() > deadline:
                return body
            time.sleep(0.02)

    def open_jobs(self, count: int) -> None:
        """``count`` unfinished Docling jobs of alice."""

        async def create(valkey: FakeAsyncRedis) -> None:
            store = JobStore(valkey, self.settings)
            for _ in range(count):
                job_id = await store.create(
                    "extract", ALICE, JobOptions(compliance="enforce"), [SCAN]
                )
                await store.add_docling_job("alice", job_id)

        self.run(create)


@pytest.fixture
def high_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MemoryGuard, "high", property(lambda self: True))


def extract(client: TestClient, url: str, **body: Any) -> Any:
    return client.post("/v1/extract", json={"url": url, **body}, headers=AUTH)


def upload(client: TestClient, body: bytes, **form: str) -> Any:
    return client.post(
        "/v1/extract/file",
        files={"file": ("scan.pdf", body, "application/pdf")},
        data=form,
        headers=AUTH,
    )


# --- sync requests ---


def test_sync_scan_answers_ocr_result() -> None:
    service = Service()
    with TestClient(service.app) as client, service.worker() as worker:
        response = extract(client, SCAN)

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "success", body["error"]
        data = body["data"]
        assert data["content"] == OCR_TEXT
        assert data["document"]["converter"] == "docling"
        assert data["document"]["ocr"] is True
        assert body["trace_id"]
        # The job, its input and its draft are gone, and nothing is left queued.
        assert service.wait_for_no_jobs() == []
        assert service.pending() == 0

    assert worker.runner is not None
    (seen,) = worker.runner.seen
    assert (seen["kind"], seen["url"], seen["body"]) == ("extract", SCAN, SCAN_BODY)
    meta: DocumentInputMeta = seen["meta"]
    assert (meta.kind, meta.final_url, meta.upstream_status) == ("pdf", SCAN, 200)
    assert meta.needs_ocr is True
    assert meta.raw_sha256 == hashlib.sha256(SCAN_BODY).hexdigest()
    draft: Envelope[ExtractResult] = seen["draft"]
    assert draft.data is not None
    assert draft.data.content == "Thin text layer."
    assert draft.data.document is not None
    assert draft.data.document.converter == "markitdown"
    assert seen["item"].client == "alice#live"
    assert service.outcomes() == {"answered": 1}


def test_sync_scan_falls_back_after_wait() -> None:
    service = Service(docling_sync_wait_seconds=1)
    with TestClient(service.app) as client:
        started = time.monotonic()
        response = extract(client, SCAN)
        waited = time.monotonic() - started

        assert response.status_code == 200
        body = response.json()
        data = body["data"]
        assert data["content"] == "Thin text layer."
        assert data["document"]["converter"] == "markitdown"
        assert OCR_UNAVAILABLE_WARNING in data["warnings"]
        # Never compared with an earlier version: the next request may get the OCR result.
        assert data["fingerprint"] is None
        assert waited >= 1
        assert data["timings"]["total_ms"] >= 1000
        assert service.stored() == []
    assert service.outcomes() == {"not_claimed": 1}


def test_image_without_worker_times_out() -> None:
    service = Service(docling_sync_wait_seconds=1)
    with TestClient(service.app) as client:
        response = extract(client, IMAGE)

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert body["error"]["code"] == "timeout"
        assert body["error"]["retryable"] is True
        assert "OCR" in body["error"]["message"]
        assert "not enabled" not in body["error"]["message"]
        assert body["data"]["content_type"] == "image/png"
        assert service.stored() == []
    assert service.outcomes() == {"not_claimed": 1}


def test_fallback_answer_is_not_cached() -> None:
    service = Service(docling_sync_wait_seconds=0)
    with TestClient(service.app) as client:
        extract(client, SCAN)

        assert service.stored("res:*") == []
        assert service.stored("fp:*") == []


# --- wait_seconds: the Docling job answered with 202 ---


def test_wait_seconds_gives_202_docling_job() -> None:
    service = Service()
    with TestClient(service.app) as client, service.worker() as worker, capture_logs() as logs:
        worker.release.clear()
        response = extract(client, SCAN, wait_seconds=1)

        assert response.status_code == 202
        accepted = response.json()["data"]
        assert "docling_job_accepted" in [event["event"] for event in logs]
        assert accepted["kind"] == "extract"
        assert accepted["total"] == 1
        pending = client.get(accepted["status_url"], headers=AUTH).json()["data"]
        assert pending["state"] in ("queued", "running")
        assert pending["items"][0]["url"] == SCAN

        worker.release.set()
        final = service.wait_for_job(client, accepted["id"])
        item = final["data"]["items"][0]
        assert item["result"]["status"] == "success"
        assert item["result"]["data"]["content"] == OCR_TEXT
        # The worker deleted the input with the outcome; the job stays for its caller.
        assert f"job:{{{accepted['id']}}}:input:0" not in service.stored()
    assert service.outcomes() == {"handed_over": 1}
    created = service.registry.get_sample_value(
        "text_extraction_jobs_created_total", {"kind": "extract"}
    )
    assert created == 1


def test_upload_default_wait_25_gives_202(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []
    real_wait = extract_api.wait_for_flow

    async def shortened(task: asyncio.Task[Any], seconds: float) -> None:
        waits.append(seconds)
        await real_wait(task, min(seconds, 0.5))

    monkeypatch.setattr(extract_api, "wait_for_flow", shortened)
    service = Service()
    with TestClient(service.app) as client, service.worker() as worker:
        worker.release.clear()  # a slow Docling worker
        response = upload(client, SCAN_BODY)  # no wait_seconds field: the default

        assert waits == [25]
        assert response.status_code == 202
        accepted = response.json()["data"]
        assert accepted["kind"] == "file"
        worker.release.set()
        final = service.wait_for_job(client, accepted["id"])
        item = final["data"]["items"][0]
        assert item["url"] is None
        assert item["result"]["data"]["mode_used"] == "upload"
        assert item["result"]["data"]["document"]["file_name"] == "scan.pdf"
        assert item["result"]["data"]["content"] == OCR_TEXT

    assert worker.runner is not None
    (seen,) = worker.runner.seen
    assert (seen["kind"], seen["url"], seen["body"]) == ("file", "", SCAN_BODY)
    assert seen["meta"].file_name == "scan.pdf"
    assert seen["item"].domain == "-"
    created = service.registry.get_sample_value(
        "text_extraction_jobs_created_total", {"kind": "file"}
    )
    assert created == 1


def test_sync_upload_answers_ocr_result() -> None:
    service = Service()
    with TestClient(service.app) as client, service.worker():
        response = upload(client, SCAN_BODY, wait_seconds="30")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["content"] == OCR_TEXT
        assert data["url"] is None
        assert service.wait_for_no_jobs() == []


def test_input_never_in_job_view() -> None:
    service = Service()
    with (
        TestClient(service.app) as client,
        service.worker() as worker,
        capture_logs() as logs,
    ):
        worker.release.clear()
        accepted = upload(client, SCAN_BODY, wait_seconds="0.2").json()["data"]
        while not worker.runner or not worker.runner.seen:  # claimed: its input is stored
            time.sleep(0.01)
        view = client.get(f"/v1/jobs/{accepted['id']}", headers=AUTH)

        text = view.text
        assert MARKER.decode() not in text
        assert "input" not in json.dumps(view.json()["data"])
        assert "body" not in view.json()["data"]["items"][0]
        worker.release.set()
        final = service.wait_for_job(client, accepted["id"])
        assert MARKER.decode() not in json.dumps(final)
    assert not any(MARKER.decode() in repr(event) for event in logs)


def test_input_ttl_set() -> None:
    service = Service(docling_input_ttl_seconds=900)
    with TestClient(service.app) as client, service.worker() as worker:
        worker.release.clear()
        accepted = extract(client, SCAN, wait_seconds=0.2).json()["data"]
        key = f"job:{{{accepted['id']}}}:input:0"

        assert 890 <= service.ttl(key) <= 900
        assert service.ttl(f"job:{{{accepted['id']}}}:meta") > 900
        worker.release.set()
        service.wait_for_job(client, accepted["id"])
        deadline = time.monotonic() + 5
        while key in service.stored() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert key not in service.stored()  # deleted with the outcome


# --- wait_seconds: the P1 job (the conversion outlasts wait_seconds) ---


def post(client: TestClient, source: str, wait_seconds: float) -> Any:
    if source == "url":
        return extract(client, SCAN, wait_seconds=wait_seconds)
    return upload(client, SCAN_BODY, wait_seconds=str(wait_seconds))


def test_slow_upload_becomes_file_job_with_input() -> None:
    service = Service(
        FakePool(seconds=SLOW_SECONDS, needs_ocr=False), docling_input_ttl_seconds=900
    )
    with TestClient(service.app) as client:
        response = upload(client, SCAN_BODY, wait_seconds="0.2")

        assert response.status_code == 202
        accepted = response.json()["data"]
        assert (accepted["kind"], accepted["state"]) == ("file", "running")
        key = f"job:{{{accepted['id']}}}:input:0"
        assert 0 < service.ttl(key) <= 900  # the upload's bytes, while the conversion runs

        final = service.wait_for_job(client, accepted["id"])
        item = final["data"]["items"][0]
        assert item["url"] is None
        data = item["result"]["data"]
        assert data["mode_used"] == "upload"
        assert data["content"] == TEXT_LAYER
        assert data["document"]["file_name"] == "scan.pdf"
        assert key not in service.wait_until_gone(key)  # deleted with the result
        assert MARKER.decode() not in json.dumps(final)
    assert service.outcomes() == {}
    created = service.registry.get_sample_value(
        "text_extraction_jobs_created_total", {"kind": "file"}
    )
    assert created == 1


@pytest.mark.parametrize("source", ["url", "upload"])
def test_ocr_after_handover_reuses_p1_job(source: str) -> None:
    service = Service(FakePool(seconds=SLOW_SECONDS))
    with TestClient(service.app) as client, service.worker() as worker:
        response = post(client, source, 0.2)

        assert response.status_code == 202
        accepted = response.json()["data"]
        assert accepted["kind"] == ("extract" if source == "url" else "file")
        final = service.wait_for_job(client, accepted["id"])
        item = final["data"]["items"][0]
        assert item["result"]["status"] == "success"
        assert item["result"]["data"]["content"] == OCR_TEXT  # written by the Docling worker
        key = f"job:{{{accepted['id']}}}:input:0"
        assert key not in service.wait_until_gone(key)
        assert service.pending() == 0

    assert worker.runner is not None
    (seen,) = worker.runner.seen
    assert seen["item"].job_id == accepted["id"]
    assert seen["body"] == SCAN_BODY
    # The upload's own input (``UploadInputMeta``) was replaced by the document's.
    meta: DocumentInputMeta = seen["meta"]
    assert meta.raw_sha256 == hashlib.sha256(SCAN_BODY).hexdigest()
    assert meta.needs_ocr is True
    assert meta.file_name == (None if source == "url" else "scan.pdf")
    assert seen["draft"] is not None
    assert service.outcomes() == {"handed_over": 1}
    created = service.registry.get_sample_value(
        "text_extraction_jobs_created_total", {"kind": accepted["kind"]}
    )
    assert created == 1  # the P1 job only: no Docling job of its own


@pytest.mark.parametrize(
    ("source", "refusal"), [("upload", "open_limit"), ("url", "backend_error")]
)
def test_p1_job_stores_fallback_when_dispatch_refused(
    source: str, refusal: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = Service(FakePool(seconds=SLOW_SECONDS))
    if refusal == "open_limit":
        service.open_jobs(5)
    else:

        async def down(self: JobStore, owner: str) -> int:
            raise RedisError("Valkey is down")

        monkeypatch.setattr(JobStore, "open_docling_jobs", down)
    with TestClient(service.app) as client:
        response = post(client, source, 0.2)

        assert response.status_code == 202
        accepted = response.json()["data"]
        final = service.wait_for_job(client, accepted["id"])
        result = final["data"]["items"][0]["result"]
        # The draft with ``ocr_unavailable``, stored in the P1 job by the request's own task.
        assert result["data"]["content"] == "Thin text layer."
        assert OCR_UNAVAILABLE_WARNING in result["data"]["warnings"]
        assert result["data"]["fingerprint"] is None
        key = f"job:{{{accepted['id']}}}:input:0"
        assert key not in service.wait_until_gone(key)
        assert service.pending() == 0
    assert service.outcomes() == {refusal: 1}


# --- back-pressure ---


@pytest.mark.usefixtures("high_memory")
def test_memory_guard_high_skips_dispatch() -> None:
    service = Service()
    with TestClient(service.app) as client:
        scan = extract(client, SCAN).json()
        image = extract(client, IMAGE).json()

        assert scan["status"] == "partial"  # the draft's own verdict: too few words
        assert OCR_UNAVAILABLE_WARNING in scan["data"]["warnings"]
        assert scan["data"]["content"] == "Thin text layer."
        assert image["status"] == "failed"
        assert (image["error"]["code"], image["error"]["retryable"]) == ("rate_limited", True)
        assert service.stored() == []
        assert service.pending() == 0
    assert service.outcomes() == {"memory_guard": 2}


def test_open_job_limit() -> None:
    service = Service()
    service.open_jobs(5)
    with TestClient(service.app) as client:
        scan = extract(client, SCAN).json()
        image = extract(client, IMAGE).json()

        assert scan["status"] == "partial"  # the draft's own verdict: too few words
        assert OCR_UNAVAILABLE_WARNING in scan["data"]["warnings"]
        assert image["status"] == "failed"
        assert (image["error"]["code"], image["error"]["retryable"]) == ("rate_limited", True)
        assert "5" in image["error"]["message"]
        assert len(service.stored("job:*:meta")) == 5  # no sixth job was left behind
        assert service.pending() == 0
    assert service.outcomes() == {"open_limit": 2}


def test_open_job_limit_counts_only_unfinished_jobs() -> None:
    service = Service()
    service.open_jobs(4)
    with TestClient(service.app) as client, service.worker():
        # The fifth open job is this request's own; it is gone once it answered.
        assert extract(client, SCAN).json()["data"]["content"] == OCR_TEXT
        assert extract(client, SCAN).json()["data"]["content"] == OCR_TEXT
    assert service.outcomes() == {"answered": 2}


# --- the switch ---


@pytest.fixture
def plain() -> Service:
    return Service(docling_enabled=False)


def test_without_docling_nothing_is_dispatched(plain: Service) -> None:
    assert plain.app.state.docling_dispatcher is None
    with TestClient(plain.app) as client:
        body = extract(client, SCAN).json()

        assert OCR_UNAVAILABLE_WARNING in body["data"]["warnings"]
        assert plain.stored() == []
    assert (
        plain.registry.get_sample_value(
            "text_extraction_docling_dispatch_total", {"outcome": "answered"}
        )
        is None
    )


def test_docling_queue_gauges_are_exported() -> None:
    service = Service()
    with TestClient(service.app):
        asyncio.run(service.app.state.queue_gauges.refresh())
        assert (
            service.registry.get_sample_value(
                "text_extraction_queue_pending_items", {"pool": "docling"}
            )
            == 0
        )


def test_docling_queue_has_no_domain_cap() -> None:
    settings = make_settings(outbound_max_concurrency_per_host=1)

    async def scenario() -> int:
        valkey = FakeAsyncRedis()
        queue: FairQueue = docling_queue(valkey, settings)
        await queue.enqueue_items("a" * 32, "alice#live", [(0, None), (1, None), (2, None)])
        claims = [await queue.claim(60) for _ in range(3)]
        return sum(claim.item is not None for claim in claims)

    assert asyncio.run(scenario()) == 3
