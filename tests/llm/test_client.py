"""The OpenAI-compatible LLM client (``llm/client.py``, ruling S9) against pytest-httpserver as
the endpoint: the request it sends (strict JSON schema, model, limits), which key it uses, the
one repair call for an answer that does not fit, how the endpoint's failures map to ``LlmError``
(and that the endpoint's text and the key never reach it), embeddings in batches, and the metrics.
The SDK's own retries (429, 5xx) are left on, with a short ``retry-after-ms`` so they are quick."""

import asyncio
import json
from collections.abc import Callable
from typing import Any

import numpy as np
import pytest
from prometheus_client import CollectorRegistry
from pydantic import BaseModel, ValidationError
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from text_extraction.envelope import ErrorCode
from text_extraction.llm import client as client_module
from text_extraction.llm.client import LlmClient, LlmError, LlmUsage, truncate_input
from text_extraction.settings import Settings

ENV_KEY = "sk-env-key-4d2a"
HEADER_KEY = "sk-header-key-9b1c"
CHAT = "/v1/chat/completions"
EMBEDDINGS = "/v1/embeddings"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"title": {"type": ["string", "null"]}},
    "required": ["title"],
    "additionalProperties": False,
}


class Answer(BaseModel):
    title: str | None


def validate(parsed: Any) -> Answer:
    return Answer.model_validate(parsed)


def settings_for(server: HTTPServer, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "auth_mode": "none",
        "llm_base_url": server.url_for("/v1"),
        "llm_allow_http": True,
        "llm_api_key": ENV_KEY,
        "llm_embedding_model": "embed-1",
    }
    values.update(overrides)
    return Settings(**values)


def completion(
    content: str | None,
    *,
    finish_reason: str = "stop",
    prompt_tokens: int = 11,
    completion_tokens: int = 5,
) -> dict[str, Any]:
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
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def serve_chat(server: HTTPServer, *replies: dict[str, Any] | Response) -> None:
    """The endpoint answers the n-th chat request with the n-th reply (the last one repeats)."""
    seen = {"count": 0}

    def handle(_request: Request) -> Response:
        reply = replies[min(seen["count"], len(replies) - 1)]
        seen["count"] += 1
        return (
            reply
            if isinstance(reply, Response)
            else Response(json.dumps(reply), content_type="application/json")
        )

    server.expect_request(CHAT, method="POST").respond_with_handler(handle)


def sent(server: HTTPServer) -> list[Request]:
    return [request for request, _response in server.log]


def chat(
    settings: Settings,
    *,
    key: str | None = None,
    registry: CollectorRegistry | None = None,
    user: str = "the page",
) -> tuple[Answer, LlmUsage]:
    async def run() -> tuple[Answer, LlmUsage]:
        client = LlmClient(settings, key, registry=registry or CollectorRegistry())
        try:
            return await client.chat_json("system text", user, SCHEMA, validate=validate)
        finally:
            await client.aclose()

    return asyncio.run(run())


def error_response(status: int, body: str = "", **headers: str) -> Response:
    return Response(body, status=status, headers=headers, content_type="application/json")


def test_chat_json_sends_strict_schema_and_parses(httpserver: HTTPServer) -> None:
    serve_chat(httpserver, completion('{"title": "Hello"}'))
    settings = settings_for(httpserver, llm_model="my-model", llm_max_tokens=777)

    value, usage = chat(settings, user="the page")

    assert value == Answer(title="Hello")
    assert usage == LlmUsage(model="my-model", calls=1, prompt_tokens=11, completion_tokens=5)
    (request,) = sent(httpserver)
    body = request.get_json()
    assert body["model"] == "my-model"
    assert body["temperature"] == 0
    assert body["max_tokens"] == 777
    assert body["messages"] == [
        {"role": "system", "content": "system text"},
        {"role": "user", "content": "the page"},
    ]
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "extraction", "strict": True, "schema": SCHEMA},
    }


def test_header_key_wins_over_env_key(httpserver: HTTPServer) -> None:
    serve_chat(httpserver, completion('{"title": null}'))

    chat(settings_for(httpserver), key=HEADER_KEY)

    (request,) = sent(httpserver)
    assert request.headers["Authorization"] == f"Bearer {HEADER_KEY}"


def test_env_key_used_without_header(httpserver: HTTPServer) -> None:
    serve_chat(httpserver, completion('{"title": null}'))

    chat(settings_for(httpserver), key=None)

    (request,) = sent(httpserver)
    assert request.headers["Authorization"] == f"Bearer {ENV_KEY}"


def test_no_key_at_all_is_refused_before_any_request(httpserver: HTTPServer) -> None:
    settings = settings_for(httpserver, llm_api_key=None)

    with pytest.raises(LlmError) as error:
        LlmClient(settings, None, registry=CollectorRegistry())

    assert error.value.code == ErrorCode.LLM_FAILED
    assert error.value.retryable is False
    assert sent(httpserver) == []


def test_invalid_json_gets_one_repair_call_then_fails(httpserver: HTTPServer) -> None:
    serve_chat(httpserver, completion("this is not json"))

    with pytest.raises(LlmError) as error:
        chat(settings_for(httpserver))

    assert error.value.code == ErrorCode.LLM_FAILED
    assert error.value.retryable is False
    assert error.value.message == "The LLM answer did not match the schema."
    assert str(error.value) == error.value.message
    first, second = sent(httpserver)
    assert len(first.get_json()["messages"]) == 2
    repair = second.get_json()["messages"]
    # system, user, the invalid answer as the model's own turn, the error as a user message
    assert [message["role"] for message in repair] == ["system", "user", "assistant", "user"]
    assert repair[:2] == first.get_json()["messages"]
    assert repair[2]["content"] == "this is not json"
    assert "Expecting value" in repair[3]["content"]  # the parser's error text


def test_validation_error_is_repaired_by_the_second_call(httpserver: HTTPServer) -> None:
    serve_chat(httpserver, completion('{"title": 5}'), completion('{"title": "Five"}'))

    value, usage = chat(settings_for(httpserver))

    assert value == Answer(title="Five")
    assert usage.calls == 2
    assert usage.prompt_tokens == 22
    assert usage.completion_tokens == 10
    repair = sent(httpserver)[1].get_json()["messages"]
    assert [message["role"] for message in repair] == ["system", "user", "assistant", "user"]
    assert repair[2]["content"] == '{"title": 5}'
    assert "title" in repair[3]["content"]  # the failing field
    assert "5" not in repair[3]["content"]  # not the invalid input echoed in the error


def test_validate_may_raise_pydantic_validation_error_or_value_error(
    httpserver: HTTPServer,
) -> None:
    serve_chat(httpserver, completion('{"title": "x"}'))

    def refuse(_parsed: Any) -> Answer:
        raise ValueError("title is not allowed")

    async def run() -> None:
        client = LlmClient(settings_for(httpserver), None, registry=CollectorRegistry())
        try:
            await client.chat_json("s", "u", SCHEMA, validate=refuse)
        finally:
            await client.aclose()

    with pytest.raises(LlmError):
        asyncio.run(run())
    assert "title is not allowed" in sent(httpserver)[1].get_json()["messages"][3]["content"]
    with pytest.raises(ValidationError):
        validate({"title": 5})


def test_length_finish_reason_is_invalid(httpserver: HTTPServer) -> None:
    """A cut-off answer may even parse; it is invalid all the same."""
    serve_chat(
        httpserver,
        completion('{"title": "Cut"}', finish_reason="length"),
        completion('{"title": "Whole"}'),
    )

    value, usage = chat(settings_for(httpserver))

    assert value == Answer(title="Whole")
    assert usage.calls == 2
    assert len(sent(httpserver)) == 2


def test_empty_choices_is_invalid(httpserver: HTTPServer) -> None:
    empty = completion(None)
    empty["choices"] = []
    serve_chat(httpserver, empty)

    with pytest.raises(LlmError) as error:
        chat(settings_for(httpserver))

    assert error.value.code == ErrorCode.LLM_FAILED
    assert error.value.retryable is False
    assert len(sent(httpserver)) == 2


def test_refusal_without_content_is_invalid(httpserver: HTTPServer) -> None:
    serve_chat(httpserver, completion(None), completion('{"title": "Ok"}'))

    value, _usage = chat(settings_for(httpserver))

    assert value == Answer(title="Ok")
    # Nothing to show the model: no assistant turn, only the error as a user message.
    repair = sent(httpserver)[1].get_json()["messages"]
    assert [message["role"] for message in repair] == ["system", "user", "user"]


def test_cut_off_answer_is_shown_to_the_model_in_the_repair_call(httpserver: HTTPServer) -> None:
    serve_chat(
        httpserver,
        completion('{"title": "Cu', finish_reason="length"),
        completion('{"title": "Whole"}'),
    )

    chat(settings_for(httpserver))

    repair = sent(httpserver)[1].get_json()["messages"]
    assert [message["role"] for message in repair] == ["system", "user", "assistant", "user"]
    assert repair[2]["content"] == '{"title": "Cu'
    assert "cut off" in repair[3]["content"]


def test_429_then_200_succeeds(httpserver: HTTPServer) -> None:
    serve_chat(
        httpserver,
        error_response(429, '{"error": {"message": "slow down"}}', **{"retry-after-ms": "10"}),
        completion('{"title": "Later"}'),
    )

    value, usage = chat(settings_for(httpserver))

    assert value == Answer(title="Later")
    assert usage.calls == 1  # the SDK's retry is not a call of ours
    assert len(sent(httpserver)) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_not_retryable_and_message_has_status_only(
    httpserver: HTTPServer, status: int
) -> None:
    echoed = f"Incorrect API key provided: {HEADER_KEY}"
    serve_chat(httpserver, error_response(status, json.dumps({"error": {"message": echoed}})))

    with pytest.raises(LlmError) as error:
        chat(settings_for(httpserver), key=HEADER_KEY)

    assert error.value.code == ErrorCode.LLM_FAILED
    assert error.value.retryable is False
    assert str(status) in error.value.message
    assert "Incorrect" not in error.value.message
    assert HEADER_KEY not in error.value.message
    # The SDK's exception, which quotes the endpoint's text, is not the cause or the context.
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert len(sent(httpserver)) == 1  # never repeated: a repair call is for bad answers only


def test_persistent_503_is_retryable(httpserver: HTTPServer) -> None:
    serve_chat(httpserver, error_response(503, "busy", **{"retry-after-ms": "10"}))

    with pytest.raises(LlmError) as error:
        chat(settings_for(httpserver))

    assert error.value.retryable is True
    assert "503" in error.value.message
    assert len(sent(httpserver)) == 3  # the SDK's two retries


def test_timeout_is_retryable(httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch) -> None:
    """``LLM_TIMEOUT_SECONDS`` is at least 5; ``model_copy`` skips the bounds so the test is
    quick, and the SDK's retries (a timeout is retried too) are switched off."""
    monkeypatch.setattr(client_module, "MAX_RETRIES", 0)
    import time

    def slow(_request: Request) -> Response:
        time.sleep(0.8)
        return Response(json.dumps(completion('{"title": null}')))

    httpserver.expect_request(CHAT, method="POST").respond_with_handler(slow)
    settings = settings_for(httpserver).model_copy(update={"llm_timeout_seconds": 0.3})

    with pytest.raises(LlmError) as error:
        chat(settings)

    assert error.value.code == ErrorCode.LLM_FAILED
    assert error.value.retryable is True


def test_unreachable_endpoint_is_retryable(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "MAX_RETRIES", 0)
    settings = settings_for(httpserver, llm_base_url="http://127.0.0.1:1/v1")

    with pytest.raises(LlmError) as error:
        chat(settings)

    assert error.value.retryable is True


def test_redirect_is_a_failure_and_the_key_is_not_sent_on(httpserver: HTTPServer) -> None:
    serve_chat(httpserver, Response("", status=307, headers={"Location": "http://127.0.0.1:1/x"}))

    with pytest.raises(LlmError) as error:
        chat(settings_for(httpserver))

    assert error.value.retryable is False
    assert "307" in error.value.message


def test_non_json_success_body_is_an_error(httpserver: HTTPServer) -> None:
    serve_chat(httpserver, Response("<html>gateway</html>", content_type="text/html"))

    with pytest.raises(LlmError) as error:
        chat(settings_for(httpserver))

    assert error.value.code == ErrorCode.LLM_FAILED
    assert "gateway" not in error.value.message


def embed_handler(seen: list[int]) -> Callable[[Request], Response]:
    """Answers each input ``"<n>:..."`` with the vector ``[n]``, in reverse order."""

    def handle(request: Request) -> Response:
        body = request.get_json()
        inputs = body["input"]
        seen.append(len(inputs))
        data = [
            {"object": "embedding", "index": index, "embedding": [float(text.split(":")[0])]}
            for index, text in enumerate(inputs)
        ]
        return Response(
            json.dumps(
                {
                    "object": "list",
                    "model": body["model"],
                    "data": list(reversed(data)),
                    "usage": {"prompt_tokens": len(inputs), "total_tokens": len(inputs)},
                }
            ),
            content_type="application/json",
        )

    return handle


def test_embed_batches_of_64_and_cuts_texts(httpserver: HTTPServer) -> None:
    sizes: list[int] = []
    httpserver.expect_request(EMBEDDINGS, method="POST").respond_with_handler(embed_handler(sizes))
    texts = [f"{index}:" + "x" * 3000 for index in range(130)]
    registry = CollectorRegistry()

    async def run() -> list[Any]:
        client = LlmClient(settings_for(httpserver), None, registry=registry)
        try:
            return [batch async for batch in client.embed_batches(texts)]
        finally:
            await client.aclose()

    batches = asyncio.run(run())

    # One float32 array per request (ruling R22), the vectors in input order.
    assert [batch.shape for batch in batches] == [(64, 1), (64, 1), (2, 1)]
    assert all(batch.dtype == np.float32 for batch in batches)
    assert np.concatenate(batches).ravel().tolist() == [float(index) for index in range(130)]
    assert sizes == [64, 64, 2]
    bodies = [request.get_json() for request in sent(httpserver)]
    assert {body["model"] for body in bodies} == {"embed-1"}
    assert all(len(text) == 2000 for body in bodies for text in body["input"])
    assert (
        registry.get_sample_value(
            "text_extraction_llm_calls_total", {"kind": "embeddings", "outcome": "ok"}
        )
        == 3
    )


def test_embed_without_texts_calls_nothing(httpserver: HTTPServer) -> None:
    async def run() -> list[Any]:
        client = LlmClient(settings_for(httpserver), None, registry=CollectorRegistry())
        try:
            return [batch async for batch in client.embed_batches([])]
        finally:
            await client.aclose()

    assert asyncio.run(run()) == []
    assert sent(httpserver) == []


def test_embed_needs_an_embedding_model(httpserver: HTTPServer) -> None:
    async def run() -> None:
        client = LlmClient(
            settings_for(httpserver, llm_embedding_model=None), None, registry=CollectorRegistry()
        )
        try:
            [batch async for batch in client.embed_batches(["a"])]
        finally:
            await client.aclose()

    with pytest.raises(LlmError) as error:
        asyncio.run(run())

    assert error.value.retryable is False
    assert sent(httpserver) == []


def test_embed_with_a_wrong_number_of_vectors_fails(httpserver: HTTPServer) -> None:
    httpserver.expect_request(EMBEDDINGS, method="POST").respond_with_json(
        {
            "object": "list",
            "model": "m",
            "data": [],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }
    )

    async def run() -> None:
        client = LlmClient(settings_for(httpserver), None, registry=CollectorRegistry())
        try:
            [batch async for batch in client.embed_batches(["a", "b"])]
        finally:
            await client.aclose()

    with pytest.raises(LlmError) as error:
        asyncio.run(run())

    assert error.value.retryable is False


def test_usage_and_metrics_counted(httpserver: HTTPServer) -> None:
    serve_chat(
        httpserver,
        completion("nope", prompt_tokens=100, completion_tokens=7),
        completion('{"title": "Yes"}', prompt_tokens=120, completion_tokens=9),
    )
    registry = CollectorRegistry()

    _value, usage = chat(settings_for(httpserver), registry=registry)

    assert usage == LlmUsage(model="gpt-6-luna", calls=2, prompt_tokens=220, completion_tokens=16)
    calls = "text_extraction_llm_calls_total"
    assert registry.get_sample_value(calls, {"kind": "chat", "outcome": "invalid"}) == 1
    assert registry.get_sample_value(calls, {"kind": "chat", "outcome": "ok"}) == 1
    assert registry.get_sample_value(calls, {"kind": "chat", "outcome": "error"}) in (None, 0)
    tokens = "text_extraction_llm_tokens_total"
    assert registry.get_sample_value(tokens, {"kind": "prompt"}) == 220
    assert registry.get_sample_value(tokens, {"kind": "completion"}) == 16


def test_an_answer_without_usage_counts_zero_tokens(httpserver: HTTPServer) -> None:
    """Compatible servers do not all report usage."""
    bare = completion('{"title": null}')
    del bare["usage"]
    serve_chat(httpserver, bare)

    _value, usage = chat(settings_for(httpserver))

    assert usage == LlmUsage(model="gpt-6-luna", calls=1, prompt_tokens=0, completion_tokens=0)


def test_failed_calls_are_counted_as_errors(httpserver: HTTPServer) -> None:
    serve_chat(httpserver, error_response(401, "no"))
    registry = CollectorRegistry()

    with pytest.raises(LlmError):
        chat(settings_for(httpserver), registry=registry)

    assert (
        registry.get_sample_value(
            "text_extraction_llm_calls_total", {"kind": "chat", "outcome": "error"}
        )
        == 1
    )


def test_two_clients_share_the_metrics_of_one_registry(httpserver: HTTPServer) -> None:
    serve_chat(httpserver, completion('{"title": null}'))
    registry = CollectorRegistry()

    chat(settings_for(httpserver), registry=registry)
    chat(settings_for(httpserver), registry=registry)  # would raise on a duplicate registration

    assert (
        registry.get_sample_value(
            "text_extraction_llm_calls_total", {"kind": "chat", "outcome": "ok"}
        )
        == 2
    )


def test_truncate_input_cuts_at_paragraph() -> None:
    text = "First paragraph.\n\nSecond paragraph is here.\nWith a line.\n\nThird one is long."

    cut, truncated = truncate_input(text, 60)

    assert truncated is True
    assert cut == "First paragraph.\n\nSecond paragraph is here.\nWith a line."


def test_truncate_input_falls_back_to_line_break_then_space_then_hard_cut() -> None:
    assert truncate_input("one line\nsecond line\nthird", 22) == ("one line\nsecond line", True)
    assert truncate_input("alpha beta gamma", 12) == ("alpha beta", True)
    assert truncate_input("abcdefghij", 6) == ("abcdef", True)


def test_truncate_input_leaves_a_text_that_fits() -> None:
    assert truncate_input("short text", 10) == ("short text", False)
    assert truncate_input("short text", 100) == ("short text", False)
    assert truncate_input("", 5) == ("", False)
