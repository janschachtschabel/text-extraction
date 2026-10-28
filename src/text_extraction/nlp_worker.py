"""The nlp worker (entry point ``text-extraction-nlp-worker``): reads the results of the ``nlp``
pool, finds personal data in their text and stores the processed results in the job (ruling N13).

It is the extract worker's ``Worker`` (``worker.py``: slot loops, leases, exhausted items, result
writes, metrics, ``/ready`` and the shutdown) with another runner. The ``pii_dispatch`` module
docstring has the contract of the pool's items: their job and draft. There are no input bytes and
nothing is fetched. This process never imports Presidio or spaCy: only its children do
(``pii/child.py``), so a crash, a hang or a memory blow-up of the analysis ends there.

**The runner** (``PiiRunner``) leases an item for ``2 * PII_TIMEOUT_SECONDS + 10`` seconds. For
each item it:

1. reads the item's draft, the unprocessed envelope (``JobStore.draft``). A draft that expired
   with its job answers ``pii_failed``, retryable, without data: the caller sends the request
   again. An envelope without data, one without ``options.pii`` and one that has a ``pii``
   report already (an item that ran twice) are returned as they are;
2. takes the effective entity types once (the request's, else ``PII_DEFAULT_ENTITIES``: ruling
   R7) and the threshold (the request's, else ``PII_SCORE_THRESHOLD``), and lists the strings
   N5 processes (``pii.fields.text_fields``). Over ``PII_MAX_CHARS`` characters in all of them
   the item fails ``too_large``, not retryable, with the content withheld (the child never sees
   it). ``annotate`` with HTML content fails ``pii_failed``, not retryable, withheld: the
   requests' side refuses it earlier;
3. chooses the model (N8): ``de`` or ``en`` in the option; else the language of the result when it
   is ``de`` or ``en``; else ``PII_FALLBACK_LANGUAGE`` with the warning
   ``pii_language_unsupported``;
4. sends the strings that need the analyser, and only those (an author's name and the uploader
   are one person as a whole: ruling R1), to a child in one call, with the models' path and size,
   by a deadline of ``PII_TIMEOUT_SECONDS`` from the moment the call is made. A child loads the
   models lazily, in the call that finds it without them (ruling R12): a child that the pool
   replaced (killed at a deadline, crashed, recycled) is loaded by its own next call, and loads
   once per lifetime, whichever child the pool hands out. The first call of a fresh child spends
   a few seconds of its deadline on the load;
5. applies the mode to the result with the spans (``pii.fields.apply_pii``: N7 resolves them,
   N2 and N10 replace them), sets the report and ``timings.pii_ms``, and counts the entities by
   type in ``text_extraction_pii_entities_total{type}``.

Failures never answer the draft: the child's deadline, a crash, an error of the child and models
that do not load (including a child that runs out of memory while it loads them: the child
reports that as ``LoadFailed``, never as the text's size) are ``failed``/``pii_failed``,
retryable; a child that runs out of memory analysing is ``too_large``, not retryable (the same
text would fail again). Spans and replacement that raise are ``pii_failed`` as well (the type is
logged, never the message: it could quote the text). Each of them is the draft with
its text, title, metadata, links, uploader and file name removed (``pii.fields.fail_closed``)
and keeps what the target answered. The ``Worker`` deletes the draft once the result is written
(also of an exhausted item, which answers ``pii_failed``, retryable, never the draft); an item
that runs again (a failed write, a Valkey error, a cancelled shutdown) finds it.

**The analysis** runs in one of ``PII_CONCURRENCY`` children (``DocumentPool``, one slot loop
each), each with ``PII_MEMORY_LIMIT_MB`` of address space, by a deadline of its own. At the
deadline the child is killed. The models' path and size go to the children in the payload, never
through this process's environment. The pool replaces a child after ``CHILD_MAX_TASKS`` calls.

**The process** serves only the internal port. Before anything else it checks the models
(``model_store``): a model of ``PII_MODEL_SIZE`` that is missing under ``PII_MODELS_PATH`` ends the
process with exit code 1 and a log line naming that path. Then it starts the children (each
imports Presidio), loads the models in each, and analyses a built-in German and a built-in English
sentence, which must give a ``PERSON`` and an ``EMAIL_ADDRESS`` each. Children that do not start
or load (``nlp_start_failed``) and a failed check (``nlp_self_check_failed``) end the process
before ``/ready`` with exit code 1 and a JSON log line naming the models' path. On SIGTERM the
loops stop claiming, the items in flight get 30 s, then they are cancelled and the children
killed; their leases expire and the items run again.

- ``--download-models`` fills ``PII_MODELS_PATH`` with the two models of ``PII_MODEL_SIZE`` (the
  Helm init container and the compose one-shot service run it; idempotent) and exits 0, or 1.
- ``--check`` checks the models, loads them in one child and analyses the two sentences, then
  exits 0, or 1 on any failure (the image build and CI run it).

Logs carry job ids, kinds, codes, entity types and counts only: never a text, a span, an entity
value, a field name of the content or a message of the child.
"""

import argparse
import asyncio
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol
from weakref import WeakKeyDictionary

import structlog
from prometheus_client import REGISTRY, CollectorRegistry, Counter
from redis.asyncio import Redis

from text_extraction import model_store
from text_extraction.documents.pool import (
    ConversionCrashed,
    ConversionError,
    ConversionOutOfMemory,
    ConversionTimeout,
    DocumentPool,
)
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.extract_models import ExtractResult, failed_extract
from text_extraction.internal import Readiness, create_internal_app
from text_extraction.jobs import JobRecord, JobStore, pii_lease_seconds
from text_extraction.pii.fields import (
    FAIL_CLOSED_MESSAGE,
    TOO_LARGE_MESSAGE,
    apply_pii,
    fail_closed,
    text_fields,
)
from text_extraction.pii.replace import hash_key
from text_extraction.pii.types import (
    ENTITY_TYPES,
    EntityType,
    PiiLanguage,
    PiiOptions,
    Span,
    model_names,
)
from text_extraction.pii_dispatch import choose_language, nlp_queue
from text_extraction.scheduler import QueueItem
from text_extraction.server import _Server, _server, configured_settings
from text_extraction.settings import Settings
from text_extraction.valkey import create_valkey
from text_extraction.worker import Worker, _serve

_log = structlog.get_logger(__name__)

# The ``DocumentPool`` target of the PII children.
PII_TARGET: Final = "text_extraction.pii.child"
LANGUAGE_UNSUPPORTED_WARNING: Final = "pii_language_unsupported"
DRAFT_EXPIRED_MESSAGE: Final = (
    "The result to process expired before PII handling ran. Send the request again."
)
ANNOTATE_HTML_MESSAGE: Final = "annotate needs format markdown or text."
# What the items in flight get after SIGTERM before they are cancelled (N13).
GRACEFUL_SECONDS: Final = 30
# Importing Presidio and spaCy takes a while on a cold volume.
_CHILD_START_SECONDS: Final = 120.0
# Loading the two models from the volume.
_LOAD_SECONDS: Final = 300.0
# Calls after which the pool replaces a child. Recycling only guards against spaCy's string
# store growing with every text, and a replacement costs a reload of the models (about 8 s of the
# 300 s an item has): far less often than the pool's default of 50 calls.
CHILD_MAX_TASKS: Final = 1000
# The built-in sentences of the self-check: a person and an e-mail address each (ruling N13).
_CHECK_TYPES: Final[list[EntityType]] = ["PERSON", "EMAIL_ADDRESS"]
_CHECK_SENTENCES: Final[tuple[tuple[PiiLanguage, str], ...]] = (
    ("de", "Herr Thomas Müller schreibt an thomas.mueller@example.org über den Termin."),
    ("en", "Mr. John Smith wrote to john.smith@example.org about the meeting."),
)


class PiiChildren(Protocol):
    """The part of ``DocumentPool`` the runner uses (a fake in tests)."""

    async def run(self, function_name: str, payload: dict[str, Any], deadline: float) -> Any: ...


class PiiChildrenPool(PiiChildren, Protocol):
    """``PiiChildren`` with the start of the children (a fake in tests)."""

    async def start(self) -> None: ...

    async def shutdown(self) -> None: ...


def create_nlp_pool(settings: Settings) -> DocumentPool:
    """The PII children of a worker (``PII_CONCURRENCY``, ``PII_MEMORY_LIMIT_MB``); not started
    yet and not loaded: the models' path and size go in the payload of each call."""
    return DocumentPool(
        settings.pii_concurrency,
        settings.pii_memory_limit_mb,
        target=PII_TARGET,
        start_timeout=_CHILD_START_SECONDS,
        max_tasks=CHILD_MAX_TASKS,
    )


def load_payload(settings: Settings) -> dict[str, Any]:
    """The volume and the size of the models, in the payload of a ``load`` or ``analyze`` call."""
    return {"models_path": str(settings.pii_models_path), "size": settings.pii_model_size}


# One counter per registry, so several workers (tests) can share one.
_ENTITIES: WeakKeyDictionary[CollectorRegistry, Counter] = WeakKeyDictionary()


def entities_counter_for(registry: CollectorRegistry) -> Counter:
    """``text_extraction_pii_entities_total{type}`` in ``registry``: the entities found, by
    type only, every type exported as 0 from the start."""
    counter = _ENTITIES.get(registry)
    if counter is None:
        counter = Counter(
            "text_extraction_pii_entities_total",
            "Entities the nlp worker found in results, by entity type.",
            ["type"],
            registry=registry,
        )
        for entity_type in ENTITY_TYPES:
            counter.labels(type=entity_type)
        _ENTITIES[registry] = counter
    return counter


def _withheld(
    draft: Envelope[ExtractResult],
    options: PiiOptions,
    *,
    code: ErrorCode = ErrorCode.PII_FAILED,
    message: str = FAIL_CLOSED_MESSAGE,
    retryable: bool = True,
) -> Envelope[ExtractResult]:
    """``draft`` as ``failed`` with ``code``, its text, metadata, links, uploader and file name
    removed (N12) and the facts of the target kept."""
    held = fail_closed(draft, options)
    error = ErrorInfo(code=code, message=message, retryable=retryable)
    return held.model_copy(update={"status": "failed", "error": error})


class PiiRunner:
    """The ``nlp`` pool's ``ItemRunner``; see the module docstring. ``pool`` is the PII
    children (``DocumentPool``)."""

    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        pool: PiiChildren,
        registry: CollectorRegistry = REGISTRY,
    ) -> None:
        self._settings = settings
        self._store = store
        self._pool = pool
        self._entities = entities_counter_for(registry)
        self.lease_seconds = pii_lease_seconds(settings)

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
        draft = await self._store.draft(job.id, item.index)
        if draft is None:
            _log.warning("pii_draft_expired")
            return failed_extract(ErrorCode.PII_FAILED, DRAFT_EXPIRED_MESSAGE, retryable=True)
        options = job.options.pii
        data = draft.data
        if options is None or data is None or data.pii is not None:
            return draft  # nothing is owed: not asked for, nothing to read, or done before
        started = time.perf_counter()
        if options.mode == "annotate" and data.format == "html":
            return _withheld(draft, options, message=ANNOTATE_HTML_MESSAGE, retryable=False)
        entities = list(options.entities or self._settings.pii_default_entities)
        fields = text_fields(data, entities)
        if sum(len(field.text) for field in fields) > self._settings.pii_max_chars:
            return _withheld(
                draft, options, code=ErrorCode.TOO_LARGE, message=TOO_LARGE_MESSAGE, retryable=False
            )
        language, fallback = choose_language(
            options.language, data.language, self._settings.pii_fallback_language
        )
        found: dict[str, list[tuple[int, int, EntityType, float]]] = {}
        needing = [(field.name, field.text) for field in fields if field.whole is None]
        if needing:
            threshold = (
                self._settings.pii_score_threshold
                if options.score_threshold is None
                else options.score_threshold
            )
            try:
                found = await self._analyze(needing, language, entities, threshold)
            except ConversionOutOfMemory:
                _log.warning("pii_child_out_of_memory")
                return _withheld(
                    draft,
                    options,
                    code=ErrorCode.TOO_LARGE,
                    message=TOO_LARGE_MESSAGE,
                    retryable=False,
                )
            except (ConversionTimeout, ConversionCrashed, ConversionError) as error:
                self._log_child_failure(error)
                return _withheld(draft, options)
        try:
            spans = {
                name: [Span(start, end, kind, score) for start, end, kind, score in field_spans]
                for name, field_spans in found.items()
            }
            key, scope = hash_key(self._settings)
            processed, warnings = apply_pii(
                data,
                options,
                spans,
                language=language,
                key=key,
                hash_scope=scope,
                max_entities=self._settings.pii_max_entities,
                entities=entities,
            )
        except Exception as error:
            # The type only: the worker's own handler would log a traceback that may quote text.
            _log.error("pii_apply_failed", error=type(error).__name__)
            return _withheld(draft, options)
        if fallback:
            warnings.insert(0, LANGUAGE_UNSUPPORTED_WARNING)
        pii_ms = round((time.perf_counter() - started) * 1000)
        processed = processed.model_copy(
            update={
                "timings": processed.timings.model_copy(update={"pii_ms": pii_ms}),
                "warnings": [*processed.warnings, *warnings],
            }
        )
        if processed.pii is not None:
            for entity_type, count in processed.pii.entities.items():
                self._entities.labels(type=entity_type).inc(count)
        return draft.model_copy(update={"data": processed})

    async def _analyze(
        self,
        fields: list[tuple[str, str]],
        language: PiiLanguage,
        entities: list[EntityType],
        threshold: float,
    ) -> dict[str, list[tuple[int, int, EntityType, float]]]:
        """The child's spans per field, within ``PII_TIMEOUT_SECONDS`` of this call. A child
        that has no models yet (a fresh one) loads them first, inside the same call."""
        deadline = asyncio.get_running_loop().time() + self._settings.pii_timeout_seconds
        payload = {
            "fields": fields,
            "language": language,
            "entities": entities,
            "threshold": threshold,
            **load_payload(self._settings),
        }
        reply: dict[str, list[tuple[int, int, EntityType, float]]] = await self._pool.run(
            "analyze", payload, deadline
        )
        return reply

    def _log_child_failure(self, error: Exception) -> None:
        """The failure's type only: a child's message may quote the text."""
        fields: dict[str, Any] = {"error": type(error).__name__}
        if isinstance(error, ConversionError):
            fields["child_error"] = error.type_name
            if error.type_name == "ModelsMissing":
                fields["models_path"] = str(self._settings.pii_models_path)
        _log.error("pii_child_failed", **fields)


# --- start-up: the models and the self-check ----------------------------------------------------


class SelfCheckFailed(Exception):  # noqa: N818 - a failed check, named as such
    """The built-in sentences did not give a person and an e-mail address each."""


class NlpStartFailed(Exception):  # noqa: N818 - a failed start, named as such
    """The PII children did not start, or did not load the models."""


async def self_check(pool: PiiChildren, settings: Settings) -> None:
    """Analyse the built-in sentences (the models are loaded in the children): each must give a
    ``PERSON`` and an ``EMAIL_ADDRESS``. Raises ``SelfCheckFailed`` when a call fails or a
    type is not found; the log names the language and the type, never the sentence's spans."""
    deadline = asyncio.get_running_loop().time() + settings.pii_timeout_seconds
    for language, sentence in _CHECK_SENTENCES:
        payload = {
            "fields": [("check", sentence)],
            "language": language,
            "entities": list(_CHECK_TYPES),
            "threshold": settings.pii_score_threshold,
        }
        try:
            reply = await pool.run("analyze", payload, deadline)
        except Exception as error:
            _log.error(
                "nlp_self_check_failed",
                language=language,
                error=type(error).__name__,
                child_error=error.type_name if isinstance(error, ConversionError) else None,
                models_path=str(settings.pii_models_path),
            )
            raise SelfCheckFailed("the built-in sentence did not analyse") from None
        found = {span[2] for span in reply.get("check", [])}
        for wanted in _CHECK_TYPES:
            if wanted not in found:
                _log.error(
                    "nlp_self_check_failed",
                    language=language,
                    reason="entity_not_found",
                    entity_type=wanted,
                    models_path=str(settings.pii_models_path),
                )
                raise SelfCheckFailed("the built-in sentence's entities were not found")
    _log.info("nlp_self_check_passed")


async def start_children(pool: PiiChildrenPool, settings: Settings, *, children: int) -> None:
    """Start ``pool``'s children, load the models in each (``children`` calls at once reach
    each child once), then the self-check. Children that do not start or load are logged with
    the models' path (never more than the error's type) and raise ``NlpStartFailed``; a failed
    check raises ``SelfCheckFailed``."""
    try:
        await pool.start()
        deadline = asyncio.get_running_loop().time() + _LOAD_SECONDS
        loads = await asyncio.gather(
            *(pool.run("load", load_payload(settings), deadline) for _ in range(children)),
            return_exceptions=True,
        )
        for outcome in loads:
            if isinstance(outcome, BaseException):
                raise outcome
    except Exception as error:
        _log.error(
            "nlp_start_failed",
            models_path=str(settings.pii_models_path),
            error_type=type(error).__name__,
            child_error=error.type_name if isinstance(error, ConversionError) else None,
        )
        raise NlpStartFailed("the PII children did not start") from None
    await self_check(pool, settings)


def models_missing(settings: Settings) -> bool:
    """Whether a model of ``PII_MODEL_SIZE`` is missing on the volume; logs the volume's path
    when one is."""
    root = settings.pii_models_path
    manifest = model_store.load_manifest("nlp").only(*model_names(settings.pii_model_size).values())
    missing = model_store.missing_models(root, manifest)
    if missing:
        _log.error("nlp_models_missing", models_path=str(root), missing=len(missing))
    return bool(missing)


# --- the process ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class NlpWorkerParts:
    """What one nlp worker process runs on: the worker, its children, and the settings."""

    worker: Worker
    pool: PiiChildrenPool
    settings: Settings

    async def start(self) -> None:
        """Start the children, load the models in each and run the self-check (before
        ``/ready``)."""
        await start_children(self.pool, self.settings, children=self.settings.pii_concurrency)


def build_nlp_worker(
    settings: Settings, valkey: Redis, registry: CollectorRegistry
) -> NlpWorkerParts:
    """The nlp worker of a process, nothing started yet."""
    pool = create_nlp_pool(settings)
    store = JobStore(valkey, settings)
    worker = Worker(
        settings,
        valkey,
        PiiRunner(settings, store, pool, registry),
        store,
        nlp_queue(valkey, settings),
        registry,
        concurrency=settings.pii_concurrency,
    )
    return NlpWorkerParts(worker=worker, pool=pool, settings=settings)


async def serve(parts: NlpWorkerParts, internal: _Server, readiness: Readiness) -> None:
    """Start the children and check them, run the worker until a signal, then let the items in
    flight finish for ``GRACEFUL_SECONDS``. The caller kills the children and closes the rest."""
    await _serve(
        parts.worker,
        internal,
        readiness,
        graceful_seconds=GRACEFUL_SECONDS,
        startup=parts.start,
    )


async def _run(settings: Settings) -> None:
    valkey = create_valkey(settings.valkey_url)
    registry = REGISTRY
    parts = build_nlp_worker(settings, valkey, registry)
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
        await parts.pool.shutdown()  # after the loops: their items are given back
        await valkey.aclose()
        _log.info("valkey_closed")


async def _check(settings: Settings) -> int:
    """Load the models in one child and analyse the built-in sentences: 0 when both gave a
    person and an e-mail address, else 1."""
    pool = create_nlp_pool(settings.model_copy(update={"pii_concurrency": 1}))
    try:
        await start_children(pool, settings, children=1)
    except (SelfCheckFailed, NlpStartFailed):
        return 1  # logged, with the models' path
    finally:
        await pool.shutdown()
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="text-extraction-nlp-worker",
        description="Run the nlp worker (pool nlp) of text-extraction.",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--download-models",
        action="store_true",
        help="Download the spaCy models of PII_MODEL_SIZE into PII_MODELS_PATH, then exit "
        "(0: in place).",
    )
    actions.add_argument(
        "--check",
        action="store_true",
        help="Load the models and analyse a German and an English sentence, then exit "
        "(0: a person and an e-mail address were found in each).",
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
    except (SelfCheckFailed, NlpStartFailed):
        sys.exit(1)  # logged, with the models' path


def _download(settings: Settings) -> int:
    manifest = model_store.load_manifest("nlp").only(*model_names(settings.pii_model_size).values())
    try:
        model_store.download_models(settings.pii_models_path, manifest)
    except (model_store.ModelDownloadFailed, OSError) as error:
        _log.error(
            "nlp_models_download_failed",
            models_path=str(settings.pii_models_path),
            reason=str(error) if isinstance(error, model_store.ModelDownloadFailed) else None,
            error_type=type(error).__name__,
        )
        return 1
    return 0
