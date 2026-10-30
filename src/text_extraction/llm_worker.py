"""The llm worker (entry point ``text-extraction-llm-worker``): reads the results of the ``llm``
pool, runs their post stage with the LLM and stores the finished results in the job (ruling S14),
with the image descriptions of ``describe_images`` (issue #20).

It is the extract worker's ``Worker`` (``worker.py``: slot loops, leases, exhausted items, result
writes, metrics, ``/ready`` and the shutdown) with another runner. The ``llm_dispatch`` module
docstring has the contract of the pool's items: their job, key and draft. Besides the operator's
endpoint (``LLM_BASE_URL``) its only outbound requests fetch a page's images for their
descriptions, through its image fetcher (``vision.fetch.create_image_fetcher``: the SSRF guard,
the job's compliance mode, the outbound gate's per-domain turns, ``USER_AGENT``; never a page,
never the caller's cookies or headers). An image upload's bytes are read from the job item's
input (``input:{index}``), which the ``Worker`` deletes with the result. It runs from the API
image: it imports the OpenAI SDK, and no model, NLP or browser library.

**The runner** (``LlmRunner``) leases an item for ``5.5 * LLM_TIMEOUT_SECONDS + 10`` seconds
(``jobs.llm_lease_seconds``: the longest deadline below plus one timeout). For each item it:

1. reads the item's draft (``JobStore.draft``): the envelope after the DOM stage, the patterns
   and PII. A draft that expired with its job answers ``internal_error``, retryable, without
   data: there is nothing to post-process, and the caller sends the request again. So does a
   draft that does not validate (logged by its error's type only: pydantic's message quotes the
   input). A draft whose post stage ran already (an item that ran twice) is answered as it is;
2. reads the job's ``structured``, ``chunking`` and ``describe_images`` and the caller's key
   (``JobStore.llm_key``, else ``LLM_API_KEY``: rule 3); for an image upload that owes its
   description, the item's stored input (gone: the image is skipped);
3. runs ``structured.post.apply_post`` with an ``LlmClient`` of that key, under an overall
   deadline of ``3 * LLM_TIMEOUT_SECONDS`` (the SDK retries a call twice, ``LLM_TIMEOUT_SECONDS``
   each). Learned selectors are stored for the job's owner and the page's host
   (``structured.store.SelectorStore``), where the pipeline reads them. The image descriptions
   (step 0) get half that deadline (``describe_seconds``) on top of it, for an item with an image
   to send, so the structured and semantic work keep the whole deadline (ruling R-V2); the images
   are fetched in the job's compliance mode. LLM failures are ``llm_failed`` in the result, with
   its content. The deadline, and an unexpected error of a later step, answer the envelope after
   step 0 without the rest of the LLM work (``finish_post(..., reason="failed")``:
   ``llm_failed``, retryable): the descriptions made stay (R-V2). No key at all, and an
   unexpected error before that, answer the draft so (logged by the error's type only).

Before any client is made, a draft that still owes the PII step is withheld
(``pii.fields.withheld``: ``pii_failed``; no path hands one over, but the LLM never sees personal
data, D3 a), and a draft that lost its content to ``JOB_ITEM_MAX_BYTES`` while it owes LLM work
or ``semantic`` chunking answers ``llm_failed``, not retryable, "The document is too large for
the LLM step." (``structured.post.finish_too_large``, ruling R20).

The ``Worker`` deletes the draft once the result is written (also of an exhausted item, which
answers its draft without the LLM work, or ``internal_error``, retryable, when the draft is
gone), and drops the caller's LLM key (a sync job's at once, a batch's with its last result).

**The process** runs ``LLM_CONCURRENCY`` slot loops and serves only the internal port;
``/ready`` answers 200 once the loops run. At start-up it starts the selector children that check
the proposals of ``learn_selectors`` (``structured.selector_pool``, ruling R19); the post stage's
CPU work runs on its post pool (``POST_CONCURRENCY`` threads, R21). No model is loaded. On SIGTERM
the loops stop claiming and the items in flight get 30 s; the ones still running are then given back
to the queue at once (``give_back_on_cancel``: an item's lease is long), and run again elsewhere.

- ``--check`` sends one tiny extraction to the configured endpoint (``LLM_BASE_URL``,
  ``LLM_MODEL``, ``LLM_API_KEY``) and exits 0 when its answer fits the schema, else 1 (an
  operator's smoke test; it needs a reachable endpoint and is not run in CI).

Logs carry job ids, error types and codes only: never a text, a value, a selector or a key.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from typing import Final, Literal

import structlog
from prometheus_client import REGISTRY, CollectorRegistry
from pydantic import ValidationError
from redis.asyncio import Redis

from text_extraction.conversion_pool import ConversionPool
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.extract_models import ExtractResult, failed_extract
from text_extraction.internal import Readiness, create_internal_app
from text_extraction.jobs import JobRecord, JobStore, llm_lease_seconds
from text_extraction.llm.basics import LlmError
from text_extraction.llm.client import LlmClient
from text_extraction.llm_dispatch import llm_queue
from text_extraction.pii.fields import needs_pii, withheld
from text_extraction.scheduler import QueueItem
from text_extraction.server import _Server, _server, configured_settings
from text_extraction.settings import Settings
from text_extraction.structured.post import (
    apply_post,
    finish_post,
    finish_too_large,
    llm_extract,
    post_done,
    too_large_for_llm,
)
from text_extraction.structured.schema import compile_schema
from text_extraction.structured.selector_pool import SelectorRunner, create_selector_runner
from text_extraction.structured.store import SelectorStore
from text_extraction.structured.types import StructuredOptions
from text_extraction.valkey import create_valkey
from text_extraction.vision.describe import needs_vision
from text_extraction.vision.fetch import ImageFetcher, ImageSource, create_image_fetcher
from text_extraction.vision.types import DescribeImagesOptions, StoredInputMeta
from text_extraction.worker import Worker, _serve

_log = structlog.get_logger(__name__)

DRAFT_EXPIRED_MESSAGE: Final = (
    "The result to post-process expired before the LLM step ran. Send the request again."
)
DRAFT_INVALID_MESSAGE: Final = (
    "The result to post-process could not be read for the LLM step. Send the request again."
)
# The tiny extraction of ``--check``.
_CHECK_SCHEMA: Final = {"type": "object", "properties": {"title": {"type": "string"}}}
_CHECK_DOCUMENT: Final = "# Check\n\nThis short page is titled Check."


class LlmRunner:
    """The ``llm`` pool's ``ItemRunner``; see the module docstring. ``selectors`` keeps the
    learned selectors; ``registry`` gets the LLM client's and the descriptions' metrics;
    ``images`` fetches the page images to describe (None: they are skipped)."""

    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        selectors: SelectorStore,
        *,
        registry: CollectorRegistry = REGISTRY,
        post_pool: ConversionPool | None = None,
        selector_runner: SelectorRunner | None = None,
        images: ImageSource | None = None,
    ) -> None:
        """``post_pool`` runs the post stage's CPU work and ``selector_runner`` the proposals of
        ``learn_selectors`` (rulings R21, R19); without them the runner makes its own."""
        self._settings = settings
        self._store = store
        self._selectors = selectors
        self._registry = registry
        self._images = images
        self._post_pool = (
            post_pool
            if post_pool is not None
            else ConversionPool(settings.post_concurrency, name="post")
        )
        self._selector_runner = (
            selector_runner if selector_runner is not None else create_selector_runner(settings)
        )
        self.lease_seconds = llm_lease_seconds(settings)
        # The item's overall deadline: three requests to the endpoint (the SDK's retries); an
        # item that owes image descriptions gets ``describe_seconds`` on top (ruling R-V2).
        self.deadline_seconds = 3 * settings.llm_timeout_seconds

    @property
    def describe_seconds(self) -> float:
        """The image descriptions' budget: half the item's deadline (ruling V-S12), given on
        top of it (R-V2)."""
        return self.deadline_seconds / 2

    async def run(
        self,
        job: JobRecord,
        item: QueueItem,
        url: str,
        *,
        gate_refusals: Literal["fail", "raise"],
    ) -> Envelope[ExtractResult]:
        # The worker deletes the draft once the result is written (``Worker._drop_input``): an
        # item that runs again finds it.
        try:
            draft = await self._store.draft(job.id, item.index)
        except ValidationError as error:
            # The type only: pydantic's message quotes the stored text.
            _log.error("llm_draft_invalid", error=type(error).__name__)
            return failed_extract(ErrorCode.INTERNAL_ERROR, DRAFT_INVALID_MESSAGE, retryable=True)
        if draft is None:
            _log.warning("llm_draft_expired")
            return failed_extract(ErrorCode.INTERNAL_ERROR, DRAFT_EXPIRED_MESSAGE, retryable=True)
        structured, chunking = job.options.structured, job.options.chunking
        describe = job.options.describe_images
        if post_done(draft, structured, chunking, describe=describe):
            return draft  # nothing is owed: done before, or nothing to read
        if needs_pii(draft, job.options.pii):
            # No path hands such a draft over, but this is the only code that sends content to
            # a third party: the LLM never sees personal data (D3 a, M-a).
            _log.error("llm_draft_owes_pii")
            return withheld(draft, job.options.pii)
        if too_large_for_llm(draft, structured, chunking, describe=describe):
            # Its content was not stored (JOB_ITEM_MAX_BYTES): the LLM work cannot be done (R20).
            return await self._post_pool.run(
                partial(
                    finish_too_large,
                    settings=self._settings,
                    describe=describe,
                    registry=self._registry,
                ),
                draft,
                structured,
                chunking,
            )
        key = await self._store.llm_key(job.id)
        try:
            client = LlmClient(self._settings, key, registry=self._registry)
        except LlmError:  # no key at all: the requests' side refuses this earlier
            _log.error("llm_key_missing")
            return await self._without_llm(draft, structured, job)
        try:
            upload = await self._upload_of(draft, describe, job, item)
        except BaseException:
            await client.aclose()
            raise
        seconds = self.deadline_seconds
        if draft.data is not None and needs_vision(draft.data, describe):
            seconds += self.describe_seconds  # the structured and semantic work keep theirs
        try:
            return await apply_post(
                draft,
                structured,
                chunking,
                settings=self._settings,
                llm=client,
                store=self._selectors,
                owner=job.owner,
                threads=self._post_pool,
                selectors=self._selector_runner,
                describe=describe,
                images=self._images,
                upload=upload,
                mode=job.options.compliance,
                describe_seconds=self.describe_seconds,
                registry=self._registry,
                deadline=asyncio.get_running_loop().time() + seconds,
            )
        except Exception as error:
            # The draft is a finished result: it keeps its content (M-b). The type only, no
            # traceback: a message may quote the page.
            _log.error("llm_post_failed", error_type=type(error).__name__)
            return await self._without_llm(draft, structured, job)
        finally:
            await client.aclose()

    async def _upload_of(
        self,
        draft: Envelope[ExtractResult],
        describe: DescribeImagesOptions | None,
        job: JobRecord,
        item: QueueItem,
    ) -> bytes | None:
        """An image upload's bytes for its description: the item's stored input (V-S7), or None
        for anything else, and when the input is gone (the image is skipped then)."""
        data = draft.data
        if data is None or data.mode_used != "upload" or not needs_vision(data, describe):
            return None
        stored = await self._store.get_input(job.id, item.index, StoredInputMeta)
        if stored is None:
            _log.warning("llm_upload_input_gone")
            return None
        return stored[0]

    async def _without_llm(
        self, draft: Envelope[ExtractResult], structured: StructuredOptions | None, job: JobRecord
    ) -> Envelope[ExtractResult]:
        """The draft's post stage without the LLM: ``llm_failed``, retryable, its content."""
        return await self._post_pool.run(
            partial(
                finish_post,
                settings=self._settings,
                reason="failed",
                describe=job.options.describe_images,
                registry=self._registry,
            ),
            draft,
            structured,
            job.options.chunking,
        )


# --- the process ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class LlmWorkerParts:
    """What one llm worker process runs on: the worker and the settings, and the selector
    children, post pool and image fetcher to close at the end."""

    worker: Worker
    settings: Settings
    selectors: SelectorRunner
    post_pool: ConversionPool
    images: ImageFetcher


def build_llm_worker(
    settings: Settings, valkey: Redis, registry: CollectorRegistry
) -> LlmWorkerParts:
    """The llm worker of a process, nothing started yet."""
    store = JobStore(valkey, settings)
    selectors = SelectorStore(valkey, settings.selector_learn_ttl_seconds)
    selector_runner = create_selector_runner(settings)
    post_pool = ConversionPool(settings.post_concurrency, name="post")
    images = create_image_fetcher(settings, valkey)
    runner = LlmRunner(
        settings,
        store,
        selectors,
        registry=registry,
        post_pool=post_pool,
        selector_runner=selector_runner,
        images=images,
    )
    worker = Worker(
        settings,
        valkey,
        runner,
        store,
        llm_queue(valkey, settings),
        registry,
        concurrency=settings.llm_concurrency,
        give_back_on_cancel=True,  # the lease is four LLM timeouts: do not leave items waiting
        post_pool=post_pool,
    )
    return LlmWorkerParts(
        worker=worker,
        settings=settings,
        selectors=selector_runner,
        post_pool=post_pool,
        images=images,
    )


async def serve(parts: LlmWorkerParts, internal: _Server, readiness: Readiness) -> None:
    """Start the selector children, run the worker until a signal, then let the items in
    flight finish for ``LLM_GRACEFUL_SECONDS``. The caller closes the rest."""
    await _serve(
        parts.worker,
        internal,
        readiness,
        graceful_seconds=parts.settings.llm_graceful_seconds,
        startup=parts.selectors.start,
    )


async def _run(settings: Settings) -> None:
    valkey = create_valkey(settings.valkey_url)
    registry = REGISTRY
    parts = build_llm_worker(settings, valkey, registry)
    readiness = Readiness()
    internal = _server(
        create_internal_app(settings, valkey, registry, readiness=readiness),
        settings.internal_host,
        settings.internal_port,
        settings.llm_graceful_seconds,
    )
    try:
        await serve(parts, internal, readiness)
    finally:
        await parts.selectors.shutdown()
        parts.post_pool.shutdown()
        await parts.images.aclose()
        await valkey.aclose()
        _log.info("valkey_closed")


async def _check(settings: Settings) -> int:
    """One tiny extraction against the configured endpoint: 0 when its answer fits the schema,
    else 1 (logged with the error's code and its message, which names the HTTP status at
    most)."""
    compiled = compile_schema(_CHECK_SCHEMA)
    try:
        client = LlmClient(settings, None, registry=CollectorRegistry())
    except LlmError as error:
        _log.error("llm_check_failed", code=error.code.value, message=error.message)
        return 1
    try:
        _values, usage, _warnings = await llm_extract(
            client,
            _CHECK_DOCUMENT,
            compiled,
            compiled.fields,
            None,
            max_chars=settings.llm_max_input_tokens * 4,
        )
    except LlmError as error:
        _log.error(
            "llm_check_failed",
            code=error.code.value,
            retryable=error.retryable,
            message=error.message,
        )
        return 1
    finally:
        await client.aclose()
    _log.info("llm_check_passed", model=usage.model, calls=usage.calls)
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="text-extraction-llm-worker",
        description="Run the llm worker (pool llm) of text-extraction.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Send one tiny extraction to the configured LLM endpoint, then exit "
        "(0: its answer fitted the schema).",
    )
    args = parser.parse_args(argv)
    settings = configured_settings()
    if args.check:
        sys.exit(asyncio.run(_check(settings)))
    asyncio.run(_run(settings))
