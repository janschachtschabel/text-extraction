"""The fast extraction pipeline behind ``POST /v1/extract``.

One request runs these steps (docs/architecture.md, "Extraction pipeline"):

1. ``resolve_mode``: the compliance mode that applies; a refused choice is a 403.
2. ``validate_url``.
2b. A URL of a supported video platform (``media/sites.py``) in a fast or ``auto`` request
   without browser options, a screenshot or a draft takes the media path (``pipeline_media``,
   M1), with the media fetcher and ``MEDIA_ENABLED``: a playlist is ``unsupported_content_type``
   at once; otherwise the canonical URL passes the result cache (filed under it, with ``media``
   and the requested ``languages``; a stored ``metadata_only`` answer is no hit for
   ``transcribe: true``) and the compliance checks of step 4, and the media steps run in the
   turn of its host, held until they end. Their answer goes through step 8's comparison and
   cache, without validators (a stale entry is extracted again). Steps 3 to 7 below do not run,
   except for a ZDF or 3sat ``.html`` page (``page_fallback``), which may be an article: a
   fresh page entry of its URL answers without a probe, and a probe that finds no video there
   sends the request on to step 3 as a page (the probe's attempt stays counted).
3. Result cache: a fresh entry is served as it is (``cache: hit``).
4. Compliance before the fetch (robots.txt, tdmrep.json, ai.txt); a block ends here. When the
   origin's files are cached, this takes no request. Otherwise the host's ``DomainGate`` turn is
   taken first and the missing files are fetched in it, one after another, before the page: so
   every request to the host counts against its limits. The fetcher checks every request with
   the gate (``DomainGate.before_request``): the turn's own requests go back to back, and each
   keeps the spacing to the latest request of any other turn.
5. The fetch, inside the host's ``DomainGate`` turn (the one of step 4, if taken), and its
   outcome reported to the gate. A stale cache entry's validators are sent; a 304 or unchanged
   bytes reuse the stored result (``cache: revalidated``). Each redirect target passes the
   compliance checks of step 4 before it is requested; one on another host gives the first
   host's turn back and takes the new host's, in which its missing files are fetched too.
6. Any other final status than 2xx (or a 304 to our validators) ends as ``target_http_error``.
7. ``documents.sniff`` names the body's kind: the declared type for HTML and XHTML, the bytes
   first otherwise (ruling F1). HTML and XHTML: decode, compliance after the fetch (headers,
   meta tags, ``noai``; may block), convert, strip domain boilerplate. ``text/plain``: decode,
   header checks, ``convert_plain``. ``text/markdown`` and ``text/x-markdown`` take that path
   too, but are read as Markdown (``documents.formats``: each output format, the first heading
   as the title). A document (PDF, Office, EPUB, CSV, an image ...): up to
   ``DOCUMENT_MAX_BYTES``, header checks, then ``pipeline_documents`` converts it (F10). One that
   needs OCR is handed to ``ocr_handoff``; without one, or while the service has no OCR, it
   answers its text layer with the warning ``ocr_unavailable`` (an image fails
   ``unsupported_content_type``). With the media steps, a body not declared as HTML that sniffs
   as audio or video (``media/sniff.py``, on its first 64 KiB: the fetcher reads a declared
   audio or video type to that head only) passes the header checks and goes to
   ``MediaSteps.media_file``. A cut body that is no media is ``unsupported_content_type``, as is
   anything else.
8. Quality verdict, language, metadata, links, fingerprint and cache. A document's result is
   final: it never goes to the browser, and a cached one is served even with ``ocr_unavailable``.
   An HTML page that does not escalate (fast) or was rendered gets the transcripts of its
   players' ``<track>`` elements appended and its embeds listed (``MediaSteps.tracks``), before
   its verdict.

Media that needs speech recognition (``transcribe: true``) becomes an ``AsrNeeded`` for
``asr_handoff``, which ``run`` calls once the deadline's steps are done, with the answer without
a transcription as its stand-in (never stored), as a document is handed to ``ocr_handoff``.

An upload (``extract_upload``, ``POST /v1/extract/file``) runs steps 7 and 8 on the bytes in hand:
no URL, no compliance checks, no result cache, a fingerprint that is neither compared nor stored
(ruling F17). A part named ``*.md`` or ``*.markdown``, declared ``application/octet-stream``
or not at all, with bytes that look like text, is an upload of ``text/markdown``
(``named_text_type``, issue #17), one named ``*.txt`` of ``text/plain``. With
``MEDIA_ENABLED``, bytes that sniff as audio or video (``sniff_media``, before the document kinds;
the part's declared type never decides) are transcribed (M14): while the service has no ASR
(``ASR_ENABLED`` and ``JOBS_ENABLED``) the upload is ``unsupported_content_type`` ("Transcription
is not enabled on this service."), over ``MEDIA_UPLOAD_MAX_BYTES`` it is ``too_large``, otherwise
an ``AsrNeeded`` (``MediaInputMeta`` with ``source: upload`` and the file name, for the result
only) goes to ``asr_handoff`` once the deadline's steps are done, as media of a URL does. Without a
handoff the stand-in is final. Any other upload over its limit (``CONVERT_MAX_BYTES`` for a page,
``DOCUMENT_MAX_BYTES`` for a document) is ``too_large`` before its bytes are hashed.

The Docling worker builds the result of a document that waited for OCR with
``convert_queued_document``: step 8 on its Docling conversion, the better of it and MarkItDown's
draft, and for a URL the fingerprint and the cache as above (F14).

The ASR worker builds the result of transcribed media with ``transcribed_result``: the
transcript (or the draft with ``asr_unavailable`` when the transcription failed), and for a URL
the fingerprint and the cache as above (M10).

All of it runs under one deadline, ``EXTRACT_TIMEOUT_SECONDS``. Every outcome of the target is
an envelope (HTTP 200) with ``status`` ``success``, ``partial`` or ``failed``; only a refused
compliance mode (403) and a Valkey outage of the outbound gate (503) leave as ``ServiceError``.
A caller that can retry later itself (the job worker) may ask for the gate's refusals
(``OutboundBusy``, ``CircuitOpen``) to be raised instead of answered as envelopes.

Only ``success`` and ``partial`` results are cached and fingerprinted: a bot challenge or an
error page is nothing to serve again, nor to compare the next version of the page with. Requests
with browser secrets (actions, cookies, headers) touch neither; screenshot requests skip the
cache, since their artifact belongs to one job, and so do requests with
``structured.learn_selectors``, which need the page's cleaned HTML (ruling R6).

An HTML page of a request with ``structured`` gets the DOM stage in step 8
(``structured/dom.py``): its options and the owner's learned selectors (read before the cache
lookup) are part of the cache key. Its selector rules run in the selector children
(``structured/selector_pool.py``, ruling R19), killed at ``STRUCTURED_SELECTOR_TIMEOUT_SECONDS``
or the deadline: their fields then stay missing with ``selectors_timed_out``.

With a ``Renderer`` (the browser worker only; docs/architecture.md, "Browser worker"), ``browser``
requests, screenshots, requests with browser options and ``auto`` items with a draft are rendered
instead of fetched, and an ``auto`` request renders when its fast result needs the browser
(``escalation.needs_browser``). A render runs step 4 for the request URL, takes the host's turn and
holds it until the render ends. Each later main-frame navigation is checked before the browser
sends it; the HTTP redirect hops and the document answers are checked when the render returns,
before any content is used. The rendered HTML then goes through step 7 (compliance after the fetch,
conversion) and step 8. An ``auto`` result that will escalate is neither cached nor fingerprinted;
of the fast result (or draft) and the render, only the better one is (``escalation.better``).
Rendered results store no validators, so they are never revalidated. Unblock mode (admins,
``compliance: ignore``, ``BROWSER_UNBLOCK_ENABLED``) uses the unblock renderer and its clearance
cookies.

A YouTube video's details whose subtitles could not be read (``escalation.needs_transcript_panel``,
while ``MEDIA_TRANSCRIPT_PANEL_ENABLED`` and without ``transcribe``) escalate too, and an ``auto``
item with such a draft is rendered by ``_panel_rendered`` instead (P13-S2): the canonical watch
URL in the standard browser (never unblock mode) with the transcript view steps, its checks and
turn as for any render. The answer comes from the page's ``get_panel`` body alone
(``MediaSteps.panel``), never from the rendered page: a transcript, stored under the video's key,
or the draft with ``transcript_panel_unavailable``, not stored.

A page over ``CONVERT_MAX_BYTES`` fails with ``too_large`` before it is decoded. CPU-bound steps
run on the ``ConversionPool`` (``CONVERT_CONCURRENCY`` threads); waiting for a slot counts
against the deadline. The deadline cannot stop a running thread, but every step bounds its
own cost. Documents convert in the child processes of the ``DocumentPool``, which are killed at
the deadline.

The site crawl (package 9, ruling P9-S10) uses four hooks. ``run(..., site_links=True)`` adds
the page's typed links (``site.links.site_links``, on the conversion pool with the rest of the
page's conversion) as ``data.site``: an HTML page's links (at most ``SITE_MAX_LINKS_PER_PAGE``,
else the warning ``links_truncated``), ``[]`` for plain text, no ``site`` for a document or
media; the option joins the cache key only when set. ``fetch_raw`` fetches a file (a sitemap)
with steps 2, 4 and 5 and the header checks of step 7, under the same deadline and gate
refusals, and returns the answer of any HTTP status as it came. ``robots_sitemaps`` reads the
``Sitemap:`` lines of an origin's robots.txt, from the cache or loaded in the host's turn.
``blocked_from_cache`` says from the cached origin files alone whether step 4 would block a URL.
"""

import asyncio
import hashlib
import html as html_lib
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any, Literal, get_args
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

import structlog
from prometheus_client import CollectorRegistry, Counter, Histogram
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from text_extraction.auth import Principal
from text_extraction.boilerplate import load_boilerplate_rules, strip_boilerplate
from text_extraction.browser_options import ArtifactRef
from text_extraction.compliance import (
    ComplianceChecker,
    ComplianceMode,
    ComplianceReport,
    RobotsSitemaps,
    blocking_code,
    resolve_mode,
    with_page_findings,
    with_redirect_findings,
)
from text_extraction.conversion_pool import ConversionPool
from text_extraction.convert import (
    Conversion,
    Link,
    OutputFormat,
    convert_html,
    convert_plain,
    decode_html,
    page_links,
)
from text_extraction.documents.formats import first_heading, from_markdown
from text_extraction.documents.pool import DocumentPool
from text_extraction.documents.routing import OCR_DISABLED_MESSAGE, ocr_available
from text_extraction.documents.sniff import media_type, named_text_type, sniff
from text_extraction.documents.types import DocumentInfo, DocumentKind, OcrOption
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.errors import ServiceError
from text_extraction.escalation import (
    REFUSAL_STATUSES,
    better,
    needs_browser,
    needs_transcript_panel,
    transcript_panel_applies,
    unblock_for,
)
from text_extraction.extract_models import CacheState, ExtractRequest, ExtractResult, Timings
from text_extraction.fetch import Fetcher, FetchError, FetchResult, Validators, split_content_type
from text_extraction.file_models import FileForm
from text_extraction.fingerprint import Fingerprint, FingerprintStore, compute_fingerprint
from text_extraction.jobs import CONTENT_NOT_STORED_WARNING
from text_extraction.media.panel import TRANSCRIPT_PANEL_UNAVAILABLE
from text_extraction.media.pool import MediaFetcher
from text_extraction.media.sites import MediaTarget, media_target
from text_extraction.media.sniff import MEDIA_HEAD_BYTES, media_type_of, sniff_media
from text_extraction.media.transcript import Transcription
from text_extraction.media.types import MediaInfo, MediaKind
from text_extraction.metadata import (
    PageMetadata,
    StructuredItems,
    detect_language,
    extract_metadata,
    metadata_with_items,
    structured_items,
)
from text_extraction.netguard import InvalidUrlError, validate_url
from text_extraction.outbound import (
    CircuitOpen,
    DomainGate,
    HostTurn,
    OutboundBusy,
    normalize_host,
    parse_retry_after,
    record_fetch_failure,
)
from text_extraction.pii.types import PiiOptions
from text_extraction.pipeline_documents import (
    OCR_NOT_HANDED_MESSAGE,
    OCR_NOT_HANDED_REASON,
    OCR_UNAVAILABLE_WARNING,
    DocumentConvert,
    DocumentInput,
    DocumentInputMeta,
    DocumentRequest,
    DocumentSteps,
    OcrNeeded,
    create_document_pool,
    document_input,
    without_ocr,
)
from text_extraction.pipeline_media import (
    PLAYLIST_MESSAGE,
    TRANSCRIPTION_DISABLED_MESSAGE,
    TRANSCRIPTION_NOT_HANDED_MESSAGE,
    AsrNeeded,
    MediaInputMeta,
    MediaSteps,
    NoVideo,
    asr_available,
    asr_failed,
    cached_details_only,
    no_speech,
    panel_result,
    transcribed,
    transcribed_content,
    transcription_final,
    without_asr,
)
from text_extraction.pipeline_render import (
    answers_for_gate,
    as_fetched,
    blocked_resources,
    caller_cookies,
    challenge_not_cleared,
    challenged_by_hosts,
    check_address,
    check_target,
    render_options,
)
from text_extraction.quality import Quality, assess, quality_error
from text_extraction.render import (
    ArtifactSink,
    ArtifactUnavailable,
    ClearanceStore,
    Rendered,
    Renderer,
    RenderOptions,
)
from text_extraction.result_cache import CachedEntry, MemoryPressure, ResultCache, cache_key
from text_extraction.settings import Settings
from text_extraction.site.links import site_links as typed_links
from text_extraction.site.types import SiteInfo
from text_extraction.structured.dom import (
    dom_options,
    dom_stage,
    reads_learned,
    reads_metadata,
    rule_requests,
    runs_dom_stage,
    selector_rules,
    structured_cache_part,
    usable_learned,
)
from text_extraction.structured.post import post_done, without_dom_stage
from text_extraction.structured.selector_pool import (
    SELECTORS_FAILED_WARNING,
    SELECTORS_TIMED_OUT_WARNING,
    SelectorRunner,
    create_selector_runner,
)
from text_extraction.structured.store import (
    LEARNED_SELECTORS_UNAVAILABLE_WARNING,
    LearnedSelectorsUnavailableError,
    SelectorStore,
    selector_host,
)
from text_extraction.structured.types import SelectorRule, StructuredOptions, StructuredResult
from text_extraction.tracing import current_trace_id
from text_extraction.vision.describe import count_images, needs_vision, undescribable_upload
from text_extraction.vision.images import describable_upload

_log = structlog.get_logger(__name__)

# Media types the fast mode converts, and how.
_KINDS: dict[str, Literal["html", "text"]] = {
    "text/html": "html",
    "application/xhtml+xml": "html",
    "text/plain": "text",
    "text/markdown": "text",
    "text/x-markdown": "text",
}
# Text types read as Markdown (issue #15).
_MARKDOWN_TYPES = frozenset({"text/markdown", "text/x-markdown"})
# A render that ends on one of these (or on a download) fetches the file directly (F18).
_DOCUMENT_TYPES = frozenset(
    {media_type(kind) for kind in get_args(DocumentKind)} | {"application/octet-stream"}
)
DOCUMENT_FETCHED_DIRECTLY_WARNING = "document_fetched_directly"
# A page of a ``site_links`` run had more links than ``SITE_MAX_LINKS_PER_PAGE``: the first ones.
LINKS_TRUNCATED_WARNING = "links_truncated"
# Kinds Docling converts without OCR: OpenDocument files have their text.
_NO_OCR_KINDS: frozenset[DocumentKind] = frozenset({"odt", "ods", "odp"})
# A result with one of these is not written to the result cache (see ``_finish``).
_UNCACHED_WARNINGS = frozenset(
    {CONTENT_NOT_STORED_WARNING, SELECTORS_TIMED_OUT_WARNING, SELECTORS_FAILED_WARNING}
)
# Fetch errors that say the host itself is in trouble; they count towards its circuit breaker.
# Upstream answers worth retrying later.
_RETRYABLE_STATUSES = frozenset({408, 425, 429})
# The fetch gets the time left before the deadline minus this, so it times out first and the
# timeout is recorded for the host.
_FETCH_MARGIN_SECONDS = 0.25
# With less time than this left when its turn comes, the target is not fetched (or rendered).
_MIN_FETCH_SECONDS = 1.0
# The renderer's deadline is this much before the extraction's, so the render ends in time for
# the conversion and the result after it.
_RENDER_MARGIN_SECONDS = 2.0
# Only the start of a page is searched for the <html lang> attribute.
_LANG_SCAN_CHARS = 65_536
_MAX_TAG_CHARS = 4096
_LANG_ATTRIBUTE = re.compile(r"""(?:^|\s)(?:xml:)?lang\s*=\s*["']?([^"'\s>]+)""", re.IGNORECASE)
_DURATION_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 3, 5, 7.5, 10, 15, 20, 25, 30)


def _with_pii_time(timings: Timings, handed_off: Timings) -> Timings:
    """This request's ``timings``, with the PII time of a handoff's answer kept: a Docling or
    ASR job's result that came back through the nlp pool carries its ``pii_ms`` (N3)."""
    return timings.model_copy(update={"pii_ms": handed_off.pii_ms})


# Hands a document that needs OCR to the Docling pool and answers for it. It gets the answer
# without OCR too (``pipeline_documents.without_ocr``), for a fallback of its own.
type OcrHandoff = Callable[[OcrNeeded, Envelope[ExtractResult]], Awaitable[Envelope[ExtractResult]]]
# Hands media that needs speech recognition to the ASR queue and answers for it; it gets the
# answer without a transcription too (``pipeline_media.without_asr``), as its stand-in.
type TranscriptionHandoff = Callable[
    [AsrNeeded, Envelope[ExtractResult]], Awaitable[Envelope[ExtractResult]]
]

_BLOCK_MESSAGES = {
    ErrorCode.BLOCKED_BY_ROBOTS: "The site's robots.txt does not allow fetching this URL.",
    ErrorCode.TDM_RESERVED: "The site reserves text and data mining rights for this URL.",
}
_QUALITY_MESSAGES = {
    ErrorCode.BOT_PROTECTION: "The page is a bot challenge, not the content.",
    ErrorCode.LIKELY_ERROR_PAGE: "The page looks like an error page.",
    ErrorCode.LOGIN_REQUIRED: "The page asks for a login.",
    ErrorCode.EMPTY_CONTENT: "No text could be extracted from the page.",
}


@dataclass(frozen=True)
class _Metrics:
    total: Counter
    duration: Histogram


# One set of metrics per registry, so several pipelines (tests, app factories) can share one.
_METRICS: WeakKeyDictionary[CollectorRegistry, _Metrics] = WeakKeyDictionary()


def _metrics_for(registry: CollectorRegistry) -> _Metrics:
    metrics = _METRICS.get(registry)
    if metrics is None:
        metrics = _Metrics(
            total=Counter(
                "text_extraction_extract_total",
                "Extractions by requested mode (upload for files) and outcome (success, partial "
                "or the error code).",
                ["mode", "outcome"],
                registry=registry,
            ),
            duration=Histogram(
                "text_extraction_extract_duration_seconds",
                "Duration of extractions by requested mode (upload for files).",
                ["mode"],
                buckets=_DURATION_BUCKETS,
                registry=registry,
            ),
        )
        _METRICS[registry] = metrics
    return metrics


def _ms(since: float) -> int:
    return round((time.perf_counter() - since) * 1000)


def _failed(
    result: ExtractResult, code: ErrorCode, message: str, *, retryable: bool
) -> Envelope[ExtractResult]:
    """A failed envelope with as much of the result as the run reached."""
    return Envelope(
        status="failed",
        data=result,
        error=ErrorInfo(code=code, message=message, retryable=retryable),
    )


@dataclass
class _Run:
    """What one request has learned so far; a failure at any step reports it."""

    req: ExtractRequest
    report: ComplianceReport
    cache: CacheState
    started: float
    raise_refusals: bool = False
    deadline: asyncio.Timeout | None = None
    host: str | None = None
    final_url: str | None = None
    upstream_status: int | None = None
    content_type: str | None = None
    attempts: int = 0
    compliance_ms: int = 0
    fetch_ms: int = 0
    convert_ms: int = 0
    render_ms: int = 0
    # Set once per request: may it read and write the result cache (no secrets, no screenshot),
    # and fingerprints (no secrets, B6)? Does unblock mode apply (B14)? Is the caller an admin?
    cacheable: bool = True
    private: bool = False
    unblock: bool = False
    admin: bool = False
    draft: Envelope[ExtractResult] | None = None  # the fast result of an ``auto`` item (B5)
    artifacts: ArtifactSink | None = None
    # A render whose result is compared with an earlier one: only the better one is stored.
    comparing: bool = False
    mode_used: Literal["fast", "browser"] = "fast"
    unblocked: bool = False
    warnings: list[str] = field(default_factory=list)  # of the renderer, also on failures
    validators: Validators = field(default_factory=Validators)  # of the fast fetch's answer
    ocr_handoff: OcrHandoff | None = None
    ocr_needed: OcrNeeded | None = None  # for the handoff, once the deadline's steps are done
    # What the fast fetch asks for: the request URL, or the file a render ended on (F18), which
    # is fetched only to convert a document.
    target: str = ""
    direct: bool = False
    media: MediaTarget | None = None  # the platform video the request URL is (step 2b)
    asr_handoff: TranscriptionHandoff | None = None
    asr_needed: AsrNeeded | None = None  # for the handoff, once the deadline's steps are done
    # The owner of learned selectors (the principal's name, D2 a), the ones read for this
    # request (by field, only those the DOM stage uses: S4) and whether reading them failed.
    owner: str | None = None
    learned: dict[str, SelectorRule] = field(default_factory=dict)
    learned_unavailable: bool = False
    site_links: bool = False  # the page's typed links go into ``data.site`` (P9-S10)

    @property
    def cache_url(self) -> str:
        """The URL the result cache and the fingerprints file the result under: a platform
        video's canonical URL (its short forms share it), else the request URL."""
        return self.media.canonical_url if self.media is not None else self.req.url

    def timings(self) -> Timings:
        return Timings(
            total_ms=_ms(self.started),
            compliance_ms=self.compliance_ms,
            fetch_ms=self.fetch_ms,
            convert_ms=self.convert_ms,
            render_ms=self.render_ms,
        )

    def result(self, **fields: Any) -> ExtractResult:
        values: dict[str, Any] = {
            "url": self.req.url,
            "final_url": self.final_url,
            "upstream_status": self.upstream_status,
            "content_type": self.content_type,
            "mode_used": self.mode_used,
            "unblocked": self.unblocked,
            "attempts": self.attempts,
            "language": None,
            "word_count": 0,
            "title": None,
            "format": self.req.format,
            "content": None,
            "quality": None,
            "timings": self.timings(),
            "compliance": self.report,
            "metadata": None,
            "links": None,
            "fingerprint": None,
            "cache": self.cache,
            "warnings": list(self.warnings),
        }
        values.update(fields)
        return ExtractResult(**values)

    def failed(
        self, code: ErrorCode, message: str, *, retryable: bool = False
    ) -> Envelope[ExtractResult]:
        return _failed(self.result(), code, message, retryable=retryable)


@dataclass(frozen=True)
class _UploadOptions:
    """The options of an upload as the conversion steps read them (a ``DocumentRequest``)."""

    format: OutputFormat
    metadata: bool
    ocr: OcrOption
    links: bool = False  # the form has no `links`: without a URL, links stay relative
    structured: StructuredOptions | None = None
    pii: PiiOptions | None = None
    # ``describe_images`` of an image upload: never failed for lacking text (ruling V-S8).
    describe_upload: bool = False


def _upload_options(form: FileForm) -> _UploadOptions:
    """The options of an upload's form as the conversion steps read them."""
    return _UploadOptions(
        format=form.format,
        metadata=form.metadata,
        ocr=form.ocr,
        structured=form.structured,
        pii=form.pii,
        describe_upload=form.describe_images is not None,
    )


@dataclass
class _UploadRun:
    """What one upload has learned so far (``extract_upload``); the conversion steps use it as
    their ``DocumentRun``. ``file_name`` goes into ``document.file_name`` and nowhere else."""

    req: _UploadOptions
    report: ComplianceReport  # decision "skipped": an upload is not checked
    file_name: str | None
    started: float = field(default_factory=time.perf_counter)
    deadline: asyncio.Timeout | None = None
    host: str | None = None  # an upload has none
    kind: str | None = None  # what the bytes are, for the log line
    content_type: str | None = None
    convert_ms: int = 0
    warnings: list[str] = field(default_factory=list)
    ocr_handoff: OcrHandoff | None = None
    ocr_needed: OcrNeeded | None = None  # for the handoff, once the deadline's steps are done
    asr_handoff: TranscriptionHandoff | None = None
    asr_needed: AsrNeeded | None = None  # likewise
    short_ok: bool = False  # a page of fewer words than QUALITY_MIN_WORDS is no "too short"

    def timings(self) -> Timings:
        return Timings(
            total_ms=_ms(self.started), compliance_ms=0, fetch_ms=0, convert_ms=self.convert_ms
        )

    def result(self, **fields: Any) -> ExtractResult:
        document = fields.get("document")
        if isinstance(document, DocumentInfo):
            fields["document"] = document.model_copy(update={"file_name": self.file_name})
        values: dict[str, Any] = {
            "url": None,
            "final_url": None,
            "upstream_status": None,
            "content_type": self.content_type,
            "mode_used": "upload",
            "attempts": 0,
            "language": None,
            "word_count": 0,
            "title": None,
            "format": self.req.format,
            "content": None,
            "quality": None,
            "timings": self.timings(),
            "compliance": self.report,
            "metadata": None,
            "links": None,
            "fingerprint": None,
            "cache": "bypass",
            "warnings": list(self.warnings),
        }
        values.update(fields)
        return ExtractResult(**values)

    def failed(
        self, code: ErrorCode, message: str, *, retryable: bool = False
    ) -> Envelope[ExtractResult]:
        return _failed(self.result(), code, message, retryable=retryable)


@dataclass(frozen=True)
class _Stored:
    key: str
    entry: CachedEntry
    result: ExtractResult
    fresh: bool


@dataclass(frozen=True)
class _Processed:
    content: str
    title: str | None
    warnings: list[str]
    quality: Quality
    language: str | None
    metadata: PageMetadata | None
    links: list[Link] | None
    fingerprint: Fingerprint | None  # not yet compared with the previous one
    structured: StructuredResult | None = None  # the DOM stage's (S3), for HTML only
    dom: "_DomWork | None" = None  # the DOM stage still to run, once ``_process`` is done
    site: SiteInfo | None = None  # the page's typed links, for a ``site_links`` run


@dataclass(frozen=True)
class _DomWork:
    """The DOM stage of an HTML page (S3) after ``_process``: its selector ``rules`` (the
    caller's over the learned, ``dom.selector_rules``) run in a selector child first (R19), then
    ``dom.dom_stage`` on the conversion pool."""

    html: str
    final_url: str
    options: StructuredOptions
    metadata: PageMetadata | None
    items: StructuredItems
    rules: dict[str, SelectorRule]


@dataclass(frozen=True)
class RawFetch:
    """What ``Pipeline.fetch_raw`` got: the answer (``fetched``, any HTTP status) or the
    ``failure`` that ended it (a compliance block, a fetch error, a gate refusal, the deadline),
    never both; the compliance ``report``, the requests sent for the URL (``attempts``) and the
    ``timings``."""

    fetched: FetchResult | None
    report: ComplianceReport
    failure: Envelope[ExtractResult] | None
    attempts: int
    timings: Timings


class Pipeline:
    """Runs one extraction from URL to result envelope. Create once per app."""

    def __init__(
        self,
        fetcher: Fetcher,
        gate: DomainGate,
        checker: ComplianceChecker,
        cache: ResultCache,
        fingerprints: FingerprintStore,
        settings: Settings,
        registry: CollectorRegistry,
        pool: ConversionPool | None = None,
        *,
        renderer: Renderer | None = None,
        unblock_renderer: Renderer | None = None,
        clearance: ClearanceStore | None = None,
        timeout_seconds: float | None = None,
        document_pool: DocumentPool | None = None,
        media_fetcher: MediaFetcher | None = None,
        selector_store: SelectorStore | None = None,
        selector_runner: SelectorRunner | None = None,
    ) -> None:
        """``pool`` runs the CPU-bound steps; without one the pipeline makes its own. So does
        ``document_pool``, the MarkItDown children; the pipeline starts it with the first
        document, unless its owner started it already. ``media_fetcher`` (started by its owner)
        probes platform videos; without it, or with ``MEDIA_ENABLED`` off, a platform URL is a
        page, a media file is ``unsupported_content_type`` and pages get no HTML5 tracks.

        Only the browser worker passes a ``renderer`` (and, with unblock mode enabled, the
        ``unblock_renderer`` and the ``clearance`` store). ``timeout_seconds`` replaces
        ``EXTRACT_TIMEOUT_SECONDS`` as the deadline; an unblock render has
        ``BROWSER_UNBLOCK_TIMEOUT_SECONDS``.

        ``selector_store`` holds the learned selectors of ``structured`` (S8); without it no
        request reads any. ``selector_runner`` runs selector rules in child processes (R19);
        without one the pipeline makes its own, whose children start with the first rule."""
        self._fetcher = fetcher
        self._gate = gate
        self._checker = checker
        self._cache = cache
        self._fingerprints = fingerprints
        self._settings = settings
        self._pool = pool if pool is not None else ConversionPool(settings.convert_concurrency)
        self._rules = load_boilerplate_rules()
        self._metrics = _metrics_for(registry)
        self._registry = registry
        self._renderer = renderer
        self._unblock_renderer = unblock_renderer
        self._clearance = clearance
        self._timeout_seconds = timeout_seconds
        self._selector_store = selector_store
        self._selectors = (
            selector_runner if selector_runner is not None else create_selector_runner(settings)
        )
        self._blocked_resources = blocked_resources(settings)
        self._documents = DocumentSteps(
            document_pool if document_pool is not None else create_document_pool(settings),
            self._pool,
            settings,
            self._envelope,
            registry,
        )
        self._media = (
            MediaSteps(
                media_fetcher, fetcher, gate, settings, self._envelope, registry, threads=self._pool
            )
            if media_fetcher is not None and settings.media_enabled
            else None
        )

    @property
    def selector_store(self) -> SelectorStore | None:
        """The store of learned selectors (S8), for the post stage's ``learn_selectors``."""
        return self._selector_store

    async def _load_learned(self, run: _Run) -> None:
        """Read the learned selectors of the request's owner, host and schema (S4), before the
        cache lookup: they join the key. A Valkey failure means none, with the warning
        ``learned_selectors_unavailable``. A request with ``pii`` reads none (R12)."""
        options = dom_options(run.req.structured, run.req.pii)
        host = selector_host(run.req.url)
        if (
            options is None
            or options.compiled is None
            or not reads_learned(options)
            or self._selector_store is None
            or run.owner is None
            or host is None
        ):
            return
        try:
            stored = await self._selector_store.get(run.owner, host, options.compiled.schema_id)
        except LearnedSelectorsUnavailableError:
            run.learned_unavailable = True
            return
        run.learned = usable_learned(options, stored)

    async def run(
        self,
        req: ExtractRequest,
        principal: Principal,
        *,
        gate_refusals: Literal["fail", "raise"] = "fail",
        artifacts: ArtifactSink | None = None,
        draft: Envelope[ExtractResult] | None = None,
        ocr_handoff: OcrHandoff | None = None,
        asr_handoff: TranscriptionHandoff | None = None,
        site_links: bool = False,
    ) -> Envelope[ExtractResult]:
        """Extract ``req.url``. Raises ``ServiceError`` only for a refused compliance mode (403)
        and an unavailable Valkey (503).

        A refusal of the outbound gate is a ``rate_limited`` (busy) or ``connection_error``
        (paused) envelope, retryable. With ``gate_refusals="raise"`` the ``OutboundBusy`` or
        ``CircuitOpen`` propagates unchanged instead; the metrics count it under the same code.

        With a renderer: ``artifacts`` stores the screenshot of a ``screenshot`` request (it is
        required then), and ``draft`` is the fast result of an ``auto`` item, which the render is
        compared with instead of fetching the page again (B5).

        A document that needs OCR goes to ``ocr_handoff``, once the steps under the deadline are
        done, with the answer without OCR (``pipeline_documents.without_ocr``); its envelope is
        the answer (the pool it hands to stores the result it keeps). Without it, the
        document's text layer answers with the warning ``ocr_unavailable``; an image fails
        ``unsupported_content_type``.

        Media that needs speech recognition (``transcribe: true``) goes to ``asr_handoff`` the
        same way, with the answer without a transcription (``pipeline_media.without_asr``).
        Without it that answer is final.

        ``site_links`` (the site crawl's pages, P9-S10) adds the page's typed links as
        ``data.site``, and keys the result cache apart.
        """
        mode = resolve_mode(req.compliance, principal, self._settings)
        private = not req.secrets().empty()
        cacheable = _cacheable(req, private)
        run = _Run(
            req=req,
            report=ComplianceReport(mode=mode, decision="skipped"),  # until it is checked
            cache="miss" if cacheable else "bypass",
            started=time.perf_counter(),
            raise_refusals=gate_refusals == "raise",
            cacheable=cacheable,
            private=private,
            unblock=unblock_for(principal, mode, self._settings),
            admin=principal.role == "admin",
            draft=draft if req.mode == "auto" else None,
            artifacts=artifacts,
            ocr_handoff=ocr_handoff,
            target=req.url,
            asr_handoff=asr_handoff,
            owner=principal.name,
            site_links=site_links,
        )
        outcome = ErrorCode.INTERNAL_ERROR.value
        try:
            envelope = await self._within_deadline(run)
            if run.ocr_needed is not None and run.ocr_handoff is not None:
                # After the deadline's steps: the handoff waits for OCR on a clock of its own
                # (DOCLING_SYNC_WAIT_SECONDS, F12).
                envelope = await run.ocr_handoff(run.ocr_needed, envelope)
            if run.asr_needed is not None and run.asr_handoff is not None:
                # Likewise: a transcription is always a job (M9).
                envelope = await run.asr_handoff(run.asr_needed, envelope)
            if envelope.data is not None:
                envelope.data.timings = _with_pii_time(run.timings(), envelope.data.timings)
                if run.learned_unavailable:
                    # Not part of the stored result: the next request may read them again.
                    envelope.data.warnings.append(LEARNED_SELECTORS_UNAVAILABLE_WARNING)
            envelope.trace_id = current_trace_id()
            outcome = _outcome(envelope)
            return envelope
        except OutboundBusy:
            outcome = ErrorCode.RATE_LIMITED.value
            raise
        except CircuitOpen:
            outcome = ErrorCode.CONNECTION_ERROR.value
            raise
        finally:
            self._observe(run, outcome)

    async def extract_upload(
        self,
        body: bytes,
        name: str | None,
        form: FileForm,
        principal: Principal,
        *,
        content_type: str | None = None,
        ocr_handoff: OcrHandoff | None = None,
        asr_handoff: TranscriptionHandoff | None = None,
        short_ok: bool = False,
    ) -> Envelope[ExtractResult]:
        """Extract an uploaded file (ruling F17). ``name`` is its sanitised file name, reported
        as ``document.file_name`` and never logged; ``content_type`` is the part's declared
        type, which decides only where the bytes do not (F1).

        Steps 7 and 8 of ``run`` on the bytes in hand, under the same deadline: HTML and plain
        text convert as pages, without a base URL (relative links stay as they are), documents
        through ``DocumentSteps``. No compliance check (``decision: skipped``), no result cache
        (``cache: bypass``); the fingerprint is computed, but neither compared nor stored. A
        document that needs OCR goes to ``ocr_handoff`` after the deadline, as in ``run``;
        without one its text layer answers with ``ocr_unavailable``.

        Audio and video (decided by the bytes, M14) go to ``asr_handoff`` the same way, with the
        answer without a transcription as its stand-in; without a handoff that answer is final.

        ``short_ok`` (``POST /v1/chunk`` with a ``text``, ruling R15): the word-count gate
        (``QUALITY_MIN_WORDS``) does not apply to an HTML or plain-text upload, since the caller
        sent the text; a text without any word is still ``failed``/``empty_content``.
        """
        mode = resolve_mode(None, principal, self._settings)
        run = _UploadRun(
            req=_upload_options(form),
            report=ComplianceReport(mode=mode, decision="skipped"),
            file_name=name,
            ocr_handoff=ocr_handoff,
            asr_handoff=asr_handoff,
            short_ok=short_ok,
        )
        outcome = ErrorCode.INTERNAL_ERROR.value
        try:
            envelope = await self._upload_within_deadline(run, body, content_type)
            if run.ocr_needed is not None and run.ocr_handoff is not None:
                # After the deadline's steps, as in `run` (F12).
                envelope = await run.ocr_handoff(run.ocr_needed, envelope)
            if run.asr_needed is not None and run.asr_handoff is not None:
                envelope = await run.asr_handoff(run.asr_needed, envelope)
            if run.ocr_needed is None or run.ocr_handoff is None:
                # A result the Docling worker makes gets this there (convert_queued_document).
                envelope = self._undescribable(envelope, form, body)
            if envelope.data is not None:
                envelope.data.timings = _with_pii_time(run.timings(), envelope.data.timings)
            envelope.trace_id = current_trace_id()
            outcome = _outcome(envelope)
            return envelope
        finally:
            # Kind and size only: never the file name.
            self._record("upload", run.started, outcome, kind=run.kind, size=len(body))

    async def convert_queued_document(
        self,
        req: ExtractRequest | FileForm,
        principal: Principal,
        document: DocumentInput,
        draft: Envelope[ExtractResult] | None,
        convert: DocumentConvert,
    ) -> Envelope[ExtractResult]:
        """The result of a document the Docling worker converts (ruling F14): ``req`` is the
        request of its job (an ``ExtractRequest`` for a URL, the ``FileForm`` of an upload),
        ``document`` its stored input, ``draft`` MarkItDown's result of it (None for an image or
        an OpenDocument file), ``convert`` the Docling conversion.

        The result is built like the inline one (F7-F9): the run starts from what the input's
        meta knows (final URL, status, compliance report, attempts), ``mode_used`` is ``fast``
        (``upload`` for an upload), ``document.converter`` is ``docling``. Under ``ocr:
        always``, or when the input's probe said the text layer needs OCR (``needs_ocr``), the
        OCR result is kept unless it failed; otherwise the better of draft and OCR result
        (``escalation.better``; on a tie the OCR result). A draft kept because the OCR failed
        gets the warning ``ocr_unavailable``; when that failure is retryable, the draft also
        loses its fingerprint, so it is neither compared nor cached and the next request gets
        the OCR. For a URL the kept result is compared with the URL's previous fingerprint and
        stored in the result cache, with the input's validators (none for a file a render ended
        on), like any fast result (``_finish``). An upload's is not: its fingerprint is
        computed, but neither compared nor stored (F17)."""
        meta = document.meta
        run: _Run | _UploadRun
        if isinstance(req, FileForm):
            run = _UploadRun(
                req=_upload_options(req),
                report=ComplianceReport(
                    mode=resolve_mode(None, principal, self._settings), decision="skipped"
                ),
                file_name=meta.file_name,
                kind=meta.kind,
                content_type=meta.content_type,
            )
        else:
            run = self._queued_run(req, principal, meta)
            await self._load_learned(run)  # the key of the URL's sync requests (S4)
        converted = await convert(run, document)
        if isinstance(converted, Envelope):
            ocr = converted
        else:
            ocr = await self._documents.envelope(
                run,
                document,
                converted,
                converter="docling",
                ocr=meta.kind not in _NO_OCR_KINDS,
                warnings=list(converted.warnings),
            )
        if draft is None:
            kept = ocr
        elif req.ocr == "always" or meta.needs_ocr:
            # The caller asked for Docling, or the probe found the text layer a scan's or broken
            # (F4, F11): any OCR result that did not fail wins. ``better`` would count the words
            # of a garbage layer, or the headers and footers Docling leaves out.
            kept = draft if ocr.status == "failed" else ocr
        else:
            kept = better(draft, ocr)
        if kept is draft and ocr.status == "failed" and kept.data is not None:
            update: dict[str, Any] = {"warnings": [*kept.data.warnings, OCR_UNAVAILABLE_WARNING]}
            if ocr.error is not None and ocr.error.retryable:
                # The OCR may work next time (models missing, the pool unavailable): the draft
                # answers, but is neither compared nor stored, as a sync request's fallback.
                update["fingerprint"] = None
            kept = kept.model_copy(update={"data": kept.data.model_copy(update=update)})
        if isinstance(run, _Run):
            # The validators fit the request URL only, not a file a render ended on (F18).
            validators = (
                Validators()
                if run.direct
                else Validators(etag=meta.etag, last_modified=meta.last_modified)
            )
            kept = await self._finish(run, kept, validators)
        if isinstance(req, FileForm):
            kept = self._undescribable(kept, req, document.body)
        kept.trace_id = current_trace_id()
        return kept

    def _undescribable(
        self, envelope: Envelope[ExtractResult], form: FileForm, body: bytes
    ) -> Envelope[ExtractResult]:
        """An image upload of ``describe_images`` whose bytes the vision model cannot take
        (``vision.images.describable_upload``: over ``LLM_VISION_MAX_IMAGE_BYTES``, or not PNG,
        JPEG or WebP by their magic bytes) gets its image ``skipped`` here, where the bytes are
        known, counted in the pipeline's registry (ruling R-V3). ``needs_vision`` is false after
        it, so the result takes no llm hop for its description, and no input is kept for it.
        Any other envelope comes back as it is."""
        describe = form.describe_images
        data = envelope.data
        if (
            describe is None
            or data is None
            or post_done(envelope, None, None, describe=describe)
            or not needs_vision(data, describe)
            or describable_upload(body, self._settings.llm_vision_max_image_bytes)
        ):
            return envelope
        outcome = undescribable_upload(data, describe)
        count_images(outcome.report, self._registry)
        warnings = list(data.warnings)
        warnings.extend(warning for warning in outcome.warnings if warning not in warnings)
        update = {"data": outcome.data.model_copy(update={"warnings": warnings})}
        return envelope.model_copy(update=update)

    def _queued_run(
        self, req: ExtractRequest, principal: Principal, meta: DocumentInputMeta
    ) -> _Run:
        """The run state of a URL's queued document, from what its fetch learned: the target
        (a file a render ended on, F18) and the run's warnings included."""
        report = meta.compliance or ComplianceReport(
            mode=resolve_mode(req.compliance, principal, self._settings), decision="skipped"
        )
        private = not req.secrets().empty()
        cacheable = _cacheable(req, private)
        return _Run(
            req=req,
            report=report,
            cache="miss" if cacheable else "bypass",
            started=time.perf_counter(),
            cacheable=cacheable,
            private=private,
            host=urlsplit(req.url).hostname,
            final_url=meta.final_url,
            upstream_status=meta.upstream_status,
            content_type=meta.content_type,
            attempts=meta.attempts,
            warnings=list(meta.warnings),
            target=meta.target or req.url,
            direct=meta.target is not None,
            owner=principal.name,
        )

    async def transcribed_result(
        self,
        req: ExtractRequest | FileForm,
        principal: Principal,
        meta: MediaInputMeta,
        draft: Envelope[ExtractResult] | None,
        transcription: Transcription | Envelope[ExtractResult],
        *,
        upstream_status: int | None = None,
        final: bool = False,
    ) -> Envelope[ExtractResult]:
        """The result of media the ASR worker transcribed (ruling M10): ``req`` is the request
        of its job (an ``ExtractRequest`` for a URL, the ``FileForm`` of an upload), ``meta``
        its stored input, ``draft`` a platform video's metadata result (None for a file or an
        upload), ``transcription`` the speech recognition, or the failed envelope that says why
        there is none (``upstream_status``: the target's answer to a failed download; ``final``:
        the failure belongs to the media, so a draft answering it is stored).

        A transcription becomes the result as the media steps build one (M6): ``# <title>`` (the
        draft's) and paragraphs, ``transcript_source: asr`` with ``asr_model``, the draft's metadata
        and details, and ``upstream_status`` the downloaded file's answer (else the draft's); its
        quality verdict decides the status. Media without speech answers the draft, final
        (``no_transcript_available``), or ``failed`` without one. A failure is answered by the draft
        with ``asr_unavailable`` (``pipeline_media.asr_failed``), or without a draft ``failed``. For
        a URL the answer is compared with the URL's previous fingerprint and stored in the result
        cache under the URL's key (a platform video's canonical URL, with ``media``), like a fast
        result (``_finish``), so the next request is a hit. An upload's is not: its fingerprint is
        computed, but neither compared nor stored (F17)."""
        run: _Run | _UploadRun
        if isinstance(req, FileForm):
            run = _UploadRun(
                req=_upload_options(req),
                report=ComplianceReport(
                    mode=resolve_mode(None, principal, self._settings), decision="skipped"
                ),
                file_name=meta.file_name,
                kind=meta.kind,
                content_type=media_type_of(meta.kind) if meta.kind is not None else None,
            )
        else:
            run = self._media_run(req, principal, meta)
            await self._load_learned(run)  # the key of the URL's sync requests (S4)
        envelope: Envelope[ExtractResult]
        if isinstance(transcription, Envelope):
            error = transcription.error or ErrorInfo(
                code=ErrorCode.INTERNAL_ERROR, message="The transcription failed.", retryable=True
            )
            envelope = asr_failed(
                run, meta, draft, error, upstream_status=upstream_status, final=final
            )
        else:
            envelope = await self._transcribed(run, meta, draft, transcription)
        if isinstance(run, _Run):
            envelope = await self._finish(run, envelope, Validators())
        envelope.trace_id = current_trace_id()
        return envelope

    async def _transcribed(
        self,
        run: _Run | _UploadRun,
        meta: MediaInputMeta,
        draft: Envelope[ExtractResult] | None,
        transcription: Transcription,
    ) -> Envelope[ExtractResult]:
        details = draft.data if draft is not None and draft.data is not None else None
        if isinstance(run, _Run):
            run.attempts += 1  # the download
            run.fetch_ms += transcription.fetch_ms
            # The downloaded file's answer, else the one the video's details came with.
            run.upstream_status = (
                transcription.upstream_status
                if transcription.upstream_status is not None
                else (details.upstream_status if details is not None else None)
            )
        if transcription.kind is not None:
            run.content_type = media_type_of(transcription.kind)
        run.convert_ms += transcription.convert_ms
        if not transcription.has_speech:
            return no_speech(run, meta, draft, transcription)
        title = details.title if details is not None else None
        started = time.perf_counter()
        built = await self._pool.run(transcribed_content, run.req.format, transcription, title)
        run.convert_ms += _ms(started)
        verdict = quality_error(built.quality, self._settings.quality_min_words)
        result = transcribed(
            run,
            meta,
            draft,
            transcription,
            built,
            metadata=run.req.metadata,
            links=run.req.links,
            kept=verdict is None or verdict[0] != "failed",
        )
        return self._envelope(result)

    def _media_run(self, req: ExtractRequest, principal: Principal, meta: MediaInputMeta) -> _Run:
        """The run state of a URL's transcribed media, from what its extraction learned: the
        compliance report, the final (or canonical) URL and the fetches so far. A platform
        video's result is filed under its canonical URL with ``media`` (step 2b)."""
        report = meta.compliance or ComplianceReport(
            mode=resolve_mode(req.compliance, principal, self._settings), decision="skipped"
        )
        private = not req.secrets().empty()
        cacheable = _cacheable(req, private)
        return _Run(
            req=req,
            report=report,
            cache="miss" if cacheable else "bypass",
            started=time.perf_counter(),
            cacheable=cacheable,
            private=private,
            host=urlsplit(req.url).hostname,
            final_url=meta.url,
            attempts=meta.attempts,
            target=req.url,
            media=media_target(req.url) if meta.source == "platform" else None,
            owner=principal.name,
        )

    # --- hooks of the site crawl (P9-S10) -------------------------------------------------------

    async def fetch_raw(
        self,
        url: str,
        principal: Principal,
        compliance: ComplianceMode | None,
        *,
        max_bytes: int,
        truncate: bool,
        gate_refusals: Literal["fail", "raise"] = "fail",
    ) -> RawFetch:
        """Fetch ``url`` (a sitemap) as ``run`` fetches a page, without converting it: the URL
        is validated (step 2), its origin's files are checked (step 4, in the host's turn when
        they are not cached), it is fetched in that turn with every redirect hop checked before
        its request and the outcome recorded for the gate (step 5), and the answer's headers
        get the checks of step 7 (``after_fetch`` without a page). A block, a fetch error, a
        refusal of the gate and the deadline (``EXTRACT_TIMEOUT_SECONDS``) end as the
        ``failure`` envelope ``run`` would answer; an answer of any HTTP status is ``fetched``.
        At most ``max_bytes`` are read; with ``truncate`` a longer body is cut there
        (``fetched.truncated``), otherwise it fails ``too_large``.

        Raises ``ServiceError`` for a refused compliance mode (403) and an unavailable Valkey
        (503), and with ``gate_refusals="raise"`` the gate's ``OutboundBusy`` or
        ``CircuitOpen``, as ``run`` does."""
        mode = resolve_mode(compliance, principal, self._settings)
        # Internal: only the URL, the mode and the format (of a failure's result) are read.
        req = ExtractRequest.model_construct(
            url=url, mode="fast", format="text", metadata=False, compliance=compliance
        )
        run = _Run(
            req=req,
            report=ComplianceReport(mode=mode, decision="skipped"),  # until it is checked
            cache="bypass",
            started=time.perf_counter(),
            raise_refusals=gate_refusals == "raise",
            cacheable=False,
            target=url,
        )
        deadline = asyncio.timeout(self._timeout_seconds or self._settings.extract_timeout_seconds)
        outcome: FetchResult | Envelope[ExtractResult]
        try:
            async with deadline:
                run.deadline = deadline
                outcome = await self._raw(run, max_bytes=max_bytes, truncate=truncate)
        except TimeoutError:
            if not deadline.expired():
                raise
            outcome = run.failed(
                ErrorCode.TIMEOUT, "The fetch did not finish in time.", retryable=True
            )
        if isinstance(outcome, Envelope):
            outcome.trace_id = current_trace_id()
            return RawFetch(None, run.report, outcome, run.attempts, run.timings())
        return RawFetch(outcome, run.report, None, run.attempts, run.timings())

    async def _raw(
        self, run: _Run, *, max_bytes: int, truncate: bool
    ) -> FetchResult | Envelope[ExtractResult]:
        """The steps of ``fetch_raw``: the answer, or the envelope of what ended it."""
        try:
            validate_url(run.target)
        except InvalidUrlError as error:
            return run.failed(ErrorCode.INVALID_URL, str(error))
        run.host = urlsplit(run.target).hostname

        async def checked_fetch() -> FetchResult | Envelope[ExtractResult]:
            turn = HostTurn(self._gate)
            try:
                run.report = await self._check_origin(run, run.target, turn)
                if (code := blocking_code(run.report)) is not None:
                    return run.failed(code, _BLOCK_MESSAGES[code])
                return await self._fetch(run, None, turn, max_bytes=max_bytes, truncate=truncate)
            finally:
                await turn.give_back()  # `_fetch` gives it back as soon as the fetch ends

        outcome = await self._guarded(run, checked_fetch())
        if isinstance(outcome, Envelope):
            return outcome
        started = time.perf_counter()
        run.report = self._checker.after_fetch(run.report, outcome, None)  # the headers only
        run.compliance_ms += _ms(started)
        if (code := blocking_code(run.report)) is not None:
            return run.failed(code, _BLOCK_MESSAGES[code])
        return outcome

    async def robots_sitemaps(
        self, url: str, *, gate_refusals: Literal["fail", "raise"] = "fail"
    ) -> RobotsSitemaps:
        """The sitemaps the robots.txt of ``url``'s origin lists (``state`` and ``urls``): from
        the compliance cache without a request, else loaded in the host's turn (robots.txt
        alone, within the 10 s budget of an origin's files) and cached for the checks. It reads
        in every compliance mode: reading robots.txt checks nothing.

        A refusal of the gate answers ``unavailable``, or with ``gate_refusals="raise"``
        propagates; an unavailable gate backend is a ``ServiceError`` (503), as in ``run``."""
        cached = await self._checker.robots_sitemaps(url, cached_only=True)
        if cached is not None:
            return cached
        turn = HostTurn(self._gate)
        try:
            await turn.take(urlsplit(url).hostname or "", None)
            loaded = await self._checker.robots_sitemaps(url, cached_only=False)
        except (OutboundBusy, CircuitOpen):
            if gate_refusals == "raise":
                raise
            return RobotsSitemaps(state="unavailable")
        except RedisError as error:
            _log.warning("outbound_gate_unavailable", error_type=type(error).__name__)
            raise ServiceError(
                ErrorCode.INTERNAL_ERROR,
                "The outbound state backend is unavailable. Try again later.",
                503,
                retryable=True,
            ) from None
        finally:
            await turn.give_back()
        if loaded is None:  # pragma: no cover - only `cached_only` answers None
            raise RuntimeError("robots.txt was not loaded")
        return loaded

    async def blocked_from_cache(self, url: str, mode: ComplianceMode) -> bool | None:
        """Whether step 4 would block ``url`` under ``mode``, judged from the cached origin
        files alone (no request): None while they are not cached."""
        report = await self._checker.cached_before_fetch(url, mode)
        if report is None:
            return None
        return blocking_code(report) is not None

    # --- steps ---------------------------------------------------------------------------------

    async def _within_deadline(self, run: _Run) -> Envelope[ExtractResult]:
        seconds = self._timeout_seconds or self._settings.extract_timeout_seconds
        chosen = self._renderer_for(run)
        if chosen is not None and chosen[1]:
            seconds = self._settings.browser_unblock_timeout_seconds
        deadline = asyncio.timeout(seconds)
        try:
            async with deadline:
                run.deadline = deadline
                return await self._extract(run)
        except TimeoutError:
            if not deadline.expired():
                raise
            return run.failed(
                ErrorCode.TIMEOUT, "The extraction did not finish in time.", retryable=True
            )

    async def _extract(self, run: _Run) -> Envelope[ExtractResult]:
        req = run.req
        try:
            validate_url(req.url)
        except InvalidUrlError as error:
            return run.failed(ErrorCode.INVALID_URL, str(error))
        run.host = urlsplit(req.url).hostname
        target = self._platform_target(run)
        if target is not None and target.video_id is None:
            return run.failed(ErrorCode.UNSUPPORTED_CONTENT_TYPE, PLAYLIST_MESSAGE)
        run.media = target
        await self._load_learned(run)
        if run.draft is not None and needs_transcript_panel(run.draft):
            # A video's details whose subtitles could not be read: its transcript view, never
            # the watch page's own text (P13-S2).
            return await self._panel_rendered(run, run.draft)

        stored = await self._cached(run) if run.cacheable else None
        if stored is not None and req.transcribe and _metadata_only(stored.result):
            stored = None  # the details only: a transcription may do better
        if stored is not None and stored.fresh:
            return self._serve_hit(run, stored)
        if target is not None:
            if target.page_fallback:
                # A page of a platform with articles too: one already known to have no video
                # is served from its page entry, without a probe.
                run.media = None
                stored = await self._cached(run) if run.cacheable else None
                if stored is not None and stored.fresh:
                    return self._serve_hit(run, stored)
                run.media = target
            # A platform result has no validators: a stale one is extracted again.
            answer = await self._guarded(run, self._platform(run, target))
            if (
                answer is not None
                and self._renderer is not None
                and self._panel_escalates(run, answer)
            ):
                return await self._panel_rendered(run, answer)
            if answer is not None:
                return answer
            # No video after all: the URL is a page (``page_fallback``), from here on as any.
        if stored is not None and stored.result.mode_used == "browser":
            stored = None  # a rendered result has no validators and is never revalidated (B10)
        if self._renders_first(run):
            return await self._rendered(run, earlier=run.draft)
        fast = await self._guarded(run, self._fetch_and_extract(run, stored))
        if self._renderer_for(run) is not None and self._escalates(run, fast):
            return await self._rendered(run, earlier=fast)
        return fast

    async def _guarded[T](self, run: _Run, step: Awaitable[T]) -> T | Envelope[ExtractResult]:
        """``step``'s envelope; a failed fetch or render and a refusal of the gate become
        failed envelopes, and an unavailable gate backend a 503."""
        try:
            return await step
        except FetchError as error:
            run.upstream_status = error.upstream_status
            return run.failed(error.code, error.message, retryable=error.retryable)
        except OutboundBusy as busy:
            if run.raise_refusals:
                raise
            return run.failed(
                ErrorCode.RATE_LIMITED,
                "Too many requests to this site are waiting; "
                f"retry after {_seconds(busy.retry_after)} s.",
                retryable=True,
            )
        except CircuitOpen as paused:
            if run.raise_refusals:
                raise
            return run.failed(
                ErrorCode.CONNECTION_ERROR,
                "Requests to this site are paused after errors or a rate limit; "
                f"retry after {_seconds(paused.retry_after)} s.",
                retryable=True,
            )
        except RedisError as error:
            _log.warning("outbound_gate_unavailable", error_type=type(error).__name__)
            raise ServiceError(
                ErrorCode.INTERNAL_ERROR,
                "The outbound state backend is unavailable. Try again later.",
                503,
                retryable=True,
            ) from None

    def _platform_target(self, run: _Run) -> MediaTarget | None:
        """Step 2b: the platform video the request URL is, when the media path takes it: media
        on, a fast or ``auto`` request without browser options, a screenshot or a draft (those
        want the page itself)."""
        req = run.req
        if (
            self._media is None
            or req.mode == "browser"
            or run.private
            or req.screenshot
            or run.draft is not None
        ):
            return None
        return media_target(req.url)

    async def _platform(self, run: _Run, target: MediaTarget) -> Envelope[ExtractResult] | None:
        """A platform video (M1): the canonical URL passes the compliance checks of step 4,
        then the media steps run in its host's turn, which is held until they end. None when a
        ``page_fallback`` target has no video: the run is reset to a page's (the probe's
        attempt stays counted), and the caller goes on with the page path."""
        if self._media is None:  # pragma: no cover - only called with media on
            raise RuntimeError("no media steps")
        run.final_url = target.canonical_url
        turn = HostTurn(self._gate)
        try:
            run.report = await self._check_origin(run, target.canonical_url, turn)
            if (code := blocking_code(run.report)) is not None:
                return run.failed(code, _BLOCK_MESSAGES[code])
            waited = time.perf_counter()
            await turn.take(urlsplit(target.canonical_url).hostname or "", run.report.crawl_delay)
            run.fetch_ms += _ms(waited)
            outcome = await self._media.platform(run, target)
        finally:
            await turn.give_back()
        if isinstance(outcome, NoVideo):
            run.media = None
            run.final_url = None
            run.report = ComplianceReport(mode=run.report.mode, decision="skipped")
            return None
        if isinstance(outcome, AsrNeeded):
            return self._without_asr(run, outcome)
        return await self._finish(run, outcome, Validators())

    async def _media_file(
        self, run: _Run, fetched: FetchResult, kind: MediaKind
    ) -> Envelope[ExtractResult]:
        """Step 7 for an audio or video file: the header checks, then the media steps."""
        if self._media is None:  # pragma: no cover - only called with media on
            raise RuntimeError("no media steps")
        run.content_type = media_type_of(kind)
        started = time.perf_counter()
        run.report = await self._pool.run(self._checker.after_fetch, run.report, fetched, None)
        run.compliance_ms += _ms(started)
        if (code := blocking_code(run.report)) is not None:
            return run.failed(code, _BLOCK_MESSAGES[code])
        outcome = await self._media.media_file(run, fetched, kind)
        if isinstance(outcome, AsrNeeded):
            return self._without_asr(run, outcome)
        return outcome  # failed: nothing to compare or store

    def _without_asr(self, run: _Run, needed: AsrNeeded) -> Envelope[ExtractResult]:
        """The answer while ``needed`` is not transcribed, never stored: with a handoff, its
        stand-in until ``run`` hands it over; without one (or for a render's extraction, which
        hands nothing on) the final answer."""
        if run.asr_handoff is not None and not run.comparing and not run.direct:
            run.asr_needed = needed  # `run` hands it over once the deadline is off
            return without_asr(needed, run)
        message = (
            TRANSCRIPTION_NOT_HANDED_MESSAGE
            if asr_available(self._settings)
            else TRANSCRIPTION_DISABLED_MESSAGE
        )
        return without_asr(needed, run, message=message)

    def _cache_options_of(self, run: _Run) -> dict[str, Any]:
        options = dom_options(run.req.structured, run.req.pii)
        return _cache_options(
            run.req,
            run.report.mode,
            media=run.media is not None,
            structured=structured_cache_part(options, run.learned) if options is not None else None,
            site_links=run.site_links,
        )

    async def _cached(self, run: _Run) -> _Stored | None:
        key = cache_key(run.cache_url, self._cache_options_of(run))
        found = await self._cache.get(key)
        if found is None:
            return None
        entry, fresh = found
        try:
            result = ExtractResult.model_validate(entry.result)
        except ValidationError:
            return None  # another shape: extract again and overwrite it
        return _Stored(key, entry, result, fresh)

    def _serve_hit(self, run: _Run, stored: _Stored) -> Envelope[ExtractResult]:
        run.cache = "hit"
        run.report = stored.result.compliance
        result = stored.result.model_copy(
            update={"url": run.req.url, "attempts": 0, "cache": "hit"}
        )
        # A video's details only keep their media error (M6), as in the fresh answer.
        envelope = cached_details_only(result, self._settings) or self._envelope(result)
        if run.req.pii is not None:  # defence in depth (R12): no DOM-stage value with pii
            envelope = without_dom_stage(envelope)
        return envelope

    async def _fetch_and_extract(
        self, run: _Run, stored: _Stored | None
    ) -> Envelope[ExtractResult]:
        turn = HostTurn(self._gate)
        try:
            run.report = await self._check_origin(run, run.target, turn)
            if (code := blocking_code(run.report)) is not None:
                return run.failed(code, _BLOCK_MESSAGES[code])
            fetched = await self._fetch(run, stored.entry.validators if stored else None, turn)
        finally:
            await turn.give_back()  # `_fetch` gives it back as soon as the fetch ends
        if fetched.not_modified:
            if stored is None:
                return run.failed(
                    ErrorCode.TARGET_HTTP_ERROR,
                    "The target answered 304 Not Modified to an unconditional request.",
                )
            # No body: the page is the stored one, so its own statements still hold.
            run.report = self._checker.after_fetch(
                with_page_findings(run.report, stored.result.compliance), fetched, None
            )
            if (code := blocking_code(run.report)) is not None:
                return run.failed(code, _BLOCK_MESSAGES[code])
            return await self._revalidated(run, stored, fetched)
        if not 200 <= fetched.status < 300:  # also a 3xx the fetcher could not follow
            failed = run.failed(
                ErrorCode.TARGET_HTTP_ERROR,
                f"The target answered with HTTP status {fetched.status}.",
                retryable=fetched.status in _RETRYABLE_STATUSES or fetched.status >= 500,
            )
            if failed.data is not None and self._assesses_refusal(run, fetched):
                failed.data.quality = await self._refusal_quality(run, fetched)
            return failed
        if self._media is not None and _KINDS.get(fetched.content_type or "") != "html":
            media_kind = await self._pool.run(
                sniff_media, fetched.content[:MEDIA_HEAD_BYTES], fetched.content_type
            )
            if media_kind is not None:
                return await self._media_file(run, fetched, media_kind)
        if fetched.truncated:  # a declared audio or video type, read to its head only
            return run.failed(
                ErrorCode.UNSUPPORTED_CONTENT_TYPE,
                f"Content type {fetched.content_type or 'unknown'} is not an audio or video "
                "file that can be read.",
            )
        kind = await self._kind(fetched)
        if kind is None:
            return run.failed(
                ErrorCode.UNSUPPORTED_CONTENT_TYPE,
                f"Content type {fetched.content_type or 'unknown'} is not supported in fast mode.",
            )
        if kind != "html" and kind != "text":
            return await self._document(run, fetched, kind, stored)
        if kind == "html" and _KINDS.get(fetched.content_type or "") != "html":
            run.content_type = "text/html"  # an HTML page declared as a document (F1)
        if run.direct:  # a file the render ended on, but no document after all
            return run.failed(
                ErrorCode.UNSUPPORTED_CONTENT_TYPE,
                f"Content type {fetched.content_type or 'unknown'} is not supported in "
                "browser mode.",
            )

        limit = self._settings.convert_max_bytes
        if len(fetched.content) > limit:
            return run.failed(
                ErrorCode.TOO_LARGE,
                f"The page has {len(fetched.content)} bytes, more than the {limit} bytes "
                "that are converted.",
            )

        started = time.perf_counter()
        text = await self._pool.run(decode_html, fetched.content, fetched.charset)
        run.convert_ms += _ms(started)
        started = time.perf_counter()
        page = text if kind == "html" else None
        run.report = await self._pool.run(self._checker.after_fetch, run.report, fetched, page)
        run.compliance_ms += _ms(started)
        if (code := blocking_code(run.report)) is not None:
            return run.failed(code, _BLOCK_MESSAGES[code])
        if (
            stored is not None
            and 200 <= fetched.status < 300
            and fetched.raw_sha256 == stored.entry.raw_sha256
        ):
            return await self._revalidated(run, stored, fetched)
        return await self._extract_content(run, fetched, kind, text)

    async def _kind(self, fetched: FetchResult) -> DocumentKind | Literal["html", "text"] | None:
        """What the fetched body is (F1). A declared HTML type keeps the page path without a
        look at the bytes; the URL's last path segment breaks the CSV tie, but not for a
        declared ``text/plain`` body: that stays plain text unless it starts with a document's
        signature (the package-2 answer)."""
        kind = _KINDS.get(fetched.content_type or "")
        if kind == "html":
            return "html"
        name = None if kind == "text" else urlsplit(fetched.final_url).path.rsplit("/", 1)[-1]
        return await self._pool.run(sniff, fetched.content, fetched.content_type, name)

    async def _document(
        self, run: _Run, fetched: FetchResult, kind: DocumentKind, stored: _Stored | None
    ) -> Envelope[ExtractResult]:
        """Step 7 for a document (F10): the byte limit, the header checks, the revalidation by
        the bytes' hash, then the conversion (``pipeline_documents``) and step 8."""
        run.content_type = media_type(kind)
        limit = self._settings.document_max_bytes
        if len(fetched.content) > limit:
            return run.failed(
                ErrorCode.TOO_LARGE,
                f"The document has {len(fetched.content)} bytes, more than the {limit} bytes "
                "that are converted.",
            )
        started = time.perf_counter()
        run.report = await self._pool.run(self._checker.after_fetch, run.report, fetched, None)
        run.compliance_ms += _ms(started)
        if (code := blocking_code(run.report)) is not None:
            return run.failed(code, _BLOCK_MESSAGES[code])
        if stored is not None and fetched.raw_sha256 == stored.entry.raw_sha256:
            return await self._revalidated(run, stored, fetched)
        when = run.deadline.when() if run.deadline is not None else None
        if when is None:  # pragma: no cover - the run always has a deadline
            when = asyncio.get_running_loop().time() + self._settings.extract_timeout_seconds
        document = document_input(
            fetched,
            kind,
            run.report,
            run.attempts,
            target=run.target if run.direct else None,
            warnings=run.warnings,
        )
        outcome = await self._documents.extract(run, document, when)
        if isinstance(outcome, OcrNeeded):
            if run.ocr_handoff is not None and not run.comparing:
                run.ocr_needed = outcome  # `run` hands it over once the deadline is off
                return without_ocr(outcome, run)  # stands in until then; never stored
            # While the service has OCR, only a render's extraction (which hands nothing on, or
            # compares with a draft) ends here without it.
            message = (
                OCR_NOT_HANDED_MESSAGE if ocr_available(self._settings) else OCR_DISABLED_MESSAGE
            )
            outcome = without_ocr(outcome, run, message=message)
        # The validators fit the request URL only; a file a render ended on is not revalidated.
        if run.target == run.req.url:
            run.validators = Validators.from_headers(fetched.headers)
        if run.comparing:
            return outcome  # the render step keeps the better of it and the draft, and stores it
        return await self._finish(run, outcome, run.validators)

    def _assesses_refusal(self, run: _Run, fetched: FetchResult) -> bool:
        """Whether a refused answer (403, 429 or 503) is assessed, whatever its content type:
        only in unblock mode, where ``auto`` tries every refusal again in the browser (B5, B14,
        B16)."""
        return (
            fetched.status in REFUSAL_STATUSES
            and run.unblock
            and run.req.mode == "auto"
            and self._settings.browser_enabled
        )

    async def _refusal_quality(self, run: _Run, fetched: FetchResult) -> Quality:
        """The quality facts of a refused answer: of its body when that is HTML within
        ``CONVERT_MAX_BYTES``, else of an empty page. ``challenge_status`` is set when the body is
        a bot challenge."""
        started = time.perf_counter()
        try:
            text = ""
            if (
                _KINDS.get(fetched.content_type or "") == "html"
                and len(fetched.content) <= self._settings.convert_max_bytes
            ):
                text = await self._pool.run(decode_html, fetched.content, fetched.charset)
            quality = await self._pool.run(assess, text, "")
        finally:
            run.convert_ms += _ms(started)
        return quality.model_copy(update={"challenge_status": quality.bot_challenge})

    async def _check_origin(self, run: _Run, url: str, turn: HostTurn) -> ComplianceReport:
        """``before_fetch`` for ``url``. Answered from the cache when it can be; otherwise in the
        turn of ``url``'s host, taken here (or kept, when ``turn`` holds it already), so the
        origin's files are fetched under the host's limits. The wait for the turn counts as
        fetch time, the checks as compliance time."""
        mode = run.report.mode
        started = time.perf_counter()
        report = await self._checker.cached_before_fetch(url, mode)
        if report is None:
            host = urlsplit(url).hostname or ""
            waited = time.perf_counter()
            # The origin's Crawl-delay is not known yet; the request after this turn keeps it.
            await turn.take(host, None)
            run.fetch_ms += _ms(waited)
            started += time.perf_counter() - waited
            report = await self._checker.before_fetch(url, mode)
        run.compliance_ms += _ms(started)
        return report

    async def _fetch(
        self,
        run: _Run,
        validators: Validators | None,
        turn: HostTurn,
        *,
        max_bytes: int | None = None,
        truncate: bool = False,
    ) -> FetchResult:
        """Fetch the target in its host's turn and report the outcome to the gate.

        A redirect target is checked against the compliance files of its origin before it is
        requested (a block ends the chain). A target on another host waits for that host's turn
        and the previous host's turn is given back first; one on the same host keeps the turn.
        Missing files of the target's origin are fetched in the target's turn.
        The outcome is recorded for the host that gave it, judged against the time that host's
        request actually had. ``max_bytes`` and ``truncate`` (``fetch_raw`` only) replace the
        fetcher's byte limit and let it cut a longer body instead of failing.
        """
        checked = {run.target}
        limits: dict[str, Any] = {}
        if max_bytes is not None:
            limits = {"max_bytes": max_bytes, "truncate": truncate}
        started = time.perf_counter()
        hop_checks = 0.0  # seconds of compliance checks inside the fetch
        budget = 0.0  # seconds the request at `turn.answering` has, for `_record_failure`
        chain_budget = 0.0  # the fetcher's deadline for the whole redirect chain ...
        chain_started = started  # ... counted from here

        async def on_redirect(url: str) -> None:
            nonlocal hop_checks, budget
            host = urlsplit(url).hostname or ""
            if not turn.holds(host):
                turn.answering = None  # until the target's turn is taken, no answer is its host's
            if url not in checked:
                checked.add(url)
                check_started = time.perf_counter()
                compliance_before = run.compliance_ms
                fetch_before = run.fetch_ms
                hop = await self._check_origin(run, url, turn)
                # Counted below from hop_checks instead: the fetch time is what remains, and a
                # wait for the target's turn is part of it.
                waited = (run.fetch_ms - fetch_before) / 1000
                hop_checks += time.perf_counter() - check_started - waited
                run.compliance_ms, run.fetch_ms = compliance_before, fetch_before
                run.report = with_redirect_findings(run.report, hop)
                if (code := blocking_code(run.report)) is not None:
                    raise FetchError(code, _BLOCK_MESSAGES[code])
            await turn.take(host, run.report.crawl_delay)
            # The hop starts now. It gets what is left of the fetcher's deadline for the whole
            # chain (hook time included), capped as any fetch is by the request's own deadline.
            budget = min(
                self._fetch_budget(run), chain_budget - (time.perf_counter() - chain_started)
            )

        try:
            await turn.take(urlsplit(run.target).hostname or "", run.report.crawl_delay)
            budget = self._fetch_budget(run)
            if budget < _MIN_FETCH_SECONDS:
                # Not the host's fault: the turn came too late. Nothing is fetched or recorded.
                raise FetchError(
                    ErrorCode.TIMEOUT,
                    "Too little time was left to fetch the target.",
                    retryable=True,
                )
            run.attempts += 1
            chain_budget, chain_started = budget, time.perf_counter()
            try:
                fetched = await self._fetcher.fetch(
                    run.target,
                    validators=validators,
                    timeout_seconds=budget,  # the whole chain's budget; a hop gets what is left
                    on_redirect=on_redirect,
                    **limits,
                )
            except FetchError as error:
                gate_timeout = error.code == ErrorCode.TIMEOUT and error.cleared_after is None
                if turn.answering is not None and not gate_timeout:  # nothing sent: not the host's
                    if error.cleared_after is not None:
                        # The gate's wait before the send is not time the host had.
                        budget = min(budget, chain_budget - error.cleared_after)
                    await self._record_failure(turn.answering, error, budget)
                raise
            # Recorded while the turn is held, so a 429 pauses the host before the next turn.
            if turn.answering is not None:
                await self._gate.record(
                    turn.answering,
                    status=fetched.status,
                    retry_after=parse_retry_after(
                        fetched.headers.get("retry-after"), now=time.time()
                    ),
                )
        finally:
            await turn.give_back()
            run.compliance_ms += round(hop_checks * 1000)
            run.fetch_ms += max(_ms(started) - round(hop_checks * 1000), 0)
        run.final_url = fetched.final_url
        run.upstream_status = fetched.status
        run.content_type = fetched.content_type
        return fetched

    def _fetch_budget(self, run: _Run) -> float:
        """Seconds the fetch may take: ``FETCH_TIMEOUT_SECONDS``, or less when the deadline is
        nearer (minus a margin, so the fetch times out first and the outcome is recorded)."""
        budget = self._settings.fetch_timeout_seconds
        when = run.deadline.when() if run.deadline is not None else None
        if when is not None:
            left = when - asyncio.get_running_loop().time() - _FETCH_MARGIN_SECONDS
            budget = min(budget, left)
        return budget

    async def _record_failure(self, host: str, error: FetchError, budget: float) -> None:
        """Report a failed fetch to the gate (``record_fetch_failure``)."""
        await record_fetch_failure(
            self._gate,
            host,
            error,
            budget=budget,
            fetch_timeout_seconds=self._settings.fetch_timeout_seconds,
        )

    async def _revalidated(
        self, run: _Run, stored: _Stored, fetched: FetchResult
    ) -> Envelope[ExtractResult]:
        """The stored result again, after the site said it has not changed."""
        run.cache = "revalidated"
        fingerprint = stored.result.fingerprint
        if fingerprint is not None:
            fingerprint = fingerprint.model_copy(
                update={"changed": False, "similarity_to_previous": 1.0}
            )
        result = stored.result.model_copy(
            update={
                "url": run.req.url,
                "attempts": run.attempts,
                "compliance": run.report,
                "fingerprint": fingerprint,
                "cache": "revalidated",
            }
        )
        # Not compared again, so renew the baseline the next real extraction compares with.
        await self._fingerprints.touch(
            run.cache_url, variant=_fingerprint_variant(run.req, stored.result)
        )
        fresh = Validators.from_headers(fetched.headers)
        validators = Validators(
            etag=fresh.etag or stored.entry.validators.etag,
            last_modified=fresh.last_modified or stored.entry.validators.last_modified,
        )
        await self._cache.put(
            stored.key,
            CachedEntry(
                stored_at=time.time(),
                validators=validators,
                raw_sha256=stored.entry.raw_sha256,
                result=result.model_dump(mode="json"),
            ),
        )
        return self._envelope(result)

    async def _extract_content(
        self,
        run: _Run,
        fetched: FetchResult,
        kind: Literal["html", "text"],
        text: str,
    ) -> Envelope[ExtractResult]:
        processed = await self._converted(run, fetched, kind, text)
        if isinstance(processed, Envelope):
            return processed
        run.validators = Validators.from_headers(fetched.headers)
        result = run.result(
            language=processed.language,
            word_count=processed.quality.word_count,
            title=processed.title,
            content=processed.content,
            quality=processed.quality,
            metadata=processed.metadata,
            links=processed.links,
            fingerprint=processed.fingerprint,
            warnings=processed.warnings,
            structured=processed.structured,
            site=processed.site,
        )
        envelope = self._envelope(result)
        if self._escalates(run, envelope):
            # B5: the browser's result, or the better of both, is stored instead of this one.
            return envelope
        if kind == "html" and self._media is not None:
            tracked = await self._media.tracks(run, text, result, raw_sha256=fetched.raw_sha256)
            if tracked is not result:
                envelope = self._envelope(tracked)
        return await self._finish(run, envelope, run.validators)

    async def _converted(
        self,
        run: "_Run | _UploadRun",
        fetched: FetchResult,
        kind: Literal["html", "text"],
        text: str,
    ) -> _Processed | Envelope[ExtractResult]:
        """``_process`` on the conversion pool, then the DOM stage of an HTML page
        (``_dom_stage``), or the failed envelope of a page that broke them."""
        started = time.perf_counter()
        learned = run.learned if isinstance(run, _Run) else {}
        site = isinstance(run, _Run) and run.site_links
        try:
            processed = await self._pool.run(
                self._process, run.req, fetched, kind, text, learned, site
            )
            if processed.dom is not None:
                processed = await self._dom_stage(run, processed, processed.dom)
            return processed
        except Exception as error:  # a page that breaks a converter fails alone
            # Traceback without locals (see logging.py); the host only, never the URL.
            _log.error(
                "conversion_failed", host=run.host, error_type=type(error).__name__, exc_info=True
            )
            return run.failed(ErrorCode.CONVERSION_FAILED, "The page could not be converted.")
        finally:
            run.convert_ms += _ms(started)

    async def _dom_stage(
        self, run: "_Run | _UploadRun", processed: _Processed, work: _DomWork
    ) -> _Processed:
        """The DOM stage of ``work``: its selector rules in a selector child, by
        ``STRUCTURED_SELECTOR_TIMEOUT_SECONDS`` and the request's deadline (R19: a run that
        does not finish leaves its fields missing with ``selectors_timed_out``, one whose child
        failed with ``selectors_failed``), then ``dom_stage`` on the conversion pool."""
        warnings = list(processed.warnings)
        compiled = work.options.compiled
        found = {}
        if work.rules and compiled is not None:
            when = run.deadline.when() if run.deadline is not None else None
            outcome = await self._selectors.run(
                work.html, work.final_url, rule_requests(compiled, work.rules), deadline=when
            )
            found = outcome.found
            if outcome.warning is not None:
                warnings.append(outcome.warning)
        stage = partial(
            dom_stage,
            learn=work.options.learn_selectors,
            learn_max_chars=self._settings.llm_max_input_tokens * 4,
            rules=work.rules,
            found=found,
            warnings=warnings,
        )
        structured = await self._pool.run(
            stage, work.html, work.final_url, work.options, work.metadata, work.items
        )
        return replace(processed, structured=structured, warnings=warnings, dom=None)

    def _escalates(self, run: _Run, envelope: Envelope[ExtractResult]) -> bool:
        """Whether this fast result of an ``auto`` request goes on to the browser (B5), also to
        YouTube's transcript view (P13-S1)."""
        return (
            run.req.mode == "auto"
            and self._settings.browser_enabled
            and needs_browser(envelope, unblock=run.unblock)
        ) or self._panel_escalates(run, envelope)

    def _panel_escalates(self, run: _Run, envelope: Envelope[ExtractResult]) -> bool:
        """Whether this fast result of an ``auto`` request goes on to YouTube's transcript view
        in the browser (P13-S1)."""
        return (
            run.req.mode == "auto"
            and self._settings.browser_enabled
            and transcript_panel_applies(envelope, self._settings, transcribe=run.req.transcribe)
        )

    async def _finish(
        self, run: _Run, envelope: Envelope[ExtractResult], validators: Validators
    ) -> Envelope[ExtractResult]:
        """Compare the result's fingerprint with the previous one and cache the result: the last
        step of an answer that is kept. A failed result is not cached, and a request with
        secrets touches neither the cache nor the fingerprints (B6). The cache entry's
        ``raw_sha256`` is the fingerprint's: the fetched bytes, or the rendered HTML."""
        result = envelope.data
        if result is None:
            return envelope
        if result.fingerprint is not None and not run.private:
            fingerprint = await self._fingerprints.compare_and_save(
                run.cache_url, result.fingerprint, variant=_fingerprint_variant(run.req, result)
            )
            result = result.model_copy(update={"fingerprint": fingerprint})
            envelope = envelope.model_copy(update={"data": result})
        # A draft stored without its content (over JOB_ITEM_MAX_BYTES) is not served again, nor
        # a result whose selector rules did not run (a queue wait, a respawned or failed child:
        # transient, as ``learned_selectors_unavailable``; the next request runs them again).
        complete = not _UNCACHED_WARNINGS.intersection(result.warnings)
        if (
            run.cacheable
            and envelope.status != "failed"
            and result.fingerprint is not None
            and complete
        ):
            await self._cache.put(
                cache_key(run.cache_url, self._cache_options_of(run)),
                CachedEntry(
                    stored_at=time.time(),
                    validators=validators,
                    raw_sha256=result.fingerprint.raw_sha256,
                    result=result.model_dump(mode="json"),
                ),
            )
        return envelope

    # --- uploads ---------------------------------------------------------------------------------

    async def _upload_within_deadline(
        self, run: _UploadRun, body: bytes, content_type: str | None
    ) -> Envelope[ExtractResult]:
        deadline = asyncio.timeout(self._timeout_seconds or self._settings.extract_timeout_seconds)
        try:
            async with deadline:
                run.deadline = deadline
                return await self._extract_upload(run, body, content_type, deadline)
        except TimeoutError:
            if not deadline.expired():
                raise
            return run.failed(
                ErrorCode.TIMEOUT, "The extraction did not finish in time.", retryable=True
            )

    async def _extract_upload(
        self, run: _UploadRun, body: bytes, content_type: str | None, deadline: asyncio.Timeout
    ) -> Envelope[ExtractResult]:
        """Step 7 for an upload: its kind by the bytes (F1), then a page's or a document's
        conversion, each with its byte limit. Audio and video come first, by their magic bytes
        alone (the declared type never makes a body media, nor stops one from being it); any
        other body over its limit is ``too_large`` before it is hashed."""
        declared, charset = split_content_type(content_type)
        if self._settings.media_enabled:
            media_kind = await self._pool.run(sniff_media, body[:MEDIA_HEAD_BYTES], declared)
            if media_kind is not None:
                return self._uploaded_media(run, body, media_kind)
        # The sniff reads bounded parts only (the head, a ZIP's central directory, OLE2
        # directory sectors), so a body over its limit is refused with its own type before the
        # whole of it is hashed.
        # A `.md` or `.txt` part sent untyped is sniffed as its text type would be (issue #17):
        # a document signature counts at the start only, and the rest is that text.
        declared = named_text_type(body, declared, run.file_name) or declared
        kind = await self._pool.run(sniff, body, declared, run.file_name)
        run.kind = kind
        if kind is None:
            run.content_type = declared
            return run.failed(
                ErrorCode.UNSUPPORTED_CONTENT_TYPE,
                f"Content type {declared or 'unknown'} is not supported.",
            )
        if kind == "html" or kind == "text":
            run.content_type = _page_type(kind, declared)
            limit = self._settings.convert_max_bytes
            if len(body) > limit:
                return run.failed(
                    ErrorCode.TOO_LARGE,
                    f"The page has {len(body)} bytes, more than the {limit} bytes that are "
                    "converted.",
                )
            raw_sha256 = await self._pool.run(_sha256, body)
            return await self._uploaded_page(run, body, kind, charset, raw_sha256)
        run.content_type = media_type(kind)
        limit = self._settings.document_max_bytes
        if len(body) > limit:
            return run.failed(
                ErrorCode.TOO_LARGE,
                f"The file has more than the {limit} bytes that are converted.",
            )
        raw_sha256 = await self._pool.run(_sha256, body)
        document = DocumentInput(
            body=body,
            meta=DocumentInputMeta(
                kind=kind,
                content_type=media_type(kind),
                file_name=run.file_name,
                charset=charset,
                raw_sha256=raw_sha256,
                compliance=run.report,
            ),
        )
        when = deadline.when()
        if when is None:  # pragma: no cover - the deadline is always set
            when = asyncio.get_running_loop().time() + self._settings.extract_timeout_seconds
        outcome = await self._documents.extract(run, document, when)
        if not isinstance(outcome, OcrNeeded):
            return outcome
        if run.ocr_handoff is not None:
            run.ocr_needed = outcome  # `extract_upload` hands it over once the deadline is off
            return without_ocr(outcome, run)  # its stand-in until then
        if ocr_available(self._settings):
            return without_ocr(outcome, run, message=OCR_NOT_HANDED_REASON)
        return without_ocr(outcome, run)

    def _uploaded_media(
        self, run: _UploadRun, body: bytes, kind: MediaKind
    ) -> Envelope[ExtractResult]:
        """An audio or video upload (M14): refused while the service has no ASR or over
        ``MEDIA_UPLOAD_MAX_BYTES``, otherwise an ``AsrNeeded`` for ``asr_handoff`` (its bytes are
        the route's to store); the answer is its stand-in, or final without a handoff."""
        run.kind = kind
        run.content_type = media_type_of(kind)
        info = MediaInfo(platform="upload", transcript_source=None)
        if not asr_available(self._settings):
            return _failed(
                run.result(media=info),
                ErrorCode.UNSUPPORTED_CONTENT_TYPE,
                TRANSCRIPTION_DISABLED_MESSAGE,
                retryable=False,
            )
        limit = self._settings.media_upload_max_bytes
        if len(body) > limit:
            return _failed(
                run.result(media=info),
                ErrorCode.TOO_LARGE,
                f"The file has more than the {limit} bytes that are transcribed.",
                retryable=False,
            )
        needed = AsrNeeded(
            draft=None,
            input=MediaInputMeta(
                source="upload",
                platform="upload",
                url=None,
                extractor=None,
                kind=kind,
                file_name=run.file_name,
                duration=None,
                languages=[],
                compliance=run.report,
                attempts=0,
            ),
        )
        if run.asr_handoff is not None:
            run.asr_needed = needed  # `extract_upload` hands it over once the deadline is off
            return without_asr(needed, run)
        return without_asr(needed, run, message=TRANSCRIPTION_NOT_HANDED_MESSAGE)

    async def _uploaded_page(
        self,
        run: _UploadRun,
        body: bytes,
        kind: Literal["html", "text"],
        charset: str | None,
        raw_sha256: str,
    ) -> Envelope[ExtractResult]:
        """An HTML or plain-text upload within ``CONVERT_MAX_BYTES``, converted like a fetched
        page without a URL."""
        started = time.perf_counter()
        text = await self._pool.run(decode_html, body, charset)
        run.convert_ms += _ms(started)
        page = FetchResult(
            url="",
            final_url="",  # no base URL: relative links stay relative
            status=200,  # not read by the converters
            headers={},
            content=body,
            content_type=run.content_type,
            charset=charset,
            elapsed_ms=0,
            redirects=[],
            not_modified=False,
            raw_sha256=raw_sha256,
        )
        processed = await self._converted(run, page, kind, text)
        if isinstance(processed, Envelope):
            return processed
        return self._envelope(
            run.result(
                language=processed.language,
                word_count=processed.quality.word_count,
                title=processed.title,
                content=processed.content,
                quality=processed.quality,
                metadata=processed.metadata,
                links=processed.links,
                fingerprint=processed.fingerprint,
                warnings=processed.warnings,
                structured=processed.structured,
            ),
            min_words=1 if run.short_ok else None,
        )

    # --- the render path (browser worker only) -------------------------------------------------

    def _renderer_for(self, run: _Run) -> tuple[Renderer, bool] | None:
        """The renderer for this request and whether it is the unblock one; None when it is not
        rendered at all (``mode: fast``, or a pipeline without a renderer)."""
        if run.req.mode == "fast":
            return None
        if run.unblock and self._unblock_renderer is not None:
            return self._unblock_renderer, True
        if self._renderer is not None:
            return self._renderer, False
        return None

    def _renders_first(self, run: _Run) -> bool:
        """``browser``, a screenshot, browser options (actions, cookies, headers: the fast fetch
        could not use them) and an ``auto`` item with a draft render without a fast fetch;
        ``auto`` otherwise renders only when its fast result needs it."""
        req = run.req
        wants = req.mode == "browser" or req.screenshot or run.private or run.draft is not None
        return wants and self._renderer_for(run) is not None

    async def _rendered(
        self, run: _Run, *, earlier: Envelope[ExtractResult] | None
    ) -> Envelope[ExtractResult]:
        """Render the page; with an ``earlier`` result (the draft or this run's fast result),
        keep the better of both and store only that one. A screenshot is never compared: its
        rendered result carries the artifact."""
        if run.req.screenshot:
            earlier = None
        run.comparing = earlier is not None
        rendered = await self._guarded(run, self._render_and_extract(run))
        if earlier is None:
            return rendered
        winner = better(earlier, rendered)
        if winner.data is not None:
            # Every fetch of the target for this item counts, the draft's too.
            drafted = run.draft.data.attempts if run.draft and run.draft.data else 0
            data = winner.data.model_copy(update={"attempts": run.attempts + drafted})
            winner = winner.model_copy(update={"data": data})
        # This run's fast result keeps its validators; a draft (fetched elsewhere) or a render
        # has none, and a run that took no fast step has none either.
        fast = winner.data is not None and winner.data.mode_used == "fast"
        return await self._finish(run, winner, run.validators if fast else Validators())

    async def _render_and_extract(self, run: _Run) -> Envelope[ExtractResult]:
        """Render the page in its host's turn and extract it (B2):

        1. the request URL's origin is checked, then the host's turn is taken and held;
        2. the renderer loads the page, and asks before each later main-frame navigation;
        3. every document answer and redirect hop is checked when the render returns, before
           any content is used, and its status is reported to the gate; then the turn is given
           back, on every path.
        """
        chosen = self._renderer_for(run)
        if chosen is None:  # pragma: no cover - only called when there is one
            raise RuntimeError("no renderer")
        renderer, unblock = chosen
        req = run.req
        if req.screenshot and run.artifacts is None:
            raise ValueError("a screenshot request needs an artifact sink")
        # What a fast step before learned is in its own result; the render starts afresh.
        run.mode_used, run.unblocked = "browser", unblock
        run.report = ComplianceReport(mode=run.report.mode, decision="skipped")
        run.final_url = run.upstream_status = run.content_type = None
        turn = HostTurn(self._gate)
        checked = {req.url}
        try:
            run.report = await self._check_origin(run, req.url, turn)
            if (code := blocking_code(run.report)) is not None:
                return run.failed(code, _BLOCK_MESSAGES[code])
            options = await self._render_options(run, unblock)
            rendered = await self._render(run, renderer, options, turn, checked)
            refused = await self._check_answers(run, rendered, turn, checked)
            if refused is not None:
                return refused
        finally:
            await turn.give_back()
        return await self._extract_rendered(run, rendered, unblock)

    async def _panel_rendered(
        self, run: _Run, draft: Envelope[ExtractResult]
    ) -> Envelope[ExtractResult]:
        """``draft``, a YouTube video's details whose subtitles could not be read, tried in the
        video's transcript view (P13-S2): its watch page is rendered in the standard browser
        with the transcript view steps (``_render_panel``), and ``MediaSteps.panel`` answers
        from the ``get_panel`` body alone (P13-S5). A transcript is stored under the video's
        key like any; the draft with ``transcript_panel_unavailable`` (no body, one without
        text, a failed render) is not. The render's document answers are added to the draft's
        (P13-S6); its time, the transcript view steps included, is the run's ``render_ms``.
        With the switch off, without a renderer or the media steps, the draft gets the warning
        without a render."""
        fmt = run.req.format
        run.media = media_target(run.req.url)  # filed under the video's key, as its details
        renderer = self._renderer
        if (
            draft.data is None
            or renderer is None
            or self._media is None
            or not self._settings.media_transcript_panel_enabled
        ):
            return panel_result(draft, None, fmt, warnings=())
        url = draft.data.final_url or run.cache_url
        attempts = run.attempts
        rendered = await self._guarded(run, self._render_panel(run, renderer, url))
        if isinstance(rendered, Envelope):  # a block, a failed render, a refused answer
            code = rendered.error.code.value if rendered.error is not None else None
            _log.info("media_transcript_panel_render_failed", error_code=code)
            body: bytes | None = None
            warnings: tuple[str, ...] = (TRANSCRIPT_PANEL_UNAVAILABLE,)
        else:
            body, warnings = rendered.transcript_panel, rendered.warnings
        answer = await self._media.panel(run, draft, body, warnings=warnings)
        answer = _with_render(answer, run, attempts)
        if answer.data is not None and answer.data.mode_used == "browser":
            return await self._finish(run, answer, Validators())  # B10: never revalidated
        return answer

    async def _render_panel(
        self, run: _Run, renderer: Renderer, url: str
    ) -> Rendered | Envelope[ExtractResult]:
        """The watch page at ``url`` (the canonical URL) rendered with ``transcript_panel`` and
        otherwise the standard options: never unblock mode, no caller options (a request with
        them never takes the media path). As any render (B2): the origin's compliance check,
        the host's turn held during the render, every document answer checked afterwards."""
        run.mode_used, run.unblocked = "browser", False
        run.host = urlsplit(url).hostname
        turn = HostTurn(self._gate)
        checked = {url}
        try:
            run.report = await self._check_origin(run, url, turn)
            if (code := blocking_code(run.report)) is not None:
                return run.failed(code, _BLOCK_MESSAGES[code])
            options = replace(await self._render_options(run, False), transcript_panel=True)
            rendered = await self._render(run, renderer, options, turn, checked, url=url)
            refused = await self._check_answers(run, rendered, turn, checked)
            if refused is not None:
                return refused
        finally:
            await turn.give_back()
        return rendered

    async def _render_options(self, run: _Run, unblock: bool) -> RenderOptions:
        """The request's browser options; an unblock render first adds the clearance cookies
        stored for the host (B14), so the caller's own cookies of the same name win."""
        host = run.host or ""
        cookies = caller_cookies(run.req, host)
        if unblock and self._clearance is not None:
            cookies = [*await self._clearance.get(host), *cookies]
        return render_options(
            run.req, cookies, admin=run.admin, unblock=unblock, blocked=self._blocked_resources
        )

    async def _render(
        self,
        run: _Run,
        renderer: Renderer,
        options: RenderOptions,
        turn: HostTurn,
        checked: set[str],
        *,
        url: str | None = None,
    ) -> Rendered:
        """Take the host's turn and render ``url`` (the request URL by default). Each later
        main-frame navigation is checked before it is sent: its URL, its origin's compliance
        files (a block refuses it) and the turn of its host, which moves along with that host's
        own crawl delay. The checks count as render time. A failed render is reported to the
        gate for the host whose turn is held, as a failed fetch is: a status the renderer saw,
        or a timeout or connection error."""
        host = normalize_host(run.host or "")
        crawl_delays = {host: run.report.crawl_delay}  # by host, as the turns are
        await turn.take(host, crawl_delays[host])
        loop = asyncio.get_running_loop()
        when = run.deadline.when() if run.deadline is not None else None
        if when is None:  # pragma: no cover - the run always has a deadline
            when = loop.time() + self._settings.browser_timeout_seconds
        deadline = when - _RENDER_MARGIN_SECONDS
        if deadline - loop.time() < _MIN_FETCH_SECONDS:
            raise FetchError(
                ErrorCode.TIMEOUT, "Too little time was left to render the target.", retryable=True
            )

        async def check(url: str) -> None:
            check_target(url)
            target = urlsplit(url).hostname or ""
            check_address(target, self._settings)
            if url not in checked:
                checked.add(url)
                compliance_ms, fetch_ms = run.compliance_ms, run.fetch_ms
                hop = await self._check_origin(run, url, turn)
                run.compliance_ms, run.fetch_ms = compliance_ms, fetch_ms
                run.report = with_redirect_findings(run.report, hop)
                if (code := blocking_code(run.report)) is not None:
                    raise FetchError(code, _BLOCK_MESSAGES[code])
                crawl_delays[normalize_host(target)] = hop.crawl_delay
            await turn.take(target, crawl_delays.get(normalize_host(target)))

        started = time.perf_counter()
        budget = deadline - loop.time()  # what the render had, for `_record_failure`
        try:
            return await renderer.render(url or run.req.url, check, options, deadline=deadline)
        except FetchError as error:
            # Recorded while the turn is held, so a 429 pauses the host before the next turn.
            if turn.answering is not None:
                await self._record_failure(turn.answering, error, budget)
            raise
        finally:
            run.render_ms += _ms(started)

    async def _check_answers(
        self, run: _Run, rendered: Rendered, turn: HostTurn, checked: set[str]
    ) -> Envelope[ExtractResult] | None:
        """Report each document answer to the gate (but a cleared challenge's, B14), then check
        every URL the render arrived at that was not checked before it was sent (HTTP redirect
        hops, which the browser follows unasked): with the cached verdicts, or with the
        origin's files fetched in its host's turn. A block discards the render: its failed
        envelope is returned."""
        run.attempts += len(rendered.responses)
        for answer in answers_for_gate(rendered, unblock=run.unblocked):
            await self._gate.record(
                urlsplit(answer.url).hostname or "",
                status=answer.status,
                retry_after=parse_retry_after(answer.headers.get("retry-after"), now=time.time()),
            )
        for answer in rendered.responses:
            for url in (*answer.redirects, answer.url):
                if url in checked:
                    continue
                checked.add(url)
                check_target(url)
                check_address(urlsplit(url).hostname or "", self._settings)
                run.report = with_redirect_findings(
                    run.report, await self._check_origin(run, url, turn)
                )
                if (code := blocking_code(run.report)) is not None:
                    return run.failed(code, _BLOCK_MESSAGES[code])
        return None

    async def _extract_rendered(
        self, run: _Run, rendered: Rendered, unblock: bool
    ) -> Envelope[ExtractResult]:
        """Judge the last document answer as fast mode would, then convert the rendered HTML."""
        req = run.req
        run.warnings = list(rendered.warnings)
        file = self._file_of(run, rendered)
        if file is not None:
            return await self._fetched_directly(run, file)
        if not rendered.responses:
            return run.failed(
                ErrorCode.INTERNAL_ERROR, "The browser reported no document.", retryable=True
            )
        last = rendered.responses[-1]
        run.final_url = rendered.final_url
        run.upstream_status = last.status
        run.content_type = last.content_type
        not_cleared = challenge_not_cleared(rendered, unblock=unblock)
        if not_cleared and not 200 <= last.status < 300:
            return run.failed(ErrorCode.BOT_PROTECTION, _QUALITY_MESSAGES[ErrorCode.BOT_PROTECTION])
        if not 200 <= last.status < 300:
            return run.failed(
                ErrorCode.TARGET_HTTP_ERROR,
                f"The target answered with HTTP status {last.status}.",
                retryable=last.status in _RETRYABLE_STATUSES or last.status >= 500,
            )
        if _KINDS.get(last.content_type or "") != "html":  # a download, text, an image ...
            return run.failed(
                ErrorCode.UNSUPPORTED_CONTENT_TYPE,
                f"Content type {last.content_type or 'unknown'} is not supported in browser mode.",
            )
        content = rendered.html.encode("utf-8", "surrogatepass")
        limit = self._settings.convert_max_bytes
        if len(content) > limit:
            return run.failed(
                ErrorCode.TOO_LARGE,
                f"The rendered page has {len(content)} bytes, more than the {limit} bytes "
                "that are converted.",
            )
        fetched = as_fetched(req.url, rendered, content, run.render_ms)
        started = time.perf_counter()
        run.report = await self._pool.run(
            self._checker.after_fetch, run.report, fetched, rendered.html
        )
        run.compliance_ms += _ms(started)
        if (code := blocking_code(run.report)) is not None:
            return run.failed(code, _BLOCK_MESSAGES[code])

        processed = await self._converted(run, fetched, "html", rendered.html)
        if isinstance(processed, Envelope):
            return processed
        quality, fingerprint = processed.quality, processed.fingerprint
        if not_cleared or challenged_by_hosts(quality, rendered):
            quality = quality.model_copy(
                update={"bot_challenge": True, "browser_recommended": True}
            )
            fingerprint = None
        screenshot = await self._store_screenshot(run, rendered) if req.screenshot else None
        if isinstance(screenshot, Envelope):
            return screenshot
        result = run.result(
            language=processed.language,
            word_count=quality.word_count,
            title=processed.title,
            content=processed.content,
            quality=quality,
            metadata=processed.metadata,
            links=processed.links,
            fingerprint=fingerprint,
            warnings=[*processed.warnings, *rendered.warnings],
            screenshot=screenshot,
            structured=processed.structured,
            site=processed.site,
        )
        if self._media is not None and screenshot is None:
            result = await self._media.tracks(
                run, rendered.html, result, raw_sha256=fetched.raw_sha256
            )
        # B7: a stored screenshot is the answer; the page's quality is reported, not judged.
        envelope = Envelope(status="success", data=result) if screenshot else self._envelope(result)
        # B6: with caller secrets the render's cookies may be the caller's own, or a session they
        # opened; they never reach the store the next admin's unblock render reads.
        if (
            unblock
            and self._clearance is not None
            and rendered.cookies
            and not quality.bot_challenge
            and not run.private
        ):
            await self._clearance.put(run.host or "", list(rendered.cookies))
        if run.comparing:
            return envelope
        return await self._finish(run, envelope, Validators())  # B10: never revalidated

    def _file_of(self, run: _Run, rendered: Rendered) -> str | None:
        """The URL of the file a render ended on, to fetch directly (F18): a download the main
        frame started, or a 2xx answer of a document type. None with caller secrets: the
        fetcher must not send the caller's cookies or headers, and without them the answer may
        differ; nor for a screenshot, which belongs to the rendered page."""
        if run.private or run.req.screenshot:
            return None
        if rendered.download_url is not None:
            return rendered.download_url
        last = rendered.responses[-1] if rendered.responses else None
        if (
            last is not None
            and 200 <= last.status < 300
            and (last.content_type or "") in _DOCUMENT_TYPES
            and not challenge_not_cleared(rendered, unblock=run.unblocked)
        ):
            return last.url
        return None

    async def _fetched_directly(self, run: _Run, url: str) -> Envelope[ExtractResult]:
        """Fetch the file a render ended on with the fast fetcher, in a turn of its own and with
        the compliance checks of fast mode, and convert it if it is a document (F18). The
        render's turn is given back already; what it learned is dropped: the result is the
        fast one of the file, with the warning ``document_fetched_directly``. The URL passes
        the SSRF checks of a navigation first."""
        run.mode_used, run.unblocked = "fast", False
        run.report = ComplianceReport(mode=run.report.mode, decision="skipped")
        run.final_url = run.upstream_status = run.content_type = None
        run.warnings = [DOCUMENT_FETCHED_DIRECTLY_WARNING]
        if urlsplit(url).scheme.lower() not in ("http", "https"):  # a blob: or data: download
            return run.failed(
                ErrorCode.UNSUPPORTED_CONTENT_TYPE,
                "The page started a download that cannot be fetched directly.",
            )
        run.target, run.direct = url, True

        async def fetch_and_extract() -> Envelope[ExtractResult]:
            check_target(url)
            check_address(urlsplit(url).hostname or "", self._settings)
            return await self._fetch_and_extract(run, None)

        return await self._guarded(run, fetch_and_extract())

    async def _store_screenshot(
        self, run: _Run, rendered: Rendered
    ) -> ArtifactRef | Envelope[ExtractResult]:
        """Store the render's screenshot (B7): its reference, or the failed envelope."""
        png, size = rendered.screenshot, rendered.screenshot_size
        if png is None or size is None or run.artifacts is None:
            return run.failed(
                ErrorCode.INTERNAL_ERROR, "The browser took no screenshot.", retryable=True
            )
        limit = self._settings.screenshot_max_bytes
        if len(png) > limit:
            return run.failed(
                ErrorCode.TOO_LARGE,
                f"The screenshot has {len(png)} bytes, more than the {limit} bytes that are kept.",
            )
        try:
            return await run.artifacts.put_screenshot(png, *size)
        except ArtifactUnavailable:
            return run.failed(
                ErrorCode.INTERNAL_ERROR,
                "The screenshot could not be stored now. Try again later.",
                retryable=True,
            )

    def _process(
        self,
        req: DocumentRequest,
        fetched: FetchResult,
        kind: Literal["html", "text"],
        text: str,
        learned: Mapping[str, SelectorRule] | None = None,
        site_links: bool = False,
    ) -> _Processed:
        """Convert and judge the decoded page, and prepare the DOM stage of ``structured`` on an
        HTML page (S3) with the ``learned`` selectors (``_DomWork``: ``_dom_stage`` runs it).
        CPU-bound: runs on the conversion pool. The page is parsed once for the metadata and
        the DOM stage. With ``site_links`` the page's typed links become its ``site`` (P9-S10):
        an HTML page's (at most ``SITE_MAX_LINKS_PER_PAGE``, else ``links_truncated``), none
        for plain text."""
        final_url = fetched.final_url
        metadata: PageMetadata | None = None
        links: list[Link] | None = None
        html_lang: str | None = None
        dom_work: _DomWork | None = None
        dom_warnings: list[str] = []
        site: SiteInfo | None = None
        site_warnings: list[str] = []
        if kind == "html":
            conversion = convert_html(text, final_url, req.format)
            host = urlsplit(final_url).hostname or ""
            content = strip_boilerplate(host, conversion.content, self._rules)
            quality = assess(text, content)
            html_lang = _html_lang(text)
            options = dom_options(req.structured, req.pii)  # none with pii (R12)
            dom = options if options is not None and runs_dom_stage(options) else None
            page_metadata: PageMetadata | None = None
            items: StructuredItems | None = None
            if dom is not None and (req.metadata or reads_metadata(dom)):
                page_metadata, items = metadata_with_items(text, final_url, fetched.headers)
            elif req.metadata:  # no DOM stage: the metadata alone, as before (R7)
                page_metadata = extract_metadata(text, final_url, fetched.headers)
            if req.metadata:
                metadata = page_metadata
            if dom is not None:
                if items is None:  # selectors, tables or learning only: the tree is enough
                    items = structured_items(text, final_url, syntaxes=False)
                rules = selector_rules(dom, learned, items, dom_warnings)
                dom_work = _DomWork(text, final_url, dom, page_metadata, items, rules)
            if req.links:
                links = page_links(text, final_url)
            if site_links:
                found = typed_links(
                    text,
                    final_url,
                    fetched.headers,
                    max_links=self._settings.site_max_links_per_page,
                )
                site = SiteInfo(
                    role="page", depth=0, parent=None, links=found.links, nofollow=found.nofollow
                )
                if found.truncated:
                    site_warnings.append(LINKS_TRUNCATED_WARNING)
        else:
            if fetched.content_type in _MARKDOWN_TYPES:
                conversion = Conversion(
                    content=from_markdown(text, req.format),
                    converter="markdown",
                    title=first_heading(text),
                )
                content = conversion.content
            else:
                conversion = convert_plain(text)
                content = conversion.content
                if req.format == "html":
                    content = f"<pre>{html_lib.escape(content)}</pre>" if content else ""
            quality = assess(None, content)
            if req.links:
                links = []
            if site_links:
                site = SiteInfo(role="page", depth=0, parent=None, links=[])
        language = detect_language(html_lang, fetched.headers.get("content-language"), content)
        verdict = quality_error(quality, self._settings.quality_min_words)
        failed = verdict is not None and verdict[0] == "failed"
        return _Processed(
            content=content,
            title=conversion.title,
            warnings=[*conversion.warnings, *dom_warnings, *site_warnings],
            quality=quality,
            language=language,
            metadata=metadata,
            links=links,
            fingerprint=None if failed else compute_fingerprint(fetched.raw_sha256, content),
            dom=dom_work,
            site=site,
        )

    def _envelope(
        self, result: ExtractResult, *, min_words: int | None = None
    ) -> Envelope[ExtractResult]:
        """The envelope for a finished result: the quality verdict decides the status.
        ``min_words`` replaces ``QUALITY_MIN_WORDS`` (an upload with ``short_ok``)."""
        min_words = self._settings.quality_min_words if min_words is None else min_words
        verdict = quality_error(result.quality, min_words) if result.quality else None
        if verdict is None:
            return Envelope(status="success", data=result)
        status, code = verdict
        message = _QUALITY_MESSAGES.get(
            code,
            f"The extracted text has {result.word_count} words, fewer than {min_words}.",
        )
        return Envelope(status=status, data=result, error=ErrorInfo(code=code, message=message))

    def _observe(self, run: _Run, outcome: str) -> None:
        """Metrics and the one log line of the request. No URL: host and path hash only."""
        self._record(
            run.req.mode,
            run.started,
            outcome,
            host=run.host,
            path_hash=_path_hash(run.req.url) if run.host else None,
            cache=run.cache,
            upstream_status=run.upstream_status,
        )

    def _record(self, mode: str, started: float, outcome: str, **fields: Any) -> None:
        """Count an extraction under ``mode`` (the requested one, or ``upload``) and log its
        line with ``fields``."""
        seconds = time.perf_counter() - started
        self._metrics.total.labels(mode=mode, outcome=outcome).inc()
        self._metrics.duration.labels(mode=mode).observe(seconds)
        _log.info(
            "extract_done",
            mode=mode,
            outcome=outcome,
            **fields,
            total_ms=round(seconds * 1000),
        )


def create_outbound(settings: Settings, valkey: Redis) -> tuple[DomainGate, Fetcher]:
    """The per-host gate in ``valkey`` and a fetcher that announces every request it sends
    (pages, redirect hops, compliance files) to that gate and reports its send, so each request
    keeps the spacing to the other turns' real starts. Pass both to ``create_pipeline``;
    ``aclose`` the fetcher last."""
    gate = DomainGate(valkey, settings)
    return gate, Fetcher(
        settings, before_request=gate.before_request, on_request_sent=gate.mark_sent
    )


def create_pipeline(
    settings: Settings,
    valkey: Redis,
    fetcher: Fetcher,
    registry: CollectorRegistry,
    pool: ConversionPool | None = None,
    *,
    gate: DomainGate | None = None,
    memory_guard: MemoryPressure | None = None,
    renderer: Renderer | None = None,
    unblock_renderer: Renderer | None = None,
    clearance: ClearanceStore | None = None,
    timeout_seconds: float | None = None,
    document_pool: DocumentPool | None = None,
    media_fetcher: MediaFetcher | None = None,
    selector_store: SelectorStore | None = None,
    selector_runner: SelectorRunner | None = None,
) -> Pipeline:
    """A pipeline whose gate, compliance checks, cache, fingerprints and learned selectors all
    live in ``valkey``.

    ``gate`` is the one ``fetcher`` reports its sends to (``create_outbound`` makes the pair);
    without it a new gate is made, which then counts its spacing from the turn starts only.
    With ``memory_guard`` the result cache skips its writes while Valkey's memory is high.
    The browser options, ``document_pool``, ``media_fetcher`` and ``selector_runner`` go to
    ``Pipeline`` unchanged.
    Without ``selector_store`` one is made on ``valkey`` (``SELECTOR_LEARN_TTL_SECONDS``)."""
    if gate is None:
        gate = DomainGate(valkey, settings)
    if selector_store is None:
        selector_store = SelectorStore(valkey, settings.selector_learn_ttl_seconds)
    return Pipeline(
        fetcher,
        gate,
        ComplianceChecker(fetcher, valkey, settings),
        ResultCache(valkey, settings, guard=memory_guard),
        FingerprintStore(valkey, settings),
        settings,
        registry,
        pool,
        renderer=renderer,
        unblock_renderer=unblock_renderer,
        clearance=clearance,
        timeout_seconds=timeout_seconds,
        document_pool=document_pool,
        media_fetcher=media_fetcher,
        selector_store=selector_store,
        selector_runner=selector_runner,
    )


# --- helpers -------------------------------------------------------------------------------------


def _cache_options(
    req: ExtractRequest,
    mode: ComplianceMode,
    *,
    media: bool = False,
    structured: Mapping[str, Any] | None = None,
    site_links: bool = False,
) -> dict[str, Any]:
    """Every request option that changes the result; the cache key covers them.

    ``mode`` keeps the results of each mode apart: ``browser`` renders, and ``auto`` keeps a
    rendered result when the fast one needed the browser. ``ocr`` matters for documents only,
    but which URL is one is known only after the fetch; it is left out while it is the default
    (``auto``), so the keys of pages and of default requests stay those of the release before
    it and a deploy keeps the cache. The browser options that are not here (actions, cookies,
    headers, a screenshot) keep a request out of the cache altogether.

    A platform video's result (``media``) is filed under its canonical URL with ``media: true``,
    apart from a page result of the same URL. The requested ``languages`` choose a video's
    subtitle track and the tracks of a page's players, so they are part of every key, but only
    when set: the keys of requests without them stay as they were.

    ``structured`` is the DOM stage's part (``structured.dom.structured_cache_part``: its
    strategy, schema, selectors, tables and learned selectors, S4), again only when there is
    one: a request without ``structured``, or whose options do not touch the DOM (``patterns``,
    ``llm``), keeps the key of a request without it; so does a request with ``pii``, which has no
    DOM stage (``dom.dom_options``, R12). ``chunking`` and the post stage's options never change
    the cached result, so they are never part of the key.

    ``site_links`` (a site crawl's page, P9-S10) adds the page's typed links to the result; it
    is part of the key only when set, so the keys of other requests stay as they were.
    """
    options: dict[str, Any] = {
        "mode": req.mode,
        "format": req.format,
        "metadata": req.metadata,
        "links": req.links,
        "compliance": mode,
    }
    if req.ocr != "auto":
        options["ocr"] = req.ocr
    if media:
        options["media"] = True
    if req.languages:
        options["languages"] = list(req.languages)
    if structured is not None:
        options["structured"] = dict(structured)
    if site_links:
        options["site_links"] = True
    return options


def _cacheable(req: ExtractRequest, private: bool) -> bool:
    """Whether a request may read and write the result cache: not with secrets, a screenshot,
    or ``learn_selectors``, which needs the page itself for its ``learn_html`` (ruling R6)."""
    learning = req.structured is not None and req.structured.learn_selectors
    return req.cache and not private and not req.screenshot and not learning


def _with_render(
    answer: Envelope[ExtractResult], run: _Run, attempts: int
) -> Envelope[ExtractResult]:
    """``answer`` (built from a draft) with the document answers ``run`` counted since it had
    ``attempts`` added to the draft's (P13-S6). The times are the run's anyway (``run``)."""
    data = answer.data
    if data is None:  # pragma: no cover - built from a draft with data
        return answer
    counted = data.attempts + run.attempts - attempts
    return answer.model_copy(update={"data": data.model_copy(update={"attempts": counted})})


def _fingerprint_variant(req: ExtractRequest, result: ExtractResult) -> str:
    """Which previous fingerprint a result is compared with: the one of the same output format,
    and for a document also of the same ``ocr`` option. ``auto``, ``never`` and ``always`` may
    read the same file with different converters (MarkItDown, Docling), so alternating them must
    not report a change; the result cache keeps them apart the same way (``_cache_options``).
    Pages keep the variant of package 4 (the format), so their stored fingerprints stay valid,
    also with HTML5 tracks; a platform video or a media file has ``<format>|media``. A media
    result (a page's players included) of a request with ``languages`` adds them: they choose
    the track, so switching them must not report a change either."""
    if result.document is not None:
        return f"{req.format}|ocr={req.ocr}"
    variant: str = req.format
    if result.media is not None:
        if result.media.platform != "html5":
            variant += "|media"
        if req.languages:
            variant += f"|languages={','.join(req.languages)}"
    return variant


def _metadata_only(result: ExtractResult) -> bool:
    """Whether a stored result is a platform video's details without a transcript that a
    transcription may still give: not when the speech recognition heard no speech or failed for
    good (``pipeline_media.transcription_final``)."""
    return (
        result.media is not None
        and result.media.transcript_source == "metadata_only"
        and not transcription_final(result)
    )


def _html_lang(html: str) -> str | None:
    """The ``lang`` attribute of the first ``<html>`` tag near the start of the page."""
    head = html[:_LANG_SCAN_CHARS]
    lowered = head.lower()
    start = lowered.find("<html")
    while start >= 0:
        after = lowered[start + 5 : start + 6]
        if after in ("", ">", "/") or after.isspace():
            end = head.find(">", start, start + _MAX_TAG_CHARS)
            tag = head[start + 5 : end if end >= 0 else start + _MAX_TAG_CHARS]
            match = _LANG_ATTRIBUTE.search(tag)
            return match.group(1) if match else None
        start = lowered.find("<html", start + 5)
    return None


def _path_hash(url: str) -> str:
    """First 12 hex characters of the SHA-256 of path and query: lets logs group requests
    without showing the URL, whose query can carry secrets."""
    parts = urlsplit(url)
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    return hashlib.sha256(target.encode()).hexdigest()[:12]


def _page_type(kind: Literal["html", "text"], declared: str | None) -> str:
    """The media type a page's result reports: a declared HTML or Markdown type as it is, else
    the type the bytes were read as."""
    if kind == "text":
        return declared if declared in _MARKDOWN_TYPES else "text/plain"
    if declared is not None and _KINDS.get(declared) == "html":
        return declared
    return "text/html"


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _outcome(envelope: Envelope[ExtractResult]) -> str:
    """Metric label: ``success``, ``partial`` or the error code of a failure."""
    if envelope.status == "failed" and envelope.error is not None:
        return envelope.error.code.value
    return envelope.status


def _seconds(value: float) -> int:
    return max(math.ceil(value), 1)
