"""Issue #20's done-when test: image descriptions end to end through the API, on one German
teaching page (``pages/bilder.html``, written for the purpose: an OpenGraph cover image and four
``<img>``: a JPEG, one under a path robots.txt disallows, one at a private address and one that
is gone), and on an image upload.

It runs in the default suite and needs no real vision model: pytest-httpserver is both the
OpenAI-compatible endpoint (``/v1/chat/completions``) and the host of the page's images. The llm
worker is the real ``Worker`` with its real ``LlmRunner`` and a real image fetcher
(``create_image_fetcher``: the SSRF guard, the compliance checks, the outbound gate) whose address
policy allows ``127.0.0.1`` only, in a thread of its own on the fakeredis server of the app, as in
``tests/structured/test_done_when.py``. For the upload, a Docling worker (the real ``Worker`` on
pool ``docling`` with the fake OCR runner of ``tests/test_docling_dispatch.py``) reads the image's
text and moves the result on to the llm worker with the image's bytes.
"""

import asyncio
import base64
import json
import threading
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from tests.fakes import fetched
from tests.structured.post_helpers import CHAT, completion
from tests.test_docling_dispatch import OCR_TEXT, FakeDoclingRunner
from tests.test_llm_dispatch import CALLER_KEY, LlmService
from tests.test_pii_dispatch import AUTH
from tests.vision.describe_helpers import JPEG, PNG, is_vision, vision_requests
from text_extraction.docling_dispatch import docling_queue
from text_extraction.jobs import JobStore
from text_extraction.llm_dispatch import create_llm_move, llm_queue
from text_extraction.llm_worker import LlmRunner
from text_extraction.structured.store import SelectorStore
from text_extraction.vision.checks import DESCRIBE_PII_MESSAGE
from text_extraction.vision.fetch import create_image_fetcher
from text_extraction.vision.types import IMAGE_NOT_DESCRIBED_WARNING
from text_extraction.worker import Worker

PAGE_URL = "https://lernen.example.org/natur/wasserkreislauf.html"
PAGE_TEMPLATE = (Path(__file__).parent / "pages" / "bilder.html").read_text(encoding="utf-8")
LLM_HEADERS = {**AUTH, "X-LLM-Api-Key": CALLER_KEY}

TITLE_PICTURE = "Wolken ziehen über einen See, an dessen Ufer Bäume stehen."
BOARD_PICTURE = "Ein Tafelbild mit Pfeilen vom See zur Wolke und zurück zum See."
DESCRIPTION_OF = {"image/png": TITLE_PICTURE, "image/jpeg": BOARD_PICTURE}

# The page's images in ``metadata.images`` order: the cover image first, then the <img>.
TITLE, BOARD, DISALLOWED, PRIVATE, GONE = (
    "/bilder/titel.png",
    "/bilder/tafel.jpg",
    "/geheim/entwurf.png",
    "http://169.254.169.254/latest/meta-data/bild.png",
    "/bilder/fehlt.png",
)


def only_loopback(ip: IPv4Address | IPv6Address) -> bool:
    return ip == IPv4Address("127.0.0.1")


def decoded(body: dict[str, Any]) -> tuple[str, bytes]:
    """The media type and bytes of a vision call's ``image_url`` data URL."""
    [part] = [part for part in body["messages"][1]["content"] if part["type"] == "image_url"]
    header, payload = part["image_url"]["url"].split(",", 1)
    assert header.startswith("data:")
    assert header.endswith(";base64")
    return header[len("data:") : -len(";base64")], base64.b64decode(payload)


def serve(server: HTTPServer) -> None:
    """The endpoint (a vision call is described by the type of its image: the PNG cover or
    upload, the JPEG board picture) and the image host: robots.txt
    disallows ``/geheim/``, ``/bilder/fehlt.png`` is gone."""

    def chat(request: Request) -> Response:
        body = request.get_json()
        assert is_vision(body)
        media_type, _ = decoded(body)
        text = DESCRIPTION_OF[media_type]
        reply = completion(json.dumps({"description": text}))
        return Response(json.dumps(reply), content_type="application/json")

    server.expect_request(CHAT, method="POST").respond_with_handler(chat)
    server.expect_request("/robots.txt").respond_with_data(
        "User-agent: *\nDisallow: /geheim/\n", content_type="text/plain"
    )
    server.expect_request(TITLE).respond_with_data(PNG, content_type="image/png")
    server.expect_request(BOARD).respond_with_data(JPEG, content_type="image/jpeg")
    server.expect_request(DISALLOWED).respond_with_data(PNG, content_type="image/png")
    server.expect_request(GONE).respond_with_data("Not found", status=404)


def image_paths(server: HTTPServer) -> list[str]:
    return [request.path for request, _ in server.log if request.path != CHAT]


async def _short_sleep(seconds: float) -> None:
    await asyncio.sleep(min(seconds, 0.02))


class Workers:
    """The real llm ``Worker`` with ``LlmRunner`` and a real image fetcher (loopback only) and,
    with ``docling``, the real Docling ``Worker`` with the fake OCR runner, whose results that
    owe their description move on to ``llm``; on a loop of their own, sharing the fakeredis
    server of the app."""

    def __init__(self, service: LlmService, *, docling: bool = False) -> None:
        self._service = service
        self._docling = docling
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        settings = self._service.settings
        valkey = FakeAsyncRedis(server=self._service.server)
        store = JobStore(valkey, settings)
        registry = CollectorRegistry()
        selectors = SelectorStore(valkey, settings.selector_learn_ttl_seconds)
        images = create_image_fetcher(settings, valkey, policy=only_loopback)
        runner = LlmRunner(settings, store, selectors, registry=registry, images=images)
        workers = [
            Worker(
                settings,
                valkey,
                runner,
                store,
                llm_queue(valkey, settings),
                registry,
                concurrency=1,
                sleep=_short_sleep,
            )
        ]
        if self._docling:
            released = threading.Event()
            released.set()
            workers.append(
                Worker(
                    settings,
                    valkey,
                    FakeDoclingRunner(store, released),
                    store,
                    docling_queue(valkey, settings),
                    registry,
                    concurrency=1,
                    sleep=_short_sleep,
                    llm_move=create_llm_move(settings, valkey, store, None, registry),
                )
            )
        self._ready.set()
        try:
            async with asyncio.TaskGroup() as running:
                for worker in workers:
                    running.create_task(worker.run(self._stop))
        finally:
            await images.aclose()

    def __enter__(self) -> "Workers":
        self._thread.start()
        assert self._ready.wait(10)
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._loop is not None
        assert self._stop is not None
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(10)


@pytest.fixture
def service(httpserver: HTTPServer) -> LlmService:
    serve(httpserver)
    page = PAGE_TEMPLATE.replace("{images}", httpserver.url_for("").rstrip("/"))
    return LlmService(
        httpserver,
        {PAGE_URL: fetched(PAGE_URL, page)},
        outbound_max_crawl_delay_seconds=0,
    )


def extract(client: TestClient, **body: Any) -> dict[str, Any]:
    """``POST /v1/extract`` of the page with ``metadata``: a 200, its body."""
    response = client.post(
        "/v1/extract", json={"url": PAGE_URL, "metadata": True, **body}, headers=LLM_HEADERS
    )
    assert response.status_code == 200, response.text
    assert CALLER_KEY not in response.text
    return cast(dict[str, Any], response.json())


def descriptions(data: dict[str, Any]) -> list[str | None]:
    return [image["description"] for image in data["metadata"]["images"]]


# --- page images ---


def test_page_images_described_through_the_llm_worker(
    service: LlmService, httpserver: HTTPServer
) -> None:
    with TestClient(service.app) as client, Workers(service):
        plain = extract(client)
        body = extract(client, describe_images={})
        assert service.wait_for_no_jobs() == []  # the llm job is gone with the answer

    assert body["status"] == "success", body["error"]
    data = body["data"]
    assert data["content"] == plain["data"]["content"]  # the page text stays the page text
    assert descriptions(data) == [TITLE_PICTURE, BOARD_PICTURE, None, None, None]
    first = data["metadata"]["images"][0]
    assert first["url"] == httpserver.url_for(TITLE)
    assert first["alt"] == "Titelbild: Wolken über einem See"
    report = data["image_descriptions"]
    assert (report["asked"], report["described"], report["skipped"]) == (5, 2, 3)
    assert report["failed"] == 0
    assert report["llm"]["calls"] == 2
    assert data["timings"]["llm_ms"] >= 0
    assert (data["word_count"], data["quality"]) == (
        plain["data"]["word_count"],
        plain["data"]["quality"],
    )
    sent = [decoded(request) for request in vision_requests(httpserver)]
    assert sorted(sent) == sorted([("image/png", PNG), ("image/jpeg", JPEG)])
    for request in vision_requests(httpserver):
        text = next(p["text"] for p in request["messages"][1]["content"] if p["type"] == "text")
        assert text.startswith("Language: de")
    assert plain["data"]["image_descriptions"] is None
    assert descriptions(plain["data"]) == [None] * 5


def test_failed_image_fetch_is_skipped_with_warning(
    service: LlmService, httpserver: HTTPServer
) -> None:
    with TestClient(service.app) as client, Workers(service):
        body = extract(client, describe_images={})

    assert body["status"] == "success", body["error"]
    data = body["data"]
    assert GONE in image_paths(httpserver)  # asked for, answered 404
    assert data["metadata"]["images"][4]["url"] == httpserver.url_for(GONE)
    assert data["metadata"]["images"][4]["description"] is None
    assert IMAGE_NOT_DESCRIBED_WARNING in data["warnings"]
    assert body["error"] is None


def test_max_images_caps_the_calls(service: LlmService, httpserver: HTTPServer) -> None:
    with TestClient(service.app) as client, Workers(service):
        body = extract(client, describe_images={"max_images": 2})

    assert body["status"] == "success", body["error"]
    data = body["data"]
    assert len(vision_requests(httpserver)) == 2
    assert descriptions(data) == [TITLE_PICTURE, BOARD_PICTURE, None, None, None]
    report = data["image_descriptions"]
    assert (report["asked"], report["described"], report["skipped"]) == (2, 2, 0)
    assert IMAGE_NOT_DESCRIBED_WARNING not in data["warnings"]
    page_images = {TITLE, BOARD, DISALLOWED, GONE}
    asked = [path for path in image_paths(httpserver) if path in page_images]
    assert asked == [TITLE, BOARD]  # nothing fetched past the cap


def test_robots_and_ssrf_refusals_never_reach_the_image(
    service: LlmService, httpserver: HTTPServer
) -> None:
    with TestClient(service.app) as client, Workers(service):
        body = extract(client, describe_images={})

    data = body["data"]
    images = data["metadata"]["images"]
    assert images[2]["url"] == httpserver.url_for(DISALLOWED)
    assert images[3]["url"] == PRIVATE
    assert (images[2]["description"], images[3]["description"]) == (None, None)
    assert IMAGE_NOT_DESCRIBED_WARNING in data["warnings"]
    paths = image_paths(httpserver)
    assert DISALLOWED not in paths  # robots.txt disallows it: never requested
    assert paths.count("/robots.txt") == 1  # read once for the image host
    assert len(vision_requests(httpserver)) == 2  # the refused images never reach the model


def test_pii_with_describe_is_422(service: LlmService, httpserver: HTTPServer) -> None:
    with TestClient(service.app) as client:
        response = client.post(
            "/v1/extract",
            json={
                "url": PAGE_URL,
                "metadata": True,
                "describe_images": {},
                "pii": {"mode": "mask"},
            },
            headers=LLM_HEADERS,
        )

    assert response.status_code == 422
    assert response.json()["error"]["message"] == DESCRIBE_PII_MESSAGE
    assert httpserver.log == []


def test_no_description_in_the_result_cache(service: LlmService, httpserver: HTTPServer) -> None:
    with TestClient(service.app) as client, Workers(service):
        described = extract(client, describe_images={})
        again = extract(client)

    assert descriptions(described["data"])[:2] == [TITLE_PICTURE, BOARD_PICTURE]
    data = again["data"]
    assert data["cache"] == "hit"
    assert descriptions(data) == [None] * 5
    assert data["image_descriptions"] is None
    assert IMAGE_NOT_DESCRIBED_WARNING not in data["warnings"]
    keys = service.stored("res:*")
    assert keys != []

    async def entries(valkey: FakeAsyncRedis) -> list[bytes]:
        return [cast(bytes, await valkey.get(key)) for key in keys]

    for stored in service.run(entries):  # the stored extraction has no description
        assert TITLE_PICTURE.encode() not in stored
        assert b'"description":null' in stored


# --- an image upload ---


def test_image_upload_described(httpserver: HTTPServer) -> None:
    serve(httpserver)
    service = LlmService(httpserver, docling_enabled=True)
    with TestClient(service.app) as client, Workers(service, docling=True):
        response = client.post(
            "/v1/extract/file",
            files={"file": ("arbeitsblatt.png", PNG, "image/png")},
            data={"describe_images": "{}"},
            headers=LLM_HEADERS,
        )
        assert response.status_code == 200, response.text
        assert service.wait_for_no_jobs() == []  # the input went with the result

    body = response.json()
    assert body["status"] == "success", body["error"]
    data = body["data"]
    assert data["mode_used"] == "upload"
    assert data["document"]["ocr"] is True
    assert data["content"] == f"{OCR_TEXT}\n\n> Image description: {TITLE_PICTURE}"
    assert data["metadata"]["images"] == [{"url": None, "alt": "", "description": TITLE_PICTURE}]
    assert data["image_descriptions"]["described"] == 1
    [request] = vision_requests(httpserver)
    assert decoded(request) == ("image/png", PNG)
