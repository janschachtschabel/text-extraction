"""The llm worker (``llm_worker.py``, ruling S14): its runner on fakeredis with pytest-httpserver
as the OpenAI-compatible endpoint (which key it sends, the draft it reads, its deadline), the
process (pool, lease, concurrency, what it imports) and ``--check``."""

import asyncio
import json
import subprocess
import sys
import time
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from prometheus_client import CollectorRegistry
from pytest_httpserver import HTTPServer
from structlog.testing import capture_logs
from werkzeug import Request, Response

from tests.structured.post_helpers import (
    ENV_KEY,
    URL,
    answer,
    chat_requests,
    envelope,
    error_response,
    serve_chat,
)
from text_extraction import llm_worker
from text_extraction.auth import Principal
from text_extraction.chunking.types import ChunkingOptions
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.extract_models import ExtractResult
from text_extraction.job_models import JobOptions
from text_extraction.jobs import CONTENT_NOT_STORED_WARNING, JobStore, llm_lease_seconds
from text_extraction.llm_dispatch import LLM_POOL, llm_queue
from text_extraction.llm_worker import LlmRunner, build_llm_worker
from text_extraction.pii.types import PiiOptions
from text_extraction.scheduler import QueueItem
from text_extraction.settings import Settings
from text_extraction.structured.post import NOT_RUN_MESSAGE, TOO_LARGE_FOR_LLM_MESSAGE
from text_extraction.structured.store import SelectorStore
from text_extraction.structured.types import StructuredOptions, StructuredResult

OWNER = Principal(name="alice", role="user", rate_limit_per_minute=None)
CALLER_KEY = "sk-caller-key-0d9e"
CONTENT = "# Kettles\n\nThe blue kettle costs 19.99 euros and boils water fast."
PRICE = StructuredOptions.model_validate(
    {"schema": {"type": "object", "properties": {"price": {"type": "number"}}}, "strategy": "llm"}
)


def settings_for(server: HTTPServer, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "auth_mode": "none",
        "llm_enabled": True,
        "llm_base_url": server.url_for("/v1"),
        "llm_allow_http": True,
        "llm_api_key": ENV_KEY,
    }
    values.update(overrides)
    return Settings(**values)


class Case:
    """A job with one item whose draft waits for the llm worker."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.valkey = FakeAsyncRedis()
        self.store = JobStore(self.valkey, settings)
        self.registry = CollectorRegistry()
        self.runner = LlmRunner(
            settings,
            self.store,
            SelectorStore(self.valkey, settings.selector_learn_ttl_seconds),
            registry=self.registry,
        )

    async def job(
        self,
        *,
        draft: Envelope[ExtractResult] | None = None,
        key: str | None = None,
        structured: StructuredOptions | None = PRICE,
        chunking: ChunkingOptions | None = None,
        pii: PiiOptions | None = None,
    ) -> tuple[Any, QueueItem]:
        options = JobOptions(
            compliance="enforce", structured=structured, chunking=chunking, pii=pii
        )
        job_id = await self.store.create("extract", OWNER, options, [URL])
        if draft is not None:
            await self.store.put_draft(job_id, 0, draft)
        if key is not None:
            await self.store.put_llm_key(job_id, key)
        record = await self.store.get(job_id)
        return record, QueueItem(
            job_id=job_id, index=0, client="alice#live", domain="shop.example.org", attempts=0
        )

    async def run(self, **job: Any) -> Envelope[ExtractResult]:
        record, item = await self.job(**job)
        return await self.runner.run(record, item, URL, gate_refusals="raise")


def run_case(settings: Settings, **job: Any) -> Envelope[ExtractResult]:
    return asyncio.run(Case(settings).run(**job))


def refuse_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """No LLM client may be made."""

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an LLM client was made")

    monkeypatch.setattr(llm_worker, "LlmClient", refuse)


def keys_sent(server: HTTPServer) -> list[str | None]:
    return [request.headers.get("Authorization") for request, _ in server.log]


# --- the runner ---


@pytest.mark.parametrize("caller", [True, False], ids=["caller-key", "env-key"])
def test_runner_uses_caller_key(httpserver: HTTPServer, caller: bool) -> None:
    serve_chat(httpserver, answer({"price": 19.99}))

    result = run_case(
        settings_for(httpserver),
        draft=envelope(CONTENT),
        key=CALLER_KEY if caller else None,
    )

    assert result.status == "success", result.error
    assert result.data is not None
    assert result.data.structured is not None
    assert result.data.structured.values == {"price": 19.99}
    assert result.data.structured.sources == {"price": "llm"}
    assert result.data.structured.stage == "done"
    assert keys_sent(httpserver) == [f"Bearer {CALLER_KEY if caller else ENV_KEY}"]
    [request] = chat_requests(httpserver)
    assert "19.99 euros" in json.dumps(request)  # the draft's content is the document


def test_runner_draft_expired_is_internal_error_retryable(httpserver: HTTPServer) -> None:
    result = run_case(settings_for(httpserver), draft=None, key=CALLER_KEY)

    assert result.status == "failed"
    assert result.error is not None
    assert (result.error.code, result.error.retryable) == (ErrorCode.INTERNAL_ERROR, True)
    assert result.error.message == llm_worker.DRAFT_EXPIRED_MESSAGE
    assert result.data is None
    assert httpserver.log == []


def test_runner_invalid_draft_is_internal_error_retryable(httpserver: HTTPServer) -> None:
    case = Case(settings_for(httpserver))

    async def scenario() -> Envelope[ExtractResult]:
        record, item = await case.job()
        await case.valkey.hset(f"job:{{{record.id}}}:drafts", "0", '{"status": "nope"}')
        return await case.runner.run(record, item, URL, gate_refusals="raise")

    result = asyncio.run(scenario())

    assert result.error is not None
    assert (result.error.code, result.error.retryable) == (ErrorCode.INTERNAL_ERROR, True)
    assert result.error.message == llm_worker.DRAFT_INVALID_MESSAGE


def test_runner_answers_a_done_draft_as_it_is(httpserver: HTTPServer) -> None:
    """A draft whose post stage ran (an item that ran twice) is answered without a call."""
    done = envelope(
        CONTENT,
        structured=StructuredResult(
            values={"price": 19.99}, sources={"price": "llm"}, missing=[], stage="done"
        ),
    )

    result = run_case(settings_for(httpserver), draft=done)

    assert result == done
    assert httpserver.log == []


def test_runner_failed_call_is_partial_with_content(httpserver: HTTPServer) -> None:
    serve_chat(httpserver, error_response(401))

    result = run_case(settings_for(httpserver), draft=envelope(CONTENT))

    assert result.status == "partial"
    assert result.error is not None
    assert (result.error.code, result.error.retryable) == (ErrorCode.LLM_FAILED, False)
    assert result.data is not None
    assert result.data.content == CONTENT
    assert result.data.structured is not None
    assert result.data.structured.missing == ["price"]


def test_runner_deadline_answers_without_the_llm(httpserver: HTTPServer) -> None:
    """The overall deadline (``3 * LLM_TIMEOUT_SECONDS``) ends a slow item: its content
    without the LLM work, ``llm_failed``, retryable."""

    def slow(_request: Request) -> Response:
        time.sleep(1.5)
        return Response(json.dumps(answer({"price": 1})), content_type="application/json")

    httpserver.expect_request("/v1/chat/completions").respond_with_handler(slow)
    case = Case(settings_for(httpserver))
    case.runner.deadline_seconds = 0.3

    started = time.monotonic()
    result = asyncio.run(case.run(draft=envelope(CONTENT)))

    assert time.monotonic() - started < 1.4
    assert result.status == "partial"
    assert result.error is not None
    assert (result.error.code, result.error.retryable) == (ErrorCode.LLM_FAILED, True)
    assert result.error.message == NOT_RUN_MESSAGE
    assert result.data is not None
    assert result.data.content == CONTENT


def test_runner_without_any_key_answers_without_the_llm(httpserver: HTTPServer) -> None:
    result = run_case(settings_for(httpserver, llm_api_key=None), draft=envelope(CONTENT))

    assert result.status == "partial"
    assert result.error is not None
    assert result.error.code == ErrorCode.LLM_FAILED
    assert result.data is not None
    assert result.data.content == CONTENT
    assert httpserver.log == []


def test_runner_draft_without_its_content_is_llm_failed_not_retryable(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I1 (ruling R20): a draft stored over JOB_ITEM_MAX_BYTES lost its content; LLM work or
    semantic chunking it owes cannot be done, and the answer says so (never a silent success).
    Retrying would store the same draft again: not retryable."""
    refuse_clients(monkeypatch)
    lost = envelope(None)
    assert lost.data is not None
    lost = lost.model_copy(
        update={"data": lost.data.model_copy(update={"warnings": [CONTENT_NOT_STORED_WARNING]})}
    )
    semantic = ChunkingOptions(strategy="semantic")
    cases: list[dict[str, Any]] = [
        {"structured": PRICE},
        {"structured": None, "chunking": semantic},
        {
            "structured": StructuredOptions.model_validate(
                {"schema": {"type": "object", "properties": {"a": {"type": "string"}}}}
            )
        },  # auto: a field still missing
    ]
    for case in cases:
        result = run_case(
            settings_for(httpserver, llm_embedding_model="embed-1"), draft=lost, **case
        )

        assert result.status == "partial", case
        assert result.error is not None
        assert result.error.code == ErrorCode.LLM_FAILED
        assert result.error.retryable is False
        assert result.error.message == TOO_LARGE_FOR_LLM_MESSAGE
        assert result.data is not None
        assert CONTENT_NOT_STORED_WARNING in result.data.warnings
    assert httpserver.log == []


def test_runner_withholds_a_draft_that_owes_pii(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-a: the runner is the only code that sends content to a third party; a draft that still
    owes the PII step is withheld before any client is made (D3 a)."""
    refuse_clients(monkeypatch)

    result = run_case(
        settings_for(httpserver), draft=envelope(CONTENT), pii=PiiOptions(mode="mask")
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == ErrorCode.PII_FAILED
    assert result.data is not None
    assert result.data.content is None
    assert result.data.structured is None
    assert httpserver.log == []


def test_runner_unexpected_error_keeps_the_content(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-b: an unexpected exception in the post stage answers the draft without the LLM work:
    ``llm_failed``, retryable, its content kept; the log has the type only."""

    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise KeyError("a page quote that must not be logged")

    monkeypatch.setattr(llm_worker, "apply_post", broken)

    with capture_logs() as logs:
        result = run_case(settings_for(httpserver), draft=envelope(CONTENT))

    assert result.status == "partial"
    assert result.error is not None
    assert (result.error.code, result.error.retryable) == (ErrorCode.LLM_FAILED, True)
    assert result.data is not None
    assert result.data.content == CONTENT
    [line] = [entry for entry in logs if entry["event"] == "llm_post_failed"]
    assert line["error_type"] == "KeyError"
    assert "page quote" not in json.dumps(logs, default=str)
    assert "exc_info" not in line


def test_runner_lease_covers_its_deadline(httpserver: HTTPServer) -> None:
    settings = settings_for(httpserver, llm_timeout_seconds=100)
    runner = Case(settings).runner

    assert runner.lease_seconds == llm_lease_seconds(settings) == 410
    assert runner.deadline_seconds == 300


# --- the process ---


def test_build_worker_serves_the_llm_pool() -> None:
    settings = Settings(auth_mode="none", llm_enabled=True, llm_concurrency=3)

    parts = build_llm_worker(settings, FakeAsyncRedis(), CollectorRegistry())

    assert parts.worker._queue.pool == LLM_POOL
    assert parts.worker._concurrency == 3
    assert parts.worker._lease_seconds == llm_lease_seconds(settings)
    assert parts.worker._llm_move is None  # the last hop
    assert parts.worker._pii_move is None
    assert parts.worker._give_back_on_cancel


def test_llm_queue_ignores_the_domain_cap() -> None:
    queue = llm_queue(FakeAsyncRedis(), Settings(auth_mode="none"))
    assert queue.pool == LLM_POOL


def test_worker_imports_nothing_heavy() -> None:
    """The llm worker runs from the API image: no model, NLP or browser library, only the
    OpenAI SDK."""
    script = """
import sys
from fakeredis import FakeAsyncRedis
from prometheus_client import CollectorRegistry
from text_extraction import llm_worker
from text_extraction.settings import Settings

settings = Settings(auth_mode="none", llm_enabled=True)
parts = llm_worker.build_llm_worker(settings, FakeAsyncRedis(), CollectorRegistry())
forbidden = {"presidio_analyzer", "presidio_anonymizer", "spacy", "thinc", "torch", "docling",
             "faster_whisper", "ctranslate2", "av", "yt_dlp", "patchright", "playwright"}
print(",".join(sorted({name.split(".")[0] for name in sys.modules} & forbidden)))
"""
    result = subprocess.run(  # noqa: S603 - our own interpreter and script
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, timeout=120
    )

    assert result.stdout.strip() == ""


# --- --check ---


def check_env(monkeypatch: pytest.MonkeyPatch, server: HTTPServer) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_BASE_URL", server.url_for("/v1"))
    monkeypatch.setenv("LLM_ALLOW_HTTP", "true")
    monkeypatch.setenv("LLM_API_KEY", ENV_KEY)


def test_check_exits_0_when_the_endpoint_extracts(
    monkeypatch: pytest.MonkeyPatch, httpserver: HTTPServer
) -> None:
    serve_chat(httpserver, answer({"title": "Check"}))
    check_env(monkeypatch, httpserver)

    with pytest.raises(SystemExit) as exit_info:
        llm_worker.main(["--check"])

    assert exit_info.value.code == 0
    [request] = chat_requests(httpserver)
    assert request["response_format"]["type"] == "json_schema"
    assert keys_sent(httpserver) == [f"Bearer {ENV_KEY}"]


@pytest.mark.parametrize("status", [401, 500])
def test_check_exits_1_when_the_endpoint_fails(
    monkeypatch: pytest.MonkeyPatch,
    httpserver: HTTPServer,
    capsys: pytest.CaptureFixture[str],
    status: int,
) -> None:
    monkeypatch.setattr("text_extraction.llm.client.MAX_RETRIES", 0)
    serve_chat(httpserver, error_response(status))
    check_env(monkeypatch, httpserver)

    with pytest.raises(SystemExit) as exit_info:
        llm_worker.main(["--check"])

    assert exit_info.value.code == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    (failed,) = [line for line in lines if line["event"] == "llm_check_failed"]
    assert failed["code"] == "llm_failed"
    assert ENV_KEY not in json.dumps(lines)


def test_check_exits_1_without_a_key(
    monkeypatch: pytest.MonkeyPatch, httpserver: HTTPServer
) -> None:
    check_env(monkeypatch, httpserver)
    monkeypatch.delenv("LLM_API_KEY")

    with pytest.raises(SystemExit) as exit_info:
        llm_worker.main(["--check"])

    assert exit_info.value.code == 1
    assert httpserver.log == []
