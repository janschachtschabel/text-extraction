"""``POST /v1/structured`` and ``POST /v1/chunk`` (ruling S15, Task 9): the same flow and checks as
``POST /v1/extract`` and ``POST /v1/extract/file``, on fakeredis with a fake fetcher; the semantic
test runs the fake llm worker and pytest-httpserver as ``tests/test_llm_dispatch.py`` does."""

import asyncio
import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest_httpserver import HTTPServer

from tests.fakes import PAGE
from tests.structured.post_helpers import serve_embeddings
from tests.test_extract_api import (
    AUTH,
    LLM_DISABLED,
    LLM_KEY,
    LLM_KEY_HEADERS,
    LLM_KEY_MISSING,
    LLM_STRUCTURED,
    SCHEMA_ONE,
    Service,
    llm_settings,
    make_settings,
    refusal,
    without_llm_hop,
)
from tests.test_file_api import BEARER, asgi_call, comparable
from tests.test_llm_dispatch import CALLER_KEY, LLM_HEADERS, LlmService, keys_sent
from text_extraction.jobs import JobStore

METADATA_TITLE: dict[str, Any] = {"schema": SCHEMA_ONE, "strategy": "metadata"}
# At least 50 words, or the upload is judged too short (partial/content_too_short).
CATS = "Cats purr and sleep all day long. " * 8
DOGS = "Dogs bark and run all day long. " * 8
MARKDOWN = f"# Cats\n\n{CATS.strip()}\n\n## Dogs\n\n{DOGS.strip()}\n"
PLAIN = f"# Not a heading\n\n{CATS.strip()}\n\n{DOGS.strip()}\n"
CHUNKING_MARKDOWN: dict[str, Any] = {"strategy": "markdown", "size": 1000, "overlap": 0}


NAMES = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota", "kappa"]
CAT_SENTENCES = " ".join(f"Cats purr softly {name}." for name in NAMES)
DOG_SENTENCES = " ".join(f"Dogs bark loudly {name}." for name in NAMES)
TWO_TOPICS = f"{CAT_SENTENCES} {DOG_SENTENCES}"


def messages(response: Any) -> str:
    """The error message and the details of a 422 body, joined."""
    error = response.json()["error"]
    return " ".join([error["message"], *(item["msg"] for item in error.get("details") or [])])


def structured(client: TestClient, headers: dict[str, str] | None = None, **body: Any) -> Any:
    return client.post("/v1/structured", json={"url": PAGE, **body}, headers=headers or AUTH)


def chunk(client: TestClient, headers: dict[str, str] | None = None, **body: Any) -> Any:
    return client.post("/v1/chunk", json=body, headers=headers or AUTH)


# --- POST /v1/structured ---


@pytest.mark.parametrize("body", [{}, {"structured": None}], ids=["missing", "null"])
def test_structured_route_requires_structured(body: dict[str, Any]) -> None:
    service = Service()
    with TestClient(service.app) as client:
        response = structured(client, **body)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_request"
    assert service.fetcher.calls == []
    assert service.keys() == []


def test_structured_route_answers_like_extract() -> None:
    service = Service()
    with TestClient(service.app) as client:
        via_extract = client.post(
            "/v1/extract",
            json={"url": PAGE, "structured": METADATA_TITLE, "cache": False},
            headers=AUTH,
        )
        via_structured = structured(client, structured=METADATA_TITLE, cache=False)

    assert via_structured.status_code == 200, via_structured.text
    expected, answer = via_extract.json(), via_structured.json()
    assert answer["status"] == "success"
    assert answer["data"]["structured"]["values"] == {"title": "Photosynthesis explained"}
    for body in (expected, answer):  # timings differ, the rest is the same answer
        body["data"].pop("timings")
        body["data"]["fingerprint"] = None
        body.pop("trace_id", None)
    assert answer == expected


def test_structured_route_wait_seconds_hands_over_a_job() -> None:
    service = Service()
    with TestClient(service.app) as client:
        response = structured(client, structured=METADATA_TITLE, wait_seconds=0)

        assert response.status_code == 202, response.text
        accepted = response.json()["data"]
        assert (accepted["kind"], accepted["total"]) == ("extract", 1)
        job = client.get(accepted["status_url"], headers=AUTH).json()
        for _ in range(100):
            if job["data"]["state"] == "completed":
                break
            job = client.get(accepted["status_url"], headers=AUTH).json()
    result = job["data"]["items"][0]["result"]
    assert result["data"]["structured"]["values"] == {"title": "Photosynthesis explained"}


def test_structured_route_runs_the_checks_of_extract() -> None:
    service = Service(llm_settings(llm_api_key=None))
    xpath = {
        "schema": SCHEMA_ONE,
        "strategy": "selectors",
        "selectors": {"title": {"xpath": "//h1"}},
    }
    with TestClient(service.app) as client:
        no_key = structured(client, structured=LLM_STRUCTURED)
        bad_key = structured(
            client, {**AUTH, "X-LLM-Api-Key": "has space"}, structured=LLM_STRUCTURED
        )
        non_admin_xpath = structured(client, structured=xpath)
        browser_off = structured(client, structured=METADATA_TITLE, mode="browser")
        ocr_off = structured(client, structured=METADATA_TITLE, ocr="always")
        pii_off = structured(client, structured=METADATA_TITLE, pii="mask")
        html_ok = structured(client, structured=METADATA_TITLE, format="html")

    refusal(no_key, 400, LLM_KEY_MISSING)
    assert bad_key.status_code == 400
    assert "X-LLM-Api-Key" in bad_key.json()["error"]["message"]
    assert LLM_KEY not in bad_key.text
    assert non_admin_xpath.status_code == 422
    assert "XPath" in non_admin_xpath.json()["error"]["message"]
    assert browser_off.status_code == 400
    assert ocr_off.status_code == 400
    assert pii_off.status_code == 400
    assert html_ok.status_code == 200  # structured needs no markdown or text
    assert service.keys() == []


def test_structured_route_without_llm_400() -> None:
    service = Service()
    with TestClient(service.app) as client:
        response = structured(client, LLM_KEY_HEADERS, structured=LLM_STRUCTURED)

    refusal(response, 400, LLM_DISABLED)


def test_structured_route_takes_chunking_too() -> None:
    service = Service()
    with TestClient(service.app) as client:
        response = structured(client, structured=METADATA_TITLE, chunking={"strategy": "fixed"})

    data = response.json()["data"]
    assert data["structured"]["values"] == {"title": "Photosynthesis explained"}
    assert data["chunks"]


def test_structured_route_keeps_the_caller_llm_key_with_its_job() -> None:
    service = Service(llm_settings(llm_api_key=None))
    without_llm_hop(service.app)
    with TestClient(service.app) as client:
        response = structured(client, LLM_KEY_HEADERS, structured=LLM_STRUCTURED)

    assert response.status_code == 200, response.text
    assert LLM_KEY not in response.text


# --- POST /v1/chunk ---


def test_chunk_route_url_and_text_exactly_one() -> None:
    service = Service()
    with TestClient(service.app) as client:
        both = chunk(client, url=PAGE, text=MARKDOWN, chunking=CHUNKING_MARKDOWN)
        neither = chunk(client, chunking=CHUNKING_MARKDOWN)
        no_chunking = chunk(client, text=MARKDOWN)
        assert service.fetcher.calls == []  # refused before anything was fetched
        by_url = chunk(client, url=PAGE, chunking={"strategy": "recursive", "size": 200})
        by_text = chunk(client, text=MARKDOWN, chunking=CHUNKING_MARKDOWN)

    for refused in (both, neither, no_chunking):
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["code"] == "invalid_request"
    assert "exactly one" in messages(both)
    assert "exactly one" in messages(neither)
    assert by_url.status_code == 200
    assert by_url.json()["data"]["mode_used"] == "fast"
    assert by_url.json()["data"]["chunks"]
    assert by_text.status_code == 200
    assert service.keys() == []


def test_chunk_url_is_extract_with_chunking() -> None:
    service = Service()
    chunking = {"strategy": "recursive", "size": 200, "overlap": 0}
    with TestClient(service.app) as client:
        via_extract = client.post(
            "/v1/extract", json={"url": PAGE, "chunking": chunking}, headers=AUTH
        )
        via_chunk = chunk(client, url=PAGE, chunking=chunking)

    texts = [item["text"] for item in via_chunk.json()["data"]["chunks"]]
    assert texts == [item["text"] for item in via_extract.json()["data"]["chunks"]]
    assert len(texts) > 1


def test_chunk_url_options_apply() -> None:
    service = Service()
    with TestClient(service.app) as client:
        browser = chunk(client, url=PAGE, chunking=CHUNKING_MARKDOWN, mode="browser")
        html = chunk(client, url=PAGE, chunking=CHUNKING_MARKDOWN, format="html")

    assert browser.status_code == 400  # browser mode is off
    assert html.status_code == 422  # chunk's format is markdown or text


def test_chunk_text_markdown_and_plain_as_upload() -> None:
    service = Service()
    with TestClient(service.app) as client:
        markdown = chunk(client, text=MARKDOWN, chunking=CHUNKING_MARKDOWN)
        plain = chunk(client, text=PLAIN, text_type="plain", chunking=CHUNKING_MARKDOWN)

    assert markdown.status_code == 200, markdown.text
    data = markdown.json()["data"]
    assert (data["mode_used"], data["url"], data["cache"]) == ("upload", None, "bypass")
    assert data["content_type"] == "text/markdown"
    assert [item["headings"] for item in data["chunks"]] == [["Cats"], ["Cats", "Dogs"]]
    assert data["chunks"][0]["text"].startswith("# Cats")
    assert data["chunks"][1]["text"].startswith("## Dogs")
    for item in data["chunks"]:
        assert data["content"][item["start"] : item["end"]] == item["text"]

    assert plain.status_code == 200, plain.text
    data = plain.json()["data"]
    assert data["mode_used"] == "upload"
    assert data["content_type"] == "text/plain"
    assert data["format"] == "text"
    assert data["chunks"]
    assert all(item["headings"] == [] for item in data["chunks"])  # headings: Markdown only
    assert data["chunks"][0]["text"].startswith("# Not a heading")
    assert service.fetcher.calls == []


def test_chunk_text_format_text() -> None:
    service = Service()
    with TestClient(service.app) as client:
        response = chunk(client, text=MARKDOWN, format="text", chunking={"strategy": "fixed"})

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["format"] == "text"
    assert all(item["headings"] == [] for item in data["chunks"])


def test_chunk_text_over_upload_limit_413() -> None:
    service = Service(make_settings(document_max_bytes=2048))
    with TestClient(service.app) as client:
        over = chunk(client, text="é" * 1025, chunking=CHUNKING_MARKDOWN)  # 2,050 bytes
        within = chunk(client, text="é" * 1024, chunking=CHUNKING_MARKDOWN)  # 2,048 bytes

    assert over.status_code == 413, over.text
    assert over.json()["error"]["code"] == "invalid_request"
    assert "2048" in over.json()["error"]["message"]
    assert within.status_code == 200, within.text
    assert service.keys() == []


def test_chunk_text_beyond_the_default_body_limit() -> None:
    # The route's body limit is the upload's, not the 64 KiB of the other JSON routes.
    service = Service()
    text = "Cats purr. " * 20_000  # 220,000 bytes
    with TestClient(service.app) as client:
        response = chunk(client, text=text, chunking={"strategy": "fixed", "size": 20_000})
        over = chunk(
            client,
            text="x" * (service.settings.document_max_bytes + 65_000),
            chunking={"strategy": "fixed"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["chunks"]
    assert over.status_code == 413


def test_chunk_text_with_url_only_options_422() -> None:
    service = Service()
    page_options: dict[str, Any] = {
        "mode": "fast",
        "links": True,
        "cache": False,
        "screenshot": True,
        "transcribe": True,
        "languages": ["en"],
        "ocr": "never",
    }
    with TestClient(service.app) as client:
        responses = [
            chunk(client, text=MARKDOWN, chunking=CHUNKING_MARKDOWN, **{name: value})
            for name, value in page_options.items()
        ]

    for response in responses:
        assert response.status_code == 422, response.text
        assert "cannot be combined with text" in messages(response)


def test_chunk_text_refuses_describe_images() -> None:
    """A text has no page and no image: describe_images is one of the page options (V-S1)."""
    service = Service()
    with TestClient(service.app) as client:
        response = chunk(
            client, text=MARKDOWN, chunking=CHUNKING_MARKDOWN, describe_images={"max_images": 2}
        )

    assert response.status_code == 422, response.text
    assert "cannot be combined with text: describe_images" in messages(response)


def test_chunk_text_becomes_a_file_job_when_handed_over() -> None:
    service = Service()
    with TestClient(service.app) as client:
        response = chunk(client, text=MARKDOWN, chunking=CHUNKING_MARKDOWN, wait_seconds=0)

        assert response.status_code == 202, response.text
        accepted = response.json()["data"]
        assert (accepted["kind"], accepted["total"]) == ("file", 1)
        job = client.get(accepted["status_url"], headers=AUTH).json()
        for _ in range(100):
            if job["data"]["state"] == "completed":
                break
            job = client.get(accepted["status_url"], headers=AUTH).json()
    item = job["data"]["items"][0]
    assert item["url"] is None
    assert item["result"]["data"]["mode_used"] == "upload"
    assert item["result"]["data"]["chunks"]


def test_chunk_text_runs_the_checks_of_the_upload() -> None:
    service = Service()
    with TestClient(service.app) as client:
        semantic = chunk(client, LLM_KEY_HEADERS, text=MARKDOWN, chunking={"strategy": "semantic"})
        pii = chunk(client, text=MARKDOWN, chunking=CHUNKING_MARKDOWN, pii="mask")
        xpath = chunk(
            client,
            text=MARKDOWN,
            chunking=CHUNKING_MARKDOWN,
            structured={
                "schema": SCHEMA_ONE,
                "strategy": "selectors",
                "selectors": {"title": {"xpath": "//h1"}},
            },
        )

    refusal(semantic, 400, LLM_DISABLED)
    assert pii.status_code == 400
    assert xpath.status_code == 422
    assert service.keys() == []


def test_chunk_text_with_semantic_needs_the_key_and_the_embedding_model() -> None:
    no_model = Service(llm_settings(llm_embedding_model=None))
    no_key = Service(llm_settings(llm_api_key=None))
    with TestClient(no_model.app) as client:
        model = chunk(client, text=MARKDOWN, chunking={"strategy": "semantic"})
    with TestClient(no_key.app) as client:
        key = chunk(client, text=MARKDOWN, chunking={"strategy": "semantic"})

    refusal(model, 400, "Semantic chunking needs LLM_EMBEDDING_MODEL.")
    refusal(key, 400, LLM_KEY_MISSING)


def test_chunk_with_structured_on_text() -> None:
    service = Service()
    with TestClient(service.app) as client:
        response = chunk(
            client,
            text="Price: 42 EUR\n",
            chunking={"strategy": "fixed"},
            structured={
                "schema": {"type": "object", "properties": {"price": {"type": "string"}}},
                "strategy": "patterns",
                "patterns": {"price": {"regex": r"Price: (\d+)", "group": 1}},
            },
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["structured"]["values"] == {"price": "42"}
    assert data["chunks"]


def test_chunk_with_semantic_goes_through_llm_hop(httpserver: HTTPServer) -> None:
    serve_embeddings(
        httpserver,
        lambda text: [1.0, 0.0] if "Cats" in text else [0.0, 1.0],
    )
    service = LlmService(httpserver, llm_embedding_model="embed-1")
    with TestClient(service.app) as client, service.workers():
        response = chunk(client, LLM_HEADERS, text=TWO_TOPICS, chunking={"strategy": "semantic"})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "success", body["error"]
        data = body["data"]
        assert data["mode_used"] == "upload"
        assert [item["text"] for item in data["chunks"]] == [CAT_SENTENCES, DOG_SENTENCES]
        assert data["timings"]["llm_ms"] >= 0
        assert CALLER_KEY not in response.text
        assert service.wait_for_no_jobs() == []
    assert set(keys_sent(httpserver)) == {f"Bearer {CALLER_KEY}"}  # the caller's key wins
    assert service.llm_outcomes() == {"answered": 1}


def test_chunk_with_semantic_waits_then_hands_over_a_file_job(httpserver: HTTPServer) -> None:
    serve_embeddings(httpserver, lambda text: [1.0, 0.0] if "Cats" in text else [0.0, 1.0])
    service = LlmService(httpserver, llm_embedding_model="embed-1", llm_sync_wait_seconds=1)
    with TestClient(service.app) as client:  # no llm worker: the request waits, then 202
        response = chunk(client, LLM_HEADERS, text=TWO_TOPICS, chunking={"strategy": "semantic"})

        assert response.status_code == 202, response.text
        accepted = response.json()["data"]
        assert (accepted["kind"], accepted["total"]) == ("file", 1)

        with service.workers():
            final = service.wait_for_job(client, accepted["id"])

    assert final["data"]["state"] == "completed"
    result = final["data"]["items"][0]["result"]
    assert result["status"] == "success", result["error"]
    assert result["data"]["mode_used"] == "upload"
    assert result["data"]["chunks"]


# --- OpenAPI ---


def test_openapi_lists_both_routes_with_examples() -> None:
    service = Service(llm_settings())
    with TestClient(service.app) as client:
        spec = client.get("/openapi.json").json()

    for path in ("/v1/structured", "/v1/chunk"):
        operation = spec["paths"][path]["post"]
        assert operation["summary"]
        assert operation["description"]
        content = operation["requestBody"]["content"]["application/json"]
        assert len(content["examples"]) >= 2
        for example in content["examples"].values():
            assert example["summary"]
            json.dumps(example["value"])
        assert {"200", "202"} <= set(operation["responses"])
        names = {parameter["name"] for parameter in operation["parameters"]}
        assert "X-LLM-Api-Key" in names

    schemas = spec["components"]["schemas"]
    assert "structured" in schemas["StructuredRequest"]["required"]
    chunk_schema = schemas["ChunkRequest"]
    assert "chunking" in chunk_schema["required"]
    assert {"url", "text", "text_type"} <= set(chunk_schema["properties"])
    assert chunk_schema["properties"]["text_type"]["default"] == "markdown"


def test_the_new_routes_are_authenticated() -> None:
    service = Service()
    with TestClient(service.app) as client:
        no_key = client.post("/v1/chunk", json={"text": MARKDOWN, "chunking": CHUNKING_MARKDOWN})
        structured_no_key = client.post(
            "/v1/structured", json={"url": PAGE, "structured": METADATA_TITLE}
        )

    assert no_key.status_code == structured_no_key.status_code == 401


# --- fix round 1: R14, R15, R16 ---


def test_chunk_text_is_limited_by_document_max_bytes_even_with_media() -> None:
    # R14: a text is never media. With ASR on, uploads may be as large as MEDIA_UPLOAD_MAX_BYTES
    # (and the body limit follows), but a text is limited by DOCUMENT_MAX_BYTES.
    settings = make_settings(
        asr_enabled=True, media_upload_max_bytes=1_000_000, document_max_bytes=4096
    )
    service = Service(settings)
    with TestClient(service.app) as client:
        over = chunk(client, text="x" * 4097, chunking={"strategy": "fixed"})
        within = chunk(client, text="word " * 800, chunking={"strategy": "fixed"})

    assert over.status_code == 413, over.text
    assert "4096" in over.json()["error"]["message"]
    assert within.status_code == 200, within.text


def test_chunk_empty_text_422() -> None:
    service = Service()
    with TestClient(service.app) as client:
        response = chunk(client, text="", chunking=CHUNKING_MARKDOWN)

    assert response.status_code == 422, response.text


def example_bodies(client: TestClient, path: str) -> dict[str, dict[str, Any]]:
    spec = client.get("/openapi.json").json()
    content = spec["paths"][path]["post"]["requestBody"]["content"]["application/json"]
    return {name: example["value"] for name, example in content["examples"].items()}


def test_chunk_openapi_text_examples_answer_success() -> None:
    # R15: the documented examples, as they are written, answer `success`: a short text is no
    # "content too short" on a chunking endpoint.
    service = Service(llm_settings())
    with TestClient(service.app) as client:
        examples = example_bodies(client, "/v1/chunk")
        text = chunk(client, **examples["text"])
        plain = chunk(client, **{**examples["text"], "text_type": "plain"})
        url = chunk(client, **{**examples["url"], "url": PAGE})

    for response in (text, plain, url):
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "success", response.json()["error"]
        assert response.json()["error"] is None
        assert response.json()["data"]["chunks"]
    assert text.json()["data"]["mode_used"] == "upload"
    assert text.json()["data"]["word_count"] < 50


def test_chunk_openapi_semantic_example_answers_success(httpserver: HTTPServer) -> None:
    serve_embeddings(httpserver, lambda text: [1.0, 0.0] if "Cats" in text else [0.0, 1.0])
    service = LlmService(httpserver, llm_embedding_model="embed-1")
    with TestClient(service.app) as client, service.workers():
        example = example_bodies(client, "/v1/chunk")["semantic"]
        response = chunk(client, LLM_HEADERS, **example)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "success", body["error"]
    assert [item["text"] for item in body["data"]["chunks"]] == [
        "Cats purr. Cats sleep.",
        "Dogs bark. Dogs run.",
    ]


def test_chunk_short_text_is_success_but_an_empty_page_is_still_failed() -> None:
    service = Service()
    with TestClient(service.app) as client:
        short = chunk(client, text="Just one short sentence.", chunking={"strategy": "fixed"})
        blank = chunk(client, text=" \n ", chunking={"strategy": "fixed"})

    assert short.json()["status"] == "success", short.json()["error"]
    assert short.json()["data"]["chunks"][0]["text"] == "Just one short sentence."
    assert blank.json()["status"] == "failed"
    assert blank.json()["error"]["code"] == "empty_content"


def test_the_word_count_gate_still_applies_to_the_upload_route() -> None:
    service = Service()
    with TestClient(service.app) as client:
        response = client.post(
            "/v1/extract/file",
            files={"file": ("short.txt", b"Just one short sentence.", "text/plain")},
            headers=AUTH,
        )

    assert response.json()["status"] == "partial"
    assert response.json()["error"]["code"] == "content_too_short"


BODY_CHUNKS = [b'{"text": "' + b"x" * 65_526] + [b"x" * 65_536] * 400  # about 26 MB
JSON_HEADERS = [(b"content-type", b"application/json")]


def test_chunk_without_key_is_401_before_the_body_is_read() -> None:
    # R16: as for the upload route, a refusal costs the pod none of the body.
    app = Service().app

    async def scenario() -> tuple[Any, Any]:
        refused = await asgi_call(app, BODY_CHUNKS, JSON_HEADERS, "/v1/chunk")
        extract = await asgi_call(
            app, [b'{"url": "https://example.org/"}'], JSON_HEADERS, "/v1/extract"
        )
        return refused, extract

    refused, extract = asyncio.run(scenario())

    assert refused.status == 401
    assert refused.pulled == 0
    assert refused.json()["error"]["code"] == "unauthorized"
    assert comparable(refused) == comparable(extract)


def test_chunk_over_rate_limit_is_429_before_the_body_is_read() -> None:
    service = Service(make_settings(rate_limit_per_minute=1))
    app = service.app

    async def scenario() -> Any:
        await app.state.rate_limiter.hit("alice", 1)  # the minute's one request is used up
        return await asgi_call(app, BODY_CHUNKS, [*JSON_HEADERS, *BEARER], "/v1/chunk")

    refused = asyncio.run(scenario())

    assert refused.status == 429
    assert refused.pulled == 0
    assert "retry-after" in refused.headers


def test_chunk_counts_once_against_the_rate_limit() -> None:
    service = Service(make_settings(rate_limit_per_minute=2))
    with TestClient(service.app) as client:
        statuses = [
            chunk(client, text=MARKDOWN, chunking=CHUNKING_MARKDOWN).status_code for _ in range(3)
        ]

    assert statuses == [200, 200, 429]


def test_chunk_authenticated_body_over_limit_is_413_without_reading_it_all() -> None:
    service = Service(make_settings(document_max_bytes=2048))
    app = service.app

    refused = asyncio.run(asgi_call(app, BODY_CHUNKS, [*JSON_HEADERS, *BEARER], "/v1/chunk"))

    assert refused.status == 413
    assert refused.pulled <= 3  # refused at the chunk that went over 2,048 + 64 KiB
    assert refused.json()["error"]["code"] == "invalid_request"


def test_chunk_invalid_json_is_422() -> None:
    service = Service()
    with TestClient(service.app) as client:
        broken = client.post(
            "/v1/chunk", content=b'{"text": ', headers={**AUTH, "content-type": "application/json"}
        )
        wrong = chunk(client, text=MARKDOWN, chunking={"strategy": "nope"})

    for response in (broken, wrong):
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "invalid_request"


def test_chunk_short_text_as_a_job_keeps_short_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    # R15 on the job path: the flag is stored with the upload's input, so a worker that converts
    # the item (tests/test_worker.py) skips the word-count gate as the request does; the
    # request's own task stores `success`.
    stored: list[Any] = []
    put_input = JobStore.put_input

    async def recording(
        self: JobStore, job_id: str, index: int, body: bytes, meta: Any, ttl: Any
    ) -> None:
        stored.append(meta)
        return await put_input(self, job_id, index, body, meta, ttl)

    monkeypatch.setattr(JobStore, "put_input", recording)
    service = Service()
    with TestClient(service.app) as client:
        response = chunk(
            client,
            text="Just one short sentence.",
            chunking={"strategy": "fixed"},
            wait_seconds=0,
        )
        assert response.status_code == 202, response.text
        accepted = response.json()["data"]
        assert accepted["kind"] == "file"
        job = client.get(accepted["status_url"], headers=AUTH).json()
        for _ in range(200):
            if job["data"]["state"] == "completed":
                break
            time.sleep(0.02)
            job = client.get(accepted["status_url"], headers=AUTH).json()

    assert [meta.short_ok for meta in stored] == [True]
    result = job["data"]["items"][0]["result"]
    assert result["status"] == "success", result["error"]
    assert result["data"]["chunks"][0]["text"] == "Just one short sentence."


def test_upload_input_of_the_file_route_keeps_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    stored: list[Any] = []
    put_input = JobStore.put_input

    async def recording(
        self: JobStore, job_id: str, index: int, body: bytes, meta: Any, ttl: Any
    ) -> None:
        stored.append(meta)
        return await put_input(self, job_id, index, body, meta, ttl)

    monkeypatch.setattr(JobStore, "put_input", recording)
    service = Service()
    with TestClient(service.app) as client:
        response = client.post(
            "/v1/extract/file",
            files={"file": ("short.txt", b"Just one short sentence.", "text/plain")},
            data={"wait_seconds": "0"},
            headers=AUTH,
        )

    assert response.status_code == 202, response.text
    assert [meta.short_ok for meta in stored] == [False]
