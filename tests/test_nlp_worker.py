"""The nlp worker (``nlp_worker.py``) with a fake child engine: its runner on the real ``Worker``,
job store and queue (fakeredis), the start-up checks, and the parent's imports in a fresh
interpreter. The real children (Presidio, spaCy, the models) are in the ``nlp``-marked tests at the
end (``uv run pytest -m nlp``)."""

import asyncio
import json
import re
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from prometheus_client import CollectorRegistry
from structlog.testing import capture_logs

from tests.pii.conftest import NlpModels, nlp_models  # noqa: F401 - the fixture of the nlp tests
from tests.pii.test_child import HAS_NLP, nlp_test
from tests.pii.test_fields import make_result
from tests.test_worker import OWNER, free_port, scenario, status_of
from text_extraction import model_store, nlp_worker
from text_extraction.documents.pool import (
    ConversionCrashed,
    ConversionError,
    ConversionOutOfMemory,
    ConversionTimeout,
)
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.extract_models import ExtractResult
from text_extraction.internal import Readiness, create_internal_app
from text_extraction.job_models import JobItem, JobOptions
from text_extraction.jobs import JobStore, pii_lease_seconds
from text_extraction.nlp_worker import (
    LANGUAGE_UNSUPPORTED_WARNING,
    NlpStartFailed,
    NlpWorkerParts,
    PiiRunner,
    self_check,
    start_children,
)
from text_extraction.pii.fields import FAIL_CLOSED_MESSAGE
from text_extraction.pii.types import PiiOptions
from text_extraction.pii_dispatch import PII_POOL, nlp_queue
from text_extraction.server import _server
from text_extraction.settings import Settings
from text_extraction.worker import Worker

URL = "https://example.org/page"
_PERSON = re.compile(r"Anna(?: Schmidt)?|Thomas Müller|John Smith")
_EMAIL = re.compile(r"[\w.]+@[\w.]+\w")


WARM = ("/models", "lg")  # the models of the default settings: what ``warm`` loads


@dataclass
class FakeChild:
    """One child of the fake pool: the models it holds and how often it loaded them."""

    loaded_for: tuple[str, str] | None = None
    loads: int = 0


class FakePiiPool:
    """The PII children: a pool of ``children`` fake children handed out first in, first out
    (``DocumentPool``'s idle queue), each with the models it holds. The fake engine finds ``Anna``,
    ``Anna Schmidt`` and the persons of the self-check's sentences as a person (0.85) and e-mail
    addresses (1.0) in every field it is given, filtered by the asked types and the threshold like
    the real child. Like the real child it loads the models of a call that names them (once per
    child and models), raises ``NotLoaded`` for a call that names none to a child that has none,
    and a child that is killed or replaced comes back as a fresh, unloaded child at the back of the
    queue."""

    def __init__(self, children: int = 1) -> None:
        self.calls: list[tuple[str, dict[str, Any], float]] = []
        self.children = [FakeChild() for _ in range(children)]  # every child ever started
        self._idle = deque(self.children)
        self.started = False
        self.finds_nothing = False
        self.failures: list[BaseException] = []  # raised by the next ``analyze`` calls, in order
        self.load_failure: BaseException | None = None

    async def start(self) -> None:
        self.started = True

    async def shutdown(self) -> None:
        pass

    def warm(self) -> None:
        """Every child holds the default settings' models, as after the start-up."""
        for child in self.children:
            child.loaded_for = WARM
            child.loads += 1

    @property
    def loads(self) -> int:
        return sum(child.loads for child in self.children)

    def calls_to(self, name: str) -> list[dict[str, Any]]:
        return [payload for function, payload, _deadline in self.calls if function == name]

    def _load(self, child: FakeChild, payload: dict[str, Any]) -> None:
        wanted = (payload["models_path"], payload["size"])
        if child.loaded_for != wanted:
            if self.load_failure is not None:
                raise self.load_failure
            child.loaded_for = wanted
            child.loads += 1

    async def run(self, function_name: str, payload: dict[str, Any], deadline: float) -> Any:
        self.calls.append((function_name, payload, deadline))
        child = self._idle.popleft()
        killed = False
        try:
            if function_name == "load":
                self._load(child, payload)
                return None
            assert function_name == "analyze"
            if self.failures:
                failure = self.failures.pop(0)
                killed = isinstance(
                    failure, (ConversionTimeout, ConversionCrashed, ConversionOutOfMemory)
                )
                raise failure
            if "models_path" in payload:
                self._load(child, payload)
            elif child.loaded_for is None:
                raise ConversionError("NotLoaded", "the models are not loaded")
            wanted = set(payload["entities"])
            reply: dict[str, list[tuple[int, int, str, float]]] = {}
            for name, text in payload["fields"]:
                found: list[tuple[int, int, str, float]] = []
                if not self.finds_nothing:
                    found += [(m.start(), m.end(), "PERSON", 0.85) for m in _PERSON.finditer(text)]
                    found += [
                        (m.start(), m.end(), "EMAIL_ADDRESS", 1.0) for m in _EMAIL.finditer(text)
                    ]
                reply[name] = [
                    span for span in found if span[2] in wanted and span[3] >= payload["threshold"]
                ]
            return reply
        finally:
            if killed:  # the pool killed the child and started a fresh one
                child = FakeChild()
                self.children.append(child)
            self._idle.append(child)


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"auth_mode": "none", "pii_enabled": True}
    values.update(overrides)
    return Settings(**values)


def draft_of(**changes: Any) -> Envelope[ExtractResult]:
    return Envelope[ExtractResult](status="success", data=make_result(**changes))


class Nlp:
    """An nlp worker on fakeredis: the real job store, queue ``nlp`` and ``Worker``, with a fake
    child pool."""

    def __init__(self, pool: Any = None, **overrides: Any) -> None:
        self.valkey = FakeAsyncRedis()
        self.settings = make_settings(**overrides)
        self.registry = CollectorRegistry()
        self.pool: Any = pool or FakePiiPool()
        self.store = JobStore(self.valkey, self.settings)
        self.queue = nlp_queue(self.valkey, self.settings)
        self.runner = PiiRunner(self.settings, self.store, self.pool, self.registry)
        self.worker = Worker(
            self.settings,
            self.valkey,
            self.runner,
            self.store,
            self.queue,
            self.registry,
            concurrency=1,
        )

    async def submit(
        self,
        draft: Envelope[ExtractResult] | None,
        pii: PiiOptions | str | None = "mask",
    ) -> str:
        """A one-item job whose item is queued on ``nlp`` with ``draft`` as its draft (None: the
        draft has expired)."""
        options = JobOptions(
            compliance="enforce",
            pii=PiiOptions.model_validate(pii) if pii is not None else None,
        )
        job_id = await self.store.create("extract", OWNER, options, [URL])
        if draft is not None:
            await self.store.put_draft(job_id, 0, draft)
        await self.queue.enqueue_items(job_id, OWNER.name, [(0, URL)])
        return job_id

    async def process_next(self, *, exhausted: bool = False) -> None:
        claim = await self.queue.claim(self.runner.lease_seconds)
        assert claim.item is not None, "nothing queued on nlp"
        await self.worker.process(claim.item, exhausted=exhausted)

    async def item(self, job_id: str) -> JobItem:
        (item,) = await self.store.items(job_id, 0, 100)
        assert item.result is not None
        return item

    async def result(self, job_id: str) -> Envelope[ExtractResult]:
        return (await self.item(job_id)).result  # type: ignore[return-value]

    def sample(self, name: str, labels: dict[str, str] | None = None) -> float:
        return self.registry.get_sample_value(name, labels or {}) or 0.0


def data_of(envelope: Envelope[ExtractResult]) -> ExtractResult:
    assert envelope.data is not None
    return envelope.data


# --- the runner ---------------------------------------------------------------------------------


@scenario
async def test_runner_masks_draft_and_reports() -> None:
    nlp = Nlp()
    draft = draft_of(
        content="Anna Schmidt schreibt an anna.schmidt@example.org.", title="Anna privat"
    )
    job_id = await nlp.submit(draft)

    await nlp.process_next()

    result = await nlp.result(job_id)
    data = data_of(result)
    assert result.status == "success"
    assert data.content == "[PERSON] schreibt an [EMAIL_ADDRESS]."
    assert data.title == "[PERSON] privat"
    assert data.metadata is not None
    assert data.metadata.authors == ["[PERSON]", "[PERSON]"]  # a whole person each, no analyser
    assert data.pii is not None
    assert (data.pii.mode, data.pii.language, data.pii.hash_scope) == ("mask", "de", None)
    assert data.pii.entities["EMAIL_ADDRESS"] >= 1
    assert data.pii.entities["PERSON"] >= 4  # content, title, two authors and more
    assert data.timings.pii_ms >= 0
    assert data.warnings == ["a_warning"]  # the extraction's, nothing new
    assert data.word_count == 20  # the extraction's, as it was
    assert draft.data is not None
    assert data.fingerprint == draft.data.fingerprint


@scenario
async def test_runner_sends_the_child_what_n13_says() -> None:
    nlp = Nlp(pii_score_threshold=0.55, pii_timeout_seconds=120)
    started = asyncio.get_running_loop().time()
    job_id = await nlp.submit(draft_of(), PiiOptions(mode="mask", entities=["PERSON", "URL"]))

    await nlp.process_next()

    payload = nlp.pool.calls_to("analyze")[-1]
    names = [name for name, _text in payload["fields"]]
    assert "content" in names
    # The authors are persons as a whole: the child is not asked about them (ruling R1).
    assert not any(name.startswith("metadata.authors") for name in names)
    assert (payload["language"], payload["entities"], payload["threshold"]) == (
        "de",
        ["PERSON", "URL"],
        0.55,
    )
    deadline = [call for call in nlp.pool.calls if call[0] == "analyze"][-1][2]
    assert 118 <= deadline - started <= 122  # PII_TIMEOUT_SECONDS from the start of the item
    assert (await nlp.result(job_id)).status == "success"


@scenario
async def test_runner_loads_the_models_it_is_configured_for() -> None:
    """Each call names the volume and the size; the child loads them itself, lazily."""
    nlp = Nlp(pii_models_path="/srv/models", pii_model_size="md")
    await nlp.submit(draft_of())

    await nlp.process_next()

    (payload,) = nlp.pool.calls_to("analyze")
    assert (payload["models_path"], payload["size"]) == ("/srv/models", "md")
    assert nlp.pool.calls_to("load") == []  # no call of its own
    assert nlp.pool.children[0].loaded_for == ("/srv/models", "md")


@scenario
async def test_runner_options_without_entities_use_the_defaults_and_a_threshold_of_zero() -> None:
    nlp = Nlp(pii_default_entities=["EMAIL_ADDRESS"])
    await nlp.submit(draft_of(), PiiOptions(mode="redact", score_threshold=0.0))

    await nlp.process_next()

    payload = nlp.pool.calls_to("analyze")[-1]
    assert payload["entities"] == ["EMAIL_ADDRESS"]
    assert payload["threshold"] == 0.0


@scenario
async def test_runner_authors_are_not_persons_when_person_is_not_asked() -> None:
    nlp = Nlp()
    job_id = await nlp.submit(draft_of(), PiiOptions(mode="mask", entities=["EMAIL_ADDRESS"]))

    await nlp.process_next()

    data = data_of(await nlp.result(job_id))
    assert data.metadata is not None
    assert data.metadata.authors == ["Anna Schmidt", "Bob Meier"]
    payload = nlp.pool.calls_to("analyze")[-1]
    assert any(name.startswith("metadata.authors") for name, _text in payload["fields"])


@scenario
async def test_runner_annotate_lists_the_entities_and_hash_reports_its_scope() -> None:
    nlp = Nlp()
    annotated = await nlp.submit(draft_of(content="Mail an anna@example.org bitte."), "annotate")
    hashed = await nlp.submit(draft_of(content="Mail an anna@example.org bitte."), "hash")

    await nlp.process_next()
    await nlp.process_next()

    report = data_of(await nlp.result(annotated)).pii
    assert report is not None
    assert report.items is not None
    assert data_of(await nlp.result(annotated)).content == "Mail an anna@example.org bitte."
    assert {(i.field, i.text) for i in report.items} >= {("content", "anna@example.org")}
    hashed_data = data_of(await nlp.result(hashed))
    assert hashed_data.pii is not None
    assert hashed_data.pii.hash_scope == "result"  # no PII_HASH_KEY
    assert re.search(r"\[EMAIL_ADDRESS:[0-9a-f]{12}\]", hashed_data.content or "")


@scenario
async def test_runner_annotate_with_html_is_withheld_not_retryable() -> None:
    nlp = Nlp()
    job_id = await nlp.submit(draft_of(format="html", content="<p>Anna</p>"), "annotate")

    await nlp.process_next()

    result = await nlp.result(job_id)
    assert (result.status, result.error.code if result.error else None) == (
        "failed",
        ErrorCode.PII_FAILED,
    )
    assert result.error is not None
    assert result.error.retryable is False
    assert data_of(result).content is None
    assert data_of(result).metadata is None


@scenario
async def test_runner_html_content_is_masked_in_its_text_nodes() -> None:
    nlp = Nlp()
    job_id = await nlp.submit(
        draft_of(format="html", content='<p class="x">Hallo Anna<!-- geheim --></p>'), "mask"
    )

    await nlp.process_next()

    content = data_of(await nlp.result(job_id)).content
    assert content == '<p class="x">Hallo [PERSON]</p>'


@scenario
async def test_runner_nothing_to_process_reports_zero_entities() -> None:
    nlp = Nlp()
    bare = draft_of(title=None, content=None, metadata=None, links=None, media=None, document=None)
    job_id = await nlp.submit(bare)

    await nlp.process_next()

    data = data_of(await nlp.result(job_id))
    assert data.pii is not None
    assert data.pii.entities == {}
    assert nlp.pool.calls == []  # no field, no child, no model load


@scenario
async def test_runner_keeps_the_status_and_error_of_the_target() -> None:
    nlp = Nlp()
    failed = Envelope[ExtractResult](
        status="failed",
        data=make_result(content="Anna Schmidt war hier"),
        error=Envelope[ExtractResult](status="failed").error,
    )
    job_id = await nlp.submit(failed)

    await nlp.process_next()

    result = await nlp.result(job_id)
    assert result.status == "failed"
    assert data_of(result).content == "[PERSON] war hier"


@scenario
async def test_runner_an_already_processed_draft_is_returned_as_it_is() -> None:
    nlp = Nlp()
    first = Nlp()
    first_id = await first.submit(draft_of(content="Anna"))
    await first.process_next()
    processed = await first.result(first_id)
    job_id = await nlp.submit(processed)

    await nlp.process_next()

    assert await nlp.result(job_id) == processed
    assert nlp.pool.calls == []


@scenario
async def test_runner_draft_expired_is_pii_failed_retryable() -> None:
    nlp = Nlp()
    job_id = await nlp.submit(None)

    await nlp.process_next()

    result = await nlp.result(job_id)
    assert result.status == "failed"
    assert result.data is None  # never an unprocessed result
    assert result.error is not None
    assert (result.error.code, result.error.retryable) == (ErrorCode.PII_FAILED, True)
    assert nlp.pool.calls == []


@scenario
async def test_runner_child_deadline_is_pii_failed_and_child_restarted() -> None:
    nlp = Nlp()
    nlp.pool.warm()
    nlp.pool.failures = [ConversionTimeout("killed")]
    first = await nlp.submit(draft_of(content="Anna Schmidt privat"))
    await nlp.process_next()  # the child runs into its deadline and is killed

    result = await nlp.result(first)
    assert result.status == "failed"
    assert result.error is not None
    assert (result.error.code, result.error.retryable) == (ErrorCode.PII_FAILED, True)
    assert result.error.message == FAIL_CLOSED_MESSAGE
    # Withheld: the text, the metadata and the links are gone; the target's facts stay.
    data = data_of(result)
    assert (data.content, data.title, data.metadata, data.links) == (None, None, None, None)
    assert data.url == URL
    assert data.upstream_status == 200
    assert len(nlp.pool.children) == 2  # the pool started a fresh child, which holds no models

    second = await nlp.submit(draft_of(content="Anna Schmidt privat"))
    await nlp.process_next()  # the fresh child loads in the call, and answers it

    assert data_of(await nlp.result(second)).content == "[PERSON] privat"
    assert [child.loads for child in nlp.pool.children] == [1, 1]


@pytest.mark.parametrize(
    ("failure", "code", "retryable"),
    [
        (ConversionTimeout("killed"), ErrorCode.PII_FAILED, True),
        (ConversionCrashed("died"), ErrorCode.PII_FAILED, True),
        (ConversionOutOfMemory("limit"), ErrorCode.TOO_LARGE, False),
        (ConversionError("RuntimeError", "Anna Schmidt quoted"), ErrorCode.PII_FAILED, True),
        (ConversionError("ModelsMissing", "gone"), ErrorCode.PII_FAILED, True),
    ],
    ids=["deadline", "crash", "memory", "child-error", "models-missing"],
)
@scenario
async def test_runner_child_failures_withhold_the_content(
    failure: BaseException, code: ErrorCode, retryable: bool
) -> None:
    nlp = Nlp()
    nlp.pool.warm()
    nlp.pool.failures = [failure]
    job_id = await nlp.submit(draft_of(content="Anna Schmidt privat"))

    with capture_logs() as logs:
        await nlp.process_next()

    result = await nlp.result(job_id)
    assert result.error is not None
    assert (result.status, result.error.code, result.error.retryable) == (
        "failed",
        code,
        retryable,
    )
    assert data_of(result).content is None
    assert "Anna" not in json.dumps(logs, default=str)  # no text, no child message in a log


@scenario
async def test_runner_models_never_loading_is_pii_failed_retryable() -> None:
    nlp = Nlp()
    nlp.pool.load_failure = ConversionError("ModelsMissing", "5 of 6 files")
    job_id = await nlp.submit(draft_of(content="Anna privat"))

    await nlp.process_next()

    result = await nlp.result(job_id)
    assert result.error is not None
    assert (result.error.code, result.error.retryable) == (ErrorCode.PII_FAILED, True)
    assert data_of(result).content is None


@scenario
async def test_runner_a_failure_while_loading_is_pii_failed_retryable_never_too_large() -> None:
    """The child reports a memory error while it builds the engine as ``LoadFailed``: the models'
    size, nothing about the text."""
    nlp = Nlp()
    nlp.pool.load_failure = ConversionError("LoadFailed", "not enough memory to load the models")
    job_id = await nlp.submit(draft_of(content="Anna privat"))

    await nlp.process_next()

    result = await nlp.result(job_id)
    assert result.error is not None
    assert (result.error.code, result.error.retryable) == (ErrorCode.PII_FAILED, True)
    assert data_of(result).content is None


@scenario
async def test_a_replaced_child_loads_once_and_no_item_fails_at_concurrency_two() -> None:
    """The pool hands children out first in, first out: the fresh child of a killed one comes
    next whatever the runner asks, and its own call loads it (ruling R12)."""
    nlp = Nlp(FakePiiPool(children=2), pii_concurrency=2)
    await start_children(nlp.pool, nlp.settings, children=2)  # both children load at start
    first = await nlp.submit(draft_of(content="Anna privat"))
    nlp.pool.failures = [ConversionTimeout("killed")]
    await nlp.process_next()  # the first child runs into its deadline and is replaced
    assert nlp.pool.children[0].loads == 1
    items = [await nlp.submit(draft_of(content="Anna privat")) for _ in range(6)]

    for _ in items:
        await nlp.process_next()

    assert (await nlp.result(first)).status == "failed"
    for job_id in items:
        assert data_of(await nlp.result(job_id)).content == "[PERSON] privat"
    # Three children lived: the two of the start and the replacement; each loaded once.
    assert [child.loads for child in nlp.pool.children] == [1, 1, 1]
    assert nlp.pool.calls_to("load") == [
        {"models_path": "/models", "size": "lg"},
        {"models_path": "/models", "size": "lg"},
    ]  # the start-up's warm-up only


@scenario
async def test_runner_a_failure_applying_the_spans_is_withheld_and_logs_the_type_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("Anna Schmidt is quoted in this message")

    monkeypatch.setattr(nlp_worker, "apply_pii", broken)
    nlp = Nlp()
    job_id = await nlp.submit(draft_of(content="Anna Schmidt privat"))

    with capture_logs() as logs:
        await nlp.process_next()

    result = await nlp.result(job_id)
    assert result.error is not None
    assert (result.status, result.error.code, result.error.retryable) == (
        "failed",
        ErrorCode.PII_FAILED,
        True,
    )
    assert data_of(result).content is None
    (line,) = [log for log in logs if log["event"] == "pii_apply_failed"]
    assert line["error"] == "ValueError"
    assert "Anna" not in json.dumps(logs, default=str)


@scenario
async def test_runner_too_much_text_is_too_large_withheld() -> None:
    nlp = Nlp(pii_max_chars=10_000)
    job_id = await nlp.submit(draft_of(content="a" * 9_990, title="b" * 11))

    await nlp.process_next()

    result = await nlp.result(job_id)
    assert result.error is not None
    assert (result.status, result.error.code, result.error.retryable) == (
        "failed",
        ErrorCode.TOO_LARGE,
        False,
    )
    data = data_of(result)
    assert (data.content, data.title, data.metadata) == (None, None, None)
    assert nlp.pool.calls == []  # the child never saw it


@scenario
async def test_runner_text_at_the_limit_is_processed() -> None:
    nlp = Nlp(pii_max_chars=10_000)
    short = draft_of(content="a" * 9_000, title=None, metadata=None, links=None)
    job_id = await nlp.submit(
        short.model_copy(
            update={
                "data": data_of(short).model_copy(update={"media": None, "document": None}),
            }
        )
    )

    await nlp.process_next()

    assert data_of(await nlp.result(job_id)).pii is not None


@pytest.mark.parametrize(
    ("language", "option", "fallback", "model", "warned"),
    [
        ("fr", "auto", "en", "en", True),
        (None, "auto", "en", "en", True),
        ("fr", "auto", "de", "de", True),
        ("fr", "de", "en", "de", False),
        ("de", "en", "en", "en", False),
        ("de", "auto", "en", "de", False),
        ("en", "auto", "de", "en", False),
    ],
    ids=["fr", "none", "fr-fallback-de", "forced-de", "forced-en", "de", "en"],
)
@scenario
async def test_runner_language_fallback_warning(
    language: str | None, option: str, fallback: str, model: str, warned: bool
) -> None:
    nlp = Nlp(pii_fallback_language=fallback)
    job_id = await nlp.submit(
        draft_of(language=language),
        PiiOptions(mode="mask", language=option),  # type: ignore[arg-type]
    )

    await nlp.process_next()

    data = data_of(await nlp.result(job_id))
    payload = nlp.pool.calls_to("analyze")[-1]
    assert payload["language"] == model
    assert data.pii is not None
    assert data.pii.language == model
    assert (LANGUAGE_UNSUPPORTED_WARNING in data.warnings) is warned
    assert data.warnings[0] == "a_warning"  # the extraction's warnings stay first
    assert data.language == language  # the extraction's language is not changed


def test_language_warning_name() -> None:
    assert LANGUAGE_UNSUPPORTED_WARNING == "pii_language_unsupported"


@scenario
async def test_runner_entity_counts_feed_the_metric_by_type_only() -> None:
    nlp = Nlp()
    await nlp.submit(draft_of(content="Anna schreibt an a@example.org", title=None))

    await nlp.process_next()

    persons = nlp.sample("text_extraction_pii_entities_total", {"type": "PERSON"})
    emails = nlp.sample("text_extraction_pii_entities_total", {"type": "EMAIL_ADDRESS"})
    assert persons >= 1
    assert emails >= 1
    assert nlp.sample("text_extraction_pii_entities_total", {"type": "IBAN_CODE"}) == 0
    labels = {
        sample.labels["type"]
        for family in nlp.registry.collect()
        if family.name == "text_extraction_pii_entities"
        for sample in family.samples
    }
    assert "Anna" not in labels
    assert labels <= {
        "PERSON",
        "LOCATION",
        "ORGANIZATION",
        "EMAIL_ADDRESS",
        "PHONE_NUMBER",
        "IBAN_CODE",
        "CREDIT_CARD",
        "IP_ADDRESS",
        "URL",
    }


@scenario
async def test_runner_logs_no_text() -> None:
    nlp = Nlp()
    nlp.pool.failures = [ConversionTimeout("Anna Schmidt")]
    nlp.pool.warm()
    await nlp.submit(draft_of(content="Anna Schmidt anna@example.org"))
    await nlp.submit(draft_of(content="Anna Schmidt anna@example.org"))

    with capture_logs() as logs:
        await nlp.process_next()
        await nlp.process_next()

    dumped = json.dumps(logs, default=str)
    assert "Anna" not in dumped
    assert "example.org" not in dumped


def test_runner_lease_covers_two_runs() -> None:
    nlp = Nlp(pii_timeout_seconds=100)

    assert nlp.runner.lease_seconds == 210 == pii_lease_seconds(nlp.settings)
    assert Nlp().runner.lease_seconds == 610


# --- the Worker for pool nlp --------------------------------------------------------------------


@scenario
async def test_exhausted_nlp_item_never_answers_draft() -> None:
    nlp = Nlp()
    job_id = await nlp.submit(draft_of(content="Anna Schmidt geheim"))
    assert await nlp.store.draft(job_id, 0) is not None

    await nlp.process_next(exhausted=True)

    result = await nlp.result(job_id)
    assert result.status == "failed"
    assert result.error is not None
    assert (result.error.code, result.error.retryable) == (ErrorCode.PII_FAILED, True)
    assert result.error.message == FAIL_CLOSED_MESSAGE
    assert result.data is None  # never the draft
    assert await nlp.store.draft(job_id, 0) is None
    assert nlp.pool.calls == []


@scenario
async def test_exhausted_item_of_another_pool_still_answers_internal_error() -> None:
    """Only pool ``nlp`` changes the exhausted item's answer."""
    from tests.test_worker import Harness

    harness = Harness()
    job_id = await harness.submit(["https://a.example/page"])
    claim = await harness.claim()
    assert claim.item is not None

    await harness.worker.process(claim.item, exhausted=True)

    (item,) = await harness.items(job_id)
    assert item.result is not None
    assert item.result.error is not None
    assert item.result.error.code == ErrorCode.INTERNAL_ERROR


@scenario
async def test_nlp_worker_drops_draft_after_result() -> None:
    nlp = Nlp()
    job_id = await nlp.submit(draft_of(content="Anna Schmidt"))

    await nlp.process_next()

    assert (await nlp.item(job_id)).state == "done"
    assert await nlp.store.draft(job_id, 0) is None
    assert await nlp.valkey.hlen(f"job:{{{job_id}}}:drafts") == 0


@scenario
async def test_a_failed_result_write_keeps_the_draft_for_the_rerun() -> None:
    nlp = Nlp()
    job_id = await nlp.submit(draft_of(content="Anna Schmidt"))
    original = nlp.store.write_result

    async def failing(*args: Any, **kwargs: Any) -> Any:
        raise OSError("valkey timed out")

    nlp.store.write_result = failing  # type: ignore[method-assign]
    claim = await nlp.queue.claim(nlp.runner.lease_seconds)
    assert claim.item is not None
    with pytest.raises(OSError, match="valkey timed out"):
        await nlp.worker.process(claim.item)

    assert await nlp.store.draft(job_id, 0) is not None  # the rerun finds it
    nlp.store.write_result = original  # type: ignore[method-assign]


@scenario
async def test_an_item_of_a_job_that_is_gone_is_dropped() -> None:
    nlp = Nlp()
    job_id = await nlp.submit(draft_of())
    await nlp.store.delete(job_id)

    await nlp.process_next()

    assert nlp.pool.calls == []
    assert nlp.sample("text_extraction_job_items_total", {"outcome": "lost"}) == 1


# --- the start-up and the process ---------------------------------------------------------------


def test_self_check_passes_when_both_languages_find_a_person_and_an_email() -> None:
    pool = FakePiiPool()
    pool.warm()
    settings = make_settings()

    asyncio.run(self_check(pool, settings))

    calls = pool.calls_to("analyze")
    assert [call["language"] for call in calls] == ["de", "en"]
    assert all(call["entities"] == ["PERSON", "EMAIL_ADDRESS"] for call in calls)


def test_check_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings()
    nothing = FakePiiPool()
    nothing.finds_nothing = True
    monkeypatch.setattr(nlp_worker, "create_nlp_pool", lambda _settings: nothing)

    assert asyncio.run(nlp_worker._check(settings)) == 1  # the fake engine finds nothing
    assert nothing.started

    working = FakePiiPool()
    monkeypatch.setattr(nlp_worker, "create_nlp_pool", lambda _settings: working)

    assert asyncio.run(nlp_worker._check(settings)) == 0
    assert len(working.calls_to("load")) == 1  # one child


def test_check_one_language_without_a_person_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    class GermanOnly(FakePiiPool):
        async def run(self, function_name: str, payload: dict[str, Any], deadline: float) -> Any:
            reply = await super().run(function_name, payload, deadline)
            if function_name == "analyze" and payload["language"] == "en":
                return {name: [] for name in reply}
            return reply

    pool = GermanOnly()
    monkeypatch.setattr(nlp_worker, "create_nlp_pool", lambda _settings: pool)

    with capture_logs() as logs:
        assert asyncio.run(nlp_worker._check(make_settings())) == 1

    (line,) = [log for log in logs if log["event"] == "nlp_self_check_failed"]
    assert line["language"] == "en"
    assert line["log_level"] == "error"


@pytest.mark.parametrize(
    "failure",
    [
        ConversionError("ModelsMissing", "x"),
        ConversionTimeout("slow"),
        ConversionCrashed("died"),
        ConversionError("OSError", "bad volume"),
    ],
    ids=["models-missing", "timeout", "crash", "error"],
)
def test_children_that_cannot_load_the_models_fail_the_start(failure: BaseException) -> None:
    pool = FakePiiPool()
    pool.load_failure = failure
    settings = make_settings(pii_models_path="/srv/models")

    with capture_logs() as logs, pytest.raises(NlpStartFailed):
        asyncio.run(start_children(pool, settings, children=2))

    (line,) = [log for log in logs if log["event"] == "nlp_start_failed"]
    assert line["models_path"] == "/srv/models"
    assert line["log_level"] == "error"
    assert "bad volume" not in json.dumps(logs, default=str)  # the type only


def test_children_that_do_not_start_are_logged_with_the_models_path() -> None:
    class NotStarting(FakePiiPool):
        async def start(self) -> None:
            raise RuntimeError("the child did not start")

    settings = make_settings(pii_models_path="/srv/models")

    with capture_logs() as logs, pytest.raises(NlpStartFailed):
        asyncio.run(start_children(NotStarting(), settings, children=1))

    (line,) = [log for log in logs if log["event"] == "nlp_start_failed"]
    assert (line["models_path"], line["error_type"]) == ("/srv/models", "RuntimeError")


def test_start_loads_every_child_once() -> None:
    pool = FakePiiPool(children=3)
    settings = make_settings(pii_concurrency=3)

    asyncio.run(start_children(pool, settings, children=3))

    assert pool.started
    assert len(pool.calls_to("load")) == 3
    assert [child.loads for child in pool.children] == [1, 1, 1]  # one each, none twice
    assert [call["language"] for call in pool.calls_to("analyze")] == ["de", "en"]


def parts_for(nlp: Nlp) -> NlpWorkerParts:
    return NlpWorkerParts(worker=nlp.worker, pool=nlp.pool, settings=nlp.settings)


@scenario
async def test_ready_false_while_models_missing() -> None:
    """A start-up whose children cannot load the models never reaches ``/ready``."""
    nlp = Nlp()
    nlp.pool.load_failure = ConversionError("ModelsMissing", "gone")
    readiness = Readiness()
    app = create_internal_app(nlp.settings, nlp.valkey, nlp.registry, readiness=readiness)
    port = free_port()
    internal = _server(app, "127.0.0.1", port)
    ready_url = f"http://127.0.0.1:{port}/ready"
    statuses: list[int | None] = []

    async def watch() -> None:
        async with httpx.AsyncClient(trust_env=False) as client:
            while True:
                statuses.append(await status_of(client, ready_url))
                await asyncio.sleep(0.02)

    watching = asyncio.create_task(watch())
    try:
        async with asyncio.timeout(30):
            with pytest.raises(NlpStartFailed):
                await nlp_worker.serve(parts_for(nlp), internal, readiness)
    finally:
        watching.cancel()

    assert not readiness.ready
    assert 200 not in statuses
    assert nlp.pool.calls_to("analyze") == []  # no loop ran, nothing was claimed


@scenario
async def test_ready_after_the_children_started_and_the_worker_stops_on_a_signal() -> None:
    import os
    import signal

    nlp = Nlp()
    readiness = Readiness()
    app = create_internal_app(nlp.settings, nlp.valkey, nlp.registry, readiness=readiness)
    port = free_port()
    internal = _server(app, "127.0.0.1", port)
    ready_url = f"http://127.0.0.1:{port}/ready"

    serving = asyncio.create_task(nlp_worker.serve(parts_for(nlp), internal, readiness))
    async with httpx.AsyncClient(trust_env=False) as client:
        deadline = time.monotonic() + 20
        while await status_of(client, ready_url) != 200:
            assert time.monotonic() < deadline, "the worker never became ready"
            await asyncio.sleep(0.05)
        assert nlp.pool.started
        assert len(nlp.pool.calls_to("load")) == 1
        os.kill(os.getpid(), signal.SIGTERM)
        async with asyncio.timeout(30):
            await serving


def test_missing_models_exit_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("PII_MODELS_PATH", str(tmp_path))

    with pytest.raises(SystemExit) as exit_info:
        nlp_worker.main([])

    assert exit_info.value.code == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    (missing,) = [line for line in lines if line["event"] == "nlp_models_missing"]
    assert missing["models_path"] == str(tmp_path)
    assert missing["missing"] == 2  # the two models of PII_MODEL_SIZE
    assert missing["level"] == "error"


def test_check_exits_1_without_models(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("PII_MODELS_PATH", str(tmp_path))

    with pytest.raises(SystemExit) as exit_info:
        nlp_worker.main(["--check"])

    assert exit_info.value.code == 1


def test_only_the_models_of_the_size_are_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ("de_core_news_md", "en_core_web_md"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "config.cfg").write_text("")
        (tmp_path / name / "meta.json").write_text("{}")

    assert not nlp_worker.models_missing(
        make_settings(pii_models_path=tmp_path, pii_model_size="md")
    )
    assert nlp_worker.models_missing(make_settings(pii_models_path=tmp_path, pii_model_size="lg"))


def test_download_models_fetches_the_models_of_the_size(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[Path, list[str]]] = []

    def download(root: Path, manifest: Any, **_kwargs: Any) -> bool:
        calls.append((root, sorted(file.path for file in manifest.files)))
        return True

    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("PII_MODELS_PATH", str(tmp_path))
    monkeypatch.setenv("PII_MODEL_SIZE", "md")
    monkeypatch.setattr(model_store, "download_models", download)

    with pytest.raises(SystemExit) as exit_info:
        nlp_worker.main(["--download-models"])

    assert exit_info.value.code == 0
    assert calls == [(tmp_path, ["de_core_news_md/", "en_core_web_md/"])]


def test_download_models_failure_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def download(root: Path, manifest: Any, **_kwargs: Any) -> bool:
        raise model_store.ModelDownloadFailed("a file did not match")

    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("PII_MODELS_PATH", str(tmp_path))
    monkeypatch.setattr(model_store, "download_models", download)

    with pytest.raises(SystemExit) as exit_info:
        nlp_worker.main(["--download-models"])

    assert exit_info.value.code == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    (failed,) = [line for line in lines if line["event"] == "nlp_models_download_failed"]
    assert failed["models_path"] == str(tmp_path)


def test_pool_is_the_pii_child_with_the_configured_size() -> None:
    settings = make_settings(pii_concurrency=2, pii_memory_limit_mb=2048)

    pool = nlp_worker.create_nlp_pool(settings)

    assert (pool._processes, pool._memory_limit_mb) == (2, 2048)
    assert pool._target == "text_extraction.pii.child"
    assert pool._max_tasks == 1000  # a replacement reloads the models: far less often
    assert "VALKEY_PASSWORD" not in pool._env


def test_build_worker_serves_the_nlp_pool() -> None:
    settings = make_settings(pii_concurrency=2)

    parts = nlp_worker.build_nlp_worker(settings, FakeAsyncRedis(), CollectorRegistry())

    assert parts.worker._queue.pool == PII_POOL
    assert parts.worker._concurrency == 2
    assert parts.worker._lease_seconds == pii_lease_seconds(settings)


def test_worker_does_not_import_presidio_or_spacy_in_parent() -> None:
    script = """
import sys
from fakeredis import FakeAsyncRedis
from prometheus_client import CollectorRegistry
from text_extraction import model_store, nlp_worker
from text_extraction.settings import Settings

settings = Settings(auth_mode="none", pii_enabled=True)
parts = nlp_worker.build_nlp_worker(settings, FakeAsyncRedis(), CollectorRegistry())
forbidden = {"presidio_analyzer", "presidio_anonymizer", "spacy", "thinc", "torch", "tldextract"}
print(",".join(sorted({name.split(".")[0] for name in sys.modules} & forbidden)))
"""
    result = subprocess.run(  # noqa: S603 - our own interpreter and script
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, timeout=120
    )

    assert result.stdout.strip() == ""


# --- the real children (marker nlp) ---------------------------------------------------------------


@nlp_test
def test_real_children_run_the_check_and_mask_a_draft(
    nlp_models: NlpModels,  # noqa: F811 - the fixture imported above
) -> None:
    assert HAS_NLP
    settings = make_settings(
        pii_models_path=nlp_models.path, pii_model_size=nlp_models.size, pii_timeout_seconds=120
    )

    async def run() -> tuple[int, Envelope[ExtractResult]]:
        exit_code = await nlp_worker._check(settings)
        # A fresh child holds no models: the first analysis loads them (ruling R12).
        nlp = Nlp(nlp_worker.create_nlp_pool(settings), **settings.model_dump())
        await nlp.pool.start()
        try:
            job_id = await nlp.submit(
                draft_of(
                    content="Maria Schneider schreibt an maria.schneider@example.org.",
                    title="Kontakt",
                    metadata=None,
                    links=None,
                    media=None,
                    document=None,
                )
            )
            await nlp.process_next()
            return exit_code, await nlp.result(job_id)
        finally:
            await nlp.pool.shutdown()

    exit_code, result = asyncio.run(run())

    assert exit_code == 0
    content = data_of(result).content
    assert content is not None
    assert "[EMAIL_ADDRESS]" in content
    assert "[PERSON]" in content
    assert "maria.schneider@example.org" not in content
    assert "Schneider" not in content
