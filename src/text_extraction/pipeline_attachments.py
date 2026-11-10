"""Step 7b of the pipeline: the attachments of a thin page (package 22).

A worksheet overview or a lesson plan often says little itself and carries its material as a
download. When a fetched or rendered HTML page has fewer than ``ATTACHMENTS_BELOW_WORDS`` words,
``AttachmentSteps.append_attachments`` follows at most ``ATTACHMENTS_MAX`` of its
``attachment_candidates`` (content links first, then Drupal file entities) and appends the text of
each one that converts, under a heading with the attachment's label: ``## label`` in Markdown, a
line of its own in text, an ``<h2>`` in HTML. This happens before the page's quality verdict
(step 8): ``word_count``, ``quality.word_count`` and the fingerprint are computed again from the
joined content. A page that is a bot challenge, a soft 404 or a login wall follows nothing, nor
does a page of a site crawl (``site_links``: its file links are listed, never fetched).

``follow_attachments`` is the part that fetches and converts; it also follows a file named
elsewhere (an edu-sharing node), whatever the page's words. Each candidate is fetched as the page
is (the pipeline's ``fetch_raw`` steps): its URL validated, its origin's compliance files checked
under the request's mode (a block ends it before its request), the fetch in the host's
``DomainGate`` turn with every redirect hop checked and a hop to another host in that host's turn,
the fetcher's SSRF guard on every connection, ``DOCUMENT_MAX_BYTES`` as the byte limit and the
header checks after the fetch (``X-Robots-Tag``, ``tdm-reservation``). The body is sniffed; only
kinds MarkItDown converts are converted (not images or OpenDocument files, which need Docling),
in the pipeline's document children, without an OCR hand-off: a document that needs OCR gives
its text layer. A candidate whose file extension or declared type names a kind that is not
converted (audio, video, archives, OpenDocument, images) is passed over without a request and
does not count against the limit; one whose declared size is over ``DOCUMENT_MAX_BYTES`` is not
fetched, and fails.

The attachments share the request's deadline and stop ``_RESERVE_SECONDS`` before it, so the
page keeps its answer. Each attachment whose text was appended adds the warning
``attachment_read``; each one tried that gave none (blocked, refused, over a limit, not a document
that converts, no words, out of time) adds ``attachment_failed``. The result stays the page's:
its status code, final URL, content type and compliance report. The result cache key does not
change: the two settings belong to the deployment.
"""

import asyncio
import html as html_lib
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath
from typing import Final, Literal, Protocol, get_args
from urllib.parse import urlsplit

import structlog

from text_extraction.compliance import ComplianceReport
from text_extraction.conversion_pool import ConversionPool
from text_extraction.convert.attachments import Attachment, attachment_candidates
from text_extraction.convert.links import DOWNLOAD_EXTS
from text_extraction.convert.markup import append_to_body
from text_extraction.documents.routing import DOCLING_KINDS
from text_extraction.documents.sniff import media_type
from text_extraction.documents.types import DocumentKind
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractResult
from text_extraction.fetch import FetchResult
from text_extraction.fingerprint import compute_fingerprint
from text_extraction.pipeline_documents import DocumentRun, DocumentSteps, OcrNeeded, document_input
from text_extraction.quality import count_words, quality_error
from text_extraction.settings import Settings

_log = structlog.get_logger(__name__)

ATTACHMENT_READ_WARNING: Final = "attachment_read"
ATTACHMENT_FAILED_WARNING: Final = "attachment_failed"
# The attachments stop this long before the request's deadline: the page's own last steps
# (fingerprint, cache, a player's tracks) still need their time.
_RESERVE_SECONDS: Final = 1.0
# The document kinds MarkItDown converts, and their media types.
_CONVERTED_KINDS: Final = frozenset(get_args(DocumentKind)) - DOCLING_KINDS
_CONVERTED_TYPES: Final = frozenset(media_type(kind) for kind in _CONVERTED_KINDS)
_MEDIA_FAMILIES: Final = ("image/", "audio/", "video/")
_BODY: Final = re.compile(r"<body[^>]*>(.*)</body\s*>", re.IGNORECASE | re.DOTALL)

type Sniffed = DocumentKind | Literal["html", "text"] | None


class AttachmentRun(DocumentRun, Protocol):
    """The part of the pipeline's run state the attachment steps use: the request's deadline,
    its compliance report and attempts (an attachment's run has its own), the time spent, and
    whether the page is one of a site crawl's (``site_links``), which fetches no files."""

    deadline: asyncio.Timeout | None
    site_links: bool
    report: ComplianceReport
    attempts: int
    fetch_ms: int
    compliance_ms: int


class AttachmentSteps[R: AttachmentRun]:
    """Follows the attachments of the pages of one pipeline. ``sub_run`` makes the run of one
    attachment of a page's run (the URL to fetch, the deadline the attachments may use),
    ``fetch`` fetches it as the page was fetched (a ``FetchResult`` of any status, or the failed
    envelope of a block, a refusal or a fetch error), ``sniff`` names the body's kind."""

    def __init__(
        self,
        settings: Settings,
        threads: ConversionPool,
        documents: DocumentSteps,
        *,
        sub_run: Callable[[R, str, asyncio.Timeout], R],
        fetch: Callable[[R], Awaitable[FetchResult | Envelope[ExtractResult]]],
        sniff: Callable[[FetchResult], Awaitable[Sniffed]],
    ) -> None:
        self._settings = settings
        self._threads = threads
        self._documents = documents
        self._sub_run = sub_run
        self._fetch = fetch
        self._sniff = sniff

    async def append_attachments(
        self, run: R, result: ExtractResult, html: str, *, raw_sha256: str
    ) -> ExtractResult:
        """``result`` of the HTML page ``html``, with the text of its attachments appended when
        it is thin (see the module docstring). ``raw_sha256`` is the hash of the page's bytes,
        for its new fingerprint."""
        base = result.final_url or result.url
        # An upload has no URL to resolve links against; a site crawl lists files, never
        # fetches them.
        if base is None or run.site_links or not self._applies(result):
            return result
        try:  # an extra: a bug in finding the candidates keeps the page as it was
            candidates = await self._threads.run(attachment_candidates, html, base)
        except Exception as error:
            _log.warning(
                "attachment_candidates_failed", host=_host_of(base), error_type=type(error).__name__
            )
            return result
        followed, warnings = await self.follow_attachments(
            run, candidates, self._settings.attachments_max
        )
        if not warnings:
            return result
        if not followed:
            return result.model_copy(update={"warnings": [*result.warnings, *warnings]})
        content = result.content or ""
        for attachment, converted in followed:
            content = _joined(content, attachment.label, converted.content or "", result.format)
        started = time.perf_counter()
        try:
            return await self._threads.run(self._rejudged, result, content, raw_sha256, warnings)
        finally:
            run.convert_ms += _ms(started)

    async def follow_attachments(
        self, run: R, candidates: list[Attachment], limit: int
    ) -> tuple[list[tuple[Attachment, ExtractResult]], list[str]]:
        """Fetch and convert up to ``limit`` of ``candidates``, in order: the ones whose text
        came out (each with its document result), and one warning per candidate tried,
        ``attachment_read`` or ``attachment_failed``, in the same order."""
        followed: list[tuple[Attachment, ExtractResult]] = []
        warnings: list[str] = []
        wanted = [candidate for candidate in candidates if _worth_fetching(candidate)][:limit]
        if not wanted:
            return followed, warnings
        loop = asyncio.get_running_loop()
        when = run.deadline.when() if run.deadline is not None else None
        if when is None:  # pragma: no cover - the run always has a deadline
            when = loop.time() + self._settings.extract_timeout_seconds
        cutoff_at = when - _RESERVE_SECONDS
        cutoff = asyncio.timeout_at(cutoff_at)
        try:
            async with cutoff:
                for candidate in wanted:
                    warnings.append(ATTACHMENT_FAILED_WARNING)  # until its text is in
                    converted = await self._follow(run, candidate, cutoff, cutoff_at)
                    if converted is not None:
                        followed.append((candidate, converted))
                        warnings[-1] = ATTACHMENT_READ_WARNING
        except TimeoutError:
            if not cutoff.expired():
                raise
            _log.info("attachment_failed", host=_host(wanted[len(warnings) - 1]), reason="timeout")
        return followed, warnings

    def _applies(self, result: ExtractResult) -> bool:
        """Whether ``result`` is a thin page whose attachments are followed."""
        quality = result.quality
        return (
            self._settings.attachments_max > 0
            and quality is not None
            and result.content is not None
            and quality.word_count < self._settings.attachments_below_words
            and not (
                quality.bot_challenge
                or quality.soft_404
                or quality.login_wall
                or quality.challenge_status
            )
        )

    async def _follow(
        self, run: R, candidate: Attachment, cutoff: asyncio.Timeout, cutoff_at: float
    ) -> ExtractResult | None:
        """The document result of one candidate whose text came out, or None (logged with
        the host and the reason, never the URL). Its run's times count as ``run``'s."""
        if candidate.size is not None and candidate.size > self._settings.document_max_bytes:
            outcome: ExtractResult | str = "too_large"
        else:
            sub = self._sub_run(run, candidate.url, cutoff)
            try:
                outcome = await self._converted(sub, candidate, cutoff_at)
            finally:
                run.fetch_ms += sub.fetch_ms
                run.compliance_ms += sub.compliance_ms
                run.convert_ms += sub.convert_ms
        if isinstance(outcome, str):
            _log.info("attachment_failed", host=_host(candidate), reason=outcome)
            return None
        return outcome

    async def _converted(
        self, sub: R, candidate: Attachment, cutoff_at: float
    ) -> ExtractResult | str:
        """Fetch, sniff and convert the attachment of ``sub``: its document result, or why it
        gave no text (an error code, a status or the kind)."""
        fetched = await self._fetch(sub)
        if isinstance(fetched, Envelope):
            return fetched.error.code.value if fetched.error is not None else "failed"
        if not 200 <= fetched.status < 300:
            return f"status_{fetched.status}"
        if len(fetched.content) > self._settings.document_max_bytes:  # the fetcher stops there
            return "too_large"
        kind = await self._sniff(fetched)
        if kind is None or kind == "html" or kind == "text" or kind in DOCLING_KINDS:
            return f"kind_{kind}"
        document = document_input(fetched, kind, sub.report, sub.attempts, target=candidate.url)
        outcome = await self._documents.extract(sub, document, cutoff_at)
        # No OCR hand-off for an attachment: its text layer, as MarkItDown read it.
        envelope = outcome.draft if isinstance(outcome, OcrNeeded) else outcome
        if envelope is None or envelope.data is None or envelope.status == "failed":
            error = envelope.error if envelope is not None else None
            return error.code.value if error is not None else "failed"
        if not envelope.data.word_count or not envelope.data.content:
            return "empty_content"
        return envelope.data

    def _rejudged(
        self, result: ExtractResult, content: str, raw_sha256: str, warnings: list[str]
    ) -> ExtractResult:
        """``result`` with the joined ``content``: its word count, and its fingerprint unless
        the verdict fails. CPU-bound: runs on the conversion pool."""
        quality = result.quality
        if quality is None:  # pragma: no cover - ``_applies`` checked it
            raise ValueError("a result without quality has no attachments")
        quality = quality.model_copy(update={"word_count": count_words(content)})
        verdict = quality_error(quality, self._settings.quality_min_words)
        failed = verdict is not None and verdict[0] == "failed"
        return result.model_copy(
            update={
                "content": content,
                "word_count": quality.word_count,
                "quality": quality,
                "fingerprint": None if failed else compute_fingerprint(raw_sha256, content),
                "warnings": [*result.warnings, *warnings],
            }
        )


def _worth_fetching(candidate: Attachment) -> bool:
    """False for a candidate whose file extension or declared type names a kind that is not
    converted: audio, video, an archive, an OpenDocument file, an image."""
    suffix = PurePosixPath(urlsplit(candidate.url).path).suffix.lower()
    if suffix in DOWNLOAD_EXTS and suffix[1:] not in _CONVERTED_KINDS:
        return False
    declared = (candidate.mime or "").split(";", 1)[0].strip().lower()
    return not (declared.startswith(_MEDIA_FAMILIES) and declared not in _CONVERTED_TYPES)


def _joined(content: str, label: str, text: str, fmt: str) -> str:
    """``text`` appended to ``content`` under a heading with ``label``, in the output format."""
    label = " ".join(label.split())
    if fmt == "html":
        found = _BODY.search(text)
        block = f"<h2>{html_lib.escape(label)}</h2>{found.group(1) if found else text}"
        return append_to_body(content, block) if content else block
    heading = f"## {label}" if fmt == "markdown" else label
    return "\n\n".join(part for part in (content.rstrip(), heading, text.strip()) if part)


def _host_of(url: str) -> str:
    return urlsplit(url).hostname or ""


def _host(candidate: Attachment) -> str:
    return _host_of(candidate.url)


def _ms(since: float) -> int:
    return round((time.perf_counter() - since) * 1000)
