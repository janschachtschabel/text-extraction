"""The hop to the ``llm`` pool (rulings S13, S14; D5 a): sync requests through ``LlmHandoff``,
on fakeredis, with a fake llm worker (the real ``Worker`` and ``LlmRunner``, in a thread of its
own) and pytest-httpserver as the OpenAI-compatible endpoint.

Every path where a request that needs the LLM gets its answer is here: the llm worker's result,
or the request's content with ``llm_failed`` (retryable), never a 5xx; and, with ``pii``, the
endpoint only ever sees the masked text.
"""

import asyncio
import json
import threading
import time
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from pytest_httpserver import HTTPServer
from redis.exceptions import ConnectionError as RedisConnectionError

from tests.fakes import PAGE, PARAGRAPH, fetched
from tests.structured.post_helpers import answer, chat_requests, serve_chat
from tests.test_nlp_worker import FakePiiPool
from tests.test_pii_dispatch import (
    AUTH,
    EMAIL,
    NAME,
    PiiService,
    answer_in_thread,
    assert_processed,
    begin_shutdown,
)
from tests.vision.describe_helpers import FakeImages, described, image_url, serve_llm
from text_extraction.app import create_public_app
from text_extraction.auth import Principal
from text_extraction.job_models import JobOptions
from text_extraction.jobs import JobStore
from text_extraction.llm_dispatch import (
    LLM_OUTCOMES,
    LlmDispatcher,
    create_llm_move,
    llm_queue,
)
from text_extraction.llm_worker import LlmRunner
from text_extraction.memory_guard import MemoryGuard
from text_extraction.nlp_worker import PiiRunner
from text_extraction.pii_dispatch import nlp_queue
from text_extraction.settings import Settings
from text_extraction.structured.post import BUSY_MESSAGE, NOT_RUN_MESSAGE
from text_extraction.structured.store import SelectorStore
from text_extraction.vision.types import StoredInputMeta
from text_extraction.worker import Worker

ENV_KEY = "sk-operator-key-31f0"
CALLER_KEY = "sk-caller-key-77c1"
LLM_HEADERS = {**AUTH, "X-LLM-Api-Key": CALLER_KEY}
HEADING: dict[str, Any] = {"type": "object", "properties": {"heading": {"type": "string"}}}
LLM_HEADING: dict[str, Any] = {"schema": HEADING, "strategy": "llm"}


async def _short_sleep(seconds: float) -> None:
    await asyncio.sleep(min(seconds, 0.02))


class FakeWorkers:
    """The real llm ``Worker`` with the real ``LlmRunner`` (the endpoint is pytest-httpserver)
    and, with ``nlp``, the real nlp ``Worker`` with ``PiiRunner`` on the fake child pool, whose
    results move on to ``llm``; on a loop of their own, sharing the fakeredis server of the
    app."""

    def __init__(
        self,
        server: FakeServer,
        settings: Settings,
        *,
        nlp: bool = False,
        images: FakeImages | None = None,
    ) -> None:
        self._server = server
        self._settings = settings
        self._nlp = nlp
        self._images = images
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        settings = self._settings
        valkey = FakeAsyncRedis(server=self._server)
        store = JobStore(valkey, settings)
        registry = CollectorRegistry()
        selectors = SelectorStore(valkey, settings.selector_learn_ttl_seconds)
        workers = [
            Worker(
                settings,
                valkey,
                LlmRunner(settings, store, selectors, registry=registry, images=self._images),
                store,
                llm_queue(valkey, settings),
                registry,
                concurrency=1,
                sleep=_short_sleep,
            )
        ]
        if self._nlp:
            children = FakePiiPool()
            await children.start()
            workers.append(
                Worker(
                    settings,
                    valkey,
                    PiiRunner(settings, store, children, registry),
                    store,
                    nlp_queue(valkey, settings),
                    registry,
                    concurrency=1,
                    sleep=_short_sleep,
                    llm_move=create_llm_move(settings, valkey, store, None, registry),
                )
            )
        self._ready.set()
        async with asyncio.TaskGroup() as running:
            for worker in workers:
                running.create_task(worker.run(self._stop))

    def __enter__(self) -> "FakeWorkers":
        self._thread.start()
        assert self._ready.wait(10)
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._loop is not None
        assert self._stop is not None
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(10)


class LlmService(PiiService):
    """The public app with the LLM on (and PII), its endpoint pytest-httpserver."""

    def __init__(
        self, endpoint: HTTPServer, answers: dict[str, Any] | None = None, **overrides: Any
    ):
        values: dict[str, Any] = {
            "llm_enabled": True,
            "llm_base_url": endpoint.url_for("/v1"),
            "llm_allow_http": True,
            "llm_api_key": ENV_KEY,
        }
        values.update(overrides)
        super().__init__(answers, **values)

    def workers(self, *, nlp: bool = False, images: FakeImages | None = None) -> FakeWorkers:
        return FakeWorkers(self.server, self.settings, nlp=nlp, images=images)

    def llm_outcomes(self) -> dict[str, float]:
        counts: dict[str, float] = {}
        for outcome in LLM_OUTCOMES:
            value = self.registry.get_sample_value(
                "text_extraction_llm_dispatch_total", {"outcome": outcome}
            )
            assert value is not None  # every outcome is exported from the start
            if value:
                counts[outcome] = value
        return counts

    def pending(self) -> int:
        """Items waiting on the ``llm`` queue."""

        async def count(valkey: FakeAsyncRedis) -> int:
            return (await llm_queue(valkey, self.settings).counts()).pending

        return int(self.run(count))


@pytest.fixture
def high_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MemoryGuard, "high", property(lambda self: True))


def extract(client: TestClient, headers: dict[str, str] | None = None, **body: Any) -> Any:
    return client.post("/v1/extract", json={"url": PAGE, **body}, headers=headers or AUTH)


def keys_sent(endpoint: HTTPServer) -> list[str | None]:
    return [request.headers.get("Authorization") for request, _ in endpoint.log]


def assert_llm_not_done(body: dict[str, Any], message: str) -> None:
    """``partial``/``llm_failed``, retryable, the content kept, the field missing."""
    assert body["status"] == "partial"
    assert body["error"]["code"] == "llm_failed"
    assert body["error"]["retryable"] is True
    assert body["error"]["message"] == message
    data = body["data"]
    assert data["content"]
    assert data["structured"]["stage"] == "done"
    assert data["structured"]["missing"] == ["heading"]


# --- sync requests ---


def test_sync_llm_request_answered_by_llm_worker(httpserver: HTTPServer) -> None:
    serve_chat(httpserver, answer({"heading": "Photosynthesis explained"}))
    service = LlmService(httpserver)
    with TestClient(service.app) as client, service.workers():
        response = extract(client, LLM_HEADERS, structured=LLM_HEADING)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "success", body["error"]
        structured = body["data"]["structured"]
        assert structured["values"] == {"heading": "Photosynthesis explained"}
        assert structured["sources"] == {"heading": "llm"}
        assert structured["stage"] == "done"
        assert structured["llm"]["calls"] == 1
        assert body["data"]["content"]
        assert body["trace_id"]
        assert CALLER_KEY not in response.text
        # The llm job is gone with the answer, its key with it.
        assert service.wait_for_no_jobs() == []
    assert keys_sent(httpserver) == [f"Bearer {CALLER_KEY}"]  # the caller's key wins
    assert service.llm_outcomes() == {"answered": 1}


def test_sync_llm_slow_answers_202_after_wait(httpserver: HTTPServer) -> None:
    # D5 (a): no llm worker yet; the request waits LLM_SYNC_WAIT_SECONDS, then answers 202.
    serve_chat(httpserver, answer({"heading": "Photosynthesis explained"}))
    service = LlmService(httpserver, llm_sync_wait_seconds=1)
    with TestClient(service.app) as client:
        started = time.monotonic()
        response = extract(client, LLM_HEADERS, structured=LLM_HEADING)
        waited = time.monotonic() - started

        assert response.status_code == 202, response.text
        accepted = response.json()["data"]
        assert (accepted["kind"], accepted["total"]) == ("extract", 1)
        assert waited >= 1

        with service.workers():
            final = service.wait_for_job(client, accepted["id"])

        assert final["data"]["state"] == "completed"
        result = final["data"]["items"][0]["result"]
        assert result["status"] == "success", result["error"]
        assert result["data"]["structured"]["values"] == {"heading": "Photosynthesis explained"}
        service.wait_for_idle()
        # A sync job's key goes as soon as its item has its result (R11).
        assert service.stored("job:*:llm_key") == []
        assert service.stored("job:*:drafts") == []
    assert keys_sent(httpserver) == [f"Bearer {CALLER_KEY}"]
    assert service.llm_outcomes() == {"handed_over": 1}  # the task saw the route take the job


def test_graceful_shutdown_answers_an_llm_wait_with_its_job_at_once(
    httpserver: HTTPServer,
) -> None:
    """Issue #19 (M11.4): a request whose long ``wait_seconds`` waits for the llm worker answers
    202 with the llm job as soon as the pod's graceful shutdown begins; the worker writes it."""
    serve_chat(httpserver, answer({"heading": "Photosynthesis explained"}))
    service = LlmService(httpserver)
    with TestClient(service.app) as client:
        response = answer_in_thread(
            lambda: extract(client, LLM_HEADERS, structured=LLM_HEADING, wait_seconds=60)
        )
        deadline = time.monotonic() + 10
        while not service.pending() and time.monotonic() < deadline:
            time.sleep(0.02)  # the request now waits for its llm job
        assert response(0.3) is None
        began = time.monotonic()
        begin_shutdown(client)
        answered = response(10)
        took = time.monotonic() - began

        assert answered is not None
        assert answered.status_code == 202, answered.text
        assert took < 3
        with service.workers():
            final = service.wait_for_job(client, answered.json()["data"]["id"])
        result = final["data"]["items"][0]["result"]
        assert result["status"] == "success", result["error"]
        assert result["data"]["structured"]["values"] == {"heading": "Photosynthesis explained"}
        service.wait_for_idle()
    assert service.llm_outcomes() == {"handed_over": 1}


def test_no_llm_worker_running_answers_202_and_job_waits(httpserver: HTTPServer) -> None:
    service = LlmService(httpserver, llm_sync_wait_seconds=1)
    with TestClient(service.app) as client:
        response = extract(client, LLM_HEADERS, structured=LLM_HEADING)

        assert response.status_code == 202, response.text
        job_id = response.json()["data"]["id"]
        service.wait_for_idle()
        time.sleep(0.3)
        body = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()

        # The job waits for an llm worker (the backlog alert's case), with what it needs.
        assert body["data"]["state"] in ("queued", "running")
        assert body["data"]["items"][0]["result"] is None
        assert service.pending() == 1
        assert service.stored("job:*:llm_key") == [f"job:{{{job_id}}}:llm_key"]
        assert service.stored("job:*:drafts") == [f"job:{{{job_id}}}:drafts"]
    assert httpserver.log == []
    assert service.llm_outcomes() == {"handed_over": 1}


@pytest.mark.usefixtures("high_memory")
def test_sync_memory_guard_is_partial_llm_failed_with_content(httpserver: HTTPServer) -> None:
    service = LlmService(httpserver)
    with TestClient(service.app) as client:
        response = extract(client, LLM_HEADERS, structured=LLM_HEADING)

        assert response.status_code == 200
        body = response.json()
        assert_llm_not_done(body, BUSY_MESSAGE)
        assert body["data"]["upstream_status"] == 200
        assert service.stored() == []  # nothing was stored
    assert httpserver.log == []
    assert service.llm_outcomes() == {"memory_guard": 1}


@pytest.mark.usefixtures("high_memory")
def test_api_pod_counts_images_in_the_apps_registry(httpserver: HTTPServer) -> None:
    """Ruling R-V6 (Task 5 review m3): the API pods count the images of ``describe_images`` in
    the app's own registry, as the workers do in theirs."""
    service = LlmService(httpserver, {PAGE: fetched(body=IMAGES_ARTICLE)})
    with TestClient(service.app) as client:
        response = extract(client, LLM_HEADERS, describe_images={}, metadata=True)

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "partial"
        assert body["error"]["message"] == BUSY_MESSAGE
        assert body["data"]["image_descriptions"]["failed"] == 2
    failed = service.registry.get_sample_value(
        "text_extraction_vision_images_total", {"outcome": "failed"}
    )
    assert failed == 2


def test_sync_valkey_down_is_partial_not_5xx(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(*args: Any, **kwargs: Any) -> str:
        raise RedisConnectionError("Error 111 connecting to valkey:6379. Connection refused.")

    monkeypatch.setattr(JobStore, "create", down)
    service = LlmService(httpserver)
    with TestClient(service.app) as client:
        response = extract(client, LLM_HEADERS, structured=LLM_HEADING)

        assert response.status_code == 200  # the hop failed, the request was fine
        assert_llm_not_done(response.json(), NOT_RUN_MESSAGE)
    assert service.llm_outcomes() == {"backend_error": 1}


def test_valkey_error_during_the_llm_wait_is_partial(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(self: Any, job_id: str, **kwargs: Any) -> Any:
        raise RedisConnectionError("Error 111 connecting to valkey:6379. Connection refused.")

    monkeypatch.setattr(LlmDispatcher, "_wait", down)
    service = LlmService(httpserver)
    with TestClient(service.app) as client:
        response = extract(client, LLM_HEADERS, structured=LLM_HEADING)

        assert response.status_code == 200
        assert_llm_not_done(response.json(), NOT_RUN_MESSAGE)
        assert service.stored() == []  # the llm job was given up
    assert service.llm_outcomes() == {"backend_error": 1}


def test_pii_then_llm_order(httpserver: HTTPServer) -> None:
    """D3 (a): with ``pii`` the nlp worker masks first and moves its result on to ``llm``; the
    endpoint only ever sees the masked text."""
    contact: dict[str, Any] = {
        "schema": {"type": "object", "properties": {"author": {"type": "string"}}},
        "strategy": "llm",
    }
    serve_chat(httpserver, answer({"author": "[PERSON]"}))
    service = LlmService(httpserver)
    with TestClient(service.app) as client, service.workers(nlp=True):
        response = extract(client, LLM_HEADERS, pii="mask", structured=contact)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "success", body["error"]
        data = body["data"]
        assert_processed(data)
        assert data["structured"]["values"] == {"author": "[PERSON]"}
        assert data["structured"]["sources"] == {"author": "llm"}
        assert service.wait_for_no_jobs() == []
    [request] = chat_requests(httpserver)
    sent = json.dumps(request)
    assert "[PERSON]" in sent
    assert "[EMAIL_ADDRESS]" in sent
    assert NAME not in sent
    assert "Schmidt" not in sent
    assert EMAIL not in sent
    assert keys_sent(httpserver) == [f"Bearer {CALLER_KEY}"]
    assert service.outcomes() == {"answered": 1}  # one hop from the request: nlp, then llm
    assert service.llm_outcomes() == {}


def test_learned_rules_stored_by_runner_and_used_next_request(httpserver: HTTPServer) -> None:
    """The llm worker stores what ``learn_selectors`` kept, for the caller's key and the page's
    host, where the pipeline reads it: the next ``auto`` request needs no LLM call."""
    serve_chat(
        httpserver,
        answer({"heading": "Photosynthesis explained"}),
        answer({"heading": {"css": "article h1", "attr": None}}),
    )
    service = LlmService(httpserver)
    learning = {**LLM_HEADING, "learn_selectors": True}
    with TestClient(service.app) as client, service.workers():
        first = extract(client, LLM_HEADERS, structured=learning)
        second = extract(client, LLM_HEADERS, structured={"schema": HEADING, "strategy": "auto"})

    assert first.status_code == second.status_code == 200
    learned = first.json()["data"]["structured"]
    assert learned["sources"] == {"heading": "llm"}
    assert learned["learned"] == {"stored": ["heading"], "rejected": []}
    again = second.json()
    assert again["status"] == "success", again["error"]
    assert again["data"]["structured"]["values"] == {"heading": "Photosynthesis explained"}
    assert again["data"]["structured"]["sources"] == {"heading": "learned"}
    assert len(chat_requests(httpserver)) == 2  # the extraction and the learning call only
    assert service.stored("sel:*") != []


def test_sync_post_stage_without_llm_work_answers_inline(httpserver: HTTPServer) -> None:
    """``auto`` that the DOM stage and the patterns fill needs no hop: no job, no LLM call."""
    service = LlmService(httpserver)
    structured = {
        "schema": HEADING,
        "patterns": {"heading": {"regex": r"green (\w+)", "group": 1}},
    }
    with TestClient(service.app) as client:
        response = extract(client, LLM_HEADERS, structured=structured)

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "success", body["error"]
        assert body["data"]["structured"]["sources"] == {"heading": "patterns"}
        assert service.stored() == []
    assert httpserver.log == []
    assert service.llm_outcomes() == {}


# --- image descriptions (issue #20, rulings V-S5, V-S7) ---

PICTURE = "Ein Balkendiagramm mit drei Balken."
IMAGES_ARTICLE = (
    "<!doctype html><html lang='de'><head><title>Brüche</title>"
    f"<meta property='og:image' content='{image_url(0)}'></head><body><article>"
    f"<h1>Brüche</h1><p>{PARAGRAPH * 3}</p><img src='{image_url(1)}' alt='Ein Kreis'>"
    "</article></body></html>"
)
# A PNG the vision model takes (the magic bytes decide, not the declared type).
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 64


def upload(
    client: TestClient,
    body: bytes,
    name: str,
    content_type: str,
    headers: dict[str, str] | None = None,
    **form: Any,
) -> Any:
    data = {
        key: value if isinstance(value, str) else json.dumps(value) for key, value in form.items()
    }
    return client.post(
        "/v1/extract/file",
        files={"file": (name, body, content_type)},
        data=data,
        headers=headers or AUTH,
    )


def stored_input(service: LlmService, job_id: str) -> tuple[bytes, StoredInputMeta] | None:
    async def read(valkey: FakeAsyncRedis) -> tuple[bytes, StoredInputMeta] | None:
        return await JobStore(valkey, service.settings).get_input(job_id, 0, StoredInputMeta)

    result: tuple[bytes, StoredInputMeta] | None = service.run(read)
    return result


def test_sync_page_with_describe_goes_to_llm_hop(httpserver: HTTPServer) -> None:
    serve_llm(httpserver, lambda _body, _n: described(PICTURE))
    service = LlmService(httpserver, {PAGE: fetched(body=IMAGES_ARTICLE)})
    images = FakeImages()
    with TestClient(service.app) as client, service.workers(images=images):
        response = extract(client, LLM_HEADERS, describe_images={}, metadata=True)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "success", body["error"]
        data = body["data"]
        described_images = [image["description"] for image in data["metadata"]["images"]]
        assert described_images == [PICTURE, PICTURE]
        assert data["image_descriptions"]["described"] == 2
        assert PICTURE not in data["content"]  # a page's content stays its text
        assert service.wait_for_no_jobs() == []
    assert [call[0] for call in images.calls] == [image_url(0), image_url(1)]
    assert keys_sent(httpserver) == [f"Bearer {CALLER_KEY}"] * 2
    assert service.llm_outcomes() == {"answered": 1}


def test_sync_upload_handoff_stores_image_input(httpserver: HTTPServer) -> None:
    """V-S7: the sync hop of an image upload stores its bytes as the llm job's input; a PDF
    stores nothing (an image over LLM_VISION_MAX_IMAGE_BYTES takes no hop for its description:
    ``test_sync_upload_over_vision_cap_takes_no_llm_hop``)."""
    service = LlmService(httpserver, llm_sync_wait_seconds=1)
    structured = {"schema": HEADING, "strategy": "llm"}
    with TestClient(service.app) as client:
        image = upload(
            client, PNG, "photo.png", "image/png", LLM_HEADERS, describe_images={}, wait_seconds=1
        )
        pdf = upload(
            client,
            b"%PDF-1.7\nscan\n%%EOF\n",
            "scan.pdf",
            "application/pdf",
            LLM_HEADERS,
            describe_images={},
            structured=structured,
            wait_seconds=1,
        )
        service.wait_for_idle()

        jobs = []
        for response in (image, pdf):
            assert response.status_code == 202, response.text
            jobs.append(response.json()["data"]["id"])
        kept = stored_input(service, jobs[0])
        assert kept is not None
        body, meta = kept
        assert body == PNG
        assert (meta.file_name, meta.content_type) == ("photo.png", "image/png")
        assert stored_input(service, jobs[1]) is None
        assert service.pending() == 2
    assert httpserver.log == []


def test_sync_upload_over_vision_cap_takes_no_llm_hop(httpserver: HTTPServer) -> None:
    """Ruling R-V3 (final review m1): an image upload over LLM_VISION_MAX_IMAGE_BYTES cannot be
    described. With ``describe_images`` alone it takes no llm hop: it answers at once, the
    image skipped, and stores no input."""
    service = LlmService(httpserver, llm_sync_wait_seconds=1, llm_vision_max_image_bytes=10_000)
    large = PNG + b"\x00" * 20_000
    with TestClient(service.app) as client:
        response = upload(
            client, large, "big.png", "image/png", LLM_HEADERS, describe_images={}, wait_seconds=1
        )
        service.wait_for_idle()

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "success", body["error"]
        data = body["data"]
        report = data["image_descriptions"]
        assert (report["asked"], report["described"], report["skipped"]) == (1, 0, 1)
        assert report["failed"] == 0
        assert "image_not_described" in data["warnings"]
        assert service.pending() == 0
        assert service.stored("job:*:input:*") == []
    assert httpserver.log == []
    assert service.llm_outcomes() == {}
    skipped = service.registry.get_sample_value(
        "text_extraction_vision_images_total", {"outcome": "skipped"}
    )
    assert skipped == 1


def test_p1_upload_job_is_described_from_its_own_input(httpserver: HTTPServer) -> None:
    """An upload handed over to its P1 job at once: the llm worker describes the P1 job's own
    input, and deletes it with the result."""
    serve_llm(httpserver, lambda _body, _n: described(PICTURE))
    service = LlmService(httpserver)
    with TestClient(service.app) as client, service.workers():
        response = upload(
            client, PNG, "photo.png", "image/png", LLM_HEADERS, describe_images={}, wait_seconds=0
        )
        assert response.status_code == 202, response.text
        accepted = response.json()["data"]
        assert accepted["kind"] == "file"
        final = service.wait_for_job(client, accepted["id"])

        result = final["data"]["items"][0]["result"]
        assert result["status"] == "success", result["error"]
        assert result["data"]["content"] == f"> Image description: {PICTURE}"
        service.wait_for_idle()
        assert service.stored("job:*:input:*") == []


def test_upload_input_budget_full_is_busy(httpserver: HTTPServer) -> None:
    """The input budget is full at the hand-off: nothing is stored, the upload answers its
    content without the LLM work, ``partial``/``llm_failed``, retryable, busy."""
    service = LlmService(httpserver, docling_input_budget_bytes=10_000, document_max_bytes=10_000)

    async def fill(valkey: FakeAsyncRedis) -> None:
        store = JobStore(valkey, service.settings)
        other = await store.create(
            "file",
            Principal(name="bob", role="user", rate_limit_per_minute=None),
            JobOptions(compliance="enforce"),
            [None],
        )
        await store.put_input(other, 0, b"f" * 9_900, StoredInputMeta(), 600)

    service.run(fill)
    with TestClient(service.app) as client:
        response = upload(client, PNG, "photo.png", "image/png", LLM_HEADERS, describe_images={})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "partial"
        assert body["error"]["code"] == "llm_failed"
        assert body["error"]["retryable"] is True
        assert body["error"]["message"] == BUSY_MESSAGE
        data = body["data"]
        assert data["content"] == ""
        assert data["image_descriptions"]["failed"] == 1
        assert service.pending() == 0
    assert httpserver.log == []
    assert service.llm_outcomes() == {"input_budget": 1}


# --- the parts ---


def test_app_has_the_llm_dispatcher_and_its_queue_gauges(httpserver: HTTPServer) -> None:
    service = LlmService(httpserver)
    assert service.app.state.llm_dispatcher is not None
    with TestClient(service.app):
        pass
    assert (
        service.registry.get_sample_value("text_extraction_queue_pending_items", {"pool": "llm"})
        is not None
    )
    plain = create_public_app(Settings(auth_mode="none"), FakeAsyncRedis(), CollectorRegistry())
    assert plain.state.llm_dispatcher is None


def test_llm_wait_is_the_sync_wait_plus_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def wait_for(self: Any, job_id: str, **limits: Any) -> None:
        calls.append(limits)

    monkeypatch.setattr(LlmDispatcher, "wait_for", wait_for)
    settings = Settings(auth_mode="none", llm_enabled=True, llm_sync_wait_seconds=12)
    dispatcher = LlmDispatcher(Any, Any, settings)  # type: ignore[arg-type]

    asyncio.run(dispatcher.wait("j", extra_seconds=3, detached=lambda: False))

    [limits] = calls
    assert limits["claim_within"] == limits["total_within"] == 15


def test_create_llm_move_only_with_the_llm() -> None:
    registry = CollectorRegistry()
    valkey = FakeAsyncRedis()
    on = Settings(auth_mode="none", llm_enabled=True)
    off = Settings(auth_mode="none")
    assert create_llm_move(on, valkey, JobStore(valkey, on), None, registry) is not None
    assert create_llm_move(off, valkey, JobStore(valkey, off), None, registry) is None
