"""The Docling worker (``docling_worker.py``) with fakes: its runner on the real ``Worker``, job
store and pipeline (fakeredis, a fake fetcher), a fake Docling pool instead of the children, the
start-up checks, and the parent's imports in a fresh interpreter. The models store is tested in
``tests/test_model_store.py``.

The real conversions (models, Tesseract) are in ``tests/docling`` (marker ``docling``)."""

import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from structlog.testing import capture_logs

from tests.fakes import fetched
from tests.test_worker import (
    OPTIONS,
    OWNER,
    SCAN_BODY,
    SCAN_URL,
    Harness,
    OcrPool,
    error_of,
    ocr_harness,
    scenario,
)
from text_extraction import docling_worker
from text_extraction.compliance import ComplianceReport
from text_extraction.docling_worker import (
    OCR_TIMEOUT_MESSAGE,
    DoclingConverter,
    DoclingRunner,
    DoclingStartFailed,
    SelfCheckFailed,
    create_docling_pool,
    self_check,
    start_children,
)
from text_extraction.documents.pool import (
    ConversionError,
    ConversionOutOfMemory,
    ConversionTimeout,
)
from text_extraction.envelope import ErrorCode
from text_extraction.extract_models import ExtractRequest
from text_extraction.file_models import UploadInputMeta
from text_extraction.job_models import JobItem, JobOptions
from text_extraction.jobs import docling_lease_seconds
from text_extraction.llm_dispatch import LlmMove, create_llm_move, llm_queue
from text_extraction.pipeline import DOCUMENT_FETCHED_DIRECTLY_WARNING
from text_extraction.pipeline_documents import OCR_UNAVAILABLE_WARNING, DocumentInputMeta
from text_extraction.structured.types import StructuredOptions
from text_extraction.vision.types import (
    IMAGE_NOT_DESCRIBED_WARNING,
    DescribeImagesOptions,
    StoredInputMeta,
)
from text_extraction.worker import Worker

OCR_TEXT = " ".join(
    [
        "Die Photosynthese. Pflanzen wandeln Lichtenergie in chemische Energie um. Dabei nehmen",
        "die Blätter Kohlenstoffdioxid aus der Luft auf und geben Sauerstoff ab.",
    ]
    * 4
)  # over QUALITY_MIN_WORDS (50): a success


def reply(content: str = OCR_TEXT, **fields: Any) -> dict[str, Any]:
    """A reply of the child's ``"docling"`` function (``DocumentConversion.from_reply``)."""
    values: dict[str, Any] = {
        "content": content,
        "title": "Die Photosynthese",
        "warnings": [],
        "pages": 1,
        "pages_converted": 1,
        "needs_ocr": False,
        "metadata": {"language": "de"},
    }
    values.update(fields)
    return values


class FakeDoclingPool:
    """The Docling children: answers ``answer`` (a reply, or an exception to raise) and records
    each call."""

    def __init__(self, answer: dict[str, Any] | BaseException | None = None) -> None:
        self.answer: dict[str, Any] | BaseException = reply() if answer is None else answer
        self.calls: list[tuple[str, dict[str, Any], float]] = []

    async def start(self) -> None:
        pass

    async def run(self, function_name: str, payload: dict[str, Any], deadline: float) -> Any:
        self.calls.append((function_name, payload, deadline))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    async def shutdown(self) -> None:
        pass


class Docling:
    """A Docling worker next to a harness: its runner on the harness's store, queue ``docling``
    and pipeline, converting with ``pool``."""

    def __init__(
        self, harness: Harness, pool: FakeDoclingPool, *, llm_move: LlmMove | None = None
    ) -> None:
        self.harness = harness
        self.pool = pool
        self.converter = DoclingConverter(pool, harness.settings, harness.registry)
        self.runner = DoclingRunner(
            harness.pipeline, harness.store, harness.settings, self.converter
        )
        self.worker = Worker(
            harness.settings,
            harness.valkey,
            self.runner,
            harness.store,
            harness.docling_queue,
            harness.registry,
            clock=harness.clock,
            concurrency=1,
            llm_move=llm_move,
        )

    async def process_next(self) -> None:
        claim = await self.harness.docling_queue.claim(self.runner.lease_seconds)
        assert claim.item is not None, "nothing queued on docling"
        await self.worker.process(claim.item)


async def moved_scan(harness: Harness, url: str = SCAN_URL) -> str:
    """A batch whose scan the extract worker moved to ``docling`` with its input and draft."""
    job_id = await harness.submit([url])
    claim = await harness.claim()
    assert claim.item is not None
    await harness.worker.process(claim.item)
    return job_id


async def moved_upload(harness: Harness) -> str:
    """An upload's job whose scan the extract worker moved to ``docling``."""
    options = JobOptions(mode="fast", compliance="enforce", cache=False, format="markdown")
    job_id = await harness.store.create("file", OWNER, options, [None])
    meta = UploadInputMeta(file_name="geheim-bericht.pdf", content_type="application/pdf")
    await harness.store.put_input(job_id, 0, SCAN_BODY, meta, 600)
    await harness.queue.enqueue_items(job_id, OWNER.name, [(0, None)])
    claim = await harness.claim()
    assert claim.item is not None
    await harness.worker.process(claim.item)
    return job_id


async def only_item(harness: Harness, job_id: str) -> JobItem:
    (item,) = await harness.items(job_id)
    assert item.result is not None
    return item


# --- the runner ---------------------------------------------------------------------------------


@scenario
async def test_runner_keeps_better_of_draft_and_ocr() -> None:
    harness = ocr_harness()
    docling = Docling(harness, FakeDoclingPool())
    job_id = await moved_scan(harness)
    assert await harness.store.draft(job_id, 0) is not None

    await docling.process_next()

    item = await only_item(harness, job_id)
    assert item.result is not None
    assert item.result.status == "success"
    data = item.result.data
    assert data is not None
    assert data.content == OCR_TEXT
    assert data.document is not None
    assert (data.document.converter, data.document.ocr, data.document.kind) == (
        "docling",
        True,
        "pdf",
    )
    assert (data.url, data.mode_used, data.content_type) == (SCAN_URL, "fast", "application/pdf")
    assert OCR_UNAVAILABLE_WARNING not in data.warnings
    assert await harness.store.draft(job_id, 0) is None


@scenario
async def test_runner_keeps_the_draft_when_ocr_reads_less() -> None:
    harness = ocr_harness()
    docling = Docling(harness, FakeDoclingPool(reply("")))  # OCR found no text at all
    job_id = await moved_scan(harness)

    await docling.process_next()

    item = await only_item(harness, job_id)
    assert item.result is not None
    data = item.result.data
    assert data is not None
    assert data.content == "Thin text layer."  # MarkItDown's text layer
    assert data.document is not None
    assert data.document.converter == "markitdown"
    assert OCR_UNAVAILABLE_WARNING in data.warnings
    assert await harness.store.draft(job_id, 0) is None


class TextLayerPool:
    """MarkItDown's children: every PDF reads ``content``, with the probe's ``needs_ocr``."""

    def __init__(self, content: str, *, needs_ocr: bool) -> None:
        self.content = content
        self.needs_ocr = needs_ocr

    async def start(self) -> None:
        pass

    async def run(self, function_name: str, payload: dict[str, Any], deadline: float) -> Any:
        return {
            "content": self.content,
            "title": None,
            "warnings": [],
            "pages": 1,
            "pages_converted": 1,
            "needs_ocr": self.needs_ocr,
            "metadata": None,
        }

    async def shutdown(self) -> None:
        pass


# A broken text layer (F4): many words, none of them readable.
GARBAGE = " ".join(f"(cid:{n})" for n in range(300))
# Docling under ``ocr: always`` leaves out headers and footers: fewer words than the text layer.
LAYER_TEXT = OCR_TEXT + " " + " ".join(["Kopfzeile Seite eins von zwei."] * 20)


def layer_harness(content: str, *, needs_ocr: bool) -> Harness:
    scan = fetched(SCAN_URL, SCAN_BODY, content_type="application/pdf")
    pool = TextLayerPool(content, needs_ocr=needs_ocr)
    return Harness({SCAN_URL: scan}, document_pool=pool, docling_enabled=True)


@scenario
async def test_a_scan_keeps_its_ocr_result_with_fewer_words() -> None:
    """The probe said the text layer is broken: any OCR result that did not fail wins, also
    against a draft with more (garbage) words (rulings F4, F11)."""
    harness = layer_harness(GARBAGE, needs_ocr=True)
    docling = Docling(harness, FakeDoclingPool())
    job_id = await moved_scan(harness)
    draft = await harness.store.draft(job_id, 0)
    assert draft is not None
    assert draft.data is not None
    assert draft.data.word_count > OCR_TEXT.count(" ") + 1  # better() would keep the draft

    await docling.process_next()

    item = await only_item(harness, job_id)
    assert item.result is not None
    data = item.result.data
    assert data is not None
    assert data.content == OCR_TEXT
    assert data.document is not None
    assert data.document.converter == "docling"
    assert OCR_UNAVAILABLE_WARNING not in data.warnings
    assert data.fingerprint is not None  # the OCR result is the final one: compared and stored


@scenario
async def test_a_scan_keeps_a_partial_ocr_result() -> None:
    """One word of OCR is ``partial``, not ``failed``: it wins against a thin text layer."""
    harness = ocr_harness()
    docling = Docling(harness, FakeDoclingPool(reply("Kurz.")))
    job_id = await moved_scan(harness)

    await docling.process_next()

    item = await only_item(harness, job_id)
    assert item.result is not None
    assert item.result.status == "partial"
    data = item.result.data
    assert data is not None
    assert data.content == "Kurz."
    assert data.document is not None
    assert data.document.converter == "docling"
    assert OCR_UNAVAILABLE_WARNING not in data.warnings


@scenario
async def test_ocr_always_keeps_the_ocr_result_with_fewer_words() -> None:
    """``ocr: always`` asked for Docling: its result wins, though it read fewer words than a good
    text layer (F11)."""
    harness = layer_harness(LAYER_TEXT, needs_ocr=False)
    docling = Docling(harness, FakeDoclingPool())
    options = JobOptions(compliance="enforce", ocr="always")
    job_id = await harness.store.create("batch", OWNER, options, [SCAN_URL])
    await harness.queue.enqueue(job_id, OWNER.name, [SCAN_URL])
    await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]
    stored = await harness.store.get_input(job_id, 0, DocumentInputMeta)
    assert stored is not None
    assert stored[1].needs_ocr is False  # moved for the option, not for the probe

    await docling.process_next()

    item = await only_item(harness, job_id)
    assert item.result is not None
    data = item.result.data
    assert data is not None
    assert data.content == OCR_TEXT
    assert data.document is not None
    assert data.document.converter == "docling"


@scenario
async def test_otherwise_the_better_of_draft_and_ocr_is_kept() -> None:
    """Neither ``ocr: always`` nor a probe that asked for OCR: ``escalation.better`` decides,
    and a draft with more words stays as it is."""
    harness = ocr_harness()
    docling = Docling(harness, FakeDoclingPool(reply("Kurz.")))
    job_id = await harness.store.create("batch", OWNER, OPTIONS, [SCAN_URL])
    meta = DocumentInputMeta(
        kind="pdf",
        content_type="application/pdf",
        final_url=SCAN_URL,
        upstream_status=200,
        raw_sha256=hashlib.sha256(SCAN_BODY).hexdigest(),
        needs_ocr=False,
    )
    await harness.store.put_input(job_id, 0, SCAN_BODY, meta, 600)
    # MarkItDown's result without the OCR fallback's warning.
    request = ExtractRequest(url=SCAN_URL, ocr="never", cache=False)
    draft = await harness.pipeline.run(request, OWNER)
    assert draft.data is not None
    await harness.store.put_draft(job_id, 0, draft)
    await harness.docling_queue.enqueue_items(job_id, OWNER.name, [(0, SCAN_URL)])

    await docling.process_next()

    item = await only_item(harness, job_id)
    assert item.result is not None
    data = item.result.data
    assert data is not None
    assert data.content == "Thin text layer."
    assert data.document is not None
    assert data.document.converter == "markitdown"
    assert OCR_UNAVAILABLE_WARNING not in data.warnings


@pytest.mark.parametrize(
    "answer",
    [ConversionError("ModelsMissing", "x"), RuntimeError("pool shut down")],
    ids=["models-missing", "pool-unavailable"],
)
@scenario
async def test_a_retryable_ocr_failure_keeps_the_draft_unstored(answer: BaseException) -> None:
    """The OCR may work on the next try: the draft answers, but is neither fingerprinted nor
    cached, so the next request gets the OCR."""
    harness = ocr_harness()
    docling = Docling(harness, FakeDoclingPool(answer))
    job_id = await moved_scan(harness)

    await docling.process_next()

    item = await only_item(harness, job_id)
    assert item.result is not None
    data = item.result.data
    assert data is not None
    assert data.content == "Thin text layer."
    assert OCR_UNAVAILABLE_WARNING in data.warnings
    assert data.fingerprint is None
    assert await harness.valkey.keys("res:*") == []
    assert await harness.valkey.keys("fp:*") == []


@scenario
async def test_a_final_ocr_failure_stores_the_draft() -> None:
    """The same bytes would fail again (here: too much memory): the draft is the result."""
    harness = ocr_harness()
    docling = Docling(harness, FakeDoclingPool(ConversionOutOfMemory("limit")))
    job_id = await moved_scan(harness)

    await docling.process_next()

    item = await only_item(harness, job_id)
    assert item.result is not None
    assert item.result.data is not None
    assert item.result.data.fingerprint is not None
    assert await harness.valkey.keys("res:*") != []
    assert await harness.valkey.keys("fp:*") != []


PAGE_URL = "https://docs.example.org/lesson.html"


@scenario
async def test_a_queued_direct_fetch_keeps_its_run_context() -> None:
    """A file a render ended on (F18), queued for OCR: the result keeps the run's target and
    warnings, and its cache entry has no validators (the file's do not fit the page)."""
    harness = ocr_harness()
    docling = Docling(harness, FakeDoclingPool())
    job_id = await harness.store.create("batch", OWNER, OPTIONS, [PAGE_URL])
    meta = DocumentInputMeta(
        kind="pdf",
        content_type="application/pdf",
        final_url=SCAN_URL,
        upstream_status=200,
        etag='"v1"',  # not to be applied, even if an input carried them
        last_modified="Tue, 01 Sep 2026 10:00:00 GMT",
        raw_sha256=hashlib.sha256(SCAN_BODY).hexdigest(),
        needs_ocr=True,
        compliance=ComplianceReport(mode="enforce", decision="allowed"),
        attempts=2,
        target=SCAN_URL,
        warnings=[DOCUMENT_FETCHED_DIRECTLY_WARNING],
    )
    await harness.store.put_input(job_id, 0, SCAN_BODY, meta, 600)
    await harness.docling_queue.enqueue_items(job_id, OWNER.name, [(0, PAGE_URL)])

    await docling.process_next()

    item = await only_item(harness, job_id)
    assert item.result is not None
    assert item.result.status == "success", item.result.error
    data = item.result.data
    assert data is not None
    assert (data.url, data.final_url, data.mode_used) == (PAGE_URL, SCAN_URL, "fast")
    assert data.warnings[0] == DOCUMENT_FETCHED_DIRECTLY_WARNING
    assert data.attempts == 2
    (key,) = await harness.valkey.keys("res:*")
    raw = await harness.valkey.get(key)
    assert raw is not None
    entry = json.loads(raw)
    assert entry["validators"] == {"etag": None, "last_modified": None}


@scenario
async def test_runner_writes_cache_for_url_items() -> None:
    harness = ocr_harness()
    docling = Docling(harness, FakeDoclingPool())
    job_id = await moved_scan(harness)

    await docling.process_next()

    item = await only_item(harness, job_id)
    assert item.result is not None
    assert item.result.data is not None
    assert item.result.data.fingerprint is not None
    assert item.result.data.fingerprint.changed is None  # first sight, but stored
    assert await harness.valkey.keys("fp:*") != []
    # The next request for the URL is served from the cache, with the OCR result.
    request = ExtractRequest(url=SCAN_URL, compliance="enforce")
    again = await harness.pipeline.run(request, OWNER)
    assert again.data is not None
    assert again.data.cache == "hit"
    assert again.data.content == OCR_TEXT
    assert again.data.document is not None
    assert again.data.document.converter == "docling"


@scenario
async def test_runner_does_not_cache_uploads() -> None:
    harness = Harness(document_pool=OcrPool(), docling_enabled=True)
    docling = Docling(harness, FakeDoclingPool())
    job_id = await moved_upload(harness)

    await docling.process_next()

    item = await only_item(harness, job_id)
    assert item.result is not None
    assert item.result.status == "success"
    data = item.result.data
    assert data is not None
    assert (data.url, data.mode_used, data.cache) == (None, "upload", "bypass")
    assert data.document is not None
    assert (data.document.converter, data.document.file_name) == ("docling", "geheim-bericht.pdf")
    assert data.fingerprint is not None
    assert data.fingerprint.changed is None
    assert await harness.valkey.keys("res:*") == []
    assert await harness.valkey.keys("fp:*") == []


def assert_draft_without_ocr(item: JobItem) -> None:
    """The item answered its text layer with ``ocr_unavailable``, neither compared nor stored."""
    assert item.result is not None
    data = item.result.data
    assert data is not None
    assert data.content == "Thin text layer."
    assert data.document is not None
    assert data.document.converter == "markitdown"
    assert OCR_UNAVAILABLE_WARNING in data.warnings
    assert data.fingerprint is None


@scenario
async def test_an_expired_input_answers_the_draft() -> None:
    """The input's TTL ran out while it waited, but the draft holds the text layer: it answers,
    as when the OCR fails retryably (neither fingerprinted nor cached)."""
    harness = ocr_harness()
    docling = Docling(harness, FakeDoclingPool())
    job_id = await moved_scan(harness)
    await harness.store.delete_input(job_id, 0)  # its TTL ran out while it waited

    await docling.process_next()

    assert_draft_without_ocr(await only_item(harness, job_id))
    assert docling.pool.calls == []
    assert await harness.store.draft(job_id, 0) is None
    assert await harness.valkey.keys("res:*") == []
    assert await harness.valkey.keys("fp:*") == []


async def moved_image(
    harness: Harness, options: JobOptions | None = None, image: bytes | None = None
) -> str:
    """An image upload's job the extract worker moved to ``docling`` (no draft); ``image`` is
    its bytes (default: ``IMAGE``)."""
    if options is None:
        options = JobOptions(mode="fast", compliance="enforce", cache=False)
    job_id = await harness.store.create("file", OWNER, options, [None])
    image = image if image is not None else b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    meta = UploadInputMeta(file_name="scan.png", content_type="image/png")
    await harness.store.put_input(job_id, 0, image, meta, 600)
    await harness.queue.enqueue_items(job_id, OWNER.name, [(0, None)])
    claim = await harness.claim()
    assert claim.item is not None
    await harness.worker.process(claim.item)
    return job_id


IMAGE = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64  # what ``moved_image`` stores
PLAIN_IMAGE_JOB = JobOptions(mode="fast", compliance="enforce", cache=False, metadata=True)
DESCRIBED_IMAGE_JOB = PLAIN_IMAGE_JOB.model_copy(
    update={"describe_images": DescribeImagesOptions()}
)
LLM_IMAGE_JOB = PLAIN_IMAGE_JOB.model_copy(
    update={
        "structured": StructuredOptions.model_validate(
            {
                "schema": {"type": "object", "properties": {"x": {"type": "string"}}},
                "strategy": "llm",
            }
        )
    }
)


def llm_docling(answer: dict[str, Any], **overrides: Any) -> tuple[Harness, Docling]:
    """A harness with OCR and the LLM, and a Docling worker that moves results to ``llm``."""
    harness = Harness(
        document_pool=OcrPool(),
        docling_enabled=True,
        llm_enabled=True,
        llm_api_key="sk-operator",
        **overrides,
    )
    move = create_llm_move(harness.settings, harness.valkey, harness.store, None, harness.registry)
    return harness, Docling(harness, FakeDoclingPool(answer), llm_move=move)


@pytest.mark.parametrize("describe", [True, False], ids=["describe", "plain"])
@scenario
async def test_image_upload_empty_ocr_text_is_success_with_describe(describe: bool) -> None:
    """V-S8: an image upload of ``describe_images`` whose OCR reads no word is no
    ``empty_content``: it goes on to its description. Without the option: unchanged."""
    harness, docling = llm_docling(reply(content=""))
    job_id = await moved_image(harness, DESCRIBED_IMAGE_JOB if describe else PLAIN_IMAGE_JOB)

    await docling.process_next()

    if not describe:
        code, _retryable, _message = error_of(await only_item(harness, job_id))
        assert code == ErrorCode.EMPTY_CONTENT
        return
    draft = await harness.store.draft(job_id, 0)  # the llm worker's now
    assert draft is not None
    assert (draft.status, draft.error) == ("success", None)
    assert draft.data is not None
    assert (draft.data.content, draft.data.word_count) == ("", 0)
    assert draft.data.document is not None
    assert draft.data.document.converter == "docling"
    assert (await llm_queue(harness.valkey, harness.settings).counts()).pending == 1


@pytest.mark.parametrize(
    ("options", "kept"),
    [(DESCRIBED_IMAGE_JOB, True), (LLM_IMAGE_JOB, False)],
    ids=["describe", "structured-only"],
)
@scenario
async def test_docling_image_upload_keeps_input_for_llm(options: JobOptions, kept: bool) -> None:
    """V-S7: an image upload's job with ``describe_images`` that moves on to ``llm`` keeps its
    input for the description (the llm worker deletes it with the result); without the option
    it goes with the move, as before."""
    harness, docling = llm_docling(reply())
    job_id = await moved_image(harness, options)

    await docling.process_next()

    assert (await llm_queue(harness.valkey, harness.settings).counts()).pending == 1
    stored = await harness.store.get_input(job_id, 0, StoredInputMeta)
    if not kept:
        assert stored is None
        return
    assert stored is not None
    body, meta = stored
    assert body == IMAGE
    assert meta.content_type == "image/png"


@scenario
async def test_image_upload_over_vision_cap_takes_no_llm_hop() -> None:
    """Ruling R-V3 (final review m1): an image upload over ``LLM_VISION_MAX_IMAGE_BYTES``
    cannot be described. Its result reports the image skipped at once, takes no llm hop for
    its description and keeps no input in the budget."""
    harness, docling = llm_docling(reply(), llm_vision_max_image_bytes=10_000)
    job_id = await moved_image(harness, DESCRIBED_IMAGE_JOB, image=IMAGE + b"\x00" * 20_000)

    await docling.process_next()

    assert (await llm_queue(harness.valkey, harness.settings).counts()).pending == 0
    assert await harness.store.get_input(job_id, 0, StoredInputMeta) is None
    item = await only_item(harness, job_id)
    assert item.result is not None
    assert item.result.status == "success", item.result.error
    data = item.result.data
    assert data is not None
    assert data.image_descriptions is not None
    report = data.image_descriptions
    assert (report.asked, report.described, report.skipped, report.failed) == (1, 0, 1, 0)
    assert IMAGE_NOT_DESCRIBED_WARNING in data.warnings
    skipped = harness.registry.get_sample_value(
        "text_extraction_vision_images_total", {"outcome": "skipped"}
    )
    assert skipped == 1


@scenario
async def test_an_expired_input_without_a_draft_fails_timeout_retryable() -> None:
    harness = Harness(document_pool=OcrPool(), docling_enabled=True)
    docling = Docling(harness, FakeDoclingPool())
    job_id = await moved_image(harness)
    assert await harness.store.draft(job_id, 0) is None
    await harness.store.delete_input(job_id, 0)

    await docling.process_next()

    code, retryable, _message = error_of(await only_item(harness, job_id))
    assert (code, retryable) == (ErrorCode.TIMEOUT, True)
    assert docling.pool.calls == []


@pytest.mark.parametrize(
    "answer",
    [reply(), ConversionError("DocumentUnreadable", "x")],
    ids=["converted", "failed"],
)
@scenario
async def test_input_deleted_after_outcome(answer: dict[str, Any] | BaseException) -> None:
    harness = ocr_harness()
    docling = Docling(harness, FakeDoclingPool(answer))
    job_id = await moved_scan(harness)
    assert await harness.valkey.keys("job:*:input:*") != []

    await docling.process_next()

    assert await harness.completed(job_id)
    assert await harness.valkey.keys("job:*:input:*") == []
    assert await harness.store.draft(job_id, 0) is None


@scenario
async def test_a_failed_result_write_keeps_the_draft_for_the_rerun() -> None:
    harness = ocr_harness()
    docling = Docling(harness, FakeDoclingPool())
    job_id = await moved_scan(harness)

    async def write_result(*_args: Any, **_kwargs: Any) -> Any:
        raise RedisConnectionError("valkey went away")

    harness.store.write_result = write_result  # type: ignore[method-assign]
    with pytest.raises(RedisConnectionError):
        await docling.process_next()

    assert await harness.store.draft(job_id, 0) is not None
    assert await harness.valkey.keys("job:*:input:*") != []


@scenario
async def test_an_exhausted_item_answers_its_draft_and_drops_it() -> None:
    """Lost too often (its children crashed): the text layer answers with ``ocr_unavailable``,
    neither compared nor stored, and the draft and the input are gone."""
    harness = ocr_harness()
    docling = Docling(harness, FakeDoclingPool(AssertionError("not converted")))
    job_id = await moved_scan(harness)
    claim = await harness.docling_queue.claim(docling.runner.lease_seconds)
    assert claim.item is not None

    await docling.worker.process(claim.item, exhausted=True)

    assert_draft_without_ocr(await only_item(harness, job_id))
    assert await harness.store.draft(job_id, 0) is None
    assert await harness.valkey.keys("job:*:input:*") == []
    assert await harness.valkey.keys("res:*") == []


@scenario
async def test_an_exhausted_item_without_a_draft_is_lost() -> None:
    harness = Harness(document_pool=OcrPool(), docling_enabled=True)
    docling = Docling(harness, FakeDoclingPool(AssertionError("not converted")))
    job_id = await moved_image(harness)
    claim = await harness.docling_queue.claim(docling.runner.lease_seconds)
    assert claim.item is not None

    await docling.worker.process(claim.item, exhausted=True)

    code, retryable, _message = error_of(await only_item(harness, job_id))
    assert (code, retryable) == (ErrorCode.INTERNAL_ERROR, True)
    assert await harness.valkey.keys("job:*:input:*") == []


@scenario
async def test_child_timeout_fails_item() -> None:
    harness = Harness(document_pool=OcrPool(), docling_enabled=True)
    docling = Docling(harness, FakeDoclingPool(ConversionTimeout("killed")))
    # An image has no draft: the timeout is the item's result.
    options = JobOptions(mode="fast", compliance="enforce", cache=False)
    job_id = await harness.store.create("file", OWNER, options, [None])
    image = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    meta = UploadInputMeta(file_name="scan.png", content_type="image/png")
    await harness.store.put_input(job_id, 0, image, meta, 600)
    await harness.queue.enqueue_items(job_id, OWNER.name, [(0, None)])
    claim = await harness.claim()
    assert claim.item is not None
    await harness.worker.process(claim.item)
    started = asyncio.get_running_loop().time()

    await docling.process_next()

    code, retryable, message = error_of(await only_item(harness, job_id))
    assert (code, retryable, message) == (ErrorCode.TIMEOUT, False, OCR_TIMEOUT_MESSAGE)
    assert message == "OCR did not finish within DOCLING_TIMEOUT_SECONDS."
    ((_name, _payload, deadline),) = docling.pool.calls
    budget = deadline - started
    assert 299 <= budget <= 301  # DOCLING_TIMEOUT_SECONDS of its own, not the extraction's


@scenario
async def test_out_of_memory_is_too_large() -> None:
    harness = ocr_harness()
    docling = Docling(harness, FakeDoclingPool(ConversionOutOfMemory("limit")))
    job_id = await moved_scan(harness)

    await docling.process_next()

    item = await only_item(harness, job_id)
    assert item.result is not None
    # The OCR failed: the text layer stays, and says it did not get the OCR it needed.
    assert item.result.data is not None
    assert item.result.data.document is not None
    assert item.result.data.document.converter == "markitdown"
    assert OCR_UNAVAILABLE_WARNING in item.result.data.warnings


@scenario
async def test_payload_sent_to_the_child() -> None:
    harness = ocr_harness(
        docling_ocr_languages="deu,eng,fra",
        docling_threads=3,
        docling_max_pages=7,
        docling_models_path="/srv/models",
    )
    docling = Docling(harness, FakeDoclingPool())
    await moved_scan(harness)

    await docling.process_next()

    ((name, payload, _deadline),) = docling.pool.calls
    assert name == "docling"
    assert payload["body"] == SCAN_BODY
    assert {key: payload[key] for key in payload if key != "body"} == {
        "kind": "pdf",
        "format": "markdown",
        "full_page_ocr": True,  # the probe said "scan"
        "languages": ["deu", "eng", "fra"],
        "threads": 3,
        "max_pages": 7,
        "models_path": "/srv/models",
        "timeout": 270.0,  # Docling's own: 0.9 of DOCLING_TIMEOUT_SECONDS, for a partial result
        "metadata": True,
        "base": SCAN_URL,
        "content_language": None,
        "max_uncompressed": harness.settings.document_max_uncompressed_bytes,
    }


@scenario
async def test_docling_conversions_are_counted() -> None:
    harness = ocr_harness()
    docling = Docling(harness, FakeDoclingPool(reply(pages=3, pages_converted=3)))
    await moved_scan(harness)

    await docling.process_next()

    labels = {"converter": "docling", "kind": "pdf", "outcome": "success"}
    assert harness.sample("text_extraction_document_conversions_total", labels) == 1
    assert harness.sample("text_extraction_document_pages_total", {"converter": "docling"}) == 3
    assert (
        harness.sample(
            "text_extraction_document_conversion_seconds_count", {"converter": "docling"}
        )
        == 1
    )


@scenario
async def test_failures_log_no_message_and_no_file_name() -> None:
    harness = Harness(document_pool=OcrPool(), docling_enabled=True)
    docling = Docling(harness, FakeDoclingPool(ConversionError("KeyError", "Geheimer Inhalt")))
    job_id = await moved_upload(harness)

    with capture_logs() as logs:
        await docling.process_next()

    item = await only_item(harness, job_id)
    assert item.result is not None
    assert item.result.data is not None
    assert OCR_UNAVAILABLE_WARNING in item.result.data.warnings  # the draft stays
    text = json.dumps(logs, default=str)
    assert "Geheimer" not in text
    assert "geheim-bericht" not in text
    assert any(log.get("error_type") == "KeyError" for log in logs)


def test_runner_lease_covers_two_conversions() -> None:
    harness = ocr_harness()
    docling = Docling(harness, FakeDoclingPool())
    assert docling.runner.lease_seconds == docling_lease_seconds(harness.settings) == 610


# --- fingerprints of documents (the ocr option is part of the variant) -------------------------


@scenario
async def test_ocr_modes_keep_their_own_fingerprints() -> None:
    """``ocr: never`` (MarkItDown's text layer) and ``ocr: auto`` (OCR) of the same URL convert
    differently: each keeps its own previous fingerprint, so alternating them does not report a
    change."""
    harness = ocr_harness()
    docling = Docling(harness, FakeDoclingPool())
    never = ExtractRequest(url=SCAN_URL, compliance="enforce", cache=False, ocr="never")
    first = await harness.pipeline.run(never, OWNER)
    assert first.data is not None
    assert first.data.fingerprint is not None
    assert first.data.fingerprint.changed is None

    await moved_scan(harness)  # ocr: auto, read with OCR
    await docling.process_next()

    second = await harness.pipeline.run(never, OWNER)
    assert second.data is not None
    assert second.data.fingerprint is not None
    assert second.data.fingerprint.changed is False


# --- start-up: the self-check and the models ----------------------------------------------------


def test_self_check_passes_on_the_built_in_scan() -> None:
    pool = FakeDoclingPool(reply("Selbsttest der Texterkennung\n\nDie Prüfung läuft über Bücher."))
    settings = ocr_harness().settings

    asyncio.run(self_check(pool, settings, children=2))

    assert len(pool.calls) == 2
    payload = pool.calls[0][1]
    assert payload["kind"] == "png"
    assert payload["body"].startswith(b"\x89PNG")
    assert payload["full_page_ocr"] is True


@pytest.mark.parametrize(
    "answer",
    [reply("Selbsttest der Texterkennung"), ConversionError("ModelsMissing", "no layout")],
    ids=["wrong-text", "error"],
)
def test_self_check_fails(answer: dict[str, Any] | BaseException) -> None:
    pool = FakeDoclingPool(answer)
    settings = ocr_harness().settings

    with pytest.raises(SelfCheckFailed):
        asyncio.run(self_check(pool, settings, children=1))


def test_missing_models_exit_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("DOCLING_MODELS_PATH", str(tmp_path))

    with pytest.raises(SystemExit) as exit_info:
        docling_worker.main([])

    assert exit_info.value.code == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    (missing,) = [line for line in lines if line["event"] == "docling_models_missing"]
    assert missing["models_path"] == str(tmp_path)
    assert missing["level"] == "error"


def test_check_exits_1_without_models(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("DOCLING_MODELS_PATH", str(tmp_path))

    with pytest.raises(SystemExit) as exit_info:
        docling_worker.main(["--check"])

    assert exit_info.value.code == 1


class NotStarting(FakeDoclingPool):
    """Children that do not start (Docling or torch do not load)."""

    async def start(self) -> None:
        raise RuntimeError("the child did not start")


def test_children_that_do_not_start_are_logged_with_the_models_path() -> None:
    settings = ocr_harness(docling_models_path="/srv/models").settings

    with capture_logs() as logs, pytest.raises(DoclingStartFailed):
        asyncio.run(start_children(NotStarting(), settings, children=1))

    (line,) = [log for log in logs if log["event"] == "docling_start_failed"]
    assert line["models_path"] == "/srv/models"
    assert line["error_type"] == "RuntimeError"
    assert line["log_level"] == "error"


@pytest.mark.parametrize("argv", [[], ["--check"]], ids=["serve", "check"])
def test_children_that_do_not_start_exit_1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("DOCLING_MODELS_PATH", str(tmp_path))
    monkeypatch.setattr(docling_worker, "models_missing", lambda _settings: False)
    monkeypatch.setattr(docling_worker, "create_docling_pool", lambda _settings: NotStarting())

    async def run(settings: Any) -> None:  # the process without its servers: the start-up only
        await start_children(NotStarting(), settings, children=1)

    monkeypatch.setattr(docling_worker, "_run", run)

    with pytest.raises(SystemExit) as exit_info:
        docling_worker.main(argv)

    assert exit_info.value.code == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    (failed,) = [line for line in lines if line["event"] == "docling_start_failed"]
    assert failed["models_path"] == str(tmp_path)
    assert failed["level"] == "error"


def test_docling_pool_gets_model_paths_through_its_own_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VALKEY_PASSWORD", "parent-secret")
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
    settings = ocr_harness(
        docling_models_path="/srv/models", docling_threads=3, docling_concurrency=2
    ).settings

    pool = create_docling_pool(settings)

    env = pool._env
    assert env["TESSDATA_PREFIX"] == "/srv/models/tessdata"
    assert env["OMP_NUM_THREADS"] == "3"
    assert env["HF_HUB_OFFLINE"] == "1"
    assert "VALKEY_PASSWORD" not in env
    assert (pool._processes, pool._memory_limit_mb) == (2, 6144)
    assert pool._target == "text_extraction.documents.docling_child"


# --- the parent process --------------------------------------------------------------------------


def test_worker_does_not_import_docling_in_parent() -> None:
    """The worker process imports neither Docling nor torch (nor MarkItDown's libraries): only
    its children do."""
    script = """
import sys
from fakeredis import FakeAsyncRedis
from prometheus_client import CollectorRegistry
from text_extraction import docling_worker
from text_extraction.settings import Settings

settings = Settings(auth_mode="none", docling_enabled=True)
parts = docling_worker.build_docling_worker(settings, FakeAsyncRedis(), CollectorRegistry())
forbidden = {"docling", "docling_core", "docling_parse", "docling_ibm_models", "torch",
             "torchvision", "transformers", "markitdown", "pdfminer", "pdfplumber", "pandas"}
loaded = sorted({name.split(".")[0] for name in sys.modules} & forbidden)
parts.conversion_pool.shutdown()
print(",".join(loaded))
"""
    result = subprocess.run(  # noqa: S603 - our own interpreter and script
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, timeout=120
    )
    assert result.stdout.strip() == ""
