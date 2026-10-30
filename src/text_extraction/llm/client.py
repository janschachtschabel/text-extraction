"""The client of the operator's OpenAI-compatible LLM endpoint (ruling S9): JSON answers that
fit a schema (``chat_json``, and ``describe_image``: the same with one image, ruling V-S9) and
embeddings (``embed_batches``, float32 batches). It is generic: the extraction prompt is built on
top of it (``structured/post.py``, ``structured/learn.py``, ``vision/prompts.py``).

**The endpoint** is operator configuration (``LLM_BASE_URL``, https unless ``LLM_ALLOW_HTTP``),
not a caller's target, so the SSRF guard does not apply. As for the external ASR endpoint it is
called directly: ``trust_env=False`` (no proxy or ``.netrc`` from the environment), no redirects
(a ``3xx`` is a failure, so a request with the key never goes to another host). The SDK retries
429, 5xx, timeouts and connection errors twice with back-off (``MAX_RETRIES``); a retry is not a
call of ours. ``LLM_TIMEOUT_SECONDS`` is the SDK's timeout of one request.

**The key:** the caller's (``X-LLM-Api-Key``, handed in as ``key``) wins over ``LLM_API_KEY``
(rule 3). The environment's ``OPENAI_API_KEY`` is never consulted; without any key
``LlmClient`` raises ``LlmError`` before a request is made.

**The SDK's environment** (issue #19): the key, the base URL, the timeout and the retries are
passed, so the SDK does not read them. It reads ``OPENAI_ORG_ID`` and ``OPENAI_PROJECT_ID``
whatever it is given (an empty value would go out as an empty header), so their headers are
omitted explicitly; the ``Authorization`` header is set explicitly too, which drops one from
``OPENAI_CUSTOM_HEADERS``. That variable and ``OPENAI_LOG`` are read in any case: start-up
refuses them while ``LLM_ENABLED`` is true (``settings.OPENAI_ENV_REFUSED``).

**Answers that do not fit:** ``chat_json`` parses the answer and hands it to ``validate``
(which raises ``ValueError``, a pydantic ``ValidationError`` included). An answer that is no
JSON, does not validate, has no choice, no content or ``finish_reason: length`` (cut off) is
invalid; it gets exactly one more call, with the model's invalid answer appended as an assistant
turn (when it gave one) and the error text as a user message, then
``LlmError(llm_failed, retryable=False)``.

**Failures** are ``LlmError`` with a fixed message that names the HTTP status at most, never the
endpoint's text (which may echo the key or the prompt). The SDK's exception is not kept as the
error's cause or context, for the same reason. 400, 401, 403, 404, 422 and other 4xx answers
are not retryable; 408, 409, 425, 429, 5xx, timeouts and connection errors are.

Prompts, documents, images, answers and keys are never logged and never a metric label: the metrics
count calls (``text_extraction_llm_calls_total{kind, outcome}``) and tokens
(``text_extraction_llm_tokens_total{kind}``) only.
"""

import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, Final, cast
from weakref import WeakKeyDictionary

import httpx2
import numpy as np
import numpy.typing as npt
import openai
from openai.types import CreateEmbeddingResponse
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessageParam,
    ChatCompletionUserMessageParam,
)
from openai.types.shared_params import ResponseFormatJSONSchema
from prometheus_client import REGISTRY, CollectorRegistry, Counter
from pydantic import ValidationError

from text_extraction.llm.basics import LlmError, truncate_input
from text_extraction.llm.usage import LlmUsage
from text_extraction.settings import Settings
from text_extraction.vision.images import data_url

__all__ = [
    "EMBED_BATCH",
    "EMBED_TEXT_CHARS",
    "MAX_RETRIES",
    "LlmClient",
    "LlmError",
    "LlmUsage",
    "truncate_input",
]

# The SDK's retries of 429, 5xx, timeouts and connection errors (tests set it to 0).
MAX_RETRIES: Final = 2
EMBED_BATCH: Final = 64  # texts of one embeddings request
EMBED_TEXT_CHARS: Final = 2000  # a text of ``embed_batches`` is cut to this
_RETRYABLE: Final = frozenset({408, 409, 425, 429})
_REPAIR_ERROR_CHARS: Final = 1000  # of the error text that goes back to the model
_NO_ANSWER: Final = "Your previous answer was empty."
_CUT_OFF: Final = "Your previous answer was cut off because it was too long."
_INVALID: Final = "Your previous answer was invalid."


@dataclass(frozen=True)
class _Metrics:
    calls: Counter
    tokens: Counter


_METRICS: WeakKeyDictionary[CollectorRegistry, _Metrics] = WeakKeyDictionary()


def _metrics_for(registry: CollectorRegistry) -> _Metrics:
    """The counters of ``registry``; a client is made per request (the key differs), so they are
    registered once per registry."""
    metrics = _METRICS.get(registry)
    if metrics is None:
        metrics = _Metrics(
            calls=Counter(
                "text_extraction_llm_calls_total",
                "Calls to the LLM endpoint by kind (chat, vision, embeddings) and outcome "
                "(ok, invalid answer, error).",
                ["kind", "outcome"],
                registry=registry,
            ),
            tokens=Counter(
                "text_extraction_llm_tokens_total",
                "Tokens the LLM endpoint reported, by kind (prompt, completion).",
                ["kind"],
                registry=registry,
            ),
        )
        _METRICS[registry] = metrics
    return metrics


class LlmClient:
    """One caller's client of the LLM endpoint. Make one per request (or job item) with that
    caller's ``key`` and close it with ``aclose``; see the module docstring."""

    def __init__(
        self, settings: Settings, key: str | None, *, registry: CollectorRegistry | None = None
    ) -> None:
        secret = settings.llm_api_key.get_secret_value() if settings.llm_api_key else None
        api_key = key or secret
        if not api_key:
            raise LlmError("No LLM API key is available.", retryable=False)
        self._model = settings.llm_model
        self._vision_model = settings.llm_vision_model or settings.llm_model
        self._embedding_model = settings.llm_embedding_model
        self._temperature = settings.llm_temperature
        self._max_tokens = settings.llm_max_tokens
        self._metrics = _metrics_for(registry if registry is not None else REGISTRY)
        timeout = settings.llm_timeout_seconds
        self._http = httpx2.AsyncClient(follow_redirects=False, trust_env=False, timeout=timeout)
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=settings.llm_base_url,
            timeout=timeout,
            max_retries=MAX_RETRIES,
            # Typed as str values, but the SDK drops an omitted header (see the docstring).
            default_headers=cast(
                Mapping[str, str],
                {
                    "Authorization": f"Bearer {api_key}",
                    "OpenAI-Organization": openai.omit,
                    "OpenAI-Project": openai.omit,
                },
            ),
            http_client=self._http,
        )

    async def aclose(self) -> None:
        await self._client.close()
        await self._http.aclose()

    async def chat_json[T](
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        *,
        validate: Callable[[Any], T],
    ) -> tuple[T, LlmUsage]:
        """The model's answer to ``system`` and ``user``, as JSON that fits ``schema`` (strict
        structured output), parsed and checked by ``validate``; see the module docstring for the
        repair call. The usage is summed over both calls."""
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        return await self._chat_json(
            messages, schema, name="extraction", model=self._model, kind="chat", validate=validate
        )

    async def describe_image[T](
        self,
        system: str,
        text: str,
        image: bytes,
        media_type: str,
        schema: dict[str, Any],
        *,
        validate: Callable[[Any], T],
    ) -> tuple[T, LlmUsage]:
        """The model's answer to ``system``, ``text`` and one ``image`` (an ``image_url`` part
        with a base64 data URL; no ``detail``, so the endpoint's default applies), as for
        ``chat_json`` but with ``LLM_VISION_MODEL`` (else ``LLM_MODEL``), the schema named
        ``image_description`` and calls counted as kind ``vision``. The repair call sends the
        image again. ``media_type`` is one of ``vision.types.VisionMediaType``."""
        user: ChatCompletionUserMessageParam = {
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": data_url(image, media_type)}},
            ],
        }
        messages: list[ChatCompletionMessageParam] = [{"role": "system", "content": system}, user]
        return await self._chat_json(
            messages,
            schema,
            name="image_description",
            model=self._vision_model,
            kind="vision",
            validate=validate,
        )

    async def _chat_json[T](
        self,
        messages: list[ChatCompletionMessageParam],
        schema: dict[str, Any],
        *,
        name: str,
        model: str,
        kind: str,
        validate: Callable[[Any], T],
    ) -> tuple[T, LlmUsage]:
        """The loop of ``chat_json`` and ``describe_image``: a call, and one repair call for an
        answer that does not fit. ``messages`` grows by the repair turns."""
        response_format: ResponseFormatJSONSchema = {
            "type": "json_schema",
            "json_schema": {"name": name, "strict": True, "schema": schema},
        }
        calls = prompt_tokens = completion_tokens = 0
        for attempt in (1, 2):
            reply = await self._call(
                lambda: self._client.chat.completions.create(
                    model=model,
                    messages=messages,
                    response_format=response_format,
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                ),
                ChatCompletion,
                kind=kind,
            )
            calls += 1
            used_prompt, used_completion = _tokens(
                reply.usage, "prompt_tokens", "completion_tokens"
            )
            prompt_tokens += used_prompt
            completion_tokens += used_completion
            self._metrics.tokens.labels("prompt").inc(used_prompt)
            self._metrics.tokens.labels("completion").inc(used_completion)
            try:
                value = _checked(reply, validate)
            except _Invalid as invalid:
                self._metrics.calls.labels(kind, "invalid").inc()
                if attempt == 1:
                    if invalid.answer is not None:  # the model sees what it got wrong
                        messages.append({"role": "assistant", "content": invalid.answer})
                    messages.append({"role": "user", "content": _repair_request(invalid.problem)})
                continue
            self._metrics.calls.labels(kind, "ok").inc()
            usage = LlmUsage(
                model=model,
                calls=calls,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            return value, usage
        raise LlmError("The LLM answer did not match the schema.", retryable=False)

    async def embed_batches(self, texts: Sequence[str]) -> AsyncGenerator[npt.NDArray[np.float32]]:
        """The embeddings of the texts (each cut to ``EMBED_TEXT_CHARS`` characters), in order,
        with ``LLM_EMBEDDING_MODEL``: one request of ``EMBED_BATCH`` texts at a time, each
        answer yielded as a float32 array of one row per text (ruling R22: the caller keeps
        what it needs of a batch, never all vectors). Vectors of different lengths in one answer
        are a ``ValueError``."""
        if not texts:
            return
        if not self._embedding_model:
            raise LlmError("No LLM embedding model is configured.", retryable=False)
        model = self._embedding_model
        for start in range(0, len(texts), EMBED_BATCH):
            batch = [text[:EMBED_TEXT_CHARS] for text in texts[start : start + EMBED_BATCH]]
            reply = await self._call(
                partial(
                    self._client.embeddings.create,
                    model=model,
                    input=batch,
                    encoding_format="float",
                ),
                CreateEmbeddingResponse,
                kind="embeddings",
            )
            self._metrics.tokens.labels("prompt").inc(_tokens(reply.usage, "prompt_tokens")[0])
            found = sorted(reply.data, key=lambda item: item.index)
            if [item.index for item in found] != list(range(len(batch))):
                self._metrics.calls.labels("embeddings", "invalid").inc()
                raise LlmError("The LLM embeddings did not match the request.", retryable=False)
            self._metrics.calls.labels("embeddings", "ok").inc()
            vectors = np.asarray([item.embedding for item in found], dtype=np.float32)
            del reply, found  # the SDK's Python floats go before the next request
            yield vectors

    async def _call[R](
        self, make: Callable[[], Awaitable[R]], expected: type[R], *, kind: str
    ) -> R:
        """One request, with the SDK's failures mapped to ``LlmError`` outside its exception. A
        body that is no JSON object comes back from the SDK as a string: unreadable."""
        failure: LlmError | None = None
        try:
            reply = await make()
            if isinstance(reply, expected):
                return reply
            failure = LlmError("The LLM endpoint answered something unreadable.", retryable=False)
        except openai.OpenAIError as error:
            failure = _failure_of(error)
        except httpx2.HTTPError:
            failure = LlmError("The LLM endpoint answered something unreadable.", retryable=False)
        self._metrics.calls.labels(kind, "error").inc()
        raise failure


def _failure_of(error: openai.OpenAIError) -> LlmError:
    """The ``LlmError`` of an SDK failure; its text is fixed (see the module docstring)."""
    if isinstance(error, openai.APITimeoutError):
        return LlmError("The LLM endpoint did not answer in time.", retryable=True)
    if isinstance(error, openai.APIConnectionError):
        return LlmError("The LLM endpoint could not be reached.", retryable=True)
    if isinstance(error, openai.APIStatusError):
        status = error.status_code
        return LlmError(
            f"The LLM endpoint answered HTTP {status}.",
            retryable=status in _RETRYABLE or status >= 500,
            status=status,
        )
    return LlmError("The LLM endpoint answered something unreadable.", retryable=False)


def _tokens(usage: object, *names: str) -> tuple[int, ...]:
    """The token counts ``names`` of a reply's ``usage``; 0 for what an endpoint leaves out (the
    SDK does not insist on the fields of a compatible server)."""
    counts = [getattr(usage, name, 0) for name in names]
    return tuple(count if isinstance(count, int) and count > 0 else 0 for count in counts)


class _Invalid(Exception):  # noqa: N818 - internal control flow
    """An answer that does not fit. ``problem`` is what the repair call tells the model;
    ``answer`` is the model's invalid text (None when it gave none), which the repair call shows
    it as its own previous turn."""

    def __init__(self, problem: str, answer: str | None = None) -> None:
        super().__init__(problem)
        self.problem = problem
        self.answer = answer


def _checked[T](reply: ChatCompletion, validate: Callable[[Any], T]) -> T:
    """The validated answer of a chat reply; ``_Invalid`` if it has none that fits."""
    if not reply.choices:
        raise _Invalid(_NO_ANSWER)
    choice = reply.choices[0]
    content = getattr(getattr(choice, "message", None), "content", None)
    if not content or not content.strip():
        raise _Invalid(_NO_ANSWER)
    if getattr(choice, "finish_reason", None) == "length":
        raise _Invalid(_CUT_OFF, content)
    try:
        return validate(json.loads(content))
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or 'answer'}: {item['msg']}"
            for item in error.errors(include_input=False, include_url=False, include_context=False)
        )
        raise _Invalid(details[:_REPAIR_ERROR_CHARS], content) from None
    except ValueError as error:  # includes json.JSONDecodeError
        raise _Invalid((str(error) or _INVALID)[:_REPAIR_ERROR_CHARS], content) from None


def _repair_request(problem: str) -> str:
    return (
        f"{problem} The answer must be only JSON that fits the schema. "
        "Answer again, with the JSON only."
    )
