"""``POST /v1/batch`` and ``GET /v1/jobs/{id}`` (``jobs_api.py``): the real job store and queue on
fakeredis, API keys for two users and an admin, and, where a job has to finish, a real
``Worker.process`` pass on a fake fetcher."""

import asyncio
import hashlib
import json
import time
from collections.abc import Callable, Coroutine, Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi import Request
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from structlog.testing import capture_logs

from tests.fakes import ARTICLE, ORIGIN, PAGE, Answer, FakeFetcher, fetched
from text_extraction.app import create_public_app
from text_extraction.auth import Principal
from text_extraction.chunking.types import ChunkingOptions
from text_extraction.envelope import ErrorCode
from text_extraction.extract_models import ExtractRequest
from text_extraction.fetch import Fetcher, FetchError
from text_extraction.job_models import MAX_BATCH_URLS, BatchRequest, JobOptions
from text_extraction.jobs import JobRecord, JobStore
from text_extraction.jobs_api import create_batch
from text_extraction.netguard import MAX_URL_LENGTH
from text_extraction.nlp_worker import PiiRunner
from text_extraction.pii.types import PiiOptions
from text_extraction.pii_dispatch import create_pii_move, nlp_queue
from text_extraction.pipeline import create_pipeline
from text_extraction.scheduler import FairQueue, QueueCounts
from text_extraction.settings import Settings
from text_extraction.structured.types import StructuredOptions
from text_extraction.vision.types import DescribeImagesOptions
from text_extraction.worker import ExtractRunner, Worker

KEYS = {"alice": "alice-key-raw-secret", "bob": "bob-key-raw-secret", "root": "root-key-raw-secret"}
ROLES = {"alice": "user", "bob": "user", "root": "admin"}
ALICE = {"Authorization": f"Bearer {KEYS['alice']}"}
BOB = {"Authorization": f"Bearer {KEYS['bob']}"}
ROOT = {"Authorization": f"Bearer {KEYS['root']}"}

NOT_FOUND_MESSAGE = "Job not found."
OTHER_PAGE = f"{ORIGIN}/learn/respiration.html"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "auth_mode": "api_key",
        "api_keys": [
            {
                "name": name,
                "role": ROLES[name],
                "sha256": hashlib.sha256(key.encode()).hexdigest(),
            }
            for name, key in KEYS.items()
        ],
        "outbound_min_delay_ms": 0,
    }
    values.update(overrides)
    return Settings(**values)


class Api:
    """The public app on a fakeredis server, plus ways to reach that server from the test."""

    def __init__(self, client: TestClient, server: FakeServer, settings: Settings) -> None:
        self.client = client
        self.server = server
        self.settings = settings

    def run[T](self, work: Callable[[FakeAsyncRedis], Coroutine[Any, Any, T]]) -> T:
        """Run ``work`` against the server with a client of its own (the app's runs in the
        test client's event loop)."""

        async def call() -> T:
            return await work(FakeAsyncRedis(server=self.server))

        return asyncio.run(call())

    def keys(self, pattern: str) -> list[str]:
        async def find(valkey: FakeAsyncRedis) -> list[str]:
            return sorted(
                str(key, "utf-8") if isinstance(key, bytes) else key
                for key in await valkey.keys(pattern)
            )

        return self.run(find)

    def work(self, answers: dict[str, Answer] | None = None, *, items: int | None = None) -> int:
        """Claim and process queued items (at most ``items``) as a worker would; returns how many
        were processed."""

        async def process(valkey: FakeAsyncRedis) -> int:
            registry = CollectorRegistry()
            fetcher = cast(Fetcher, FakeFetcher(answers or {PAGE: fetched(body=ARTICLE)}))
            store = JobStore(valkey, self.settings)
            worker = Worker(
                self.settings,
                valkey,
                ExtractRunner(
                    create_pipeline(self.settings, valkey, fetcher, registry),
                    store,
                    self.settings,
                    None,
                ),
                store,
                FairQueue(valkey, self.settings),
                registry,
                pii_move=create_pii_move(self.settings, valkey, store, None, registry),
            )
            queue = FairQueue(valkey, self.settings)
            processed = 0
            while items is None or processed < items:
                claim = await queue.claim(60)
                if claim.item is None:
                    break
                await worker.process(claim.item, exhausted=claim.exhausted)
                processed += 1
            return processed

        return self.run(process)

    def work_nlp(self) -> int:
        """Claim and process the items queued on ``nlp`` as the nlp worker would (with the fake
        child pool of its tests); returns how many were processed."""
        from tests.test_nlp_worker import FakePiiPool

        async def process(valkey: FakeAsyncRedis) -> int:
            registry = CollectorRegistry()
            store = JobStore(valkey, self.settings)
            queue = nlp_queue(valkey, self.settings)
            runner = PiiRunner(self.settings, store, FakePiiPool(), registry)
            worker = Worker(self.settings, valkey, runner, store, queue, registry)
            processed = 0
            while (claim := await queue.claim(runner.lease_seconds)).item is not None:
                await worker.process(claim.item, exhausted=claim.exhausted)
                processed += 1
            return processed

        return self.run(process)

    def batch(self, urls: list[str], headers: dict[str, str] = ALICE, **options: Any) -> Any:
        return self.client.post("/v1/batch", json={"urls": urls, **options}, headers=headers)

    def job(self, job_id: str, headers: dict[str, str] = ALICE, **query: Any) -> Any:
        return self.client.get(f"/v1/jobs/{job_id}", params=query, headers=headers)

    def delete(self, job_id: str, headers: dict[str, str] = ALICE) -> Any:
        return self.client.delete(f"/v1/jobs/{job_id}", headers=headers)


@pytest.fixture
def make_api() -> Iterator[Callable[..., Api]]:
    clients: list[TestClient] = []

    def build(**overrides: Any) -> Api:
        settings = make_settings(**overrides)
        server = FakeServer()
        app = create_public_app(settings, FakeAsyncRedis(server=server), CollectorRegistry())
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return Api(client, server, settings)

    yield build
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def api(make_api: Callable[..., Api]) -> Api:
    return make_api()


def test_batch_returns_202_and_job_reports_progress(api: Api) -> None:
    response = api.batch(
        [PAGE, OTHER_PAGE, f"{ORIGIN}/a"], headers={**ALICE, "x-client-trace-id": "t-1"}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "success"
    assert body["trace_id"] == "t-1"
    accepted = body["data"]
    job_id = accepted["id"]
    assert len(job_id) == 32
    assert accepted["kind"] == "batch"
    assert accepted["state"] == "queued"
    assert accepted["total"] == 3
    assert accepted["status_url"] == f"/v1/jobs/{job_id}"

    queued = api.job(job_id).json()
    assert queued["status"] == "success"
    view = queued["data"]
    assert view["state"] == "queued"
    assert view["finished_at"] is None
    assert view["progress"] == {"total": 3, "done": 0, "success": 0, "partial": 0, "failed": 0}
    assert [(item["index"], item["state"]) for item in view["items"]] == [
        (0, "pending"),
        (1, "pending"),
        (2, "pending"),
    ]
    assert [item["url"] for item in view["items"]] == [PAGE, OTHER_PAGE, f"{ORIGIN}/a"]
    assert view["next_offset"] is None

    assert api.work({PAGE: fetched(body=ARTICLE)}, items=1) == 1
    running = api.job(job_id).json()
    assert running["status"] == "success"
    assert running["data"]["state"] == "running"
    assert running["data"]["progress"]["done"] == 1
    assert running["data"]["items"][0]["state"] == "done"
    assert running["data"]["items"][0]["result"]["status"] == "success"

    api.work({PAGE: fetched(body=ARTICLE), OTHER_PAGE: fetched(OTHER_PAGE, body=ARTICLE)})
    done = api.job(job_id).json()
    # Item 2 is a 404 page: the batch completed with a failed item.
    assert done["data"]["state"] == "completed"
    assert done["data"]["finished_at"] is not None
    assert done["data"]["progress"]["done"] == 3
    assert done["status"] == "partial"


def test_duplicate_and_invalid_urls_get_their_own_items(api: Api) -> None:
    response = api.batch([PAGE, PAGE, "not a url"])
    assert response.status_code == 202
    assert response.json()["data"]["total"] == 3
    job_id = response.json()["data"]["id"]

    counts = api.run(lambda valkey: FairQueue(valkey, api.settings).counts())
    assert counts.pending == 3  # one queue item per index, none merged

    assert api.work({PAGE: fetched(body=ARTICLE)}) == 3

    view = api.job(job_id).json()
    assert view["status"] == "partial"
    items = view["data"]["items"]
    assert [item["index"] for item in items] == [0, 1, 2]
    assert [item["url"] for item in items] == [PAGE, PAGE, "not a url"]
    assert [item["result"]["status"] for item in items] == ["success", "success", "failed"]
    assert items[2]["result"]["error"]["code"] == "invalid_url"
    assert view["data"]["progress"] == {
        "total": 3,
        "done": 3,
        "success": 2,
        "partial": 0,
        "failed": 1,
    }


def test_every_item_failed_makes_the_job_failed(api: Api) -> None:
    job_id = api.batch(["not a url", "also not a url"]).json()["data"]["id"]

    api.work()

    view = api.job(job_id).json()
    assert view["status"] == "failed"
    assert view["data"]["state"] == "completed"
    assert view["error"] == {
        "code": "invalid_url",
        "message": "All 2 items failed.",
        "retryable": False,
        "details": None,
    }


def test_failed_job_error_is_the_most_frequent_code_and_any_retryable(api: Api) -> None:
    slow = f"{ORIGIN}/slow"
    answers: dict[str, Answer] = {slow: FetchError(ErrorCode.TIMEOUT, "Timed out.", retryable=True)}
    job_id = api.batch([slow, "not a url", "also not a url"]).json()["data"]["id"]
    api.work(answers)

    view = api.job(job_id).json()
    assert view["status"] == "failed"
    assert view["error"]["code"] == "invalid_url"  # two against one
    assert view["error"]["message"] == "All 3 items failed."
    assert view["error"]["retryable"] is True  # the timeout is retryable
    # The same error whichever page is read.
    assert api.job(job_id, offset=2, limit=1).json()["error"] == view["error"]


def test_failed_job_error_breaks_a_tie_by_the_first_item(api: Api) -> None:
    slow = f"{ORIGIN}/slow"
    answers: dict[str, Answer] = {slow: FetchError(ErrorCode.TIMEOUT, "Timed out.", retryable=True)}
    job_id = api.batch(["not a url", slow]).json()["data"]["id"]
    api.work(answers)

    error = api.job(job_id).json()["error"]
    assert error["code"] == "invalid_url"
    assert error["retryable"] is True


def test_failed_job_has_no_error_while_running_or_partial(api: Api) -> None:
    job_id = api.batch(["not a url", PAGE]).json()["data"]["id"]
    api.work(items=1)
    assert api.job(job_id).json()["error"] is None  # still running

    api.work()
    view = api.job(job_id).json()
    assert view["status"] == "partial"
    assert view["error"] is None


def test_batch_size_limit_is_422(make_api: Callable[..., Api]) -> None:
    api = make_api(batch_max_urls=3)

    assert api.batch([PAGE] * 3).status_code == 202
    response = api.batch([PAGE] * 4)

    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["details"][0]["loc"] == ["body", "urls"]
    assert "at most 3" in body["error"]["details"][0]["msg"]
    assert len(api.keys("job:*:meta")) == 1  # the oversized batch left no job


@pytest.mark.parametrize(
    "body",
    [
        {"urls": []},
        {"urls": [""]},
        {"urls": ["https://example.org/" + "a" * 2048]},
        {"urls": [PAGE], "wait": 3},
        {"urls": [PAGE], "format": "pdf"},
        {},
    ],
)
def test_invalid_batch_body_is_422(api: Api, body: dict[str, Any]) -> None:
    response = api.client.post("/v1/batch", json=body, headers=ALICE)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert api.keys("job:*") == []


def test_requires_key(api: Api) -> None:
    assert api.client.post("/v1/batch", json={"urls": [PAGE]}).status_code == 401
    assert api.client.get("/v1/jobs/" + "a" * 32).status_code == 401


def test_options_are_stored_with_the_job(api: Api) -> None:
    job_id = api.batch([PAGE], format="text", links=True, cache=False).json()["data"]["id"]

    async def read(valkey: FakeAsyncRedis) -> dict[str, str]:
        raw = await JobStore(valkey, api.settings).get(job_id)
        assert raw is not None
        return {"owner": raw.owner, "options": raw.options.model_dump_json()}

    stored = api.run(read)
    assert stored["owner"] == "alice"
    assert '"format":"text"' in stored["options"]
    assert '"links":true' in stored["options"]
    assert '"cache":false' in stored["options"]
    assert '"compliance":"enforce"' in stored["options"]


def test_foreign_job_is_404_admin_sees_it(api: Api) -> None:
    job_id = api.batch([PAGE]).json()["data"]["id"]

    foreign = api.job(job_id, headers=BOB)
    assert foreign.status_code == 404
    body = foreign.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "not_found"
    assert body["error"]["retryable"] is False
    # Same answer as for an id that does not exist: ids leak nothing.
    assert api.job("f" * 32, headers=BOB).json()["error"] == body["error"]

    assert api.job(job_id, headers=ALICE).status_code == 200
    seen = api.job(job_id, headers=ROOT)
    assert seen.status_code == 200
    assert seen.json()["data"]["id"] == job_id


@pytest.mark.parametrize("job_id", ["f" * 32, "not-an-id", "A" * 32, "a" * 31, "a" * 33, "../x"])
def test_unknown_or_malformed_id_is_404(api: Api, job_id: str) -> None:
    response = api.job(job_id)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_expired_job_is_404(api: Api) -> None:
    job_id = api.batch([PAGE]).json()["data"]["id"]
    assert api.job(job_id).status_code == 200

    async def expire(valkey: FakeAsyncRedis) -> None:
        await valkey.pexpire(f"job:{{{job_id}}}:meta", 1)

    api.run(expire)
    time.sleep(0.05)

    response = api.job(job_id)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_paging_and_next_offset(api: Api) -> None:
    urls = [f"{ORIGIN}/page-{n}" for n in range(5)]
    job_id = api.batch(urls).json()["data"]["id"]

    def page(**query: Any) -> dict[str, Any]:
        response = api.job(job_id, **query)
        assert response.status_code == 200
        data: dict[str, Any] = response.json()["data"]
        return data

    first = page(limit=2)
    assert [item["index"] for item in first["items"]] == [0, 1]
    assert first["next_offset"] == 2

    second = page(offset=2, limit=2)
    assert [item["index"] for item in second["items"]] == [2, 3]
    assert second["next_offset"] == 4

    last = page(offset=4, limit=2)
    assert [item["index"] for item in last["items"]] == [4]
    assert last["next_offset"] is None

    exact = page(offset=3, limit=2)
    assert [item["index"] for item in exact["items"]] == [3, 4]
    assert exact["next_offset"] is None

    beyond = page(offset=10**30)
    assert beyond["items"] == []
    assert beyond["next_offset"] is None
    assert beyond["progress"]["total"] == 5

    default = page()
    assert len(default["items"]) == 5
    assert page(limit=500)["next_offset"] is None


@pytest.mark.parametrize("query", ["limit=0", "limit=501", "offset=-1", "limit=x", "offset=x"])
def test_bad_paging_is_422(api: Api, query: str) -> None:
    job_id = api.batch([PAGE]).json()["data"]["id"]

    response = api.client.get(f"/v1/jobs/{job_id}?{query}", headers=ALICE)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_open_batch_limit_is_429(make_api: Callable[..., Api]) -> None:
    api = make_api(batch_max_open_per_client=2)
    first = api.batch([PAGE]).json()["data"]["id"]
    second = api.batch([PAGE]).json()["data"]["id"]

    refused = api.batch([PAGE])

    assert refused.status_code == 429
    body = refused.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "rate_limited"
    assert body["error"]["retryable"] is True
    assert len(api.keys("job:*:meta")) == 2  # the refused batch left no job

    # The limit is per client.
    assert api.batch([PAGE], headers=BOB).status_code == 202
    assert api.batch([PAGE], headers=ROOT).status_code == 202

    # A completed batch no longer counts.
    assert api.work(items=1) == 1
    assert api.job(first).json()["data"]["state"] == "completed"
    assert api.job(second).json()["data"]["state"] == "queued"
    accepted = api.batch([PAGE])
    assert accepted.status_code == 202
    assert api.batch([PAGE]).status_code == 429

    # An expired batch no longer counts either.
    async def expire(valkey: FakeAsyncRedis) -> None:
        await valkey.delete(f"job:{{{second}}}:meta")

    api.run(expire)
    assert api.batch([PAGE]).status_code == 202


def test_concurrent_requests_cannot_exceed_the_open_limit(
    make_api: Callable[..., Api], monkeypatch: pytest.MonkeyPatch
) -> None:
    api = make_api(batch_max_open_per_client=1)
    assert api.batch([PAGE]).status_code == 202

    # A request that passed the count before the other one added its job.
    async def stale_count(*_: object) -> int:
        return 0

    monkeypatch.setattr("text_extraction.jobs_api._open_count", stale_count)
    refused = api.batch([PAGE])

    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "rate_limited"
    assert len(api.keys("job:*:meta")) == 1  # it gave its job back
    counts = api.run(lambda valkey: FairQueue(valkey, api.settings).counts())
    assert counts.pending == 1  # and queued nothing


def test_open_set_key_escapes_the_owner(api: Api) -> None:
    from text_extraction.jobs_api import _open_key

    assert _open_key("alice") == "jobs:{owner:alice}:open"
    assert _open_key("a}b{c%d:e") == "jobs:{owner:a%7Db%7Bc%25d:e}:open"
    api.batch([PAGE])
    assert api.keys("jobs:*") == ["jobs:{owner:alice}:open"]


def test_ignore_compliance_needs_admin_403(api: Api) -> None:
    refused = api.batch([PAGE], compliance="ignore")

    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "forbidden"
    assert api.keys("job:*") == []
    assert api.keys("jobs:*") == []

    allowed = api.batch([PAGE], headers=ROOT, compliance="ignore")
    assert allowed.status_code == 202
    job_id = allowed.json()["data"]["id"]

    async def options(valkey: FakeAsyncRedis) -> str:
        record = await JobStore(valkey, api.settings).get(job_id)
        assert record is not None
        return record.options.compliance

    assert api.run(options) == "ignore"


def test_valkey_down_is_503_for_create_and_get(make_api: Callable[..., Api]) -> None:
    api = make_api(rate_limit_enabled=False)
    job_id = api.batch([PAGE]).json()["data"]["id"]
    api.server.connected = False

    created = api.batch([PAGE], headers={**ALICE, "x-client-trace-id": "t-down"})
    read = api.job(job_id)

    for response in (created, read):
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "failed"
        assert body["error"]["code"] == "internal_error"
        assert body["error"]["retryable"] is True
    assert created.json()["trace_id"] == "t-down"

    api.server.connected = True
    assert api.job(job_id).status_code == 200


class BrokenQueue:
    """A queue whose enqueue fails the way Valkey does."""

    def __init__(self) -> None:
        self.calls = 0

    async def enqueue(self, job_id: str, client: str, urls: list[str]) -> None:
        self.calls += 1
        raise ConnectionError("Valkey went away")


def test_enqueue_failure_deletes_the_job_and_is_503(make_api: Callable[..., Api]) -> None:
    api = make_api(batch_max_open_per_client=1)
    queue = BrokenQueue()
    api.client.app.state.job_queue = queue  # type: ignore[attr-defined]

    response = api.batch([PAGE, OTHER_PAGE])

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "internal_error"
    assert error["retryable"] is True
    assert queue.calls == 1  # enqueued once, never retried
    assert api.keys("job:*") == []

    async def open_jobs(valkey: FakeAsyncRedis) -> int:
        return int(await valkey.scard("jobs:{owner:alice}:open"))

    assert api.run(open_jobs) == 0
    # The failed batch does not hold the client's only slot.
    api.client.app.state.job_queue = FairQueue(  # type: ignore[attr-defined]
        FakeAsyncRedis(server=api.server), api.settings
    )
    assert api.batch([PAGE]).status_code == 202


def test_jobs_disabled_is_503(make_api: Callable[..., Api]) -> None:
    api = make_api(jobs_enabled=False)

    for response in (api.batch([PAGE]), api.job("a" * 32)):
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "failed"
        assert body["error"]["code"] == "internal_error"
        assert body["error"]["message"] == "Jobs are disabled on this deployment."
    assert api.keys("job:*") == []
    # Authentication still comes first.
    assert api.client.post("/v1/batch", json={"urls": [PAGE]}).status_code == 401


def test_job_routes_in_openapi_internal_routes_not(api: Api) -> None:
    paths = api.client.get("/openapi.json").json()["paths"]

    assert "post" in paths["/v1/batch"]
    assert "get" in paths["/v1/jobs/{job_id}"]
    assert not {"/health", "/ready", "/metrics"} & set(paths)


def test_openapi_batch_pii_takes_a_bare_mode_or_the_object(api: Api) -> None:
    spec = api.client.get("/openapi.json").json()
    body = spec["paths"]["/v1/batch"]["post"]["requestBody"]["content"]["application/json"]
    name = body["schema"]["$ref"].rsplit("/", 1)[1]
    pii = spec["components"]["schemas"][name]["properties"]["pii"]

    assert pii["anyOf"] == [
        {"type": "string", "enum": ["mask", "redact", "hash", "annotate"]},
        {"$ref": "#/components/schemas/PiiOptions"},
        {"type": "null"},
    ]


def test_discard_survives_a_second_cancellation(api: Api) -> None:
    """A request cancelled while it enqueues, and cancelled again while its job is discarded,
    still gives the job back."""
    started = asyncio.Event()
    deleted = asyncio.Event()

    class HangingQueue:
        async def enqueue(self, job_id: str, client: str, urls: list[str]) -> None:
            started.set()
            await asyncio.sleep(60)

    async def scenario(valkey: FakeAsyncRedis) -> list[str]:
        store = JobStore(valkey, api.settings)
        real_delete = store.delete

        async def slow_delete(job_id: str) -> None:
            await asyncio.sleep(0.05)
            await real_delete(job_id)
            deleted.set()

        store.delete = slow_delete  # type: ignore[method-assign]
        state = SimpleNamespace(
            settings=api.settings,
            valkey=valkey,
            job_store=store,
            job_queue=HangingQueue(),
            memory_guard=SimpleNamespace(high=False),
        )
        request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=state)))
        principal = Principal(name="alice", role="user", rate_limit_per_minute=None)
        task = asyncio.create_task(create_batch(BatchRequest(urls=[PAGE]), request, principal))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0.01)  # the discard is under way
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(deleted.wait(), 2)
        await asyncio.sleep(0.05)  # the discard goes on to leave the open set
        return sorted(str(key) for key in await valkey.keys("job:*"))

    assert api.run(scenario) == []
    assert api.keys("jobs:*") == []  # the id left the open set, which emptied


def test_oversized_body_is_refused_before_it_is_validated(make_api: Callable[..., Api]) -> None:
    api = make_api(batch_max_urls=3)
    bound = 3 * (MAX_URL_LENGTH + 8)
    # Besides its size the body has an unknown field and an empty URL: had the model seen it,
    # the answer would list those, not only the size.
    body = json.dumps({"urls": ["", "x" * 20], "pad": "x" * (bound + 10_000)})
    assert len(body) > bound + 4096

    response = api.client.post(
        "/v1/batch", content=body, headers={**ALICE, "content-type": "application/json"}
    )

    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["message"].startswith("The request body is too large")
    assert "details" not in error or error["details"] is None
    assert api.keys("job:*") == []


def test_body_at_the_bound_is_not_refused_for_its_size(make_api: Callable[..., Api]) -> None:
    api = make_api(batch_max_urls=3)
    url = "https://example.org/" + "a" * (MAX_URL_LENGTH - len("https://example.org/"))
    assert len(url) == MAX_URL_LENGTH

    assert api.batch([url] * 3).status_code == 202


def test_missing_content_length_is_bounded_by_the_model(api: Api) -> None:
    def chunks() -> Iterator[bytes]:
        yield b'{"urls": ['
        yield b",".join([b'"a"'] * (MAX_BATCH_URLS + 1))
        yield b"]}"

    response = api.client.post(
        "/v1/batch", content=chunks(), headers={**ALICE, "content-type": "application/json"}
    )

    assert response.status_code == 422
    assert "content-length" not in response.request.headers
    message = response.json()["error"]["details"][0]["msg"]
    assert str(MAX_BATCH_URLS) in message  # the model's limit, not BATCH_MAX_URLS (1000)
    assert api.keys("job:*") == []


def test_model_limit_matches_the_setting_maximum() -> None:
    assert Settings.model_fields["batch_max_urls"].metadata[-1].le == MAX_BATCH_URLS


# --- extract jobs whose pod stopped (P1) -------------------------------------------------------

LOST_MESSAGE = "The pod running this extraction stopped."
ALICE_PRINCIPAL = Principal(name="alice", role="user", rate_limit_per_minute=None)


def start_extract_job(api: Api, *, lease_seconds: float, age_seconds: float = 0) -> str:
    """An `extract` job as the sync-to-job hand-over leaves it: running with a lease that began
    ``age_seconds`` ago."""

    async def create(valkey: FakeAsyncRedis) -> str:
        store = JobStore(valkey, api.settings, clock=lambda: time.time() - age_seconds)
        options = JobOptions(compliance="enforce")
        job_id = await store.create("extract", ALICE_PRINCIPAL, options, [PAGE])
        await store.mark_running(job_id, lease_seconds=lease_seconds)
        return job_id

    return api.run(create)


def stored(api: Api, job_id: str, kind: str, field: str) -> bytes | str | None:
    async def read(valkey: FakeAsyncRedis) -> bytes | str | None:
        return await valkey.hget(f"job:{{{job_id}}}:{kind}", field)

    return api.run(read)


def test_lost_running_job_reports_internal_error(api: Api) -> None:
    job_id = start_extract_job(api, lease_seconds=35, age_seconds=100)

    response = api.job(job_id)

    assert response.status_code == 200
    body = response.json()
    view = body["data"]
    assert view["kind"] == "extract"
    assert view["state"] == "completed"
    assert view["finished_at"] is not None
    assert view["progress"] == {"total": 1, "done": 1, "success": 0, "partial": 0, "failed": 1}
    assert view["next_offset"] is None
    item = view["items"][0]
    assert (item["index"], item["url"], item["state"]) == (0, PAGE, "done")
    assert item["result"]["status"] == "failed"
    assert item["result"]["data"] is None
    assert item["result"]["error"] == {
        "code": "internal_error",
        "message": LOST_MESSAGE,
        "retryable": True,
        "details": None,
    }
    assert body["status"] == "failed"
    assert body["error"] == {
        "code": "internal_error",
        "message": "All 1 items failed.",
        "retryable": True,
        "details": None,
    }
    # The synthetic result is a view: nothing was stored (P1).
    assert stored(api, job_id, "results", "0") is None
    assert stored(api, job_id, "meta", "state") == b"running"


def test_running_job_with_a_live_lease_is_shown_as_it_is(api: Api) -> None:
    job_id = start_extract_job(api, lease_seconds=35)

    view = api.job(job_id).json()["data"]

    assert view["state"] == "running"
    assert view["progress"]["done"] == 0
    assert view["items"][0]["state"] == "pending"
    assert view["items"][0]["result"] is None


def test_completed_job_with_a_past_lease_shows_its_stored_result(api: Api) -> None:
    job_id = start_extract_job(api, lease_seconds=35, age_seconds=100)

    async def finish(valkey: FakeAsyncRedis) -> None:
        registry = CollectorRegistry()
        fetcher = cast(Fetcher, FakeFetcher({PAGE: fetched(body=ARTICLE)}))
        pipeline = create_pipeline(api.settings, valkey, fetcher, registry)
        envelope = await pipeline.run(ExtractRequest(url=PAGE), ALICE_PRINCIPAL)
        write = await JobStore(valkey, api.settings).write_result(job_id, 0, envelope)
        assert write == "last"

    api.run(finish)

    body = api.job(job_id).json()

    assert body["status"] == "success"
    assert body["data"]["state"] == "completed"
    assert body["data"]["progress"]["success"] == 1
    result = body["data"]["items"][0]["result"]
    assert result["status"] == "success"
    assert result["data"]["title"]


def test_lost_job_is_visible_to_its_owner_and_admins_only(api: Api) -> None:
    job_id = start_extract_job(api, lease_seconds=35, age_seconds=100)

    assert api.job(job_id, BOB).status_code == 404
    assert api.job(job_id, ROOT).json()["data"]["items"][0]["result"]["error"]["message"] == (
        LOST_MESSAGE
    )


def test_running_batch_without_a_lease_is_never_lost(api: Api) -> None:
    job_id = api.batch([PAGE, OTHER_PAGE]).json()["data"]["id"]
    api.work({PAGE: fetched(body=ARTICLE)}, items=1)

    view = api.job(job_id).json()["data"]

    assert view["state"] == "running"
    assert [item["state"] for item in view["items"]].count("pending") == 1


def test_batch_stores_the_trace_id_of_its_request(api: Api) -> None:
    response = api.client.post(
        "/v1/batch", json={"urls": [PAGE]}, headers={**ALICE, "x-client-trace-id": "trace-b"}
    )
    job_id = response.json()["data"]["id"]

    record = api.run(lambda valkey: JobStore(valkey, api.settings).get(job_id))

    assert record is not None
    assert record.trace_id == "trace-b"


def stored_job(api: Api, job_id: str) -> JobRecord | None:
    return api.run(lambda valkey: JobStore(valkey, api.settings).get(job_id))


def test_batch_stores_the_ocr_option_in_the_job_options(api: Api) -> None:
    chosen = api.batch([PAGE], ocr="never").json()["data"]["id"]
    default = api.batch([PAGE]).json()["data"]["id"]

    records = [stored_job(api, chosen), stored_job(api, default)]

    assert [record.options.ocr for record in records if record is not None] == ["never", "auto"]


def test_job_whose_lease_was_cleared_is_never_lost(api: Api) -> None:
    # An extract job whose item moved to the browser pool (Task 8): its P1 lease is cleared.
    job_id = start_extract_job(api, lease_seconds=35, age_seconds=100)
    api.run(lambda valkey: JobStore(valkey, api.settings).clear_lease(job_id))

    view = api.job(job_id).json()["data"]

    assert view["state"] == "running"
    assert view["items"][0]["state"] == "pending"


def queue_counts(api: Api, pool: str) -> QueueCounts:
    return api.run(lambda valkey: FairQueue(valkey, api.settings, pool).counts())


def test_batch_browser_mode_goes_to_browser_pool(make_api: Callable[..., Api]) -> None:
    api = make_api(browser_enabled=True)

    response = api.batch([PAGE, OTHER_PAGE], mode="browser")

    assert response.status_code == 202
    assert queue_counts(api, "browser").pending == 2
    assert queue_counts(api, "extract").pending == 0
    record = api.run(
        lambda valkey: JobStore(valkey, api.settings).get(response.json()["data"]["id"])
    )
    assert record is not None
    assert record.options.mode == "browser"
    # auto stays in the extract pool: the extract worker moves what needs the browser (B5).
    assert api.batch([PAGE]).status_code == 202
    assert queue_counts(api, "extract").pending == 1


def test_batch_ocr_always_while_docling_off_is_400(make_api: Callable[..., Api]) -> None:
    api = make_api()
    response = api.batch([PAGE], ocr="always")

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["message"] == "OCR is not enabled on this service."
    assert api.keys("job*") == []

    enabled = make_api(docling_enabled=True)
    assert enabled.batch([PAGE], ocr="always").status_code == 202


def test_batch_browser_mode_disabled_is_400(api: Api) -> None:
    response = api.batch([PAGE], mode="browser")

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["message"] == "Browser mode is not enabled on this service."
    assert api.keys("job*") == []


@pytest.mark.parametrize(
    "extra",
    [
        {"actions": [{"type": "scroll"}]},
        {"cookies": [{"name": "a", "value": "b"}]},
        {"headers": {"x-token": "t"}},
        {"screenshot": True},
    ],
)
def test_batch_rejects_actions_cookies_headers(make_api: Callable[..., Api], extra: Any) -> None:
    api = make_api(browser_enabled=True)

    body = {"urls": [PAGE], "mode": "browser", **extra}
    response = api.client.post("/v1/batch", json=body, headers=ALICE)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert api.keys("job*") == []


# --- PII (N4, N11) ---

PII_NAME = "Anna Schmidt"
PII_ARTICLE = ARTICLE.replace(
    "</article>", f"<p>Written by {PII_NAME}, anna.schmidt@example.org.</p></article>"
)


def test_pii_disabled_is_400(api: Api) -> None:
    response = api.batch([PAGE], pii="mask")

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["message"] == "PII handling is not enabled on this service."
    assert api.keys("job*") == []


def test_batch_annotate_with_html_is_422(make_api: Callable[..., Api]) -> None:
    api = make_api(pii_enabled=True)

    response = api.batch([PAGE], format="html", pii="annotate")

    assert response.status_code == 422
    assert response.json()["error"]["message"] == "annotate needs format markdown or text."
    assert api.keys("job*") == []


def test_batch_items_processed_by_nlp(make_api: Callable[..., Api]) -> None:
    api = make_api(pii_enabled=True)
    response = api.batch([PAGE, OTHER_PAGE], pii={"mode": "mask", "entities": ["PERSON"]})
    assert response.status_code == 202
    job_id = response.json()["data"]["id"]

    assert api.work({PAGE: fetched(body=PII_ARTICLE), OTHER_PAGE: fetched(OTHER_PAGE)}) == 2
    # Both items moved on to nlp: the job is not done before the nlp worker wrote them.
    assert api.job(job_id).json()["data"]["state"] == "running"
    assert api.work_nlp() == 2

    view = api.job(job_id).json()
    assert view["status"] == "success"
    assert view["data"]["state"] == "completed"
    named, other = (item["result"]["data"] for item in view["data"]["items"])
    assert PII_NAME not in named["content"]
    assert "[PERSON]" in named["content"]
    assert "anna.schmidt@example.org" in named["content"]  # only PERSON was asked for
    assert named["pii"]["entities"] == {"PERSON": 1}
    assert other["pii"] == {
        "mode": "mask",
        "language": other["pii"]["language"],
        "entities": {},
        "items": None,
        "hash_scope": None,
    }

    async def options(valkey: FakeAsyncRedis) -> JobOptions:
        record = await JobStore(valkey, api.settings).get(job_id)
        assert record is not None
        return record.options

    assert api.run(options).pii == PiiOptions(mode="mask", entities=["PERSON"])


def test_batch_xpath_selectors_need_an_admin_key(api: Api) -> None:
    """Ruling R4, on the batch route: no job for a user's XPath; an admin's is taken."""
    structured = {
        "schema": {"type": "object", "properties": {"title": {"type": "string"}}},
        "strategy": "selectors",
        "selectors": {"title": {"xpath": "//h1"}},
    }

    refused = api.batch([PAGE], structured=structured)
    assert refused.status_code == 422
    assert refused.json()["error"]["message"] == "XPath selectors need an admin key; use CSS."
    assert api.keys("job:*:meta") == []

    assert api.batch([PAGE], headers=ROOT, structured=structured).status_code == 202


# --- structured and chunking on the batch route (rulings S1, S9, S12) --------------------------

LLM_KEY = "sk-llm-caller-key-9c2b"
LLM_HEADERS = {**ALICE, "X-LLM-Api-Key": LLM_KEY}
LLM_SCHEMA: dict[str, Any] = {"type": "object", "properties": {"title": {"type": "string"}}}
LLM_STRUCTURED: dict[str, Any] = {"schema": LLM_SCHEMA, "strategy": "llm"}


def llm_overrides(**more: Any) -> dict[str, Any]:
    return {"llm_enabled": True, "llm_embedding_model": "embed-1", **more}


def test_batch_stores_structured_and_chunking_in_the_job_options(
    make_api: Callable[..., Api],
) -> None:
    """The options of a batch reach its job (review item M2.6)."""
    api = make_api(**llm_overrides(llm_api_key="sk-operator-key"))
    structured = {
        "schema": LLM_SCHEMA,
        "strategy": "patterns",
        "patterns": {"title": {"regex": r"\w+"}},
        "tables": True,
    }
    chunking = {"strategy": "fixed", "size": 400, "overlap": 40}

    response = api.batch([PAGE], structured=structured, chunking=chunking)
    plain = api.batch([PAGE])

    assert response.status_code == 202, response.text
    assert plain.status_code == 202

    async def options(valkey: FakeAsyncRedis, job_id: str) -> JobOptions:
        record = await JobStore(valkey, api.settings).get(job_id)
        assert record is not None
        return record.options

    stored = api.run(lambda valkey: options(valkey, response.json()["data"]["id"]))
    assert stored.structured == StructuredOptions.model_validate(structured)
    assert stored.chunking == ChunkingOptions(strategy="fixed", size=400, overlap=40)
    unset = api.run(lambda valkey: options(valkey, plain.json()["data"]["id"]))
    assert (unset.structured, unset.chunking) == (None, None)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"structured": LLM_STRUCTURED}, "LLM features are not enabled on this service."),
        (
            {"structured": {"schema": LLM_SCHEMA, "learn_selectors": True}},
            "LLM features are not enabled on this service.",
        ),
        (
            {"chunking": {"strategy": "semantic"}},
            "LLM features are not enabled on this service.",
        ),
    ],
    ids=["llm", "learn", "semantic"],
)
def test_batch_400_for_llm_features_without_llm(
    api: Api, body: dict[str, Any], message: str
) -> None:
    response = api.batch([PAGE], headers=LLM_HEADERS, **body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.json()["error"]["message"] == message
    assert api.keys("job:*") == []


def test_batch_400_without_any_llm_key_and_422_for_the_combinations(
    make_api: Callable[..., Api],
) -> None:
    api = make_api(**llm_overrides(pii_enabled=True))

    missing = api.batch([PAGE], structured=LLM_STRUCTURED)
    html = api.batch([PAGE], format="html", chunking={"strategy": "fixed"})
    pii = api.batch([PAGE], pii="mask", structured={**LLM_STRUCTURED, "strategy": "metadata"})
    learn = api.batch(
        [PAGE],
        headers=LLM_HEADERS,
        structured={"schema": LLM_SCHEMA, "strategy": "patterns", "learn_selectors": True},
    )

    assert missing.status_code == 400
    assert missing.json()["error"]["message"] == "No LLM key: send X-LLM-Api-Key."
    assert [r.status_code for r in (html, pii, learn)] == [422, 422, 422]
    assert html.json()["error"]["message"] == "Chunking needs format markdown or text."
    assert pii.json()["error"]["message"] == (
        "Strategy metadata reads the raw page and cannot be combined with pii."
    )
    assert learn.json()["error"]["message"] == "learn_selectors needs strategy llm or auto."
    assert api.keys("job:*") == []


def test_llm_key_stored_with_batch_for_the_job_ttl(make_api: Callable[..., Api]) -> None:
    api = make_api(**llm_overrides(job_ttl_seconds=7200))

    with capture_logs() as logs:
        accepted = api.batch([PAGE, OTHER_PAGE], headers=LLM_HEADERS, structured=LLM_STRUCTURED)
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["data"]["id"]
    view = api.job(job_id)

    async def stored(valkey: FakeAsyncRedis) -> tuple[str | None, int]:
        key = await JobStore(valkey, api.settings).llm_key(job_id)
        return key, await valkey.ttl(f"job:{{{job_id}}}:llm_key")

    key, ttl = api.run(stored)
    assert key == LLM_KEY
    assert 7200 - 5 < ttl <= 7200  # as long as the job: its items move to pool llm at any time
    assert LLM_KEY not in view.text
    assert LLM_KEY not in accepted.text
    assert not any(LLM_KEY in repr(event) for event in logs)


def test_batch_stores_no_llm_key_without_llm_work(make_api: Callable[..., Api]) -> None:
    api = make_api(**llm_overrides())

    plain = api.batch([PAGE], headers=LLM_HEADERS)
    patterns = api.batch(
        [PAGE],
        headers=LLM_HEADERS,
        structured={
            "schema": LLM_SCHEMA,
            "strategy": "patterns",
            "patterns": {"title": {"regex": r"\w+"}},
        },
    )

    assert plain.status_code == patterns.status_code == 202
    assert api.keys("job:*:llm_key") == []


def test_batch_refuses_an_invalid_llm_key_header(make_api: Callable[..., Api]) -> None:
    api = make_api(**llm_overrides(llm_api_key="sk-operator-key"))

    response = api.batch(
        [PAGE], headers={**ALICE, "X-LLM-Api-Key": "has space"}, structured=LLM_STRUCTURED
    )

    assert response.status_code == 400
    assert "has space" not in response.text
    assert api.keys("job:*") == []


# --- describe_images on the batch route (issue #20, ruling V-S1) -------------------------------


def test_batch_stores_describe_images_in_the_job_options(make_api: Callable[..., Api]) -> None:
    api = make_api(**llm_overrides(llm_api_key="sk-operator-key"))

    response = api.batch([PAGE], describe_images={"max_images": 4})
    plain = api.batch([PAGE])

    assert response.status_code == 202, response.text
    assert plain.status_code == 202

    async def options(valkey: FakeAsyncRedis, job_id: str) -> JobOptions:
        record = await JobStore(valkey, api.settings).get(job_id)
        assert record is not None
        return record.options

    stored = api.run(lambda valkey: options(valkey, response.json()["data"]["id"]))
    assert stored.describe_images == DescribeImagesOptions(max_images=4)
    unset = api.run(lambda valkey: options(valkey, plain.json()["data"]["id"]))
    assert unset.describe_images is None


# --- DELETE /v1/jobs/{id} (P10-S10) ----------------------------------------------------------


def open_jobs(api: Api, owner: str) -> set[Any]:
    async def members(valkey: FakeAsyncRedis) -> set[Any]:
        return set(await valkey.smembers(f"jobs:{{owner:{owner}}}:open"))

    return api.run(members)


def test_delete_own_job_then_404(api: Api) -> None:
    job_id = api.batch([PAGE, OTHER_PAGE], headers={**ALICE, "x-client-trace-id": "t-d"}).json()[
        "data"
    ]["id"]
    assert api.work({PAGE: fetched(body=ARTICLE)}, items=1) == 1  # one result is stored
    assert api.keys(f"job:{{{job_id}}}:*")

    with capture_logs() as logs:
        response = api.delete(job_id, headers={**ALICE, "x-client-trace-id": "t-d"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["error"] is None
    assert body["trace_id"] == "t-d"
    assert body["data"] == {"id": job_id, "kind": "batch"}
    # Every key of the job is gone, and with it the owner's open slot.
    assert api.keys(f"job:{{{job_id}}}*") == []
    assert open_jobs(api, "alice") == set()
    deleted = [log for log in logs if log["event"] == "job_deleted"]
    assert [(log["job_id"], log["kind"]) for log in deleted] == [(job_id, "batch")]

    for answer in (api.job(job_id), api.delete(job_id)):
        assert answer.status_code == 404
        assert answer.json()["error"]["code"] == "not_found"
        assert answer.json()["error"]["message"] == NOT_FOUND_MESSAGE


@pytest.mark.parametrize("job_id", ["f" * 32, "not-an-id", "A" * 32])
def test_delete_unknown_or_malformed_id_is_404(api: Api, job_id: str) -> None:
    response = api.delete(job_id)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_delete_needs_a_key(api: Api) -> None:
    job_id = api.batch([PAGE]).json()["data"]["id"]

    assert api.client.delete(f"/v1/jobs/{job_id}").status_code == 401
    assert api.job(job_id).status_code == 200


def test_delete_foreign_job_404_admin_ok(api: Api) -> None:
    job_id = api.batch([PAGE]).json()["data"]["id"]

    foreign = api.delete(job_id, headers=BOB)
    assert foreign.status_code == 404
    # The same answer as for a job that does not exist: ids leak nothing.
    assert foreign.json()["error"] == api.delete("f" * 32, headers=BOB).json()["error"]
    assert api.job(job_id).status_code == 200  # still there

    by_admin = api.delete(job_id, headers=ROOT)
    assert by_admin.status_code == 200
    assert by_admin.json()["data"] == {"id": job_id, "kind": "batch"}
    assert api.job(job_id).status_code == 404
    assert api.keys(f"job:{{{job_id}}}*") == []


def test_delete_frees_open_slot(make_api: Callable[..., Api]) -> None:
    api = make_api(batch_max_open_per_client=1)
    first = api.batch([PAGE]).json()["data"]["id"]
    assert api.batch([PAGE]).status_code == 429

    assert api.delete(first).status_code == 200
    second = api.batch([PAGE])
    assert second.status_code == 202
    assert api.batch([PAGE]).status_code == 429

    # An admin's delete frees the owner's slot, not the admin's.
    assert api.delete(second.json()["data"]["id"], headers=ROOT).status_code == 200
    assert open_jobs(api, "alice") == set()
    assert api.batch([PAGE]).status_code == 202


def test_delete_counts_against_the_rate_limit(make_api: Callable[..., Api]) -> None:
    api = make_api(rate_limit_per_minute=2)
    job_id = api.batch([PAGE]).json()["data"]["id"]  # hit 1

    assert api.delete(job_id).status_code == 200  # hit 2
    refused = api.delete(job_id)
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "rate_limited"


def test_delete_jobs_disabled_503(make_api: Callable[..., Api]) -> None:
    api = make_api(jobs_enabled=False)

    response = api.delete("a" * 32)

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["message"] == "Jobs are disabled on this deployment."
    # Authentication still comes first.
    assert api.client.delete(f"/v1/jobs/{'a' * 32}").status_code == 401


def test_delete_valkey_down_is_503(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    from redis.exceptions import ConnectionError as RedisConnectionError

    job_id = api.batch([PAGE]).json()["data"]["id"]
    store = api.client.app.state.job_store  # type: ignore[attr-defined]

    async def down(*_: object) -> None:
        raise RedisConnectionError("valkey is down")

    monkeypatch.setattr(store, "delete", down)
    response = api.delete(job_id)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "internal_error"
    assert response.json()["error"]["retryable"] is True
    monkeypatch.undo()
    assert api.job(job_id).status_code == 200  # nothing was answered as deleted


def test_openapi_lists_delete_jobs(api: Api) -> None:
    spec = api.client.get("/openapi.json").json()

    operation = spec["paths"]["/v1/jobs/{job_id}"]["delete"]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    envelope = spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
    data = envelope["properties"]["data"]["anyOf"][0]["$ref"].rsplit("/", 1)[-1]
    assert set(spec["components"]["schemas"][data]["properties"]) == {"id", "kind"}
