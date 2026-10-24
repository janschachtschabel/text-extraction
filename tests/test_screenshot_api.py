"""``POST /v1/screenshot`` and ``GET /v1/jobs/{id}/artifacts/{name}`` (``screenshot_api.py``), on
the browser-enabled app of ``test_extract_api`` with its fake browser worker."""

import asyncio
import hashlib
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient

from tests.fakes import PAGE
from tests.test_extract_api import (
    ADMIN_AUTH,
    AUTH,
    BrowserCase,
    browser_settings,
)
from text_extraction.browser_models import ScreenshotResult

PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 256
BOB_KEY = "bob-key-0123456789-raw-secret"
BOB = {"Authorization": f"Bearer {BOB_KEY}"}


def case_with_bob(**overrides: Any) -> BrowserCase:
    keys = [
        *browser_settings().model_dump()["api_keys"],
        {"name": "bob", "role": "user", "sha256": hashlib.sha256(BOB_KEY.encode()).hexdigest()},
    ]
    return BrowserCase(api_keys=keys, **overrides)


def take(client: TestClient, **body: Any) -> Any:
    return client.post("/v1/screenshot", json={"url": PAGE, **body}, headers=AUTH)


def test_screenshot_returns_artifact_ref() -> None:
    case = BrowserCase()
    with TestClient(case.app) as client, case.worker(artifact=PNG) as worker:
        response = take(client, full_page=False, cookies=[{"name": "s", "value": "v"}])

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "success"
        data = body["data"]
        assert set(data) == set(ScreenshotResult.model_fields)
        assert "content" not in data
        assert data["mode_used"] == "browser"
        shot = data["screenshot"]
        job = worker.seen[0].job
        assert shot["url"] == f"/v1/jobs/{job.id}/artifacts/screenshot.png"
        assert (shot["content_type"], shot["bytes"], shot["width"]) == ("image/png", len(PNG), 1920)
        assert job.kind == "screenshot"
        assert (job.options.mode, job.options.screenshot, job.options.full_page) == (
            "browser",
            True,
            False,
        )
        assert job.options.metadata is False
        assert worker.seen[0].secrets.cookies[0].value == "v"


def test_screenshot_flag_on_extract_keeps_the_extract_result() -> None:
    case = BrowserCase()
    with TestClient(case.app) as client, case.worker(artifact=PNG) as worker:
        response = client.post("/v1/extract", json={"url": PAGE, "screenshot": True}, headers=AUTH)

        data = response.json()["data"]
        assert data["content"] == "Rendered text."
        assert data["screenshot"]["name"] == "screenshot.png"
        job = worker.seen[0].job
        assert (job.kind, job.options.mode, job.options.screenshot) == ("extract", "auto", True)
        assert case.fetcher.calls == []  # a screenshot always renders


def test_artifact_download_png() -> None:
    case = case_with_bob()
    with TestClient(case.app) as client, case.worker(artifact=PNG):
        url = take(client).json()["data"]["screenshot"]["url"]

        response = client.get(url, headers=AUTH)

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content == PNG
        assert client.get(url, headers=ADMIN_AUTH).status_code == 200  # admins see all jobs
        spec = client.get("/openapi.json").json()
        route = spec["paths"]["/v1/jobs/{job_id}/artifacts/{name}"]["get"]
        assert "image/png" in route["responses"]["200"]["content"]
        assert "application/json" not in route["responses"]["200"]["content"]
        assert {"404", "503"} <= set(route["responses"])
        assert "/v1/screenshot" in spec["paths"]


@pytest.mark.parametrize(
    "path",
    [
        "/v1/jobs/{job}/artifacts/screenshot.png#bob",
        "/v1/jobs/{unknown}/artifacts/screenshot.png",
        "/v1/jobs/not-a-job/artifacts/screenshot.png",
        "/v1/jobs/{job}/artifacts/other.png",
        "/v1/jobs/{job}/artifacts/SCREENSHOT.PNG",
        "/v1/jobs/{job}/artifacts/" + "a" * 65,
        "/v1/jobs/{plain}/artifacts/screenshot.png",
    ],
)
def test_foreign_or_unknown_artifact_is_404(path: str) -> None:
    case = case_with_bob()
    with TestClient(case.app) as client, case.worker(artifact=PNG) as worker:
        take(client)
        plain = client.post("/v1/extract", json={"url": PAGE, "mode": "browser"}, headers=AUTH)
        assert plain.status_code == 200
        job, plain_job = worker.seen[0].job.id, worker.seen[1].job.id
        auth = AUTH
        if path.endswith("#bob"):
            path, auth = path.removesuffix("#bob"), BOB
        url = path.format(job=job, unknown="f" * 32, plain=plain_job)
        if path.startswith("/v1/jobs/{plain}"):
            # The plain render's job has no screenshot (the fake worker stored one for every
            # job, so remove it to model a missing artifact).
            async def drop() -> None:
                valkey = FakeAsyncRedis(server=case.server)
                await valkey.delete(f"job:{{{plain_job}}}:artifact:screenshot.png")

            asyncio.run(drop())

        response = client.get(url, headers=auth)

        assert response.status_code == 404
        body = response.json()
        assert (body["status"], body["error"]["code"]) == ("failed", "not_found")


def test_artifact_route_with_jobs_disabled_is_503() -> None:
    case = BrowserCase(jobs_enabled=False)
    with TestClient(case.app) as client:
        response = client.get(f"/v1/jobs/{'f' * 32}/artifacts/screenshot.png", headers=AUTH)

        assert response.status_code == 503


def test_screenshot_disabled_is_400() -> None:
    case = BrowserCase(browser_enabled=False)
    with TestClient(case.app) as client:
        response = take(client)

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"
        assert case.keys("job*") == []


def test_screenshot_with_wait_seconds_answers_202() -> None:
    case = BrowserCase()
    with TestClient(case.app) as client, case.worker(artifact=PNG) as worker:
        worker.hold.clear()
        response = take(client, wait_seconds=0)

        assert response.status_code == 202
        accepted = response.json()["data"]
        assert accepted["kind"] == "screenshot"
        assert worker.claimed.wait(10)
        assert accepted["id"] == worker.seen[0].job.id
        worker.hold.set()
        final = case.wait_for_job(client, accepted["id"])
        # A screenshot job's item is shown as stored: the extraction envelope with the artifact.
        item = final["data"]["items"][0]["result"]
        assert item["data"]["screenshot"]["name"] == "screenshot.png"
        assert client.get(item["data"]["screenshot"]["url"], headers=AUTH).content == PNG


def test_screenshot_without_worker_is_timeout() -> None:
    case = BrowserCase(browser_queue_wait_seconds=0)
    with TestClient(case.app) as client:
        response = take(client)

        body = response.json()
        assert response.status_code == 200
        assert (body["status"], body["error"]["code"], body["error"]["retryable"]) == (
            "failed",
            "timeout",
            True,
        )
        assert body["data"]["screenshot"] is None
        assert case.keys("job*") == []


def test_screenshot_evaluate_by_user_is_403() -> None:
    case = BrowserCase()
    with TestClient(case.app) as client:
        response = take(client, actions=[{"type": "evaluate", "script": "1"}])

        assert response.status_code == 403
        assert case.keys("job*") == []
