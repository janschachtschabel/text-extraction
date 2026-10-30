"""``POST /v1/site-structure`` (``site_api.py``, ruling P9-S13): the real job store, crawl store
and queue on fakeredis behind the public app, and, where items have to finish, a real worker
pass with the site runner on a fake fetcher."""

import asyncio
from collections.abc import Callable
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis
from prometheus_client import CollectorRegistry

from tests.fakes import PARAGRAPH, FakeFetcher, fetched
from tests.test_jobs_api import ALICE, BOB, ROOT, Api
from tests.test_jobs_api import make_api as make_api  # the fixture
from tests.test_memory_guard import StubGuard
from text_extraction.conversion_pool import ConversionPool
from text_extraction.fetch import Fetcher
from text_extraction.jobs import JobStore, job_key
from text_extraction.pipeline import create_pipeline
from text_extraction.scheduler import FairQueue
from text_extraction.site.runner import SiteRunner
from text_extraction.site.store import SiteStore
from text_extraction.site.types import SiteNode, SiteOptions
from text_extraction.worker import ExtractRunner, Worker

PATH = "/v1/site-structure"
SITE = "https://lernen.example.org"
START = f"{SITE}/start/"
START_PAGE = f"""<!doctype html><html><head><title>Start</title></head>
<body><main><p>{PARAGRAPH * 3}</p><a href="/a.html">A</a></main></body></html>"""


def site(api: Api, url: str = START, headers: dict[str, str] = ALICE, **options: Any) -> Any:
    return api.client.post(PATH, json={"url": url, **options}, headers=headers)


def work_site(api: Api, answers: dict[str, Any]) -> int:
    """Claim and process the queued items as an extract worker with the site runner would;
    returns how many were processed."""

    async def process(valkey: FakeAsyncRedis) -> int:
        registry = CollectorRegistry()
        settings = api.settings
        fetcher = cast(Fetcher, FakeFetcher(answers))
        pool = ConversionPool(1)
        try:
            pipeline = create_pipeline(settings, valkey, fetcher, registry, pool)
            store = JobStore(valkey, settings)
            queue = FairQueue(valkey, settings)
            runner = SiteRunner(
                pipeline, store, SiteStore(valkey, store, settings), queue, settings, pool
            )
            worker = Worker(
                settings,
                valkey,
                ExtractRunner(pipeline, store, settings, None, site=runner),
                store,
                queue,
                registry,
            )
            processed = 0
            while (claim := await queue.claim(60)).item is not None:
                await worker.process(claim.item, exhausted=claim.exhausted)
                processed += 1
            return processed
        finally:
            pool.shutdown()

    return api.run(process)


@pytest.fixture
def api(make_api: Callable[..., Api]) -> Api:
    return make_api()


def test_accepts_and_queues_item_zero(api: Api) -> None:
    response = site(api, depth=1, max_pages=10, scope="path", metadata=False, cache=False)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "success"
    data = body["data"]
    job_id = data["id"]
    assert data == {
        "id": job_id,
        "kind": "site",
        "state": "queued",
        "total": 1,
        "status_url": f"/v1/jobs/{job_id}",
    }

    async def stored(valkey: FakeAsyncRedis) -> Any:
        store = JobStore(valkey, api.settings)
        record = await store.get(job_id)
        node = await valkey.hget(job_key(job_id, "nodes"), "0")
        claim = await FairQueue(valkey, api.settings).claim(60)
        return record, node, claim, await FairQueue(valkey, api.settings).counts()

    record, node, claim, counts = api.run(stored)
    assert record.kind == "site"
    assert record.owner == "alice"
    assert record.progress.total == 1
    assert record.options.mode == "fast"
    assert record.options.compliance == api.settings.compliance_default
    assert record.options.site == SiteOptions(
        depth=1, max_pages=10, scope="path", metadata=False, cache=False
    )
    assert SiteNode.model_validate_json(node) == SiteNode(role="page", depth=0, parent=None)
    # One queue entry, for the start URL's host, item 0 of the job, for the caller.
    item = claim.item
    assert (item.job_id, item.index, item.client, item.domain) == (
        job_id,
        0,
        "alice",
        "lernen.example.org",
    )
    assert counts.pending == 0  # it was the only one
    registry: CollectorRegistry = api.client.app.state.registry  # type: ignore[attr-defined]
    assert registry.get_sample_value("text_extraction_jobs_created_total", {"kind": "site"}) == 1


def test_site_kind_is_exported_before_the_first_job(api: Api) -> None:
    registry: CollectorRegistry = api.client.app.state.registry  # type: ignore[attr-defined]
    assert registry.get_sample_value("text_extraction_jobs_created_total", {"kind": "site"}) == 0


@pytest.mark.parametrize(
    "url",
    ["ftp://lernen.example.org/", "https://user@lernen.example.org/", "not a url", "https:///x"],
)
def test_invalid_url_422(api: Api, url: str) -> None:
    response = site(api, url)

    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "invalid_request"
    assert [detail["loc"] for detail in body["error"]["details"]] == [["body", "url"]]
    assert url not in response.text  # the reason, not the URL
    assert api.keys("job:*") == []
    assert api.keys("jobs:*") == []


@pytest.mark.parametrize(
    ("options", "field"),
    [
        ({"depth": 3}, "depth"),
        ({"max_pages": 51}, "max_pages"),
        ({"depth": 11}, "depth"),  # over the model's own bound
        ({"max_pages": 0}, "max_pages"),
        ({"unknown": 1}, "unknown"),
    ],
)
def test_depth_and_pages_over_settings_422(
    make_api: Callable[..., Api], options: dict[str, Any], field: str
) -> None:
    api = make_api(site_max_depth=2, site_max_pages=50)

    response = site(api, **options)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert [detail["loc"] for detail in error["details"]] == [["body", field]]
    assert api.keys("job:*") == []
    # At the settings' bounds the request is accepted.
    assert site(api, depth=2, max_pages=50).status_code == 202


def test_ignore_for_user_403_and_admin_ok(api: Api) -> None:
    refused = site(api, compliance="ignore")

    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "forbidden"
    assert api.keys("job:*") == []
    assert api.keys("jobs:*") == []

    allowed = site(api, headers=ROOT, compliance="ignore")
    assert allowed.status_code == 202
    job_id = allowed.json()["data"]["id"]

    async def compliance(valkey: FakeAsyncRedis) -> str:
        record = await JobStore(valkey, api.settings).get(job_id)
        assert record is not None
        return record.options.compliance

    assert api.run(compliance) == "ignore"


def test_jobs_disabled_503(make_api: Callable[..., Api]) -> None:
    api = make_api(jobs_enabled=False)

    response = site(api)

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["message"] == "Jobs are disabled on this deployment."
    assert api.keys("job:*") == []
    # Authentication still comes first.
    assert api.client.post(PATH, json={"url": START}).status_code == 401


def test_memory_guard_503(api: Api) -> None:
    api.client.app.state.memory_guard = StubGuard(high=True)  # type: ignore[attr-defined]

    response = site(api)

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "internal_error"
    assert error["retryable"] is True
    assert "memory" in error["message"]
    assert api.keys("job:*") == []

    api.client.app.state.memory_guard.high = False  # type: ignore[attr-defined]
    assert site(api).status_code == 202


def test_open_limit_shared_with_batches_429(make_api: Callable[..., Api]) -> None:
    api = make_api(batch_max_open_per_client=5)
    for _ in range(4):
        assert api.batch([START]).status_code == 202
    assert site(api).status_code == 202

    for refused in (site(api), api.batch([START])):
        assert refused.status_code == 429
        error = refused.json()["error"]
        assert error["code"] == "rate_limited"
        assert error["retryable"] is True
    assert len(api.keys("job:*:meta")) == 5  # the refused ones left no job
    assert site(api, headers=BOB).status_code == 202  # the limit is per client


def test_enqueue_failure_discards_the_job_and_its_crawl_keys(
    make_api: Callable[..., Api],
) -> None:
    api = make_api(batch_max_open_per_client=1)

    class BrokenQueue:
        calls = 0

        async def enqueue_items(self, *_args: Any) -> None:
            BrokenQueue.calls += 1
            raise ConnectionError("Valkey went away")

    api.client.app.state.job_queue = BrokenQueue()  # type: ignore[attr-defined]

    response = site(api)

    assert response.status_code == 503
    error = response.json()["error"]
    assert (error["code"], error["retryable"]) == ("internal_error", True)
    assert BrokenQueue.calls == 1
    assert api.keys("job:*") == []  # the job and node 0, seen and counters are gone
    assert api.keys("jobs:*") == []  # and the job left the open set
    registry: CollectorRegistry = api.client.app.state.registry  # type: ignore[attr-defined]
    assert registry.get_sample_value("text_extraction_jobs_created_total", {"kind": "site"}) == 0


def test_valkey_down_503(make_api: Callable[..., Api]) -> None:
    api = make_api(rate_limit_enabled=False)
    api.server.connected = False

    response = site(api)

    assert response.status_code == 503
    assert response.json()["error"]["retryable"] is True


def test_get_job_pages_site_items(api: Api) -> None:
    job_id = site(api, depth=0).json()["data"]["id"]
    answers = {START: fetched(START, START_PAGE)}

    processed = work_site(api, answers)

    # The start page, robots.txt (a 404 here) and the guessed /sitemap.xml (a 404: absent).
    assert processed == 3
    view = api.job(job_id).json()
    data = view["data"]
    assert data["kind"] == "site"
    assert data["state"] == "completed"
    assert data["progress"]["total"] == 3
    roles = [(item["index"], item["result"]["data"]["site"]["role"]) for item in data["items"]]
    assert roles == [(0, "page"), (1, "robots"), (2, "sitemap")]
    start = data["items"][0]["result"]["data"]
    assert start["content"] is None
    assert [link["crawl"] for link in start["site"]["links"]] == ["depth"]
    # Paged as any job.
    second = api.job(job_id, offset=1, limit=1).json()["data"]
    assert [item["index"] for item in second["items"]] == [1]
    assert second["next_offset"] == 2


def test_openapi_lists_route_with_example(api: Api) -> None:
    spec = api.client.get("/openapi.json").json()
    operation = spec["paths"][PATH]["post"]

    assert operation["summary"]
    assert "202" in operation["responses"]
    assert "site" in operation["description"]
    content = operation["requestBody"]["content"]["application/json"]
    examples = content["examples"]
    assert examples
    for example in examples.values():
        assert example["value"]["url"].startswith("https://")
    name = content["schema"]["$ref"].rsplit("/", 1)[1]
    properties = spec["components"]["schemas"][name]["properties"]
    assert {"url", "depth", "max_pages", "sitemaps", "scope", "compliance"} <= set(properties)
    # The job route says that a site job grows.
    assert "site" in spec["paths"]["/v1/jobs/{job_id}"]["get"]["description"]


def test_body_limit_is_the_default_request_limit(make_api: Callable[..., Api]) -> None:
    api = make_api(request_max_body_bytes=2048)

    response = api.client.post(
        PATH,
        content=b'{"url": "' + b"a" * 4096 + b'"}',
        headers={**ALICE, "content-type": "application/json"},
    )

    assert response.status_code == 413
    assert api.keys("job:*") == []


def test_concurrent_cancel_gives_the_site_job_back(api: Api) -> None:
    """Cancelled while it enqueues: the job, its crawl keys and the open-set entry go."""
    from fastapi import Request

    from text_extraction.auth import Principal
    from text_extraction.site.types import SiteStructureRequest
    from text_extraction.site_api import create_site_structure

    started = asyncio.Event()

    class HangingQueue:
        async def enqueue_items(self, *_args: Any) -> None:
            started.set()
            await asyncio.sleep(60)

    async def scenario(valkey: FakeAsyncRedis) -> list[str]:
        from types import SimpleNamespace

        store = JobStore(valkey, api.settings)
        state = SimpleNamespace(
            settings=api.settings,
            valkey=valkey,
            job_store=store,
            site_store=SiteStore(valkey, store, api.settings),
            job_queue=HangingQueue(),
            memory_guard=SimpleNamespace(high=False),
            job_metrics=SimpleNamespace(),
        )
        request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=state)))
        principal = Principal(name="alice", role="user", rate_limit_per_minute=None)
        task = asyncio.create_task(
            create_site_structure(SiteStructureRequest(url=START), request, principal)
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)
        return sorted(str(key) for key in await valkey.keys("job*"))

    assert api.run(scenario) == []
