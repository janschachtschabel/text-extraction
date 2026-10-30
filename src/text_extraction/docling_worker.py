"""The Docling worker (entry point ``text-extraction-docling-worker``): reads the documents of the
``docling`` pool with OCR, layout and table models and stores their results in the job.

It is the extract worker's ``Worker`` (``worker.py``: slot loops, leases, exhausted items, result
writes, metrics, ``/ready`` and the shutdown) with another runner (ruling F14); an OCR result of
a job with ``pii`` moves on to the ``nlp`` pool there (``pii_dispatch.PiiMove``), and the item's
input and draft are then left to the nlp worker. The
``docling_dispatch`` module docstring has the contract of the pool's items: their job, input and
draft. This process never imports Docling or torch: only its children do
(``documents/docling_child.py``), so a crash, a hang or a memory blow-up of a conversion ends
there.

**The runner** (``DoclingRunner``) leases an item for ``2 * DOCLING_TIMEOUT_SECONDS + 10``
seconds. For each item it:

1. reads the item's input (``DocumentInputMeta`` and the bytes) and its draft (MarkItDown's
   result, None for an image or an OpenDocument file). An input that expired before the claim
   answers the draft with ``ocr_unavailable``, neither fingerprinted nor cached; without a
   draft the item fails ``timeout``, retryable: the caller sends the document again;
2. rebuilds the request from the job: its options with its URL, or an upload's form;
3. runs ``Pipeline.convert_queued_document`` with the Docling conversion (``DoclingConverter``):
   the result is built as inline, the OCR result is kept (under ``ocr: always`` or for a scan,
   unless it failed; otherwise the better of draft and OCR result), and a URL's kept result
   goes to the result cache and the fingerprints (an upload's does not);
4. leaves the input and the draft to the ``Worker``, which deletes both after the result write
   (also of an exhausted item). An item that runs again (a failed write, a Valkey error, a
   cancelled shutdown) finds both.

**The conversion** runs in one of ``DOCLING_CONCURRENCY`` children (``DocumentPool``, one slot
loop each), each with ``DOCLING_MEMORY_LIMIT_MB`` of address space, by a deadline of its own:
``DOCLING_TIMEOUT_SECONDS`` from its start, not the extraction's. At the deadline the child is
killed and the item fails ``timeout``, not retryable: the same file would time out again. The
children get the models' paths, ``OMP_NUM_THREADS`` (``DOCLING_THREADS``) and Hugging Face's
offline switch through their own environment, never through this process's. Failures map as for
MarkItDown (``pipeline_documents.failed_conversion``); a model missing on the volume is
``internal_error``, retryable. ``text_extraction_document_conversions_total`` and its siblings
count the conversions with ``converter="docling"`` (F19).

**The process** serves only the internal port. Before anything else it checks the models
(``model_store``): a file of the manifest that is missing under ``DOCLING_MODELS_PATH``
ends the process with exit code 1 and a log line naming that path. Then it starts the children
(each imports Docling), and each converts the built-in one-page scan
(``documents/scan_check.png``), which loads the models; children that do not start
(``docling_start_failed``) or a failed check (``docling_self_check_failed``) end the process
before ``/ready`` with exit code 1 and a JSON log line naming the models' path. On SIGTERM
the loops stop claiming, the items in flight get 30 s, then they are cancelled and the children
killed; their leases expire and the items run again.

- ``--download-models`` fills ``DOCLING_MODELS_PATH`` from the manifest (the Helm init container
  and the compose one-shot service run it; idempotent) and exits 0, or 1 when a file fails.
- ``--check`` checks the models and converts the built-in scan in one child, then exits 0, or 1
  on any failure (the image build and CI run it).

Logs carry job ids, kinds and codes only, never a URL, a file name, a converter's message or a
document byte.
"""

import argparse
import asyncio
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Final, Literal, Protocol

import structlog
from prometheus_client import REGISTRY, CollectorRegistry
from redis.asyncio import Redis

from text_extraction import model_store
from text_extraction.conversion_pool import ConversionPool
from text_extraction.docling_dispatch import docling_queue, draft_without_ocr
from text_extraction.documents.pool import ConversionError, DocumentPool
from text_extraction.documents.types import DocumentConversion
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.extract_models import ExtractRequest, ExtractResult, failed_extract
from text_extraction.fetch import Fetcher
from text_extraction.file_models import form_of
from text_extraction.internal import Readiness, create_internal_app
from text_extraction.jobs import JobRecord, JobStore, docling_lease_seconds
from text_extraction.llm_dispatch import create_llm_move
from text_extraction.memory_guard import MemoryGuard
from text_extraction.pii_dispatch import create_pii_move
from text_extraction.pipeline import Pipeline, create_outbound, create_pipeline
from text_extraction.pipeline_documents import (
    CONVERSION_FAILURES,
    DocumentInput,
    DocumentInputMeta,
    DocumentRun,
    document_metrics_for,
    failed_conversion,
)
from text_extraction.scheduler import QueueItem
from text_extraction.server import _Server, _server, configured_settings
from text_extraction.settings import Settings
from text_extraction.valkey import create_valkey
from text_extraction.worker import Worker, _serve, job_principal

_log = structlog.get_logger(__name__)

# The ``DocumentPool`` target of the Docling children.
DOCLING_TARGET: Final = "text_extraction.documents.docling_child"
OCR_TIMEOUT_MESSAGE: Final = "OCR did not finish within DOCLING_TIMEOUT_SECONDS."
INPUT_EXPIRED_MESSAGE: Final = (
    "The document expired before a worker read it with OCR. Send it again."
)
_MODELS_MISSING_MESSAGE: Final = "OCR is not available at the moment. Try again later."
# What the items in flight get after SIGTERM before they are cancelled (F14).
GRACEFUL_SECONDS: Final = 30
# Importing Docling and torch takes a while on a cold volume.
_CHILD_START_SECONDS: Final = 180.0
# The built-in scan of the self-check and words its OCR must find (they prove the German model).
_CHECK_IMAGE: Final = "scan_check.png"
_CHECK_WORDS: Final = ("selbsttest", "prüfung", "läuft")
# Docling's own ``document_timeout``, as a share of DOCLING_TIMEOUT_SECONDS: it stops between
# pages and answers what it has (``pages_partial``) before the child is killed at the deadline.
DOCLING_TIMEOUT_SHARE: Final = 0.9


class DocumentRunner(Protocol):
    """The part of ``DocumentPool`` the conversion uses (a fake in tests)."""

    async def run(self, function_name: str, payload: dict[str, Any], deadline: float) -> Any: ...


class DocumentChildren(DocumentRunner, Protocol):
    """``DocumentRunner`` with the start of the children (a fake in tests)."""

    async def start(self) -> None: ...


def create_docling_pool(settings: Settings) -> DocumentPool:
    """The Docling children of a worker (``DOCLING_CONCURRENCY``, ``DOCLING_MEMORY_LIMIT_MB``);
    not started yet. The models' paths, the thread count and Hugging Face's offline switch go
    through the children's own environment."""
    models = settings.docling_models_path
    return DocumentPool(
        settings.docling_concurrency,
        settings.docling_memory_limit_mb,
        target=DOCLING_TARGET,
        start_timeout=_CHILD_START_SECONDS,
        extra_env={
            "TESSDATA_PREFIX": str(models / "tessdata"),
            "OMP_NUM_THREADS": str(settings.docling_threads),
            # Nothing is fetched: the models come from the volume only.
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HOME": str(Path(tempfile.gettempdir()) / "text-extraction-hf"),
        },
    )


def docling_payload(
    document: DocumentInput, fmt: str, settings: Settings, *, full_page_ocr: bool | None = None
) -> dict[str, Any]:
    """The child's payload for ``document`` (``documents/docling_convert.convert``). Full-page
    OCR when the probe said "scan" (``needs_ocr``), unless ``full_page_ocr`` says otherwise."""
    meta = document.meta
    return {
        "body": document.body,
        "kind": meta.kind,
        "format": fmt,
        "full_page_ocr": meta.needs_ocr if full_page_ocr is None else full_page_ocr,
        "languages": settings.docling_ocr_languages.split(","),
        "threads": settings.docling_threads,
        "max_pages": settings.docling_max_pages,
        "models_path": str(settings.docling_models_path),
        "timeout": settings.docling_timeout_seconds * DOCLING_TIMEOUT_SHARE,
        "metadata": True,  # for the language; the result keeps it only when asked
        "base": meta.final_url,
        "content_language": meta.content_language,
        "max_uncompressed": settings.document_max_uncompressed_bytes,
    }


class DoclingConverter:
    """The Docling conversion of one document in a child (a ``DocumentConvert``): its reply, or
    the run's failed envelope. Counted in ``registry`` with ``converter="docling"``."""

    def __init__(
        self, pool: DocumentRunner, settings: Settings, registry: CollectorRegistry
    ) -> None:
        self._pool = pool
        self._settings = settings
        self._metrics = document_metrics_for(registry)

    async def __call__(
        self, run: DocumentRun, document: DocumentInput
    ) -> DocumentConversion | Envelope[ExtractResult]:
        kind = document.meta.kind
        payload = docling_payload(document, run.req.format, self._settings)
        # A budget of its own: the document waited in the queue, not in an extraction.
        deadline = asyncio.get_running_loop().time() + self._settings.docling_timeout_seconds
        started = time.perf_counter()
        outcome: DocumentConversion | Envelope[ExtractResult]
        try:
            outcome = DocumentConversion.from_reply(
                await self._pool.run("docling", payload, deadline)
            )
        except CONVERSION_FAILURES as error:
            if isinstance(error, ConversionError) and error.type_name == "ModelsMissing":
                _log.error(
                    "docling_models_missing",
                    models_path=str(self._settings.docling_models_path),
                )
                outcome = run.failed(
                    ErrorCode.INTERNAL_ERROR, _MODELS_MISSING_MESSAGE, retryable=True
                )
            else:
                # Not retryable: the child had all of DOCLING_TIMEOUT_SECONDS.
                outcome = failed_conversion(
                    run, kind, error, timeout_message=OCR_TIMEOUT_MESSAGE, timeout_retryable=False
                )
        finally:
            run.convert_ms += round((time.perf_counter() - started) * 1000)
        self._metrics.observe_outcome("docling", kind, outcome, time.perf_counter() - started)
        return outcome


class DoclingRunner:
    """The ``docling`` pool's ``ItemRunner``; see the module docstring. ``convert`` is the
    Docling conversion (``DoclingConverter``)."""

    def __init__(
        self,
        pipeline: Pipeline,
        store: JobStore,
        settings: Settings,
        convert: DoclingConverter,
    ) -> None:
        self._pipeline = pipeline
        self._store = store
        self._convert = convert
        self.lease_seconds = docling_lease_seconds(settings)

    async def run(
        self,
        job: JobRecord,
        item: QueueItem,
        url: str,
        *,
        gate_refusals: Literal["fail", "raise"],
    ) -> Envelope[ExtractResult] | None:
        # The worker deletes the input and the draft once the result is written (``Worker.
        # _drop_input``): an item that runs again finds both.
        stored = await self._store.get_input(job.id, item.index, DocumentInputMeta)
        draft = await self._store.draft(job.id, item.index)
        if stored is None:
            _log.info("docling_input_expired", draft=draft is not None)
            fallback = draft_without_ocr(draft)
            if fallback is not None:
                return fallback
            return failed_extract(ErrorCode.TIMEOUT, INPUT_EXPIRED_MESSAGE, retryable=True)
        body, meta = stored
        request = (
            form_of(job.options)
            if job.kind == "file"
            else ExtractRequest(url=url, **job.options.model_dump())
        )
        return await self._pipeline.convert_queued_document(
            request, job_principal(job), DocumentInput(body, meta), draft, self._convert
        )


# --- start-up: the models and the self-check ----------------------------------------------------


class SelfCheckFailed(Exception):  # noqa: N818 - a failed check, named as such
    """The built-in scan did not convert to its text."""


def _check_document() -> DocumentInput:
    body = resources.files("text_extraction.documents").joinpath(_CHECK_IMAGE).read_bytes()
    meta = DocumentInputMeta(kind="png", content_type="image/png", raw_sha256="0" * 64)
    return DocumentInput(body, meta)


async def self_check(pool: DocumentRunner, settings: Settings, *, children: int) -> None:
    """Convert the built-in scan in each of ``children`` children (they run one call at a
    time, so as many calls at once reach each child once): this loads their models. Raises
    ``SelfCheckFailed`` when one fails or its text lacks the expected words."""
    document = _check_document()
    payload = docling_payload(document, "text", settings, full_page_ocr=True)
    deadline = asyncio.get_running_loop().time() + settings.docling_timeout_seconds
    replies = await asyncio.gather(
        *(pool.run("docling", payload, deadline) for _ in range(children)),
        return_exceptions=True,
    )
    for reply in replies:
        if isinstance(reply, BaseException):
            if not isinstance(reply, Exception):
                raise reply
            # The type only: a converter's message may quote a path or a document.
            error_type = reply.type_name if isinstance(reply, ConversionError) else None
            _log.error(
                "docling_self_check_failed",
                error=type(reply).__name__,
                child_error=error_type,
                models_path=str(settings.docling_models_path),
            )
            raise SelfCheckFailed("the built-in scan did not convert") from None
        text = DocumentConversion.from_reply(reply).content.casefold()
        if not all(word in text for word in _CHECK_WORDS):
            _log.error(
                "docling_self_check_failed",
                reason="text_not_found",
                models_path=str(settings.docling_models_path),
            )
            raise SelfCheckFailed("the built-in scan's text was not found")
    _log.info("docling_self_check_passed", children=children)


class DoclingStartFailed(Exception):  # noqa: N818 - a failed start, named as such
    """The Docling children did not start (Docling or torch do not load)."""


async def start_children(pool: DocumentChildren, settings: Settings, *, children: int) -> None:
    """Start ``pool``'s children, then the self-check in each. Children that do not start are
    logged with the models' path (never more than the error's type) and raise
    ``DoclingStartFailed``; a failed check raises ``SelfCheckFailed``."""
    try:
        await pool.start()
    except Exception as error:
        _log.error(
            "docling_start_failed",
            models_path=str(settings.docling_models_path),
            error_type=type(error).__name__,
        )
        raise DoclingStartFailed("the Docling children did not start") from None
    await self_check(pool, settings, children=children)


def models_missing(settings: Settings) -> bool:
    """Whether a file of the models manifest is missing on the volume; logs the volume's path
    when one is."""
    root = settings.docling_models_path
    missing = model_store.missing_models(root, model_store.load_manifest("docling"))
    if missing:
        _log.error("docling_models_missing", models_path=str(root), missing=len(missing))
    return bool(missing)


# --- the process ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class DoclingWorkerParts:
    """What one Docling worker process runs on: the worker, its memory guard, its children, and
    the fetcher and conversion pool of its pipeline to close at the end (nothing is fetched)."""

    worker: Worker
    guard: MemoryGuard
    docling_pool: DocumentPool
    fetcher: Fetcher
    conversion_pool: ConversionPool
    settings: Settings
    post_pool: ConversionPool  # the post stage's (POST_CONCURRENCY, ruling R21)

    async def start(self) -> None:
        """Start the children, then the self-check in each (before ``/ready``)."""
        await start_children(
            self.docling_pool, self.settings, children=self.settings.docling_concurrency
        )


def build_docling_worker(
    settings: Settings, valkey: Redis, registry: CollectorRegistry
) -> DoclingWorkerParts:
    """The Docling worker of a process, nothing started yet. Its result cache skips writes
    while the memory guard reports Valkey as nearly full, as in the extract worker."""
    guard = MemoryGuard(valkey, settings.valkey_memory_guard_ratio, registry)
    gate, fetcher = create_outbound(settings, valkey)
    conversion_pool = ConversionPool(settings.convert_concurrency)
    post_pool = ConversionPool(settings.post_concurrency, name="post")
    pipeline = create_pipeline(
        settings, valkey, fetcher, registry, conversion_pool, gate=gate, memory_guard=guard
    )
    docling_pool = create_docling_pool(settings)
    store = JobStore(valkey, settings)
    worker = Worker(
        settings,
        valkey,
        DoclingRunner(
            pipeline, store, settings, DoclingConverter(docling_pool, settings, registry)
        ),
        store,
        docling_queue(valkey, settings),
        registry,
        concurrency=settings.docling_concurrency,
        pii_move=create_pii_move(settings, valkey, store, guard, registry),
        llm_move=create_llm_move(settings, valkey, store, guard, registry),
        post_pool=post_pool,
    )
    return DoclingWorkerParts(
        worker=worker,
        guard=guard,
        docling_pool=docling_pool,
        fetcher=fetcher,
        conversion_pool=conversion_pool,
        settings=settings,
        post_pool=post_pool,
    )


async def serve(parts: DoclingWorkerParts, internal: _Server, readiness: Readiness) -> None:
    """Start the children and check them, run the worker until a signal, then let the items in
    flight finish for ``GRACEFUL_SECONDS``. The caller kills the children and closes the rest."""
    await _serve(
        parts.worker,
        internal,
        readiness,
        graceful_seconds=GRACEFUL_SECONDS,
        guard=parts.guard,
        startup=parts.start,
    )


async def _run(settings: Settings) -> None:
    valkey = create_valkey(settings.valkey_url)
    registry = REGISTRY
    parts = build_docling_worker(settings, valkey, registry)
    readiness = Readiness()
    internal = _server(
        create_internal_app(settings, valkey, registry, readiness=readiness),
        settings.internal_host,
        settings.internal_port,
        GRACEFUL_SECONDS,
    )
    try:
        await serve(parts, internal, readiness)
    finally:
        await parts.docling_pool.shutdown()  # after the loops: their items are given back
        await parts.fetcher.aclose()
        parts.conversion_pool.shutdown()
        parts.post_pool.shutdown()
        await valkey.aclose()
        _log.info("valkey_closed")


async def _check(settings: Settings) -> int:
    """Convert the built-in scan in one child: 0 when its text was found, else 1."""
    pool = create_docling_pool(settings.model_copy(update={"docling_concurrency": 1}))
    try:
        await start_children(pool, settings, children=1)
    except (SelfCheckFailed, DoclingStartFailed):
        return 1  # logged, with the models' path
    finally:
        await pool.shutdown()
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="text-extraction-docling-worker",
        description="Run the Docling worker (pool docling) of text-extraction.",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--download-models",
        action="store_true",
        help="Download the models of the manifest into DOCLING_MODELS_PATH, then exit "
        "(0: in place).",
    )
    actions.add_argument(
        "--check",
        action="store_true",
        help="Convert the built-in scan once, then exit (0: its text was found).",
    )
    args = parser.parse_args(argv)
    settings = configured_settings()
    if args.download_models:
        sys.exit(_download(settings))
    if models_missing(settings):
        sys.exit(1)
    if args.check:
        sys.exit(asyncio.run(_check(settings)))
    try:
        asyncio.run(_run(settings))
    except (SelfCheckFailed, DoclingStartFailed):
        sys.exit(1)  # logged, with the models' path


def _download(settings: Settings) -> int:
    try:
        model_store.download_models(
            settings.docling_models_path, model_store.load_manifest("docling")
        )
    except (model_store.ModelDownloadFailed, OSError) as error:
        _log.error(
            "docling_models_download_failed",
            models_path=str(settings.docling_models_path),
            reason=str(error) if isinstance(error, model_store.ModelDownloadFailed) else None,
            error_type=type(error).__name__,
        )
        return 1
    return 0
