"""The load test's fake OpenAI-compatible server (`tests/load/fake_llm.py`): answers the
service's LLM calls with valid OpenAI-shaped JSON after a fixed delay and counts the bytes of
each route, for the LLM cost note (P11-S6)."""

import json
import time

import pytest
from openai.types import CreateEmbeddingResponse
from openai.types.chat import ChatCompletion
from starlette.testclient import TestClient

from tests.load.fake_llm import create_app

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": ["string", "null"]},
        "price": {"type": "number"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "kind": {"type": "string", "enum": ["course", "worksheet"]},
        "author": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "verified": {"type": "boolean"}},
            "required": ["name", "verified"],
            "additionalProperties": False,
        },
    },
    "required": ["title", "price", "tags", "kind", "author"],
    "additionalProperties": False,
}


def chat_body(text: str) -> dict[str, object]:
    return {
        "model": "test-model",
        "messages": [{"role": "system", "content": "Extract."}, {"role": "user", "content": text}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "fields", "strict": True, "schema": SCHEMA},
        },
        "temperature": 0,
        "max_tokens": 500,
    }


def test_fake_llm_counts_request_bytes() -> None:
    first = json.dumps(chat_body("a page")).encode()
    second = json.dumps(chat_body("another, longer page")).encode()
    embeddings = json.dumps({"model": "embed", "input": ["one", "two"]}).encode()
    headers = {"Content-Type": "application/json"}

    with TestClient(create_app(delay_ms=0)) as client:
        assert client.get("/stats").json() == {
            "chat_completions": {"requests": 0, "request_bytes": 0, "response_bytes": 0},
            "embeddings": {"requests": 0, "request_bytes": 0, "response_bytes": 0},
        }
        answers = [
            client.post("/v1/chat/completions", content=first, headers=headers),
            client.post("/v1/chat/completions", content=second, headers=headers),
            client.post("/v1/embeddings", content=embeddings, headers=headers),
        ]
        stats = client.get("/stats").json()

    assert [answer.status_code for answer in answers] == [200, 200, 200]
    assert stats["chat_completions"]["requests"] == 2
    assert stats["chat_completions"]["request_bytes"] == len(first) + len(second)
    assert stats["chat_completions"]["response_bytes"] == sum(
        len(answer.content) for answer in answers[:2]
    )
    assert stats["embeddings"] == {
        "requests": 1,
        "request_bytes": len(embeddings),
        "response_bytes": len(answers[2].content),
    }


def test_chat_answer_is_an_openai_completion_that_fits_the_schema() -> None:
    body = json.dumps(chat_body("a page")).encode()

    with TestClient(create_app(delay_ms=0)) as client:
        answer = client.post("/v1/chat/completions", content=body)

    completion = ChatCompletion.model_validate(answer.json())
    content = completion.choices[0].message.content
    assert content is not None
    value = json.loads(content)
    assert value == {
        "title": None,
        "price": 0,
        "tags": [],
        "kind": "course",
        "author": {"name": "", "verified": False},
    }
    assert completion.choices[0].finish_reason == "stop"
    assert completion.usage is not None
    # Tokens are approximated as UTF-8 bytes / 4 (P11-S6).
    assert completion.usage.prompt_tokens == len(body) // 4
    assert completion.usage.completion_tokens == len(content.encode()) // 4


def test_embeddings_answer_one_vector_per_input_in_order() -> None:
    with TestClient(create_app(delay_ms=0)) as client:
        answer = client.post("/v1/embeddings", json={"model": "embed", "input": ["a", "b", "a"]})
        single = client.post("/v1/embeddings", json={"model": "embed", "input": "one text"})

    reply = CreateEmbeddingResponse.model_validate(answer.json())
    assert [item.index for item in reply.data] == [0, 1, 2]
    vectors = [item.embedding for item in reply.data]
    assert vectors[0] == vectors[2]  # the same text, the same vector
    assert vectors[0] != vectors[1]
    assert all(any(value != 0 for value in vector) for vector in vectors)
    assert len(CreateEmbeddingResponse.model_validate(single.json()).data) == 1


def test_the_delay_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_LLM_DELAY_MS", "150")

    with TestClient(create_app()) as client:
        started = time.monotonic()
        client.post("/v1/embeddings", json={"model": "embed", "input": ["a"]})
        elapsed = time.monotonic() - started

    assert elapsed >= 0.15
