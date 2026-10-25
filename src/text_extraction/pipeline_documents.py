"""The conversion branch of the pipeline for documents (PDF, Office, EPUB, CSV, images, ...).

``pipeline.py`` keeps the turn, the compliance checks and the cache; for a body that
``documents.sniff`` names a document kind, it hands the conversion to ``DocumentSteps.extract``
(rulings F9-F11):

1. Kinds only Docling converts (OpenDocument, images) need OCR: an ``OcrNeeded`` without a
   draft while the service has Docling and the request does not say ``ocr: never``; otherwise
   ``unsupported_content_type``.
2. Every other kind is converted by MarkItDown in a ``DocumentPool`` child, by the extraction's
   deadline (``documents/markitdown_convert.py``). The child's failures map to envelopes: a
   container bomb or a conversion out of memory is ``too_large``, an unreadable or
   password-protected document ``conversion_failed`` (not retryable: the same bytes would fail
   again). The deadline is ``timeout``, retryable as the extraction's own: the child gets only
   what the fetch left of ``EXTRACT_TIMEOUT_SECONDS``, or found no free child in time.
3. The result is judged like ``text/plain`` (``assess(None, content)``, ``QUALITY_MIN_WORDS``)
   and never escalates to the browser. The child always reads the metadata, because the
   language comes with it (``dc:language``, then ``Content-Language``, then the text); the
   result carries it only for ``metadata: true``.
4. When the document needs OCR (``routing.needs_ocr``: a scan or broken text layer under
   ``auto``, any PDF under ``always``) the MarkItDown result becomes the draft of an
   ``OcrNeeded``, with the bytes and what the Docling worker needs to know about them
   (``DocumentInput``). The caller hands it on, or answers ``without_ocr``: the draft with the
   warning ``ocr_unavailable``. A scan that needs OCR the service does not have answers its text
   layer with that warning straight away.

Our own faults (an exception the child should never raise, a dead child, a malformed reply) are
logged by their type, never with a message: a converter's message may quote the document. The
pool starts its children when the process starts (``start_documents``), or with the first
document.

Metrics (F19), per registry like the pipeline's: ``text_extraction_document_conversions_total``
by converter, kind and outcome (``success`` or the error code), the conversion's seconds and the
pages converted, by converter. ``document_metrics_for`` hands them to other converters.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol
from weakref import WeakKeyDictionary

import structlog
from prometheus_client import CollectorRegistry, Counter, Histogram
from pydantic import BaseModel

from text_extraction.compliance import ComplianceReport
from text_extraction.conversion_pool import ConversionPool
from text_extraction.convert import OutputFormat
from text_extraction.documents.pool import (
    ConversionCrashed,
    ConversionError,
    ConversionOutOfMemory,
    ConversionTimeout,
    DocumentPool,
)
from text_extraction.documents.routing import (
    DOCLING_KINDS,
    OCR_DISABLED_MESSAGE,
    needs_ocr,
    wants_ocr,
)
from text_extraction.documents.sniff import media_type
from text_extraction.documents.types import (
    DocumentConversion,
    DocumentInfo,
    DocumentKind,
    OcrOption,
)
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.extract_models import ExtractResult
from text_extraction.fetch import FetchResult, Validators
from text_extraction.fingerprint import Fingerprint, compute_fingerprint
from text_extraction.metadata import PageMetadata
from text_extraction.quality import Quality, assess, quality_error
from text_extraction.settings import Settings

_log = structlog.get_logger(__name__)

# The ``DocumentPool`` target of MarkItDown children (``documents/child.py``).
MARKITDOWN_TARGET: Final = "text_extraction.documents.child"
# Added to a document result whose text did not get the OCR it needed.
OCR_UNAVAILABLE_WARNING: Final = "ocr_unavailable"
# The error of a document without a text layer (an image) whose extraction has no OCR handoff
# while the service has OCR: an upload's (``OCR_NOT_HANDED_REASON``), a render's (the browser
# worker hands nothing on; ``OCR_NOT_HANDED_MESSAGE``, which says what to do instead).
OCR_NOT_HANDED_REASON: Final = (
    "The document needs OCR, which this extraction cannot hand on to the OCR workers."
)
OCR_NOT_HANDED_MESSAGE: Final = (
    f"{OCR_NOT_HANDED_REASON} Extract the document's own URL with mode fast."
)
# The child's deadline is this much before the extraction's, so its timeout is ours to answer.
_POOL_MARGIN_SECONDS: Final = 0.25

_FAILED_MESSAGE: Final = "The document could not be converted."
_UNAVAILABLE_MESSAGE: Final = "The document converter is not available. Try again later."
# Seconds of one conversion: MarkItDown takes up to the extraction's deadline, Docling (OCR) up
# to DOCLING_TIMEOUT_SECONDS.
_SECONDS_BUCKETS: Final = (0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600)
# What the child raises for documents it cannot convert; any other type is a fault of ours.
_DOCUMENT_ERRORS: Final[dict[str, tuple[ErrorCode, str]]] = {
    "DocumentTooLarge": (
        ErrorCode.TOO_LARGE,
        "The document unpacks to more than this service converts.",
    ),
    "DocumentUnreadable": (ErrorCode.CONVERSION_FAILED, _FAILED_MESSAGE),
    "PdfUnreadable": (ErrorCode.CONVERSION_FAILED, _FAILED_MESSAGE),
    "PasswordProtected": (ErrorCode.CONVERSION_FAILED, "The PDF is password-protected."),
}


class DocumentInputMeta(BaseModel):
    """What the Docling worker needs to know about a document's bytes (ruling F12); stored as
    JSON next to them."""

    kind: DocumentKind
    content_type: str  # the sniffed media type
    file_name: str | None = None  # uploads only
    final_url: str | None = None  # None for an upload
    upstream_status: int | None = None
    etag: str | None = None  # the answer's validators, for the result cache
    last_modified: str | None = None
    content_language: str | None = None
    charset: str | None = None  # of the Content-Type header
    raw_sha256: str
    needs_ocr: bool = False  # the PDF probe's verdict: a scan or a broken text layer (F4)
    compliance: ComplianceReport | None = None  # the checks the document passed
    attempts: int = 0  # fetches of the target so far


@dataclass(frozen=True)
class DocumentInput:
    """A document's bytes and what is known about them."""

    body: bytes
    meta: DocumentInputMeta


@dataclass(frozen=True)
class OcrNeeded:
    """A document that needs Docling. ``draft`` is MarkItDown's result (None for kinds only
    Docling converts); it is neither cached nor fingerprinted yet."""

    draft: Envelope[ExtractResult] | None
    input: DocumentInput


class DocumentRequest(Protocol):
    """The options of a request that the conversion steps read: an ``ExtractRequest``, or the
    options of an upload."""

    @property
    def format(self) -> OutputFormat: ...

    @property
    def metadata(self) -> bool: ...

    @property
    def links(self) -> bool: ...

    @property
    def ocr(self) -> OcrOption: ...


class DocumentRun(Protocol):
    """The part of the pipeline's run state the document steps use: of a URL's run or an
    upload's. ``warnings`` come first in a result (``document_fetched_directly``)."""

    @property
    def req(self) -> DocumentRequest: ...

    host: str | None
    convert_ms: int
    warnings: list[str]

    def result(self, **fields: Any) -> ExtractResult: ...

    def failed(
        self, code: ErrorCode, message: str, *, retryable: bool = False
    ) -> Envelope[ExtractResult]: ...


@dataclass(frozen=True)
class _Judged:
    quality: Quality
    language: str | None
    metadata: PageMetadata | None
    fingerprint: Fingerprint | None  # not yet compared with the previous one


Converter = Literal["markitdown", "docling"]


@dataclass(frozen=True)
class DocumentMetrics:
    conversions: Counter
    seconds: Histogram
    pages: Counter

    def observe(
        self,
        converter: Converter,
        kind: DocumentKind,
        outcome: str,
        seconds: float,
        pages: int | None = None,
    ) -> None:
        """One conversion: ``outcome`` is ``success`` or the error code it failed with."""
        self.conversions.labels(converter=converter, kind=kind, outcome=outcome).inc()
        self.seconds.labels(converter=converter).observe(seconds)
        if pages:
            self.pages.labels(converter=converter).inc(pages)


# One set of metrics per registry, so several pipelines (tests, app factories) can share one.
_METRICS: WeakKeyDictionary[CollectorRegistry, DocumentMetrics] = WeakKeyDictionary()


def document_metrics_for(registry: CollectorRegistry) -> DocumentMetrics:
    metrics = _METRICS.get(registry)
    if metrics is None:
        metrics = DocumentMetrics(
            conversions=Counter(
                "text_extraction_document_conversions_total",
                "Document conversions by converter, kind and outcome (success or the error code).",
                ["converter", "kind", "outcome"],
                registry=registry,
            ),
            seconds=Histogram(
                "text_extraction_document_conversion_seconds",
                "Duration of document conversions, waiting for a free process included.",
                ["converter"],
                buckets=_SECONDS_BUCKETS,
                registry=registry,
            ),
            pages=Counter(
                "text_extraction_document_pages_total",
                "Pages of documents converted (after the page limit).",
                ["converter"],
                registry=registry,
            ),
        )
        _METRICS[registry] = metrics
    return metrics


def create_document_pool(settings: Settings) -> DocumentPool:
    """The MarkItDown children of a process (``DOCUMENT_CONCURRENCY``,
    ``DOCUMENT_MEMORY_LIMIT_MB``); not started yet."""
    return DocumentPool(
        settings.document_concurrency,
        settings.document_memory_limit_mb,
        target=MARKITDOWN_TARGET,
    )


def document_input(
    fetched: FetchResult,
    kind: DocumentKind,
    compliance: ComplianceReport,
    attempts: int,
) -> DocumentInput:
    """The input of a fetched document."""
    validators = Validators.from_headers(fetched.headers)
    return DocumentInput(
        body=fetched.content,
        meta=DocumentInputMeta(
            kind=kind,
            content_type=media_type(kind),
            final_url=fetched.final_url,
            upstream_status=fetched.status,
            etag=validators.etag,
            last_modified=validators.last_modified,
            content_language=fetched.headers.get("content-language"),
            charset=fetched.charset,
            raw_sha256=fetched.raw_sha256,
            compliance=compliance,
            attempts=attempts,
        ),
    )


def without_ocr(
    needed: OcrNeeded, run: DocumentRun, *, message: str = OCR_DISABLED_MESSAGE
) -> Envelope[ExtractResult]:
    """The answer when the OCR is not done: the draft with the warning ``ocr_unavailable``, or
    ``unsupported_content_type`` with ``message`` for a document without one."""
    draft = needed.draft
    if draft is None or draft.data is None:
        return run.failed(ErrorCode.UNSUPPORTED_CONTENT_TYPE, message)
    warnings = [*draft.data.warnings, OCR_UNAVAILABLE_WARNING]
    return draft.model_copy(update={"data": draft.data.model_copy(update={"warnings": warnings})})


def _ms(since: float) -> int:
    return round((time.perf_counter() - since) * 1000)


class DocumentSteps:
    """Converts the documents of one pipeline. ``pool`` holds the MarkItDown children,
    ``threads`` runs the parent's CPU-bound steps, ``judge`` turns a result into its envelope
    (the quality verdict decides the status); the conversions are counted in ``registry``."""

    def __init__(
        self,
        pool: DocumentPool,
        threads: ConversionPool,
        settings: Settings,
        judge: Callable[[ExtractResult], Envelope[ExtractResult]],
        registry: CollectorRegistry,
    ) -> None:
        self.pool = pool
        self._threads = threads
        self._settings = settings
        self._judge = judge
        self._metrics = document_metrics_for(registry)

    async def extract(
        self, run: DocumentRun, document: DocumentInput, deadline: float
    ) -> Envelope[ExtractResult] | OcrNeeded:
        """The document's envelope, or an ``OcrNeeded`` for the caller to hand on. ``deadline``
        is the extraction's (``loop.time()``)."""
        req, kind = run.req, document.meta.kind
        if kind in DOCLING_KINDS:
            if needs_ocr(kind, None, req.ocr, self._settings):
                return OcrNeeded(draft=None, input=document)
            if req.ocr == "never":
                return run.failed(
                    ErrorCode.UNSUPPORTED_CONTENT_TYPE,
                    f"Content type {document.meta.content_type} is read with OCR only, and the "
                    "request has ocr: never.",
                )
            return run.failed(ErrorCode.UNSUPPORTED_CONTENT_TYPE, OCR_DISABLED_MESSAGE)
        conversion = await self._convert(run, document, deadline)
        if isinstance(conversion, Envelope):
            return conversion
        started = time.perf_counter()
        try:
            judged = await self._threads.run(self._judged, document, conversion)
        except RuntimeError as error:  # the conversion pool is shut down
            return self._unavailable(run, error)
        except Exception as error:  # e.g. metadata that is no PageMetadata
            return self._our_fault(run, kind, type(error).__name__)
        finally:
            run.convert_ms += _ms(started)
        handing_on = needs_ocr(kind, conversion, req.ocr, self._settings)
        warnings = [*run.warnings, *conversion.warnings]
        if not handing_on and wants_ocr(kind, conversion, req.ocr):
            warnings.append(OCR_UNAVAILABLE_WARNING)
        envelope = self._judge(
            run.result(
                content_type=document.meta.content_type,
                language=judged.language,
                word_count=judged.quality.word_count,
                title=conversion.title,
                content=conversion.content,
                quality=judged.quality,
                metadata=judged.metadata if req.metadata else None,
                links=[] if req.links else None,
                fingerprint=judged.fingerprint,
                warnings=warnings,
                document=DocumentInfo(
                    kind=kind,
                    converter="markitdown",
                    pages=conversion.pages,
                    pages_converted=conversion.pages_converted,
                    ocr=False,
                ),
            )
        )
        if not handing_on:
            return envelope
        meta = document.meta.model_copy(update={"needs_ocr": conversion.needs_ocr})
        return OcrNeeded(draft=envelope, input=DocumentInput(document.body, meta))

    async def _convert(
        self, run: DocumentRun, document: DocumentInput, deadline: float
    ) -> DocumentConversion | Envelope[ExtractResult]:
        """MarkItDown's conversion in a child, or the failed envelope."""
        req, meta = run.req, document.meta
        payload = {
            "body": document.body,
            "kind": meta.kind,
            "format": req.format,
            "metadata": True,  # for the language; the result keeps it only when asked
            "base": meta.final_url,
            "content_language": meta.content_language,
            "charset": meta.charset,
            "max_pages": self._settings.document_max_pages,
            "max_uncompressed": self._settings.document_max_uncompressed_bytes,
        }
        started = time.perf_counter()
        outcome: DocumentConversion | Envelope[ExtractResult]
        try:
            await self.pool.start()  # at once when the process started it already
            reply = await self.pool.run("markitdown", payload, deadline - _POOL_MARGIN_SECONDS)
            outcome = DocumentConversion.from_reply(reply)
        except ConversionError as error:
            known = _DOCUMENT_ERRORS.get(error.type_name)
            if known is None:  # never its message: it may quote the document
                outcome = self._our_fault(run, meta.kind, error.type_name)
            else:
                outcome = run.failed(*known)
        except ConversionTimeout:
            outcome = run.failed(
                ErrorCode.TIMEOUT, "The document did not convert in time.", retryable=True
            )
        except ConversionOutOfMemory:
            outcome = run.failed(
                ErrorCode.TOO_LARGE, "The document needs more memory than this service allows."
            )
        except (ConversionCrashed, ValueError, LookupError, TypeError) as error:
            outcome = self._our_fault(run, meta.kind, type(error).__name__)
        except RuntimeError as error:  # the pool is shut down, or its children do not start
            outcome = self._unavailable(run, error)
        finally:
            run.convert_ms += _ms(started)
        self._observe(meta.kind, outcome, time.perf_counter() - started)
        return outcome

    def _observe(
        self,
        kind: DocumentKind,
        outcome: DocumentConversion | Envelope[ExtractResult],
        seconds: float,
    ) -> None:
        if isinstance(outcome, DocumentConversion):
            self._metrics.observe("markitdown", kind, "success", seconds, outcome.pages_converted)
            return
        code = outcome.error.code.value if outcome.error is not None else "unknown"
        self._metrics.observe("markitdown", kind, code, seconds)

    @staticmethod
    def _unavailable(run: DocumentRun, error: RuntimeError) -> Envelope[ExtractResult]:
        # The document pool or the conversion pool is shut down, or children do not start.
        _log.error("document_converter_unavailable", host=run.host, error_type=type(error).__name__)
        return run.failed(ErrorCode.INTERNAL_ERROR, _UNAVAILABLE_MESSAGE, retryable=True)

    def _judged(self, document: DocumentInput, conversion: DocumentConversion) -> _Judged:
        """Quality, language, metadata and fingerprint, like a ``text/plain`` page (F9).
        CPU-bound: runs on the conversion pool."""
        content = conversion.content
        quality = assess(None, content)
        metadata = (
            None
            if conversion.metadata is None
            else PageMetadata.model_validate(conversion.metadata)
        )
        verdict = quality_error(quality, self._settings.quality_min_words)
        failed = verdict is not None and verdict[0] == "failed"
        return _Judged(
            quality=quality,
            language=metadata.language if metadata is not None else None,
            metadata=metadata,
            fingerprint=None if failed else compute_fingerprint(document.meta.raw_sha256, content),
        )

    @staticmethod
    def _our_fault(
        run: DocumentRun, kind: DocumentKind, error_type: str
    ) -> Envelope[ExtractResult]:
        # The type only, and no traceback: the message of a child's exception, or of a
        # validation error, may quote the document.
        _log.error("document_conversion_failed", host=run.host, kind=kind, error_type=error_type)
        return run.failed(ErrorCode.CONVERSION_FAILED, _FAILED_MESSAGE)
