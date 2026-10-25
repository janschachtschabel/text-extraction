"""The ASR dispatch (rulings M9, M11, M14): ``POST /v1/extract`` with ``transcribe: true`` and
``POST /v1/extract/file`` with an audio or video file, on fakeredis.

The pipeline probes platform videos with a fake ``MediaFetcher`` and fetches media files from a
fake fetcher. Nothing waits for a transcription: whatever needs one becomes a job on pool ``asr``
at once (202), with the metadata result as its draft and the ``MediaInputMeta`` as its input.
"""

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from pydantic import BaseModel
from redis.exceptions import RedisError
from structlog.testing import capture_logs

from tests.fakes import FakeFetcher, fetched
from tests.test_pipeline_media import MP3, YT, YT_ID, FakeMediaFetcher, probe
from text_extraction import extract_api
from text_extraction.app import create_public_app
from text_extraction.asr_dispatch import (
    ASR_KEY_HEADER,
    ASR_OUTCOMES,
    asr_lease_seconds,
    asr_queue,
    draft_without_asr,
)
from text_extraction.auth import Principal
from text_extraction.conversion_pool import ConversionPool
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractResult
from text_extraction.fetch import Fetcher
from text_extraction.job_models import JobOptions
from text_extraction.jobs import JobRecord, JobStore
from text_extraction.media.pool import MediaFetcher, MediaProbe
from text_extraction.media.sites import MediaTarget
from text_extraction.memory_guard import MemoryGuard
from text_extraction.pipeline import create_pipeline
from text_extraction.pipeline_media import ASR_UNAVAILABLE_WARNING, MediaInputMeta
from text_extraction.server import graceful_shutdown_seconds
from text_extraction.settings import Settings

USER_KEY = "user-key-0123456789-raw-secret"
AUTH = {"Authorization": f"Bearer {USER_KEY}"}
ALICE = Principal(name="alice", role="user", rate_limit_per_minute=None)
BOB = Principal(name="bob", role="user", rate_limit_per_minute=None)
FILE = "https://cdn.example/lessons/photosynthesis.mp3?token=secret"
OGG = b"OggS\x00\x02" + b"\x00" * 64
# The caller's key of the external transcription endpoint: no job view or log line may carry it.
ASR_KEY = "sk-asr-caller-key-5d1e7c"
# A small input budget and the largest upload within it.
BUDGET: dict[str, Any] = {
    "docling_input_budget_bytes": 1_000_000,
    "document_max_bytes": 50_000,
    "media_upload_max_bytes": 1_000_000,
}
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
        "asr_enabled": True,
        "asr_input_ttl_seconds": 1800,
    }
    values.update(overrides)
    return Settings(**values)


class Filler(BaseModel):
    """The meta of an input that only takes up the budget."""


def slow(answer: MediaProbe) -> Callable[[MediaTarget], Awaitable[MediaProbe]]:
    async def probing(target: MediaTarget) -> MediaProbe:
        await asyncio.sleep(SLOW_SECONDS)
        return answer

    return probing


class Service:
    """The public app with ASR on, on a fakeredis server, with a fake media fetcher."""

    def __init__(self, media: Any = None, **overrides: Any) -> None:
        self.settings = make_settings(**overrides)
        self.server = FakeServer()
        self.registry = CollectorRegistry()
        valkey = FakeAsyncRedis(server=self.server)
        self.app = create_public_app(self.settings, valkey, self.registry)
        self.fetcher = FakeFetcher({FILE: fetched(FILE, MP3, content_type="audio/mpeg")})
        self.media = FakeMediaFetcher(media if media is not None else probe(track=None))
        self.app.state.pipeline = create_pipeline(
            self.settings,
            valkey,
            cast(Fetcher, self.fetcher),
            self.registry,
            ConversionPool(1),
            memory_guard=self.app.state.memory_guard,
            media_fetcher=cast(MediaFetcher, self.media),
        )

    def run(self, work: Callable[[FakeAsyncRedis], Awaitable[Any]]) -> Any:
        async def scenario() -> Any:
            return await work(FakeAsyncRedis(server=self.server))

        return asyncio.run(scenario())

    def store(self, call: Callable[[JobStore], Awaitable[Any]]) -> Any:
        return self.run(lambda valkey: call(JobStore(valkey, self.settings)))

    def stored(self, pattern: str = "job:*") -> list[str]:
        async def scan(valkey: FakeAsyncRedis) -> list[str]:
            return sorted(
                key.decode() if isinstance(key, bytes) else key
                for key in await valkey.keys(pattern)
            )

        return list(self.run(scan))

    def ttl(self, key: str) -> int:
        async def read(valkey: FakeAsyncRedis) -> int:
            return int(await valkey.ttl(key))

        return int(self.run(read))

    def claim(self) -> Any:
        async def claiming(valkey: FakeAsyncRedis) -> Any:
            return (await asr_queue(valkey, self.settings).claim(60)).item

        return self.run(claiming)

    def pending(self) -> int:
        async def count(valkey: FakeAsyncRedis) -> int:
            return (await asr_queue(valkey, self.settings).counts()).pending

        return int(self.run(count))

    def outcomes(self) -> dict[str, float]:
        counts: dict[str, float] = {}
        for outcome in ASR_OUTCOMES:
            value = self.registry.get_sample_value(
                "text_extraction_asr_dispatch_total", {"outcome": outcome}
            )
            assert value is not None  # every outcome is exported from the start
            if value:
                counts[outcome] = value
        return counts

    def created(self, kind: str) -> float:
        value = self.registry.get_sample_value("text_extraction_jobs_created_total", {"kind": kind})
        return value or 0.0

    def open_jobs(self, count: int) -> None:
        """``count`` unfinished ASR jobs of alice."""

        async def create(store: JobStore) -> None:
            for _ in range(count):
                job_id = await store.create(
                    "extract", ALICE, JobOptions(compliance="enforce"), [YT]
                )
                await store.add_asr_job("alice", job_id)

        self.store(create)

    def fill_budget(self, *, leave: int = 10) -> str:
        """Another key's input that leaves ``leave`` bytes of the input budget; its job's id."""

        async def fill(store: JobStore) -> str:
            job_id = await store.create("extract", BOB, JobOptions(compliance="enforce"), [YT])
            size = self.settings.docling_input_budget_bytes - len(Filler().model_dump_json())
            await store.put_input(job_id, 0, b"f" * (size - leave), Filler(), 600)
            return job_id

        return str(self.store(fill))


@pytest.fixture
def high_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MemoryGuard, "high", property(lambda self: True))


def extract(
    client: TestClient, url: str, headers: dict[str, str] | None = None, **body: Any
) -> Any:
    return client.post(
        "/v1/extract", json={"url": url, **body}, headers={**AUTH, **(headers or {})}
    )


def upload(
    client: TestClient,
    body: bytes = OGG,
    headers: dict[str, str] | None = None,
    **form: str,
) -> Any:
    return client.post(
        "/v1/extract/file",
        files={"file": ("talk.ogg", body, "audio/ogg")},
        data=form,
        headers={**AUTH, **(headers or {})},
    )


# --- sync requests: always a job ---


def test_transcribe_without_subtitles_answers_202() -> None:
    service = Service()
    with TestClient(service.app) as client:
        started = time.monotonic()
        response = extract(client, YT, transcribe=True)
        waited = time.monotonic() - started

        assert response.status_code == 202, response.text
        accepted = response.json()["data"]
        assert (accepted["kind"], accepted["total"]) == ("extract", 1)
        assert accepted["state"] == "queued"
        assert waited < 5  # at once, not after the 20 s that transcribe waits by default
        job_id = accepted["id"]
        view = client.get(accepted["status_url"], headers=AUTH).json()["data"]
        assert view["state"] == "queued"
        assert view["items"][0]["url"] == YT
        assert view["items"][0]["result"] is None

    record = service.store(lambda store: store.get(job_id))
    assert record.kind == "extract"
    assert record.options.transcribe is True
    # The metadata result is the item's draft, neither fingerprinted nor cached yet.
    draft: Envelope[ExtractResult] = service.store(lambda store: store.draft(job_id, 0))
    assert draft.data is not None
    assert draft.data.media is not None
    assert draft.data.media.transcript_source == "metadata_only"
    assert draft.data.title == "Photosynthesis for kids"
    assert ASR_UNAVAILABLE_WARNING not in draft.data.warnings
    assert service.stored("res:*") == []
    # The input: what the ASR worker needs to know, with an empty body.
    stored = service.store(lambda store: store.get_input(job_id, 0, MediaInputMeta))
    body, meta = stored
    assert body == b""
    assert (meta.source, meta.platform, meta.extractor) == ("platform", "youtube", "youtube")
    assert meta.url == f"https://www.youtube.com/watch?v={YT_ID}"
    assert meta.duration == 212.0
    assert meta.compliance is not None
    assert 1790 <= service.ttl(f"job:{{{job_id}}}:input:0") <= 1800
    # Queued on pool ``asr`` under the key's live client.
    item = service.claim()
    assert (item.job_id, item.index, item.client) == (job_id, 0, "alice#live")
    assert service.outcomes() == {"handed_over": 1}
    assert service.created("extract") == 1


def test_transcribe_with_subtitles_answers_200() -> None:
    service = Service(probe())
    with TestClient(service.app) as client:
        response = extract(client, YT, transcribe=True)

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "success", body["error"]
        assert body["data"]["media"]["transcript_source"] == "manual_subtitles"
        assert service.stored() == []
        assert service.pending() == 0
    assert service.outcomes() == {}
    assert service.created("extract") == 0


def test_media_file_url_hands_over_without_a_draft() -> None:
    service = Service()
    with TestClient(service.app) as client:
        response = extract(client, FILE, transcribe=True)

        assert response.status_code == 202
        job_id = response.json()["data"]["id"]
    assert service.store(lambda store: store.draft(job_id, 0)) is None
    body, meta = service.store(lambda store: store.get_input(job_id, 0, MediaInputMeta))
    assert body == b""
    assert (meta.source, meta.kind, meta.url) == ("file", "mp3", FILE)
    assert service.outcomes() == {"handed_over": 1}


def test_upload_hands_over_202_with_input_bytes() -> None:
    service = Service()
    with TestClient(service.app) as client:
        started = time.monotonic()
        response = upload(client)  # the form's wait_seconds (25) does not apply to ASR
        waited = time.monotonic() - started

        assert response.status_code == 202, response.text
        accepted = response.json()["data"]
        assert accepted["kind"] == "file"
        assert waited < 5
        job_id = accepted["id"]
        view = client.get(accepted["status_url"], headers=AUTH).json()["data"]
        assert view["items"][0]["url"] is None

    body, meta = service.store(lambda store: store.get_input(job_id, 0, MediaInputMeta))
    assert body == OGG
    assert (meta.source, meta.platform, meta.kind) == ("upload", "upload", "ogg")
    assert meta.file_name == "talk.ogg"
    assert service.store(lambda store: store.draft(job_id, 0)) is None
    assert 1790 <= service.ttl(f"job:{{{job_id}}}:input:0") <= 1800  # ASR_INPUT_TTL_SECONDS

    async def reserved(valkey: FakeAsyncRedis) -> int:
        return int(await valkey.hget("inputs:{budget}:size", f"{job_id}:0") or 0)

    assert service.run(reserved) == len(OGG) + len(meta.model_dump_json())
    item = service.claim()
    assert (item.job_id, item.domain, item.client) == (job_id, "-", "alice#live")
    assert service.outcomes() == {"handed_over": 1}
    assert service.created("file") == 1


def test_handed_over_job_is_never_deleted_by_the_request() -> None:
    """Nothing waits: the job stays for the ASR worker and its caller."""
    service = Service()
    with TestClient(service.app) as client:
        job_id = extract(client, YT, transcribe=True).json()["data"]["id"]
        time.sleep(0.3)

        assert f"job:{{{job_id}}}:meta" in service.stored()
        assert service.pending() == 1


# --- after a hand-over to a P1 job ---


@pytest.mark.parametrize("source", ["url", "upload"])
def test_transcription_after_handover_reuses_p1_job(source: str) -> None:
    service = Service(slow(probe(track=None)))
    with TestClient(service.app) as client:
        if source == "url":
            response = extract(client, YT, transcribe=True, wait_seconds=0.2)
        else:
            response = upload(client, wait_seconds="0")

        assert response.status_code == 202
        accepted = response.json()["data"]
        assert accepted["kind"] == ("extract" if source == "url" else "file")
        assert accepted["state"] == "running"
        job_id = accepted["id"]
        deadline = time.monotonic() + 5
        while service.pending() == 0 and time.monotonic() < deadline:
            time.sleep(0.02)

        item = service.claim()
        assert item.job_id == job_id
        record = service.store(lambda store: store.get(job_id))
        assert record.lease_until is None  # the ASR pool has its own time
        assert record.state == "running"
        view = client.get(accepted["status_url"], headers=AUTH).json()["data"]
        assert view["state"] == "running"  # not shown as lost
        assert view["items"][0]["result"] is None  # nothing stored by the request

    body, meta = service.store(lambda store: store.get_input(job_id, 0, MediaInputMeta))
    assert meta.source == ("platform" if source == "url" else "upload")
    assert body == (b"" if source == "url" else OGG)  # the upload's own input was replaced
    assert service.outcomes() == {"handed_over": 1}
    assert service.created(accepted["kind"]) == 1  # the P1 job only


def test_a_failed_p1_job_leaves_the_asr_job_to_the_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route claimed the hand-over but could not create its P1 job: the ASR job the flow
    dispatches then is the one the route answers 202 with."""

    async def no_job(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(extract_api, "create_p1_job", no_job)
    service = Service(slow(probe(track=None)))
    with TestClient(service.app) as client:
        response = extract(client, YT, transcribe=True, wait_seconds=0.2)

        assert response.status_code == 202
        accepted = response.json()["data"]
        assert accepted["kind"] == "extract"
        item = service.claim()
        assert item.job_id == accepted["id"]
    assert service.outcomes() == {"handed_over": 1}
    assert service.created("extract") == 1


# --- back-pressure ---


def test_open_limit() -> None:
    service = Service()
    service.open_jobs(3)
    with TestClient(service.app) as client:
        video = extract(client, YT, transcribe=True)
        media_file = extract(client, FILE, transcribe=True)
        uploaded = upload(client)

        assert video.status_code == 200
        body = video.json()
        assert body["status"] == "partial"
        assert body["error"]["code"] == "no_transcript_available"
        assert ASR_UNAVAILABLE_WARNING in body["data"]["warnings"]
        assert body["data"]["fingerprint"] is None
        for refused in (media_file.json(), uploaded.json()):
            assert refused["status"] == "failed"
            assert (refused["error"]["code"], refused["error"]["retryable"]) == (
                "rate_limited",
                True,
            )
            assert "3" in refused["error"]["message"]
        assert len(service.stored("job:*:meta")) == 3  # no fourth job was left behind
        assert service.stored("job:*:input:*") == []
        assert service.pending() == 0
    assert service.outcomes() == {"open_limit": 3}


def test_open_limit_counts_only_unfinished_jobs() -> None:
    service = Service()
    service.open_jobs(2)
    finished = Envelope[ExtractResult](status="failed")
    with TestClient(service.app) as client:
        first = extract(client, YT, transcribe=True)  # the third open job
        assert first.status_code == 202
        service.store(lambda store: store.write_result(first.json()["data"]["id"], 0, finished))

        assert extract(client, YT, transcribe=True).status_code == 202
    assert service.outcomes() == {"handed_over": 2}


@pytest.mark.usefixtures("high_memory")
def test_memory_guard_high() -> None:
    service = Service()
    with TestClient(service.app) as client:
        video = extract(client, YT, transcribe=True).json()
        media_file = extract(client, FILE, transcribe=True).json()
        uploaded = upload(client).json()

        assert video["status"] == "partial"
        assert ASR_UNAVAILABLE_WARNING in video["data"]["warnings"]
        assert video["data"]["fingerprint"] is None
        for refused in (media_file, uploaded):
            assert refused["status"] == "failed"
            assert (refused["error"]["code"], refused["error"]["retryable"]) == (
                "rate_limited",
                True,
            )
        assert service.stored() == []
        assert service.pending() == 0
    assert service.outcomes() == {"memory_guard": 3}


def test_input_budget_full() -> None:
    service = Service(**BUDGET)
    filler = service.fill_budget()
    with TestClient(service.app) as client:
        video = extract(client, YT, transcribe=True).json()
        media_file = extract(client, FILE, transcribe=True).json()
        uploaded = upload(client).json()

        assert video["status"] == "partial"
        assert ASR_UNAVAILABLE_WARNING in video["data"]["warnings"]
        assert video["data"]["fingerprint"] is None
        for refused in (media_file, uploaded):
            assert refused["status"] == "failed"
            assert (refused["error"]["code"], refused["error"]["retryable"]) == (
                "rate_limited",
                True,
            )
        assert service.stored("job:*:meta") == [f"job:{{{filler}}}:meta"]  # none left behind
        assert service.stored("jobs:*") == []  # and none counts as alice's open job
        assert service.pending() == 0
    assert service.outcomes() == {"input_budget": 3}


def test_backend_error_answers_the_draft_or_503(monkeypatch: pytest.MonkeyPatch) -> None:
    async def down(self: JobStore, owner: str) -> int:
        raise RedisError("Valkey is down")

    monkeypatch.setattr(JobStore, "open_asr_jobs", down)
    service = Service()
    with TestClient(service.app, raise_server_exceptions=False) as client:
        video = extract(client, YT, transcribe=True)
        media_file = extract(client, FILE, transcribe=True)

        assert video.status_code == 200
        assert ASR_UNAVAILABLE_WARNING in video.json()["data"]["warnings"]
        assert media_file.status_code == 503
        assert media_file.json()["error"]["retryable"] is True
    assert service.outcomes() == {"backend_error": 2}


# --- the caller's key of the external endpoint (M11) ---


def test_secret_header_stored_and_never_in_job_view() -> None:
    service = Service()
    with TestClient(service.app) as client, capture_logs() as logs:
        accepted = extract(client, YT, headers={ASR_KEY_HEADER: ASR_KEY}, transcribe=True)
        uploaded = upload(client, headers={ASR_KEY_HEADER: ASR_KEY})

        assert (accepted.status_code, uploaded.status_code) == (202, 202)
        job_ids = [accepted.json()["data"]["id"], uploaded.json()["data"]["id"]]
        for job_id in job_ids:
            view = client.get(f"/v1/jobs/{job_id}", headers=AUTH)
            assert view.status_code == 200
            assert ASR_KEY not in view.text

    longest = min(
        service.settings.job_ttl_seconds,
        service.settings.asr_input_ttl_seconds + asr_lease_seconds(service.settings),
    )
    for job_id in job_ids:

        async def read(store: JobStore, job: str = job_id) -> tuple[str | None, JobRecord | None]:
            return await store.asr_key(job), await store.get(job)

        key, record = service.store(read)
        assert key == ASR_KEY
        assert 0 < service.ttl(f"job:{{{job_id}}}:asr_key") <= longest
        assert record is not None
        assert record.has_secrets is False  # not a browser job's secrets
    assert not any(ASR_KEY in repr(event) for event in logs)


def test_secret_header_is_not_stored_without_a_transcription() -> None:
    service = Service(probe())  # subtitles: no job
    with TestClient(service.app) as client:
        response = extract(client, YT, headers={ASR_KEY_HEADER: ASR_KEY}, transcribe=True)

        assert response.status_code == 200
        assert service.stored() == []


def test_deleting_the_job_deletes_the_key() -> None:
    service = Service()
    with TestClient(service.app) as client:
        job_id = extract(client, YT, headers={ASR_KEY_HEADER: ASR_KEY}, transcribe=True).json()[
            "data"
        ]["id"]
    service.store(lambda store: store.delete(job_id))
    assert service.stored() == []


@pytest.mark.parametrize(
    "value", ["has space", "x" * 1025, "tab\there"], ids=["space", "too long", "tab"]
)
def test_an_invalid_secret_header_is_400(value: str) -> None:
    service = Service()
    with TestClient(service.app) as client:
        response = extract(client, YT, headers={ASR_KEY_HEADER: value}, transcribe=True)
        uploaded = upload(client, headers={ASR_KEY_HEADER: value})

    for refused in (response, uploaded):
        assert refused.status_code == 400
        assert refused.json()["error"]["code"] == "invalid_request"
        assert value not in refused.text
    assert service.stored() == []


def test_the_secret_header_is_documented() -> None:
    service = Service()
    with TestClient(service.app) as client:
        spec = client.get("/openapi.json").json()

    for path in ("/v1/extract", "/v1/extract/file"):
        parameters = spec["paths"][path]["post"]["parameters"]
        names = [parameter["name"] for parameter in parameters]
        assert ASR_KEY_HEADER in names, path


# --- the pieces ---


def test_asr_queue_has_no_domain_cap() -> None:
    settings = make_settings(outbound_max_concurrency_per_host=1)

    async def scenario() -> int:
        valkey = FakeAsyncRedis()
        queue = asr_queue(valkey, settings)
        assert queue.pool == "asr"
        await queue.enqueue_items("a" * 32, "alice#live", [(0, None), (1, None), (2, None)])
        claims = [await queue.claim(60) for _ in range(3)]
        return sum(claim.item is not None for claim in claims)

    assert asyncio.run(scenario()) == 3


def test_asr_lease_covers_two_transcriptions() -> None:
    assert asr_lease_seconds(make_settings(asr_timeout_seconds=600)) == 2 * 600 + 10


def test_draft_without_asr() -> None:
    assert draft_without_asr(None) is None
    service = Service()
    with TestClient(service.app) as client:
        job_id = extract(client, YT, transcribe=True).json()["data"]["id"]
    draft = service.store(lambda store: store.draft(job_id, 0))
    answered = draft_without_asr(draft)
    assert answered is not None
    assert answered.data is not None
    assert ASR_UNAVAILABLE_WARNING in answered.data.warnings
    assert answered.data.fingerprint is None
    assert answered.data.content == draft.data.content


def test_open_asr_jobs_are_counted_per_owner() -> None:
    async def scenario() -> tuple[int, int, int]:
        store = JobStore(FakeAsyncRedis(), make_settings())
        jobs = [
            await store.create("extract", ALICE, JobOptions(compliance="enforce"), [YT])
            for _ in range(3)
        ]
        for job_id in jobs:
            await store.add_asr_job("alice", job_id)
        await store.write_result(jobs[0], 0, Envelope[ExtractResult](status="failed"))
        await store.delete(jobs[1])
        await store.drop_asr_job("alice", jobs[2])
        other = await store.add_docling_job("alice", jobs[2])  # Docling's count is its own
        return await store.open_asr_jobs("alice"), await store.open_asr_jobs("bob"), other

    assert asyncio.run(scenario()) == (0, 0, 1)


def test_the_asr_queue_gauges_are_exported() -> None:
    service = Service()
    with TestClient(service.app):
        asyncio.run(service.app.state.queue_gauges.refresh())
        assert (
            service.registry.get_sample_value(
                "text_extraction_queue_pending_items", {"pool": "asr"}
            )
            == 0
        )


def test_without_asr_nothing_is_dispatched() -> None:
    service = Service(asr_enabled=False)
    assert service.app.state.asr_dispatcher is None
    assert (
        service.registry.get_sample_value(
            "text_extraction_asr_dispatch_total", {"outcome": "handed_over"}
        )
        is None
    )


def test_nothing_waits_for_asr_at_shutdown() -> None:
    assert graceful_shutdown_seconds(make_settings()) == graceful_shutdown_seconds(
        make_settings(asr_enabled=False)
    )


def test_job_view_never_shows_the_input() -> None:
    service = Service()
    with TestClient(service.app) as client:
        marker = b"OggS\x00\x02MARKER-5e1f" + b"\x00" * 32
        job_id = upload(client, marker).json()["data"]["id"]
        view = client.get(f"/v1/jobs/{job_id}", headers=AUTH)

        assert "MARKER-5e1f" not in view.text
        assert "input" not in json.dumps(view.json()["data"])
