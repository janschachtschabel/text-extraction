"""The route cores (ruling P10-S1): each public route's body as a function of the app state, so
that MCP tools and the UI's reading views run exactly the route's checks and answers."""

import asyncio
import contextlib
import hashlib
import json
from collections.abc import Awaitable, Callable, Iterator
from functools import partial
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from pydantic import BaseModel

import text_extraction.pipeline as pipeline_module
from tests.fakes import ARTICLE, PAGE, FakeFetcher, fetched
from text_extraction.app import create_public_app
from text_extraction.auth import Principal, principal_for
from text_extraction.browser_models import ScreenshotRequest
from text_extraction.envelope import ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.extract_api import respond
from text_extraction.extract_models import ChunkRequest, ExtractRequest, StructuredRequest
from text_extraction.fetch import Fetcher
from text_extraction.file_api import run_upload, upload_file_limit
from text_extraction.file_models import FileForm
from text_extraction.info import service_info
from text_extraction.job_models import BatchRequest, JobOptions
from text_extraction.jobs import SCREENSHOT_ARTIFACT
from text_extraction.jobs_api import check_jobs_enabled, read_job, start_batch
from text_extraction.pipeline import create_pipeline
from text_extraction.ratelimit import RateLimiter, check_rate
from text_extraction.screenshot_api import read_artifact, run_screenshot
from text_extraction.settings import Settings
from text_extraction.site.types import SiteStructureRequest
from text_extraction.site_api import start_site
from text_extraction.structured_api import run_chunk, run_structured

USER_KEY = "alice-key-0123456789-raw-secret"
SLOW_KEY = "slow-key-0123456789-raw-secret"
AUTH = {"Authorization": f"Bearer {USER_KEY}"}
UNAUTHORIZED_MESSAGE = "A valid API key is required."
# Fields that differ between two calls of the same request: new ids and times, the trace id,
# and the fingerprint's comparison with the call before (the first call stored it).
VOLATILE = frozenset(
    {"trace_id", "id", "status_url", "timings", "similarity_to_previous", "changed"}
)
TEXT = "# Notes\n\nPlants turn light into sugar.\n\n## Roots\n\nRoots take up water.\n"
SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}}}


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
                "name": "slow",
                "role": "user",
                "sha256": hashlib.sha256(SLOW_KEY.encode()).hexdigest(),
                "rate_limit_per_minute": 2,
            },
        ],
        "outbound_min_delay_ms": 0,
        "rate_limit_per_minute": 10_000,
        "batch_max_open_per_client": 10,
    }
    values.update(overrides)
    return Settings(**values)


class SlowFetcher(FakeFetcher):
    """Answers like `FakeFetcher`, after a pause: long enough to outlast a short wait."""

    async def fetch(self, url: str, **options: Any) -> Any:
        await asyncio.sleep(0.3)
        return await super().fetch(url, **options)


@contextlib.contextmanager
def running_app(fetcher: FakeFetcher | None = None, **overrides: Any) -> Iterator[TestClient]:
    settings = make_settings(**overrides)
    registry = CollectorRegistry()
    app = create_public_app(settings, FakeAsyncRedis(), registry)
    fake = fetcher or FakeFetcher({PAGE: fetched(body=ARTICLE)})
    app.state.pipeline = create_pipeline(settings, app.state.valkey, cast(Fetcher, fake), registry)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def client() -> Iterator[TestClient]:
    with running_app() as test_client:
        yield test_client


def state_of(client: TestClient) -> Any:
    return cast(FastAPI, client.app).state


def call[T](client: TestClient, core: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
    """Run a core in the app's event loop, as a route of the running app would."""
    portal = client.portal
    assert portal is not None
    return portal.call(partial(core, *args, **kwargs))


def as_json(answer: BaseModel | Response) -> Any:
    if isinstance(answer, Response):
        return json.loads(bytes(answer.body))
    return answer.model_dump(mode="json", by_alias=True)


def stable(value: Any) -> Any:
    """The answer without the fields that differ from call to call."""
    if isinstance(value, dict):
        return {key: stable(item) for key, item in value.items() if key not in VOLATILE}
    if isinstance(value, list):
        return [stable(item) for item in value]
    return value


def alice(client: TestClient) -> Principal:
    return principal_for(state_of(client).settings, f"Bearer {USER_KEY}")


def test_principal_for() -> None:
    open_settings = make_settings(auth_mode="none")
    anonymous = Principal(name="anonymous", role="admin", rate_limit_per_minute=None)
    assert principal_for(open_settings, None) == anonymous
    assert principal_for(open_settings, "Bearer anything") == anonymous

    settings = make_settings()
    assert principal_for(settings, f"Bearer {USER_KEY}") == Principal("alice", "user", None)
    assert principal_for(settings, f"bearer {SLOW_KEY}") == Principal("slow", "user", 2)
    for header in (None, "", "Bearer ", "Bearer wrong-key", USER_KEY, f"Basic {USER_KEY}"):
        with pytest.raises(ServiceError) as caught:
            principal_for(settings, header)
        assert caught.value.http_status == 401
        assert caught.value.code == ErrorCode.UNAUTHORIZED
        assert caught.value.message == UNAUTHORIZED_MESSAGE
        assert caught.value.headers == {"WWW-Authenticate": "Bearer"}


def test_check_rate_shares_bucket_with_routes(client: TestClient) -> None:
    state = state_of(client)
    # One fixed window for the whole test, so a minute boundary cannot reset the bucket.
    state.rate_limiter = RateLimiter(state.valkey, 10_000, clock=lambda: 6_000_030.0)
    slow = principal_for(state.settings, f"Bearer {SLOW_KEY}")
    headers = {"Authorization": f"Bearer {SLOW_KEY}"}

    assert client.get("/v1/info", headers=headers).status_code == 200
    call(client, check_rate, state, slow)
    refused = client.get("/v1/info", headers=headers)

    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "rate_limited"
    with pytest.raises(ServiceError) as caught:
        call(client, check_rate, state, slow)
    assert caught.value.http_status == 429
    assert caught.value.retryable
    assert caught.value.headers == {"Retry-After": "30"}


def test_check_jobs_enabled() -> None:
    check_jobs_enabled(make_settings())
    with pytest.raises(ServiceError) as caught:
        check_jobs_enabled(make_settings(jobs_enabled=False))
    assert caught.value.http_status == 503
    assert caught.value.message == "Jobs are disabled on this deployment."


def test_service_info() -> None:
    answer = service_info(Principal("alice", "user", None))

    assert answer.status == "success"
    assert answer.data is not None
    assert (answer.data.principal, answer.data.role, answer.data.name) == (
        "alice",
        "user",
        "text-extraction",
    )


def test_cores_answer_like_routes(client: TestClient) -> None:
    state = state_of(client)
    principal = alice(client)
    extract_body = {"url": PAGE, "cache": False}
    structured_body = {
        "url": PAGE,
        "cache": False,
        "structured": {"strategy": "metadata", "schema": SCHEMA},
    }
    chunk_body = {"text": TEXT, "chunking": {"strategy": "recursive", "size": 200}}
    batch_body = {"urls": [PAGE, f"{PAGE}&b=1"], "format": "text"}
    site_body = {"url": "https://example.org/", "depth": 1, "max_pages": 5}
    cases: list[tuple[str, Any, Any]] = [
        (
            "respond",
            client.post("/v1/extract", json=extract_body, headers=AUTH),
            call(
                client,
                respond,
                state,
                ExtractRequest.model_validate(extract_body),
                principal,
                "extract",
            ),
        ),
        (
            "run_structured",
            client.post("/v1/structured", json=structured_body, headers=AUTH),
            call(
                client,
                run_structured,
                state,
                principal,
                StructuredRequest.model_validate(structured_body),
                asr_key=None,
                llm_key=None,
            ),
        ),
        (
            "run_chunk",
            client.post("/v1/chunk", json=chunk_body, headers=AUTH),
            call(
                client,
                run_chunk,
                state,
                principal,
                ChunkRequest.model_validate(chunk_body),
                asr_key=None,
                llm_key=None,
            ),
        ),
        (
            "run_upload",
            client.post(
                "/v1/extract/file",
                files={"file": ("notes.md", TEXT.encode(), "text/markdown")},
                data={"format": "text"},
                headers=AUTH,
            ),
            call(
                client,
                run_upload,
                state,
                principal,
                FileForm(format="text"),
                TEXT.encode(),
                "notes.md",
                "text/markdown",
                asr_header=None,
                llm_header=None,
            ),
        ),
        (
            "start_batch",
            client.post("/v1/batch", json=batch_body, headers=AUTH),
            call(
                client,
                start_batch,
                state,
                principal,
                BatchRequest.model_validate(batch_body),
                asr_key=None,
                llm_key=None,
            ),
        ),
        (
            "start_site",
            client.post("/v1/site-structure", json=site_body, headers=AUTH),
            call(
                client, start_site, state, principal, SiteStructureRequest.model_validate(site_body)
            ),
        ),
    ]
    job_id = cases[4][1].json()["data"]["id"]
    cases.append(
        (
            "read_job",
            client.get(f"/v1/jobs/{job_id}", params={"offset": 1, "limit": 1}, headers=AUTH),
            call(client, read_job, state, principal, job_id, 1, 1),
        )
    )

    for name, route, core in cases:
        assert route.status_code in (200, 202), name
        expected = stable(route.json())
        assert expected["status"] in ("success", "partial"), name
        assert stable(as_json(core)) == expected, name


def test_run_upload_refuses_large_before_converting(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = state_of(client)
    monkeypatch.setattr(state.settings, "document_max_bytes", 4096)
    limit = upload_file_limit(state.settings)
    data = b"%PDF-1.7\n" + b"x" * limit
    pipeline = state.pipeline
    convert = pipeline.extract_upload
    seen: list[int] = []

    async def recording(body: bytes, *args: Any, **kwargs: Any) -> Any:
        seen.append(len(body))
        return await convert(body, *args, **kwargs)

    def never(_body: bytes) -> str:
        raise AssertionError("an upload over the limit must not be hashed")

    monkeypatch.setattr(pipeline, "extract_upload", recording)
    monkeypatch.setattr(pipeline_module, "_sha256", never)

    answer = call(
        client,
        run_upload,
        state,
        alice(client),
        FileForm(),
        data,
        "big.pdf",
        "application/pdf",
        asr_header=None,
        llm_header=None,
    )

    body = as_json(answer)
    assert body["status"] == "failed"
    assert body["error"]["code"] == "too_large"
    # The pipeline gets what the route's bounded read would give it: the limit and one byte.
    assert seen == [limit + 1]


def test_jsonresponse_handover_answers_like_route() -> None:
    """The 202 hand-over of a slow extraction: the core's `JSONResponse` is the route's."""
    with running_app(SlowFetcher({PAGE: fetched(body=ARTICLE)})) as client:
        state = state_of(client)
        body = {"url": PAGE, "cache": False, "wait_seconds": 0.05}
        route = client.post("/v1/extract", json=body, headers=AUTH)
        core = call(
            client, respond, state, ExtractRequest.model_validate(body), alice(client), "extract"
        )

        assert route.status_code == 202
        assert isinstance(core, Response)
        assert core.status_code == 202
        assert stable(as_json(core)) == stable(route.json())
        assert stable(route.json())["data"] == {"kind": "extract", "state": "running", "total": 1}


def test_run_screenshot_answers_like_route() -> None:
    """Browser mode on but no browser worker: both answer `failed`/`timeout` at once."""
    with running_app(browser_enabled=True, browser_queue_wait_seconds=0) as client:
        state = state_of(client)
        body = {"url": PAGE}
        route = client.post("/v1/screenshot", json=body, headers=AUTH)
        core = call(
            client, run_screenshot, state, alice(client), ScreenshotRequest.model_validate(body)
        )

        assert route.status_code == 200
        expected = stable(route.json())
        assert expected["status"] == "failed"
        assert stable(as_json(core)) == expected


def test_run_screenshot_refused_like_route(client: TestClient) -> None:
    """Browser mode off: the route's 400 is the core's `ServiceError`."""
    route = client.post("/v1/screenshot", json={"url": PAGE}, headers=AUTH)
    with pytest.raises(ServiceError) as caught:
        call(
            client,
            run_screenshot,
            state_of(client),
            alice(client),
            ScreenshotRequest(url=PAGE),
        )

    assert route.status_code == caught.value.http_status == 400
    error = route.json()["error"]
    assert (error["code"], error["message"]) == (caught.value.code, caught.value.message)


def test_read_artifact_answers_like_route(client: TestClient) -> None:
    state = state_of(client)
    principal = alice(client)
    png = b"\x89PNG\r\n\x1a\n-a-small-picture"

    async def stored() -> str:
        job_id: str = await state.job_store.create(
            "screenshot", principal, JobOptions(compliance="enforce"), [PAGE]
        )
        await state.job_store.put_artifact(job_id, SCREENSHOT_ARTIFACT, png)
        return job_id

    job_id = call(client, stored)
    path = f"/v1/jobs/{job_id}/artifacts/{SCREENSHOT_ARTIFACT}"
    route = client.get(path, headers=AUTH)

    assert route.status_code == 200
    assert route.content == call(
        client, read_artifact, state, principal, job_id, SCREENSHOT_ARTIFACT
    )
    assert route.content == png

    missing = client.get(f"/v1/jobs/{job_id}/artifacts/other.png", headers=AUTH)
    with pytest.raises(ServiceError) as caught:
        call(client, read_artifact, state, principal, job_id, "other.png")
    assert missing.status_code == caught.value.http_status == 404
    assert missing.json()["error"]["message"] == caught.value.message
