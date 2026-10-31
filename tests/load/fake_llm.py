"""A fake OpenAI-compatible server for the load test: the service's structured-output and
semantic-chunking calls get valid answers after a fixed delay, and their prompt and completion
bytes are counted per route for the LLM cost note (ruling P11-S6).

    python -m tests.load.fake_llm --host 0.0.0.0 --port 8000

- ``POST /v1/chat/completions``: a chat completion whose message is the smallest JSON value
  that fits the request's ``response_format`` schema (``null`` where the schema allows it,
  else an empty string, 0, ``false``, the first enum value or an empty array).
- ``POST /v1/embeddings``: one vector per input, derived from the text's hash (equal texts
  get equal vectors).
- ``GET /stats``: per route so far the requests, the prompt bytes and the completion bytes.
  A chat's prompt is its whole request body (P11-S6 counts from the request bodies), its
  completion the answer's message content, never the JSON around it. An embedding's prompt is
  its input texts (what an embedding endpoint bills); it has no completion.

Both answers wait ``FAKE_LLM_DELAY_MS`` (default 800) first. Their ``usage`` approximates the
tokens as those UTF-8 bytes / 4.
"""

import argparse
import asyncio
import hashlib
import json
import os
import time
from collections.abc import Sequence
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

DEFAULT_DELAY_MS = 800
EMBEDDING_DIMENSIONS = 64
BYTES_PER_TOKEN = 4
ROUTES = ("chat_completions", "embeddings")


def instance(schema: Any) -> Any:
    """The smallest value that fits a JSON schema of the subset the service sends."""
    if not isinstance(schema, dict):
        return None
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    for key in ("anyOf", "oneOf"):
        if schema.get(key):
            return instance(schema[key][0])
    kind = schema.get("type")
    if isinstance(kind, list):
        if "null" in kind:
            return None
        kind = kind[0] if kind else None
    if kind == "object":
        properties = schema.get("properties") or {}
        return {name: instance(value) for name, value in properties.items()}
    return {"array": [], "string": "", "number": 0, "integer": 0, "boolean": False}.get(str(kind))


def _vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest() * 2  # 64 bytes
    return [(byte - 127.5) / 127.5 for byte in digest[:EMBEDDING_DIMENSIONS]]


def _tokens(size: int) -> int:
    return size // BYTES_PER_TOKEN


def create_app(delay_ms: int | None = None) -> Starlette:
    """The app; without ``delay_ms`` the delay is ``FAKE_LLM_DELAY_MS`` (default 800)."""
    delay = (
        delay_ms
        if delay_ms is not None
        else int(os.environ.get("FAKE_LLM_DELAY_MS", DEFAULT_DELAY_MS))
    ) / 1000
    counts = {route: {"requests": 0, "prompt_bytes": 0, "completion_bytes": 0} for route in ROUTES}

    def answer(
        route: str, prompt_bytes: int, completion_bytes: int, payload: dict[str, Any]
    ) -> Response:
        counts[route]["requests"] += 1
        counts[route]["prompt_bytes"] += prompt_bytes
        counts[route]["completion_bytes"] += completion_bytes
        return Response(json.dumps(payload).encode(), media_type="application/json")

    async def chat_completions(request: Request) -> Response:
        raw = await request.body()
        await asyncio.sleep(delay)
        body = json.loads(raw)
        response_format = body.get("response_format") or {}
        schema = (response_format.get("json_schema") or {}).get("schema")
        message = json.dumps(instance(schema)) if schema is not None else "{}"
        completion_bytes = len(message.encode())
        prompt, completion = _tokens(len(raw)), _tokens(completion_bytes)
        return answer(
            "chat_completions",
            len(raw),
            completion_bytes,
            {
                "id": f"chatcmpl-fake-{counts['chat_completions']['requests'] + 1}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.get("model", "fake"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": message},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "total_tokens": prompt + completion,
                },
            },
        )

    async def embeddings(request: Request) -> Response:
        raw = await request.body()
        await asyncio.sleep(delay)
        body = json.loads(raw)
        texts = body.get("input")
        texts = [texts] if isinstance(texts, str) else list(texts or [])
        prompt_bytes = sum(len(str(text).encode()) for text in texts)
        prompt = _tokens(prompt_bytes)
        return answer(
            "embeddings",
            prompt_bytes,
            0,
            {
                "object": "list",
                "model": body.get("model", "fake"),
                "data": [
                    {"object": "embedding", "index": index, "embedding": _vector(str(text))}
                    for index, text in enumerate(texts)
                ],
                "usage": {"prompt_tokens": prompt, "total_tokens": prompt},
            },
        )

    async def stats(request: Request) -> Response:
        return JSONResponse(counts)

    return Starlette(
        routes=[
            Route("/v1/chat/completions", chat_completions, methods=["POST"]),
            Route("/v1/embeddings", embeddings, methods=["POST"]),
            Route("/stats", stats, methods=["GET"]),
        ]
    )


def main(argv: Sequence[str] | None = None) -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
