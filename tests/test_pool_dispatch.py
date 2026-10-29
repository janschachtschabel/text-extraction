"""``PoolDispatcher`` (``pool_dispatch.py``) and ``DoclingDispatcher`` (``docling_dispatch.py``) on
fakeredis with the real job store and queues. The browser pool's behaviour is covered by
``test_browser_dispatch.py``."""

import asyncio
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis
from prometheus_client import CollectorRegistry
from redis.exceptions import ConnectionError as RedisConnectionError

from text_extraction.auth import Principal
from text_extraction.browser_dispatch import BrowserDispatcher
from text_extraction.docling_dispatch import (
    DoclingDispatcher,
    DoclingHandoff,
    OpenLimitReached,
    docling_queue,
)
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractResult
from text_extraction.job_models import JobOptions
from text_extraction.jobs import JobStore
from text_extraction.pii.types import PiiOptions
from text_extraction.pii_dispatch import PiiDispatcher, PiiHandoff, PiiMove, nlp_queue
from text_extraction.pipeline_documents import DocumentInput, DocumentInputMeta, OcrNeeded
from text_extraction.pool_dispatch import Handover, LastHopHandoff, LastHopMove, PoolDispatcher
from text_extraction.queue_metrics import dispatch_counter_for
from text_extraction.scheduler import FairQueue, QueueItem
from text_extraction.settings import Settings
from text_extraction.structured.types import StructuredOptions

ALICE = Principal(name="alice", role="user", rate_limit_per_minute=None)
SETTINGS = Settings(auth_mode="none", docling_enabled=True)
OPTIONS = JobOptions(compliance="enforce")
URL = "https://docs.example.org/scan.pdf"


def scenario(test: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., None]:
    """Run an async test body on its own event loop (the suite has no async plugin)."""

    @wraps(test)
    def run(*args: Any, **kwargs: Any) -> None:
        asyncio.run(test(*args, **kwargs))

    return run


class FakeTime:
    """A monotonic clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 100.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


def never() -> bool:
    return False


def needed(draft: Envelope[ExtractResult] | None = None) -> OcrNeeded:
    meta = DocumentInputMeta(kind="pdf", content_type="application/pdf", raw_sha256="0" * 64)
    return OcrNeeded(draft=draft, input=DocumentInput(b"%PDF-1.7\n", meta))


class Parts:
    def __init__(self, settings: Settings = SETTINGS) -> None:
        self.valkey = FakeAsyncRedis()
        self.time = FakeTime()
        self.store = JobStore(self.valkey, settings)
        self.queue = docling_queue(self.valkey, settings)
        self.registry = CollectorRegistry()
        self.docling = DoclingDispatcher(
            self.store,
            self.queue,
            settings,
            registry=self.registry,
            clock=self.time.clock,
            sleep=self.time.sleep,
        )

    def count(self, outcome: str) -> float:
        value = self.registry.get_sample_value(
            "text_extraction_docling_dispatch_total", {"outcome": outcome}
        )
        assert value is not None
        return value


@scenario
async def test_a_new_job_is_deleted_when_prepare_fails() -> None:
    parts = Parts()
    dispatcher = PoolDispatcher(
        "docling", parts.store, parts.queue, SETTINGS, CollectorRegistry(), outcomes=()
    )

    async def refuse(job_id: str) -> None:
        raise OpenLimitReached

    with pytest.raises(OpenLimitReached):
        await dispatcher.submit_job("extract", OPTIONS, ALICE, URL, prepare=refuse)

    assert await parts.valkey.keys("job:*") == []
    assert (await parts.queue.counts()).pending == 0


@scenario
async def test_wait_ends_after_total_within_also_once_claimed() -> None:
    parts = Parts()
    job_id = await parts.docling.submit("extract", OPTIONS, ALICE, URL, needed())
    await parts.store.mark_running(job_id, lease_seconds=60)  # claimed, but never answered

    result = await parts.docling.wait(job_id, detached=never)

    assert result is None
    assert 20 <= parts.time.now - 100 <= 21
    assert parts.count("no_result") == 1


@scenario
async def test_wait_with_extra_seconds_waits_longer() -> None:
    parts = Parts()
    job_id = await parts.docling.submit("extract", OPTIONS, ALICE, URL, needed())

    assert await parts.docling.wait(job_id, extra_seconds=30, detached=never) is None

    assert 50 <= parts.time.now - 100 <= 51
    assert parts.count("not_claimed") == 1


@scenario
async def test_submit_stores_input_and_counts_the_open_job() -> None:
    parts = Parts()
    draft = Envelope[ExtractResult](status="partial")

    job_id = await parts.docling.submit("file", OPTIONS, ALICE, None, needed(draft))

    record = await parts.store.get(job_id)
    assert record is not None
    assert (record.kind, record.state) == ("file", "queued")
    assert await parts.store.url(job_id, 0) == ""
    stored = await parts.store.get_input(job_id, 0, DocumentInputMeta)
    assert stored is not None
    assert stored[0] == b"%PDF-1.7\n"
    assert await parts.store.draft(job_id, 0) == draft
    assert await parts.store.open_docling_jobs("alice") == 1
    claim = await parts.queue.claim(60)
    assert claim.item is not None
    assert (claim.item.job_id, claim.item.client, claim.item.domain) == (job_id, "alice#live", "-")


@scenario
async def test_open_limit_refuses_before_any_input_is_stored() -> None:
    parts = Parts(Settings(auth_mode="none", docling_enabled=True, docling_max_open_per_client=1))
    await parts.docling.submit("extract", OPTIONS, ALICE, URL, needed())

    with pytest.raises(OpenLimitReached):
        await parts.docling.submit("extract", OPTIONS, ALICE, URL, needed())

    assert len(await parts.valkey.keys("job:*:meta")) == 1
    assert len(await parts.valkey.keys("job:*:input:*")) == 1
    assert parts.count("open_limit") == 1


@scenario
async def test_a_reused_job_over_the_limit_is_kept() -> None:
    parts = Parts(Settings(auth_mode="none", docling_enabled=True, docling_max_open_per_client=1))
    await parts.docling.submit("extract", OPTIONS, ALICE, URL, needed())
    p1 = await parts.store.create("extract", ALICE, OPTIONS, [URL])
    await parts.store.mark_running(p1, lease_seconds=60)

    with pytest.raises(OpenLimitReached):
        await parts.docling.submit("extract", OPTIONS, ALICE, URL, needed(), job_id=p1)

    record = await parts.store.get(p1)
    assert record is not None
    assert record.lease_until is not None  # still the P1 job's: it stores the fallback
    assert await parts.store.open_docling_jobs("alice") == 1


@scenario
async def test_a_reused_job_is_queued_and_its_lease_cleared() -> None:
    parts = Parts()
    p1 = await parts.store.create("file", ALICE, OPTIONS, [None])
    await parts.store.mark_running(p1, lease_seconds=60)

    assert await parts.docling.submit("file", OPTIONS, ALICE, None, needed(), job_id=p1) == p1

    record = await parts.store.get(p1)
    assert record is not None
    assert (record.state, record.lease_until) == ("running", None)
    assert (await parts.queue.counts()).pending == 1
    assert parts.count("handed_over") == 1


@scenario
async def test_dispatch_counters_of_two_pools_share_a_registry() -> None:
    parts = Parts()
    BrowserDispatcher(
        parts.store, FairQueue(parts.valkey, SETTINGS, "browser"), SETTINGS, registry=parts.registry
    )

    assert (
        parts.registry.get_sample_value(
            "text_extraction_browser_dispatch_total", {"outcome": "rendered"}
        )
        == 0
    )
    assert parts.count("answered") == 0


# --- the caller's LLM key and the longer waits (rulings S9, S13) --------------------------------

LLM_OPTIONS = JobOptions(
    compliance="enforce",
    structured=StructuredOptions.model_validate(
        {"schema": {"type": "object", "properties": {"a": {"type": "string"}}}, "strategy": "llm"}
    ),
)


@scenario
async def test_a_new_job_keeps_the_callers_llm_key() -> None:
    parts = Parts()
    dispatcher = PoolDispatcher(
        "docling", parts.store, parts.queue, SETTINGS, CollectorRegistry(), outcomes=()
    )

    job_id = await dispatcher.submit_job("extract", OPTIONS, ALICE, URL, llm_key="sk-caller")
    without = await dispatcher.submit_job("extract", OPTIONS, ALICE, URL)

    assert await parts.store.llm_key(job_id) == "sk-caller"
    assert await parts.store.llm_key(without) is None


@scenario
async def test_a_reused_job_keeps_the_key_it_has() -> None:
    parts = Parts()
    p1 = await parts.store.create("extract", ALICE, OPTIONS, [URL])
    await parts.store.put_llm_key(p1, "sk-p1")
    await parts.store.mark_running(p1, lease_seconds=60)
    dispatcher = PoolDispatcher(
        "docling", parts.store, parts.queue, SETTINGS, CollectorRegistry(), outcomes=()
    )

    await dispatcher.submit_job("extract", OPTIONS, ALICE, URL, job_id=p1, llm_key="sk-other")

    assert await parts.store.llm_key(p1) == "sk-p1"


@scenario
async def test_a_new_job_with_a_key_is_deleted_when_the_dispatch_fails() -> None:
    parts = Parts()
    dispatcher = PoolDispatcher(
        "docling", parts.store, parts.queue, SETTINGS, CollectorRegistry(), outcomes=()
    )

    async def refuse(job_id: str) -> None:
        raise OpenLimitReached

    with pytest.raises(OpenLimitReached):
        await dispatcher.submit_job(
            "extract", OPTIONS, ALICE, URL, prepare=refuse, llm_key="sk-caller"
        )

    assert await parts.valkey.keys("job:*") == []  # the key went with the job


@scenario
async def test_the_docling_dispatcher_passes_the_key_on() -> None:
    parts = Parts()

    job_id = await parts.docling.submit(
        "extract", LLM_OPTIONS, ALICE, URL, needed(), llm_key="sk-caller"
    )

    assert await parts.store.llm_key(job_id) == "sk-caller"


def test_pool_waits_are_longer_with_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """A browser, Docling or nlp job of a request that may need the LLM completes only once the
    llm worker wrote its result: the request waits ``LLM_SYNC_WAIT_SECONDS`` longer for it."""
    calls: list[dict[str, Any]] = []

    async def wait_for(self: Any, job_id: str, **limits: Any) -> None:
        calls.append(limits)

    for dispatcher_class in (BrowserDispatcher, DoclingDispatcher, PiiDispatcher):
        monkeypatch.setattr(dispatcher_class, "wait_for", wait_for)
    settings = Settings(
        auth_mode="none",
        browser_timeout_seconds=40,
        docling_sync_wait_seconds=10,
        pii_sync_wait_seconds=20,
        llm_sync_wait_seconds=30,
    )
    store = cast(JobStore, None)
    browser = BrowserDispatcher(store, cast(Any, None), settings)
    docling = DoclingDispatcher(store, cast(Any, None), settings)
    nlp = PiiDispatcher(store, cast(Any, None), settings)

    async def scenario_() -> None:
        for llm in (False, True):
            await browser.wait("j", unblock=False, detached=lambda: False, pii=True, llm=llm)
            await docling.wait("j", detached=lambda: False, pii=True, llm=llm)
            await nlp.wait("j", extra_seconds=5, detached=lambda: False, llm=llm)

    asyncio.run(scenario_())
    plain_browser, plain_docling, plain_nlp, llm_browser, llm_docling, llm_nlp = calls
    assert llm_browser["result_within"] == plain_browser["result_within"] + 30
    assert llm_browser["claim_within"] == plain_browser["claim_within"]
    assert llm_docling["total_within"] == plain_docling["total_within"] + 30
    assert llm_docling["claim_within"] == plain_docling["claim_within"]
    assert plain_nlp["total_within"] == 25
    assert llm_nlp["total_within"] == 55
    assert llm_nlp["claim_within"] == 55


class LowGuard:
    high = False


@pytest.mark.parametrize(
    ("options", "llm_enabled", "llm"),
    [(LLM_OPTIONS, True, True), (OPTIONS, True, False), (LLM_OPTIONS, False, False)],
    ids=["llm-work", "no-llm-work", "service-without-llm"],
)
def test_handoffs_wait_longer_for_stored_options_with_llm_work(
    monkeypatch: pytest.MonkeyPatch, options: JobOptions, llm_enabled: bool, llm: bool
) -> None:
    """The Docling and the nlp handoff of a request that may need the LLM (its stored options,
    on a service with an LLM) wait ``LLM_SYNC_WAIT_SECONDS`` longer (ruling S13): they pass
    ``llm`` to their pool's wait."""
    seen: list[bool] = []

    async def wait(self: Any, job_id: str, **limits: Any) -> None:
        seen.append(limits["llm"])

    monkeypatch.setattr(DoclingDispatcher, "wait", wait)
    monkeypatch.setattr(PiiDispatcher, "wait", wait)
    settings = Settings(auth_mode="none", docling_enabled=True, llm_enabled=llm_enabled)
    parts = Parts(settings)
    nlp = PiiDispatcher(parts.store, nlp_queue(parts.valkey, settings), settings)
    named = Envelope[ExtractResult].model_validate(
        {
            "status": "success",
            "data": {
                "url": URL,
                "final_url": URL,
                "upstream_status": 200,
                "content_type": "text/html",
                "mode_used": "fast",
                "attempts": 1,
                "language": "en",
                "word_count": 3,
                "title": "Page",
                "format": "markdown",
                "content": "Written by Anna Schmidt.",
                "quality": None,
                "timings": {"total_ms": 1, "compliance_ms": 0, "fetch_ms": 0, "convert_ms": 1},
                "compliance": {"mode": "enforce"},
                "metadata": None,
                "links": None,
                "fingerprint": None,
                "cache": "miss",
                "warnings": [],
            },
        }
    )
    with_pii = options.model_copy(update={"pii": PiiOptions(mode="mask")})

    async def scenario_() -> None:
        docling = DoclingHandoff(
            parts.docling,
            LowGuard(),
            Handover(),
            kind="extract",
            options=options,
            principal=ALICE,
            url=URL,
        )
        await docling(needed(), named)
        pii = PiiHandoff(
            nlp, LowGuard(), kind="extract", options=with_pii, principal=ALICE, url=URL
        )
        await pii(named, Handover())

    asyncio.run(scenario_())

    assert seen == [llm, llm]


# --- the last hop: LastHopHandoff and LastHopMove (ruling S13) ------------------------------------

TOY_OUTCOMES = (
    "answered",
    "not_claimed",
    "no_result",
    "handed_over",
    "backend_error",
    "memory_guard",
)


class HighGuard:
    high = True


class ToyHandoff(LastHopHandoff):
    """A last hop on pool ``toy``: an envelope owes it while its status is ``partial``; the
    unserved answer is ``failed`` with the reason as the message."""

    def __init__(self, dispatcher: PoolDispatcher, guard: Any, *, result: Any = None) -> None:
        super().__init__(
            dispatcher, guard, pool="toy", kind="extract", options=OPTIONS, principal=ALICE, url=URL
        )
        self.result = result
        self.waited: list[str] = []

    def needs(self, envelope: Envelope[ExtractResult]) -> bool:
        return envelope.status == "partial"

    def unserved(self, envelope: Envelope[ExtractResult], reason: Any) -> Envelope[ExtractResult]:
        return envelope.model_copy(update={"status": "failed", "trace_id": reason})

    async def _wait(
        self, job_id: str, *, extra_seconds: float, detached: Callable[[], bool]
    ) -> Envelope[ExtractResult] | None:
        self.waited.append(job_id)
        if isinstance(self.result, Exception):
            raise self.result
        return cast(Envelope[ExtractResult] | None, self.result)


class Toy:
    def __init__(self) -> None:
        self.valkey = FakeAsyncRedis()
        self.store = JobStore(self.valkey, SETTINGS)
        self.queue = FairQueue(self.valkey, SETTINGS, "toy")
        self.registry = CollectorRegistry()
        self.dispatcher = PoolDispatcher(
            "toy", self.store, self.queue, SETTINGS, self.registry, outcomes=TOY_OUTCOMES
        )

    def count(self, outcome: str) -> float | None:
        return self.registry.get_sample_value(
            "text_extraction_toy_dispatch_total", {"outcome": outcome}
        )


OWING = Envelope[ExtractResult](status="partial")


@scenario
async def test_last_hop_handoff_passes_an_envelope_that_owes_nothing() -> None:
    toy = Toy()
    handoff = ToyHandoff(toy.dispatcher, LowGuard())
    done = Envelope[ExtractResult](status="success")

    assert await handoff(done, Handover()) is done
    assert handoff.waited == []
    assert await toy.valkey.keys("job:*") == []


@scenario
async def test_last_hop_handoff_busy_while_the_guard_is_high() -> None:
    toy = Toy()
    handoff = ToyHandoff(toy.dispatcher, HighGuard())

    answer = await handoff(OWING, Handover())

    assert answer is not None
    assert (answer.status, answer.trace_id) == ("failed", "busy")
    assert toy.count("memory_guard") == 1
    assert await toy.valkey.keys("job:*") == []


@scenario
async def test_last_hop_handoff_answers_the_pools_result_and_deletes_its_job() -> None:
    toy = Toy()
    result = Envelope[ExtractResult](status="success")
    handoff = ToyHandoff(toy.dispatcher, LowGuard(), result=result)
    handover = Handover()

    answer = await handoff(OWING, handover)

    assert answer is result
    assert handover.pool == "toy"  # claimed for this pool's job
    assert handoff.waited == [handover.pool_job]
    assert await toy.valkey.keys("job:*") == []  # answered: the job is gone


@pytest.mark.parametrize("outcome", [None, RedisConnectionError("down")], ids=["none", "valkey"])
def test_last_hop_handoff_failed_without_a_result(outcome: Exception | None) -> None:
    async def scenario_() -> None:
        toy = Toy()
        handoff = ToyHandoff(toy.dispatcher, LowGuard(), result=outcome)

        answer = await handoff(OWING, Handover())

        assert answer is not None
        assert (answer.status, answer.trace_id) == ("failed", "failed")
        assert await toy.valkey.keys("job:*") == []  # abandoned

    asyncio.run(scenario_())


@scenario
async def test_last_hop_handoff_puts_the_envelope_on_the_p1_job() -> None:
    toy = Toy()
    handoff = ToyHandoff(toy.dispatcher, LowGuard())
    handover = Handover()
    p1 = await toy.store.create("extract", ALICE, OPTIONS, [URL])
    assert handover.claim_p1()
    handover.settle(p1)

    assert await handoff(OWING, handover) is None  # the pool's worker writes item 0

    assert handoff.waited == []
    assert await toy.store.draft(p1, 0) is not None
    assert (await toy.queue.counts()).pending == 1


@scenario
async def test_last_hop_move_stores_the_draft_and_queues_the_item() -> None:
    toy = Toy()
    job_id = await toy.store.create("batch", ALICE, OPTIONS, [URL])
    job = await toy.store.get(job_id)
    assert job is not None
    item = QueueItem(job_id=job_id, index=0, client="alice", attempts=1, domain="docs.example.org")
    counter = dispatch_counter_for(toy.registry, TOY_OUTCOMES, pool="toy")

    assert await LastHopMove(toy.store, toy.queue, LowGuard(), outcomes=counter)(job, item, OWING)
    assert await toy.store.draft(job_id, 0) is not None
    assert (await toy.queue.counts()).pending == 1

    refused = LastHopMove(toy.store, toy.queue, HighGuard(), outcomes=counter)
    assert not await refused(job, item, OWING)
    assert toy.count("memory_guard") == 1


def dom_draft() -> Envelope[ExtractResult]:
    """A result with a page's text and a DOM-stage value read from its raw HTML."""
    return Envelope[ExtractResult].model_validate(
        {
            "status": "success",
            "data": {
                "url": URL,
                "final_url": URL,
                "upstream_status": 200,
                "content_type": "text/html",
                "mode_used": "fast",
                "attempts": 1,
                "language": "en",
                "word_count": 3,
                "title": "Page",
                "format": "markdown",
                "content": "Written by Anna Schmidt.",
                "quality": None,
                "timings": {"total_ms": 1, "compliance_ms": 0, "fetch_ms": 0, "convert_ms": 1},
                "compliance": {"mode": "enforce"},
                "metadata": None,
                "links": None,
                "fingerprint": None,
                "cache": "miss",
                "warnings": [],
                "structured": {"values": {"author": "Anna Schmidt"}, "stage": "dom"},
            },
        }
    )


def test_the_pii_hop_drops_dom_values_from_its_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defence in depth (R12 b): what the nlp worker gets, from a sync request or a move, has
    no DOM-stage ``structured``: only patterns and the LLM fill fields after the masking."""
    pii = JobOptions(compliance="enforce", pii=PiiOptions(mode="mask"))

    async def no_result(self: Any, job_id: str, **limits: Any) -> None:
        return None

    monkeypatch.setattr(PiiDispatcher, "wait", no_result)

    async def scenario_() -> tuple[Any, Any]:
        settings = Settings(auth_mode="none", pii_enabled=True)
        parts = Parts(settings)
        queue = nlp_queue(parts.valkey, settings)
        drafts: list[Any] = []
        put_draft = parts.store.put_draft

        async def kept(job_id: str, index: int, envelope: Envelope[ExtractResult]) -> None:
            drafts.append(envelope)
            await put_draft(job_id, index, envelope)

        monkeypatch.setattr(parts.store, "put_draft", kept)
        nlp = PiiDispatcher(parts.store, queue, settings)
        handoff = PiiHandoff(nlp, LowGuard(), kind="extract", options=pii, principal=ALICE, url=URL)
        await handoff(dom_draft(), Handover())

        job_id = await parts.store.create("batch", ALICE, pii, [URL])
        job = await parts.store.get(job_id)
        assert job is not None
        item = QueueItem(job_id=job_id, index=0, client="alice", attempts=1, domain="d")
        await PiiMove(parts.store, queue, LowGuard())(job, item, dom_draft())
        return drafts[0], drafts[1]

    sync, moved = asyncio.run(scenario_())

    for draft in (sync, moved):
        assert draft.data.structured is None
        assert draft.data.content == "Written by Anna Schmidt."  # for the nlp worker to mask
