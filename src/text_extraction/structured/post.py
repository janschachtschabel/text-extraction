"""The post stage of ``structured`` and ``chunking`` (ruling S5): what runs on the final content.

The DOM stage (``dom.py``) ran on the page's HTML; PII may have masked the content since. The
post stage reads ``data.content`` as it is now (masked content gives masked values and chunks,
D3 a) and runs, in this order:

1. ``patterns`` (``patterns.py``): strategy ``patterns`` for every field with a rule, ``auto``
   for the fields still missing. They run once per result, within a total budget of 2 s, and
   their outcome stays on the result (``patterns_done``, ruling R10): ``run_post_patterns``
   runs this step alone before routing, ``needs_llm`` runs no regex, and the later steps reuse
   it.
2. The LLM (``llm_extract``): strategy ``llm`` for every field, ``auto`` for the fields still
   missing (so the order of ``auto`` is selectors, metadata, patterns, LLM: ruling R3). The
   request carries only those fields' strict schema; values are only ever taken from the answer
   as the compiled schema validates it, for the fields asked (Review Focus 1).
3. ``learn_selectors`` (``learn.py``) for the values the LLM found; the kept rules are stored
   for the caller's own key (``owner``), the request URL's host and the schema (S8), as the
   pipeline reads them. With no field for the LLM there is nothing to learn: no LLM call, the
   warning ``nothing_to_learn`` (R8). Learning is a side effect: a failed learning call keeps
   the status, with the warning ``selector_learning_failed`` (R9); a page over
   ``STRUCTURED_MAX_ELEMENTS`` learns nothing (``selectors_skipped_large_page``).
4. Tables from Markdown pipe tables for results without a DOM stage (documents, text), when
   ``tables`` is asked.
5. Chunking (S10) of the content; ``semantic`` embeds with the LLM client.
6. ``stage: "done"``, ``learn_html`` cleared (no answer carries it), and every value checked
   by the compiled model: one that fails is dropped to missing with the warning
   ``structured_value_invalid``.

**Two entry points.** ``apply_post`` (async) runs it with an LLM client (the llm worker,
ruling S14). ``finish_post`` (sync, no LLM) is the guard at every finish point: an envelope
whose post stage has not run gets it without the LLM. LLM work that was asked and not done
(the extraction, and with it learning, which only learns the LLM's values, or ``semantic``)
turns a ``success`` into ``partial`` with ``llm_failed``, retryable (``BUSY_MESSAGE`` for
``reason="busy"``, else ``NOT_RUN_MESSAGE``); its fields stay in ``missing`` and
``semantic`` falls back to ``recursive`` with the warning ``chunking_fallback_recursive``.
``auto`` on a service without an LLM is no failure: the fields stay missing, with the warning
``llm_unavailable``. A failing LLM call in ``apply_post`` does the same with the call's
``LlmError`` (a learning call excepted, R9). A result that already is ``partial`` or
``failed`` keeps its status and error. The content always stays.

**Where it runs** (ruling S13). Every finish point runs it last, after the PII step and before
``pii.fields.fail_closed`` and the answer or write: the sync flows (``extract_api._flow``,
``file_api._flow``), the P1 job's write (``extract_api._store_result``) and ``Worker._finish``
of every pool; ``post_pending`` says whether it is still owed there. A result that moves to the
``nlp`` pool gets it on that pool. The result cache keeps the DOM stage's result, never the post
stage's: a cache hit gets a post stage of its own (other ``chunking``, other chunks).

An envelope without data, or ``failed`` without content, gets no post stage (``structured`` and
``chunks`` stay null). Empty content gives ``chunks: []`` and every field missing, without an
LLM call. Patterns, chunking, the fallback and the check of learned selectors run in a thread
in ``apply_post`` (CPU-bound); the sync ``run_post_patterns``, ``needs_llm`` (cheap) and
``finish_post`` run where they are called, so callers on an event loop call
``run_post_patterns`` and ``finish_post`` via ``asyncio.to_thread``. The LLM's time is added
to ``timings.llm_ms``. Nothing here logs the content, a value, a pattern, a selector or a key.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal

import structlog
from pydantic import ValidationError
from redis.exceptions import RedisError

from text_extraction.chunking.semantic import semantic_chunks
from text_extraction.chunking.split import WARNING_FALLBACK_RECURSIVE, chunk
from text_extraction.chunking.types import Chunk, ChunkingOptions
from text_extraction.envelope import Envelope, ErrorInfo
from text_extraction.extract_models import ExtractResult
from text_extraction.llm.basics import LlmError, truncate_input
from text_extraction.llm.usage import LlmUsage
from text_extraction.pii.fields import needs_pii
from text_extraction.pii.types import PiiOptions
from text_extraction.settings import Settings, llm_available
from text_extraction.structured.dom import SELECTORS_SKIPPED_WARNING, TABLES_TRUNCATED_WARNING
from text_extraction.structured.learn import LearnPageTooLargeError, learn
from text_extraction.structured.patterns import VALUE_INVALID_WARNING, markdown_tables, run_patterns
from text_extraction.structured.prompts import document_message, extraction_system
from text_extraction.structured.schema import CompiledSchema
from text_extraction.structured.store import (
    LEARNED_SELECTORS_UNAVAILABLE_WARNING,
    SelectorStore,
    selector_host,
)
from text_extraction.structured.types import (
    LearnReport,
    Source,
    StructuredOptions,
    StructuredResult,
)

if TYPE_CHECKING:  # the OpenAI SDK only where a client is made (``llm/basics.py``)
    from text_extraction.llm.client import LlmClient

__all__ = [
    "BUSY_MESSAGE",
    "LLM_INPUT_TRUNCATED_WARNING",
    "LLM_UNAVAILABLE_WARNING",
    "NOTHING_TO_LEARN_WARNING",
    "NOT_RUN_MESSAGE",
    "SELECTOR_LEARNING_FAILED_WARNING",
    "apply_post",
    "finish_post",
    "llm_extract",
    "needs_llm",
    "patterns_ran",
    "post_done",
    "post_pending",
    "run_post_patterns",
    "without_dom_stage",
]

_log = structlog.get_logger(__name__)

BUSY_MESSAGE: Final = "The LLM step is busy; retry later."
NOT_RUN_MESSAGE: Final = "The LLM step did not run; retry later."
LLM_UNAVAILABLE_WARNING: Final = "llm_unavailable"
LLM_INPUT_TRUNCATED_WARNING: Final = "llm_input_truncated"
NOTHING_TO_LEARN_WARNING: Final = "nothing_to_learn"
SELECTOR_LEARNING_FAILED_WARNING: Final = "selector_learning_failed"
_EMBEDDINGS_MISMATCH: Final = "The LLM embeddings did not match the request."


# --- the LLM extraction ------------------------------------------------------------------------


async def llm_extract(
    client: "LlmClient",
    content: str,
    compiled: CompiledSchema,
    fields: Sequence[str],
    instructions: str | None,
    *,
    max_chars: int,
) -> tuple[dict[str, Any], LlmUsage, list[str]]:
    """The values the LLM finds in ``content`` for ``fields`` (each one, None when the content
    does not state it), its usage and the warnings (``llm_input_truncated`` when the content
    was cut to ``max_chars``, ``LLM_MAX_INPUT_TOKENS * 4``). See ``prompts.py`` for the
    messages; an answer that does not validate gets ``chat_json``'s repair call. Keys the
    answer has besides ``fields`` are dropped. Raises ``LlmError``."""
    asked = list(fields)
    text, cut = truncate_input(content, max_chars)

    def validate(parsed: Any) -> dict[str, Any]:
        if not isinstance(parsed, dict):
            raise ValueError("The answer must be a JSON object.")
        model = compiled.model.model_validate({name: parsed.get(name) for name in asked})
        dumped = model.model_dump(mode="json", by_alias=True)
        return {name: dumped.get(name) for name in asked}

    values, usage = await client.chat_json(
        extraction_system(instructions),
        document_message(text),
        compiled.strict_json_schema(asked),
        validate=validate,
    )
    return values, usage, [LLM_INPUT_TRUNCATED_WARNING] if cut else []


# --- when the stage runs -----------------------------------------------------------------------


def _eligible(envelope: Envelope[ExtractResult]) -> bool:
    """Whether the envelope gets a post stage: it has data, and content unless it failed."""
    data = envelope.data
    return data is not None and not (envelope.status == "failed" and not data.content)


def _structured_done(data: ExtractResult, structured: StructuredOptions | None) -> bool:
    return structured is None or (data.structured is not None and data.structured.stage == "done")


def _chunks_done(data: ExtractResult, chunking: ChunkingOptions | None) -> bool:
    return chunking is None or data.chunks is not None


def post_done(
    envelope: Envelope[ExtractResult],
    structured: StructuredOptions | None,
    chunking: ChunkingOptions | None,
) -> bool:
    """Whether the post stage has nothing (more) to do: ``structured`` is at ``stage: "done"``
    (or not asked) and the chunks are there (or not asked). An envelope that gets no post stage
    (no data; ``failed`` without content) counts as done."""
    data = envelope.data
    if data is None or not _eligible(envelope):
        return True
    return _structured_done(data, structured) and _chunks_done(data, chunking)


def post_pending(
    envelope: Envelope[ExtractResult],
    structured: StructuredOptions | None,
    chunking: ChunkingOptions | None,
    pii: PiiOptions | None,
) -> bool:
    """Whether a finish point still owes this envelope its post stage (ruling S13): the stage
    has not run (``post_done``), and the envelope does not owe the PII step any more. The post
    stage only ever reads the final content (D3 a): an envelope that still owes PII (no nlp
    worker took it) is withheld by ``pii.fields.fail_closed`` right after (which drops
    ``structured`` and ``chunks`` too, R13), so it gets none."""
    if post_done(envelope, structured, chunking):
        return False
    return pii is None or not needs_pii(envelope, pii)


def without_dom_stage(envelope: Envelope[ExtractResult]) -> Envelope[ExtractResult]:
    """The envelope without a DOM stage's ``structured`` (``stage: "dom"``), for a request with
    ``pii`` (ruling R12 b, D3 a): the DOM stage read the raw page, which the nlp step does not
    mask, so only ``patterns`` and the LLM may fill fields, on the masked content. Called where
    the PII hop takes an envelope (``PiiHandoff``, ``PiiMove``) and on a cache hit of a ``pii``
    request; the pipeline runs no DOM stage for such a request in the first place
    (``dom.dom_options``). Any other envelope comes back as it is."""
    data = envelope.data
    if data is None or data.structured is None or data.structured.stage != "dom":
        return envelope
    return envelope.model_copy(update={"data": data.model_copy(update={"structured": None})})


def _missing(data: ExtractResult, compiled: CompiledSchema) -> list[str]:
    dom = data.structured
    if dom is None:
        return list(compiled.fields)
    return [name for name in compiled.fields if dom.values.get(name) is None]


def needs_llm(
    envelope: Envelope[ExtractResult],
    structured: StructuredOptions | None,
    chunking: ChunkingOptions | None,
    settings: Settings,
) -> bool:
    """Whether the post stage of this envelope needs the LLM (S5, rulings R8, R10): the service
    has one, the stage has not run, there is content, and strategy ``llm``; or ``auto`` with
    fields missing after the DOM stage and the patterns; or chunking ``semantic``.
    ``learn_selectors`` needs the LLM only through those (it learns from the LLM's values).

    It runs no regex: the patterns' outcome is the one ``run_post_patterns`` kept on the result.
    Before they ran, a field a pattern might fill counts as missing; so call
    ``run_post_patterns`` (off the event loop) first, to send fewer results to the LLM."""
    data = envelope.data
    if data is None or not llm_available(settings) or post_done(envelope, structured, chunking):
        return False
    content = data.content or ""
    if not content.strip():
        return False
    if chunking is not None and chunking.strategy == "semantic" and data.chunks is None:
        return True
    compiled = structured.compiled if structured is not None else None
    if structured is None or compiled is None or _structured_done(data, structured):
        return False
    if structured.strategy == "llm":
        return True
    return structured.strategy == "auto" and bool(_missing(data, compiled))


def patterns_ran(envelope: Envelope[ExtractResult]) -> bool:
    """Whether the post stage's patterns have run on this envelope's result (R10)."""
    data = envelope.data
    return data is not None and data.structured is not None and bool(data.structured.patterns_done)


def run_post_patterns(
    envelope: Envelope[ExtractResult],
    structured: StructuredOptions | None,
    *,
    settings: Settings,
) -> Envelope[ExtractResult]:
    """Step 1 of the post stage alone: the patterns, run once and kept on the result (still
    ``stage: "dom"``, ``patterns_done`` set), so that ``needs_llm`` can route on their outcome
    and the later steps reuse it (R10). Sync and CPU-bound (up to
    ``PATTERNS_TOTAL_BUDGET_SECONDS``): call it off the event loop. An envelope whose patterns
    ran, whose post stage ran, or that gets none comes back as it is."""
    data = envelope.data
    if (
        data is None
        or structured is None
        or structured.compiled is None
        or patterns_ran(envelope)
        or post_done(envelope, structured, None)
    ):
        return envelope
    work = _Post.start(envelope, data, structured, None, settings)
    if work.result is None:
        return envelope
    work.run_patterns()
    warnings = list(data.warnings)
    warnings.extend(warning for warning in work.warnings if warning not in warnings)
    update = {"structured": work.result, "warnings": warnings}
    return envelope.model_copy(update={"data": data.model_copy(update=update)})


# --- the work ----------------------------------------------------------------------------------


@dataclass
class _Post:
    """The post stage of one envelope, as it goes."""

    envelope: Envelope[ExtractResult]
    data: ExtractResult
    options: StructuredOptions | None
    chunking: ChunkingOptions | None
    settings: Settings
    result: StructuredResult | None = None
    chunks: list[Chunk] | None = None
    warnings: list[str] = field(default_factory=list)
    failure: LlmError | None = None
    usage: LlmUsage | None = None
    llm_ms: int = 0

    @classmethod
    def start(
        cls,
        envelope: Envelope[ExtractResult],
        data: ExtractResult,
        options: StructuredOptions | None,
        chunking: ChunkingOptions | None,
        settings: Settings,
    ) -> "_Post":
        """The work on ``data``, the envelope's data."""
        work = cls(envelope, data, options, chunking, settings)
        if options is not None and not _structured_done(data, options):
            work.result = _starting_result(data.structured, options)
        return work

    @property
    def content(self) -> str:
        return self.data.content or ""

    @property
    def compiled(self) -> CompiledSchema | None:
        if self.options is None or self.result is None:
            return None
        return self.options.compiled

    def note(self, *warnings: str) -> None:
        for warning in warnings:
            if warning not in self.warnings:
                self.warnings.append(warning)

    def fail(self, failure: LlmError) -> None:
        if self.failure is None:
            self.failure = failure

    def found(self, values: dict[str, Any], source: Source) -> None:
        if self.result is None:
            return
        for name, value in values.items():
            if value is not None:
                self.result.values[name] = value
                self.result.sources[name] = source

    def missing(self) -> list[str]:
        if self.result is None:
            return []
        return [name for name, value in self.result.values.items() if value is None]

    # 1. patterns
    def pattern_fields(self) -> list[str]:
        compiled = self.compiled
        if self.options is None or compiled is None:
            return []
        if self.options.strategy == "patterns":
            names: Sequence[str] = compiled.fields
        elif self.options.strategy == "auto":
            names = self.missing()
        else:
            return []
        return [name for name in names if name in self.options.patterns]

    def run_patterns(self) -> None:
        """Step 1, once per result: a result whose patterns ran keeps their outcome."""
        if self.result is None or self.result.patterns_done:
            return
        fields = self.pattern_fields()
        if fields and self.options is not None:
            values, warnings = run_patterns(
                self.content,
                self.options,
                fields,
                timeout=self.settings.structured_regex_timeout_seconds,
            )
            self.note(*warnings)
            self.found(values, "patterns")
        self.result.patterns_done = True

    # 2. the LLM
    def llm_fields(self) -> list[str]:
        compiled = self.compiled
        if self.options is None or compiled is None or not self.content.strip():
            return []
        if self.options.strategy == "llm":
            return list(compiled.fields)
        if self.options.strategy == "auto":
            return self.missing()
        return []

    def llm_not_done(self, message: str, *, optional: bool) -> None:
        """LLM work that was asked did not run: ``llm_failed``, except ``optional`` work (the
        LLM step of ``auto``) on a service without an LLM: ``llm_unavailable``."""
        if optional and not llm_available(self.settings):
            self.note(LLM_UNAVAILABLE_WARNING)
        else:
            self.fail(LlmError(message, retryable=True))

    def used(self, usage: LlmUsage, started: float) -> None:
        self.llm_ms += round((time.perf_counter() - started) * 1000)
        if usage.calls:
            self.usage = usage if self.usage is None else _sum(self.usage, usage)

    # 3. learning
    def learnable(self) -> dict[str, Any]:
        if self.result is None or self.options is None or not self.options.learn_selectors:
            return {}
        return {
            name: value
            for name, value in self.result.values.items()
            if value is not None and self.result.sources.get(name) == "llm"
        }

    # 4. tables
    def tables(self) -> None:
        if self.result is None or self.options is None or not self.options.tables:
            return
        if self.result.tables is None:  # no DOM stage: a document or a text
            tables, cut = markdown_tables(self.content)
            self.result.tables = tables
            if cut:
                self.note(TABLES_TRUNCATED_WARNING)

    # 5. chunking
    def wants_chunks(self) -> bool:
        return self.chunking is not None and self.data.chunks is None

    def chunk(self, options: ChunkingOptions) -> None:
        chunks, warnings = chunk(
            self.content,
            options,
            markdown=self.data.format == "markdown",
            max_chunks=self.settings.chunk_max_chunks,
        )
        self.chunks = chunks
        self.note(*warnings)

    def chunk_without_embeddings(self) -> None:
        """Chunking without the LLM: ``semantic`` falls back to ``recursive``."""
        if self.chunking is None:
            return
        options = self.chunking
        if options.strategy == "semantic" and self.content.strip():
            options = options.model_copy(update={"strategy": "recursive"})
            self.note(WARNING_FALLBACK_RECURSIVE)
        self.chunk(options)

    # 6. the answer
    def finished(self) -> Envelope[ExtractResult]:
        update: dict[str, Any] = {}
        if self.result is not None:
            update["structured"] = self._checked_result(self.result)
        if self.wants_chunks():
            update["chunks"] = self.chunks if self.chunks is not None else []
        warnings = list(self.data.warnings)
        warnings.extend(warning for warning in self.warnings if warning not in warnings)
        update["warnings"] = warnings
        if self.llm_ms:
            update["timings"] = self.data.timings.model_copy(
                update={"llm_ms": self.data.timings.llm_ms + self.llm_ms}
            )
        status, error = self.envelope.status, self.envelope.error
        if self.failure is not None and status == "success":
            status = "partial"
            error = ErrorInfo(
                code=self.failure.code,
                message=self.failure.message,
                retryable=self.failure.retryable,
            )
        return self.envelope.model_copy(
            update={"status": status, "error": error, "data": self.data.model_copy(update=update)}
        )

    def _checked_result(self, result: StructuredResult) -> StructuredResult:
        compiled = self.options.compiled if self.options is not None else None
        values = dict(result.values)
        if compiled is not None:
            for name, value in result.values.items():
                if value is None:
                    continue
                checked = _checked(compiled, name, value)
                if checked is None:
                    self.note(VALUE_INVALID_WARNING)
                values[name] = checked
        sources = {
            name: source for name, source in result.sources.items() if values.get(name) is not None
        }
        llm = result.llm
        if self.usage is not None:
            llm = self.usage if llm is None else _sum(llm, self.usage)
        return result.model_copy(
            update={
                "values": values,
                "sources": sources,
                "missing": [name for name, value in values.items() if value is None],
                "llm": llm,
                "stage": "done",
                "learn_html": None,
                "patterns_done": None,
            }
        )


def _starting_result(dom: StructuredResult | None, options: StructuredOptions) -> StructuredResult:
    """The result the post stage starts from: the DOM stage's (copied), else an empty one with
    every field missing."""
    compiled = options.compiled
    fields = compiled.fields if compiled is not None else ()
    if dom is None:
        return StructuredResult(values=dict.fromkeys(fields), stage="dom")
    values = {name: dom.values.get(name) for name in fields} if compiled is not None else {}
    return dom.model_copy(
        update={
            "values": values,
            "sources": dict(dom.sources),
            "tables": list(dom.tables) if dom.tables is not None else None,
        }
    )


def _checked(compiled: CompiledSchema, name: str, value: Any) -> Any | None:
    """``value`` as the compiled model has it; None when it does not fit the field."""
    try:
        model = compiled.model.model_validate({name: value})
    except ValidationError:
        return None
    return model.model_dump(mode="json", by_alias=True).get(name)


def _sum(first: LlmUsage, second: LlmUsage) -> LlmUsage:
    return LlmUsage(
        model=first.model,
        calls=first.calls + second.calls,
        prompt_tokens=first.prompt_tokens + second.prompt_tokens,
        completion_tokens=first.completion_tokens + second.completion_tokens,
    )


# --- the guard without the LLM -----------------------------------------------------------------


def finish_post(
    envelope: Envelope[ExtractResult],
    structured: StructuredOptions | None,
    chunking: ChunkingOptions | None,
    *,
    settings: Settings,
    reason: Literal["busy", "failed"] | None = None,
) -> Envelope[ExtractResult]:
    """The post stage without the LLM, for an envelope whose post stage has not run (see the
    module docstring); an envelope that has it, or gets none, comes back as it is. ``reason``
    says why the LLM work did not run (``busy``: the memory guard; ``failed`` or None:
    anything else)."""
    data = envelope.data
    if data is None or post_done(envelope, structured, chunking):
        return envelope
    work = _Post.start(envelope, data, structured, chunking, settings)
    message = BUSY_MESSAGE if reason == "busy" else NOT_RUN_MESSAGE
    if work.result is not None:
        work.run_patterns()
        asked = work.llm_fields()
        if asked:
            optional = structured is not None and structured.strategy == "auto"
            work.llm_not_done(message, optional=optional)
        if structured is not None and structured.learn_selectors:
            work.result.learned = LearnReport(stored=[], rejected=[])
            if not asked:
                work.note(NOTHING_TO_LEARN_WARNING)
        work.tables()
    if work.wants_chunks():
        semantic = work.chunking is not None and work.chunking.strategy == "semantic"
        if semantic and work.content.strip():
            work.llm_not_done(message, optional=False)
        work.chunk_without_embeddings()
    return work.finished()


# --- the stage with the LLM --------------------------------------------------------------------


async def apply_post(
    envelope: Envelope[ExtractResult],
    structured: StructuredOptions | None,
    chunking: ChunkingOptions | None,
    *,
    settings: Settings,
    llm: "LlmClient | None",
    store: SelectorStore | None,
    owner: str,
) -> Envelope[ExtractResult]:
    """The post stage with the LLM client ``llm`` (see the module docstring); without one it is
    ``finish_post``. ``store`` and ``owner`` (the API key's name) keep the learned selectors;
    without them nothing is stored. An envelope that has had its post stage, or gets none,
    comes back as it is. LLM failures become ``llm_failed`` in the answer, never an
    exception."""
    data = envelope.data
    if data is None or post_done(envelope, structured, chunking):
        return envelope
    if llm is None:
        return await asyncio.to_thread(
            finish_post, envelope, structured, chunking, settings=settings
        )
    work = _Post.start(envelope, data, structured, chunking, settings)
    if work.result is not None and structured is not None:
        await asyncio.to_thread(work.run_patterns)
        await _extract(work, llm, structured)
        if structured.learn_selectors:
            await _learn(work, llm, store, owner)
        work.tables()
    if work.wants_chunks():
        await _chunk(work, llm)
    return work.finished()


async def _extract(work: _Post, llm: "LlmClient", structured: StructuredOptions) -> None:
    fields = work.llm_fields()
    compiled = work.compiled
    if not fields or compiled is None:
        return
    started = time.perf_counter()
    try:
        values, usage, warnings = await llm_extract(
            llm,
            work.content,
            compiled,
            fields,
            structured.instructions,
            max_chars=work.settings.llm_max_input_tokens * 4,
        )
    except LlmError as error:
        work.llm_ms += round((time.perf_counter() - started) * 1000)
        work.fail(error)
        return
    work.used(usage, started)
    work.note(*warnings)
    work.found(values, "llm")


async def _learn(work: _Post, llm: "LlmClient", store: SelectorStore | None, owner: str) -> None:
    result = work.result
    if result is None:
        return
    report = LearnReport(stored=[], rejected=[])
    result.learned = report
    values = work.learnable()
    compiled = work.compiled
    html = result.learn_html
    host = selector_host(work.data.url) if work.data.url else None
    if not values:
        if work.failure is None:  # no field went to the LLM, or it found none of them (R8)
            work.note(NOTHING_TO_LEARN_WARNING)
        return
    if compiled is None or not html or store is None or not owner or host is None:
        return
    started = time.perf_counter()
    try:
        rules, report, usage = await learn(
            llm, html, compiled, values, base_url=work.data.final_url or work.data.url
        )
    except LearnPageTooLargeError:
        work.note(SELECTORS_SKIPPED_WARNING)
        result.learned = LearnReport(stored=[], rejected=list(values))
        return
    except LlmError:  # learning is a side effect: the values stand (R9)
        work.llm_ms += round((time.perf_counter() - started) * 1000)
        work.note(SELECTOR_LEARNING_FAILED_WARNING)
        result.learned = LearnReport(stored=[], rejected=list(values))
        return
    work.used(usage, started)
    if rules:
        try:
            await store.put(owner, host, compiled.schema_id, rules)
        except (RedisError, OSError) as error:
            _log.warning("learned_selectors_write_failed", error=type(error).__name__)
            report = report.model_copy(update={"stored": []})
            work.note(LEARNED_SELECTORS_UNAVAILABLE_WARNING)
    result.learned = report


async def _chunk(work: _Post, llm: "LlmClient") -> None:
    options = work.chunking
    if options is None:
        return
    if options.strategy != "semantic" or not work.content.strip():
        await asyncio.to_thread(work.chunk, options)
        return
    try:
        chunks, warnings = await semantic_chunks(
            work.content,
            options,
            _timed(llm.embed, work),
            markdown=work.data.format == "markdown",
            max_chunks=work.settings.chunk_max_chunks,
        )
    except LlmError as error:
        work.fail(error)
        await asyncio.to_thread(work.chunk_without_embeddings)
        return
    except ValueError:  # embeddings of another count or length than the sentences
        work.fail(LlmError(_EMBEDDINGS_MISMATCH, retryable=False))
        await asyncio.to_thread(work.chunk_without_embeddings)
        return
    work.chunks = chunks
    work.note(*warnings)


def _timed(
    embed: Callable[[Sequence[str]], Awaitable[list[list[float]]]], work: _Post
) -> Callable[[Sequence[str]], Awaitable[list[list[float]]]]:
    """``embed`` with its time added to the LLM's."""

    async def timed(texts: Sequence[str]) -> list[list[float]]:
        started = time.perf_counter()
        try:
            return await embed(texts)
        finally:
            work.llm_ms += round((time.perf_counter() - started) * 1000)

    return timed
