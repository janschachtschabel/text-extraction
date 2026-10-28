"""The hop to the ``nlp`` pool (rulings N11, N12): sync requests through ``PiiHandoff``, on
fakeredis, with a fake nlp worker (the real ``Worker`` and ``PiiRunner`` with the fake child pool
of the nlp worker tests, in a thread of its own).

Every path where a request with ``pii`` gets its answer is here: it is the processed result, or
``pii_failed`` without the text, never the unprocessed envelope.
"""

import asyncio
import hashlib
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from redis.exceptions import ConnectionError as RedisConnectionError

from tests.fakes import ARTICLE, PAGE, PARAGRAPH, FakeFetcher, fetched
from tests.test_extract_api import GatedPipeline
from tests.test_nlp_worker import FakePiiPool
from text_extraction.app import create_public_app
from text_extraction.auth import Principal
from text_extraction.browser_dispatch import BrowserDispatcher
from text_extraction.compliance import ComplianceReport
from text_extraction.conversion_pool import ConversionPool
from text_extraction.docling_dispatch import DoclingDispatcher
from text_extraction.documents.pool import DocumentPool
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.escalation import BROWSER_UNAVAILABLE_WARNING
from text_extraction.extract_api import _store_result
from text_extraction.extract_models import ExtractResult, Timings, failed_extract
from text_extraction.fetch import Fetcher
from text_extraction.job_models import JobOptions
from text_extraction.jobs import JobStore
from text_extraction.memory_guard import MemoryGuard
from text_extraction.nlp_worker import PiiRunner
from text_extraction.pii.fields import FAIL_CLOSED_MESSAGE, TOO_LARGE_MESSAGE, fail_closed
from text_extraction.pii.types import PiiOptions
from text_extraction.pii_dispatch import (
    PII_BUSY_MESSAGE,
    PII_OUTCOMES,
    PII_POOL,
    nlp_queue,
    withheld,
    without_findings,
)
from text_extraction.pipeline import create_pipeline
from text_extraction.pipeline_documents import OCR_UNAVAILABLE_WARNING
from text_extraction.pool_dispatch import Handover
from text_extraction.queue_metrics import JobMetrics
from text_extraction.settings import Settings
from text_extraction.worker import Worker

USER_KEY = "user-key-0123456789-raw-secret"
AUTH = {"Authorization": f"Bearer {USER_KEY}"}
ALICE = Principal(name="alice", role="user", rate_limit_per_minute=None)
NAME = "Anna Schmidt"
EMAIL = "anna.schmidt@example.org"
# A page whose text names a person and an e-mail address (the fake child finds both).
NAMED_ARTICLE = ARTICLE.replace(
    "</article>", f"<p>Written by {NAME}, write to {EMAIL} with questions.</p></article>"
)
# A JavaScript shell with a few words and a name: an ``auto`` request escalates it.
NAMED_SHELL = (
    "<html><head><title>Learning app</title></head><body><div id='root'></div>"
    f"<p>{NAME} made this learning app about photosynthesis; it loads here in a moment.</p>"
    "<script src='/app.js'></script></body></html>"
)
SCAN = "https://docs.example.org/scan.pdf"
SCAN_BODY = b"%PDF-1.7\nscan\n%%EOF\n"


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
        "pii_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


async def _short_sleep(seconds: float) -> None:
    await asyncio.sleep(min(seconds, 0.02))


class FakeNlpWorker:
    """The real ``Worker`` on pool ``nlp`` with the real ``PiiRunner`` and the fake child pool,
    on a loop of its own, sharing the fakeredis server of the app."""

    def __init__(self, server: FakeServer, settings: Settings) -> None:
        self.pool = FakePiiPool()
        self._server = server
        self._settings = settings
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        valkey = FakeAsyncRedis(server=self._server)
        store = JobStore(valkey, self._settings)
        registry = CollectorRegistry()
        worker = Worker(
            self._settings,
            valkey,
            PiiRunner(self._settings, store, self.pool, registry),
            store,
            nlp_queue(valkey, self._settings),
            registry,
            concurrency=1,
            sleep=_short_sleep,
        )
        self._ready.set()
        await worker.run(self._stop)

    def __enter__(self) -> "FakeNlpWorker":
        self._thread.start()
        assert self._ready.wait(10)
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._loop is not None
        assert self._stop is not None
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(10)

    def analysed(self) -> int:
        return len(self.pool.calls_to("analyze"))


class FakeDocumentPool:
    """MarkItDown's children: every PDF has a thin text layer, with a name, that needs OCR."""

    async def start(self) -> None:
        pass

    async def run(self, function_name: str, payload: dict[str, Any], deadline: float) -> Any:
        return {
            "content": f"Thin text layer by {NAME}.",
            "title": "Scan",
            "warnings": [],
            "pages": 2,
            "pages_converted": 2,
            "needs_ocr": True,
            "metadata": None,
        }

    async def shutdown(self) -> None:
        pass


class PiiService:
    """The public app with PII on, on a fakeredis server that the fake workers share."""

    def __init__(self, answers: dict[str, Any] | None = None, **overrides: Any) -> None:
        self.settings = make_settings(**overrides)
        self.server = FakeServer()
        self.registry = CollectorRegistry()
        valkey = FakeAsyncRedis(server=self.server)
        self.app = create_public_app(self.settings, valkey, self.registry)
        self.fetcher = FakeFetcher(
            answers
            or {
                PAGE: fetched(body=NAMED_ARTICLE),
                SCAN: fetched(SCAN, SCAN_BODY, content_type="application/pdf"),
            }
        )
        self.real = create_pipeline(
            self.settings,
            valkey,
            cast(Fetcher, self.fetcher),
            self.registry,
            ConversionPool(1),
            memory_guard=self.app.state.memory_guard,
            document_pool=cast(DocumentPool, FakeDocumentPool()),
        )
        self.app.state.pipeline = self.real

    def nlp(self) -> FakeNlpWorker:
        return FakeNlpWorker(self.server, self.settings)

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

    def outcomes(self) -> dict[str, float]:
        counts: dict[str, float] = {}
        for outcome in PII_OUTCOMES:
            value = self.registry.get_sample_value(
                "text_extraction_nlp_dispatch_total", {"outcome": outcome}
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

    def wait_for_idle(self) -> None:
        deadline = time.monotonic() + 10
        while self.app.state.background and time.monotonic() < deadline:
            time.sleep(0.01)


@pytest.fixture
def high_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MemoryGuard, "high", property(lambda self: True))


def extract(client: TestClient, url: str = PAGE, **body: Any) -> Any:
    return client.post("/v1/extract", json={"url": url, **body}, headers=AUTH)


def assert_processed(data: dict[str, Any]) -> None:
    """The answer was handled by the nlp worker: masked, with its report."""
    assert data["pii"] is not None
    assert data["pii"]["mode"] == "mask"
    assert NAME not in str(data)
    assert EMAIL not in str(data)


def assert_withheld(body: dict[str, Any], message: str = FAIL_CLOSED_MESSAGE) -> None:
    """``pii_failed``, retryable, and none of the text."""
    assert body["status"] == "failed"
    assert body["error"]["code"] == "pii_failed"
    assert body["error"]["retryable"] is True
    assert body["error"]["message"] == message
    data = body["data"]
    assert data is not None
    assert (data["content"], data["title"], data["metadata"], data["links"]) == (
        None,
        None,
        None,
        None,
    )
    assert data["pii"] is None
    assert NAME not in str(body)
    assert "Anna" not in str(body)


# --- sync requests ---


def test_sync_fast_result_goes_through_nlp() -> None:
    service = PiiService()
    with TestClient(service.app) as client, service.nlp() as nlp:
        response = extract(client, pii="mask")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "success", body["error"]
        data = body["data"]
        assert_processed(data)
        assert "[PERSON]" in data["content"]
        assert "[EMAIL_ADDRESS]" in data["content"]
        assert data["pii"]["entities"]["PERSON"] >= 1
        assert data["mode_used"] == "fast"
        assert data["fingerprint"] is not None  # the extraction's, as it was (N3)
        assert body["trace_id"]
        # The nlp job and its draft are gone, and nothing is left queued.
        assert service.wait_for_no_jobs() == []
    assert nlp.analysed() == 1
    assert service.outcomes() == {"answered": 1}


def test_sync_cache_hit_goes_through_nlp() -> None:
    service = PiiService()
    with TestClient(service.app) as client, service.nlp() as nlp:
        first = extract(client, pii="mask").json()
        second = extract(client, pii="mask").json()
        plain = extract(client).json()

        assert first["data"]["cache"] == "miss"
        assert_processed(first["data"])
        assert second["data"]["cache"] == "hit"
        assert_processed(second["data"])
        # The cache keeps the extraction, not the masked result (N9), under the same key.
        assert plain["data"]["cache"] == "hit"
        assert NAME in plain["data"]["content"]
        assert plain["data"]["pii"] is None
    assert len(service.fetcher.page_calls()) == 1
    assert nlp.analysed() == 2


def test_sync_slow_pii_answers_202_after_wait() -> None:
    # D4 (a): no nlp worker yet; the request waits PII_SYNC_WAIT_SECONDS, then answers 202.
    service = PiiService(pii_sync_wait_seconds=1)
    with TestClient(service.app) as client:
        started = time.monotonic()
        response = extract(client, pii="mask")
        waited = time.monotonic() - started

        assert response.status_code == 202
        accepted = response.json()["data"]
        assert (accepted["kind"], accepted["total"]) == ("extract", 1)
        assert waited >= 1

        with service.nlp():
            final = service.wait_for_job(client, accepted["id"])

        assert final["data"]["state"] == "completed"
        result = final["data"]["items"][0]["result"]
        assert result["status"] == "success"
        assert_processed(result["data"])
        service.wait_for_idle()
        assert service.app.state.background == set()
    assert service.outcomes() == {"handed_over": 1}  # the task saw the route take the job


@pytest.mark.usefixtures("high_memory")
def test_sync_memory_guard_is_pii_failed_without_content() -> None:
    service = PiiService()
    with TestClient(service.app) as client:
        response = extract(client, pii="mask")

        assert response.status_code == 200
        body = response.json()
        assert_withheld(body, PII_BUSY_MESSAGE)
        assert body["data"]["upstream_status"] == 200  # what the target answered stays
        assert body["data"]["compliance"]["decision"] == "allowed"
        assert service.stored() == []  # nothing was stored
    assert service.outcomes() == {"memory_guard": 1}


def test_sync_valkey_down_is_pii_failed_not_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    async def down(*args: Any, **kwargs: Any) -> str:
        raise RedisConnectionError("Error 111 connecting to valkey:6379. Connection refused.")

    monkeypatch.setattr(JobStore, "create", down)
    service = PiiService()
    with TestClient(service.app) as client:
        response = extract(client, pii="mask")

        assert response.status_code == 200  # the hop failed, the request was fine
        assert_withheld(response.json())
    assert service.outcomes() == {"backend_error": 1}


def test_a_result_with_nothing_to_process_answers_with_zero_entities() -> None:
    service = PiiService({PAGE: fetched(status=404, body="")})
    with TestClient(service.app) as client:
        response = extract(client, pii="hash")

        body = response.json()
        assert body["status"] == "failed"
        assert body["error"]["code"] == "target_http_error"  # the target's own answer
        assert body["data"]["pii"] == {
            "mode": "hash",
            "language": "en",
            "entities": {},
            "items": None,
            "hash_scope": "result",
        }
        assert service.stored() == []  # no hop for nothing
    assert service.outcomes() == {}


def test_ocr_fallback_draft_is_processed_or_withheld() -> None:
    # The key is at its open Docling jobs: the text layer answers with ``ocr_unavailable``, and
    # goes through nlp. (A request with ``pii`` waits for OCR until its route hands it over.)
    service = PiiService(docling_enabled=True, docling_max_open_per_client=1)

    async def one_open_job(valkey: FakeAsyncRedis) -> str:
        store = JobStore(valkey, service.settings)
        job_id = await store.create("extract", ALICE, JobOptions(compliance="enforce"), [SCAN])
        await store.add_docling_job("alice", job_id)
        return job_id

    open_job = service.run(one_open_job)
    with TestClient(service.app) as client, service.nlp():
        response = extract(client, SCAN, pii="mask")

        body = response.json()
        assert response.status_code == 200
        data = body["data"]
        assert OCR_UNAVAILABLE_WARNING in data["warnings"]
        assert data["content"] == "Thin text layer by [PERSON]."
        assert_processed(data)
        # The nlp job is gone with the answer; only the other open job is left.
        assert service.stored("job:*:meta") == [f"job:{{{open_job}}}:meta"]


@pytest.mark.usefixtures("high_memory")
def test_ocr_fallback_draft_is_withheld_while_memory_is_high() -> None:
    service = PiiService(docling_enabled=True)
    with TestClient(service.app) as client:
        response = extract(client, SCAN, pii="mask")

        assert response.status_code == 200
        assert_withheld(response.json(), PII_BUSY_MESSAGE)


def test_browser_fallback_is_processed_or_withheld() -> None:
    # No browser worker: the fast result answers with ``browser_unavailable``, through nlp.
    service = PiiService(
        {PAGE: fetched(body=NAMED_SHELL)}, browser_enabled=True, browser_queue_wait_seconds=0.3
    )
    with TestClient(service.app) as client, service.nlp():
        response = extract(client, pii="mask")

        body = response.json()
        assert response.status_code == 200
        data = body["data"]
        assert data["mode_used"] == "fast"
        assert BROWSER_UNAVAILABLE_WARNING in data["warnings"]
        assert_processed(data)
        assert service.wait_for_no_jobs() == []


def test_browser_fallback_then_slow_nlp_answers_202_with_the_nlp_job() -> None:
    """The hand-over is claimed again for the nlp job once the browser job was given up."""
    service = PiiService(
        {PAGE: fetched(body=NAMED_SHELL)},
        browser_enabled=True,
        browser_queue_wait_seconds=0.3,
        pii_sync_wait_seconds=1,
    )
    with TestClient(service.app) as client:
        response = extract(client, pii="mask")

        assert response.status_code == 202
        job_id = response.json()["data"]["id"]
        with service.nlp():
            final = service.wait_for_job(client, job_id)

        result = final["data"]["items"][0]["result"]
        assert BROWSER_UNAVAILABLE_WARNING in result["data"]["warnings"]
        assert_processed(result["data"])
        service.wait_for_idle()
    assert service.outcomes() == {"handed_over": 1}


def test_p1_job_result_is_processed() -> None:
    service = PiiService()
    gated = GatedPipeline(service.real)
    service.app.state.pipeline = gated
    with TestClient(service.app) as client:
        response = extract(client, pii="mask", wait_seconds=0.05)

        assert response.status_code == 202
        accepted = response.json()["data"]
        assert accepted["kind"] == "extract"
        gated.release.set()
        with service.nlp():
            final = service.wait_for_job(client, accepted["id"])

        assert final["data"]["state"] == "completed"
        result = final["data"]["items"][0]["result"]
        assert result["status"] == "success"
        assert_processed(result["data"])
        service.wait_for_idle()
    assert service.outcomes() == {"handed_over": 1}  # the envelope went onto the P1 job


def test_p1_task_raised_is_withheld() -> None:
    service = PiiService()
    gated = GatedPipeline(service.real, fail=RuntimeError(f"boom near {NAME}"))
    service.app.state.pipeline = gated
    with TestClient(service.app) as client:
        response = extract(client, pii="mask", wait_seconds=0.05)

        assert response.status_code == 202
        gated.release.set()
        final = service.wait_for_job(client, response.json()["data"]["id"])

        result = final["data"]["items"][0]["result"]
        assert result["status"] == "failed"
        assert result["error"]["code"] == "internal_error"
        assert result["data"] is None
        assert NAME not in str(final)


def test_p1_store_withholds_an_unprocessed_envelope() -> None:
    """The guard of the P1 store: a flow that answered an unprocessed envelope (a pod without
    the hop) never stores its text."""

    async def scenario(valkey: FakeAsyncRedis) -> Any:
        settings = make_settings()
        store = JobStore(valkey, settings)
        pii = PiiOptions(mode="mask")
        job_id = await store.create(
            "extract", ALICE, JobOptions(compliance="enforce", pii=pii), [PAGE]
        )
        envelope = Envelope[ExtractResult](status="success", data=_result(content=NAME))
        metrics = cast(JobMetrics, _NoMetrics())
        await _store_result(store, metrics, job_id, time.monotonic(), envelope, pii=pii)
        return (await store.items(job_id, 0, 1))[0].result

    stored = PiiService().run(scenario)
    assert stored.status == "failed"
    assert stored.error.code == ErrorCode.PII_FAILED
    assert stored.data.content is None
    assert stored.data.title is None


# --- the parts ---


def test_handover_is_claimed_again_after_a_released_pool_job() -> None:
    async def scenario() -> None:
        handover = Handover()
        assert handover.claim_after_release(PII_POOL)  # nothing claimed yet: a plain claim
        assert handover.pool == PII_POOL

        browser = Handover()
        assert browser.claim_pool("browser")
        browser.settle("browser-job")
        assert not browser.claim_after_release(PII_POOL)  # still waited for: not released
        assert browser.release()
        assert browser.claim_after_release(PII_POOL)
        assert (browser.pool, browser.pool_job) == (PII_POOL, None)
        browser.settle("nlp-job")
        assert browser.detach()  # the route may answer 202 with the nlp job now
        assert browser.pool_job == "nlp-job"

        route = Handover()
        assert route.claim_p1()
        assert not route.claim_after_release(PII_POOL)  # the route's P1 job

        detached = Handover()
        assert detached.claim_pool("browser")
        detached.settle("browser-job")
        assert detached.detach()
        assert not detached.claim_after_release(PII_POOL)  # the route answered with that job

    asyncio.run(scenario())


def test_without_findings_reports_zero_entities_only_when_nothing_is_owed() -> None:
    settings = make_settings(pii_hash_key="k" * 40)
    empty = Envelope[ExtractResult](
        status="failed", data=_result(content=None, title=None, language="fr")
    )

    annotated = without_findings(empty, PiiOptions(mode="annotate"), settings)
    assert annotated.data is not None
    assert annotated.data.pii is not None
    assert annotated.data.pii.model_dump() == {
        "mode": "annotate",
        "language": "en",  # the fallback
        "entities": {},
        "items": [],
        "hash_scope": None,
    }
    hashed = without_findings(empty, PiiOptions(mode="hash", language="de"), settings)
    assert hashed.data is not None
    assert hashed.data.pii is not None
    assert (hashed.data.pii.language, hashed.data.pii.hash_scope) == ("de", "service")
    # Owed, not asked for, or without data: as it is.
    owed = Envelope[ExtractResult](status="success", data=_result(content=NAME))
    assert without_findings(owed, PiiOptions(mode="mask"), settings) is owed
    assert without_findings(empty, None, settings) is empty
    bare = failed_extract(ErrorCode.INTERNAL_ERROR, "Internal server error.", retryable=False)
    assert without_findings(bare, PiiOptions(mode="mask"), settings) is bare


def test_without_findings_leaves_a_withheld_answer_alone() -> None:
    """An answer the hop withheld (``pii_failed``, or the nlp worker's ``too_large``) has no
    text left, but it was not processed: it gets no report of zero entities."""
    settings = make_settings()
    pii = PiiOptions(mode="mask")
    owed = Envelope[ExtractResult](status="success", data=_result(content=NAME))
    for held in (
        withheld(owed, pii),
        withheld(owed, pii, PII_BUSY_MESSAGE),
        fail_closed(owed, pii).model_copy(
            update={
                "error": ErrorInfo(
                    code=ErrorCode.TOO_LARGE, message=TOO_LARGE_MESSAGE, retryable=False
                )
            }
        ),
    ):
        assert held.data is not None
        assert held.data.content is None
        assert without_findings(held, pii, settings) is held
    # A target's own too_large without text is a failure with nothing to process.
    target = Envelope[ExtractResult](
        status="failed",
        data=_result(content=None, title=None),
        error=ErrorInfo(code=ErrorCode.TOO_LARGE, message="The document is too large."),
    )
    reported = without_findings(target, pii, settings)
    assert reported.data is not None
    assert reported.data.pii is not None
    assert reported.data.pii.entities == {}


def test_pool_waits_are_longer_with_pii(monkeypatch: pytest.MonkeyPatch) -> None:
    """A browser or Docling job of a request with ``pii`` completes only once the nlp worker
    wrote its result: the request waits ``PII_SYNC_WAIT_SECONDS`` longer for it."""
    calls: list[dict[str, Any]] = []

    async def wait_for(self: Any, job_id: str, **limits: Any) -> None:
        calls.append(limits)

    monkeypatch.setattr(BrowserDispatcher, "wait_for", wait_for)
    monkeypatch.setattr(DoclingDispatcher, "wait_for", wait_for)
    settings = make_settings(
        browser_timeout_seconds=40, docling_sync_wait_seconds=10, pii_sync_wait_seconds=20
    )
    store = cast(JobStore, None)
    browser = BrowserDispatcher(store, cast(Any, None), settings)
    docling = DoclingDispatcher(store, cast(Any, None), settings)

    async def scenario() -> None:
        for pii in (False, True):
            await browser.wait("j", unblock=False, detached=lambda: False, pii=pii)
            await docling.wait("j", detached=lambda: False, pii=pii)

    asyncio.run(scenario())
    plain_browser, plain_docling, pii_browser, pii_docling = calls
    assert pii_browser["result_within"] == plain_browser["result_within"] + 20
    assert pii_browser["claim_within"] == plain_browser["claim_within"]
    assert pii_docling["total_within"] == plain_docling["total_within"] + 20
    assert pii_docling["claim_within"] == plain_docling["claim_within"]


def test_app_has_the_nlp_dispatcher_and_its_queue_gauges() -> None:
    service = PiiService()
    assert service.app.state.pii_dispatcher is not None
    with TestClient(service.app):
        pass
    assert (
        service.registry.get_sample_value("text_extraction_queue_pending_items", {"pool": "nlp"})
        is not None
    )
    plain = create_public_app(make_settings(pii_enabled=False), FakeAsyncRedis())
    assert plain.state.pii_dispatcher is None


# --- helpers ---


class _NoMetrics:
    """``JobMetrics`` that count nothing."""

    def __init__(self) -> None:
        self.created = _Labels()
        self.duration = _Labels()


class _Labels:
    def labels(self, **labels: str) -> "_Labels":
        return self

    def inc(self, amount: float = 1) -> None:
        pass

    def observe(self, value: float) -> None:
        pass


def _result(**changes: Any) -> ExtractResult:
    values: dict[str, Any] = {
        "url": PAGE,
        "final_url": PAGE,
        "upstream_status": 200,
        "content_type": "text/html",
        "mode_used": "fast",
        "attempts": 1,
        "language": "en",
        "word_count": 2,
        "title": "A page",
        "format": "markdown",
        "content": PARAGRAPH,
        "quality": None,
        "timings": Timings(total_ms=1, compliance_ms=0, fetch_ms=1, convert_ms=0),
        "compliance": ComplianceReport(mode="enforce"),
        "metadata": None,
        "links": None,
        "fingerprint": None,
        "cache": "miss",
        "warnings": [],
    }
    values.update(changes)
    return ExtractResult(**values)
