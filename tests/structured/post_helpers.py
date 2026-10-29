"""Shared fakes of the post-stage tests: results and envelopes, settings with an LLM endpoint
(pytest-httpserver, OpenAI-compatible) and the replies it gives."""

import json
from typing import Any

from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from text_extraction.compliance import ComplianceReport
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractResult, Timings
from text_extraction.settings import Settings
from text_extraction.structured.types import StructuredResult

CHAT = "/v1/chat/completions"
EMBEDDINGS = "/v1/embeddings"
URL = "https://shop.example.org/item/1"
ENV_KEY = "sk-env-key-77aa"


def schema(**properties: Any) -> dict[str, Any]:
    return {"type": "object", "properties": properties}


STRING: dict[str, Any] = {"type": "string"}
NUMBER: dict[str, Any] = {"type": "number"}
STRINGS: dict[str, Any] = {"type": "array", "items": {"type": "string"}}


def result(
    content: str | None,
    *,
    structured: StructuredResult | None = None,
    format: str = "markdown",
    url: str | None = URL,
    content_type: str = "text/html",
    llm_ms: int = 0,
) -> ExtractResult:
    return ExtractResult(
        url=url,
        final_url=url,
        upstream_status=200 if url else None,
        content_type=content_type,
        mode_used="fast" if url else "upload",
        attempts=1 if url else 0,
        language="en",
        word_count=len((content or "").split()),
        title=None,
        format=format,  # type: ignore[arg-type]
        content=content,
        quality=None,
        timings=Timings(total_ms=5, compliance_ms=0, fetch_ms=1, convert_ms=1, llm_ms=llm_ms),
        compliance=ComplianceReport(mode="report"),
        metadata=None,
        links=None,
        fingerprint=None,
        cache="miss",
        warnings=[],
        structured=structured,
    )


def envelope(
    content: str | None,
    *,
    status: str = "success",
    structured: StructuredResult | None = None,
    **fields: Any,
) -> Envelope[ExtractResult]:
    return Envelope[ExtractResult](
        status=status,  # type: ignore[arg-type]
        data=result(content, structured=structured, **fields),
    )


def dom_result(values: dict[str, Any], **fields: Any) -> StructuredResult:
    """What the DOM stage left: the values found (None: missing)."""
    sources = fields.pop("sources", {name: "metadata" for name, v in values.items() if v})
    return StructuredResult(
        values=values,
        sources=sources,
        missing=[name for name, value in values.items() if value is None],
        stage="dom",
        **fields,
    )


def llm_settings(server: HTTPServer | None = None, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "auth_mode": "none",
        "llm_enabled": server is not None,
        "llm_base_url": server.url_for("/v1") if server is not None else "https://llm.invalid/v1",
        "llm_allow_http": True,
        "llm_api_key": ENV_KEY,
        "llm_embedding_model": "embed-1",
    }
    values.update(overrides)
    return Settings(**values)


def completion(content: str | None, *, finish_reason: str = "stop") -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-6-luna",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
    }


def answer(values: dict[str, Any]) -> dict[str, Any]:
    return completion(json.dumps(values))


def serve_chat(server: HTTPServer, *replies: dict[str, Any] | Response) -> None:
    """The endpoint answers the n-th chat request with the n-th reply (the last one repeats)."""
    seen = {"count": 0}

    def handle(_request: Request) -> Response:
        reply = replies[min(seen["count"], len(replies) - 1)]
        seen["count"] += 1
        if isinstance(reply, Response):
            return reply
        return Response(json.dumps(reply), content_type="application/json")

    server.expect_request(CHAT, method="POST").respond_with_handler(handle)


def serve_embeddings(server: HTTPServer, vector_of: Any) -> None:
    """Embeddings: ``vector_of(text)`` for each input text."""

    def handle(request: Request) -> Response:
        texts = request.get_json()["input"]
        data = [
            {"object": "embedding", "index": index, "embedding": vector_of(text)}
            for index, text in enumerate(texts)
        ]
        body = {
            "object": "list",
            "data": data,
            "model": "embed-1",
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }
        return Response(json.dumps(body), content_type="application/json")

    server.expect_request(EMBEDDINGS, method="POST").respond_with_handler(handle)


def chat_requests(server: HTTPServer) -> list[dict[str, Any]]:
    return [request.get_json() for request, _ in server.log if request.path == CHAT]


def error_response(status: int) -> Response:
    return Response(
        '{"error": {"message": "nope"}}', status=status, content_type="application/json"
    )
