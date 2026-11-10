"""Step 4b of the pipeline: an edu-sharing render page read through the REST API (package 22).

A render URL of an edu-sharing repository (``edusharing.render_node``) serves an Angular shell
without text. Before its page is fetched, a request that wants no page as a browser shows it
(not ``browser``, a screenshot, browser options or an ``auto`` item with a draft; the pipeline
decides) and is not a site crawl's page asks the repository for the node's metadata instead
(``EduSharingSteps.node_result``):

1. The request URL gets step 4 as the page would (its origin's compliance files under the
   request's mode, in the host's turn when they are not cached). A block, or a refusal of the
   outbound gate, leaves the request to today's path, which answers it.
2. The metadata URL (``edusharing.metadata_url``, same origin) is fetched as an attachment is
   (``_attachment_run``, the pipeline's ``fetch_raw`` steps: its URL validated, its own
   compliance check, the host's ``DomainGate`` turn, each redirect hop checked, the SSRF guard),
   with ``Accept: application/json``, within ``CONVERT_MAX_BYTES`` and half of the time left.
   Any other outcome than a 2xx JSON answer with a ``node`` object (a block, 401, 404, 5xx,
   HTML, broken JSON, a timeout) leaves the request to today's path, without an error or a
   warning of its own; it is logged as ``edusharing_fallback`` with the host and the reason.
3. ``edusharing.node_page`` writes the node's page (title, description, keywords, licence, web
   link, file), which is converted and judged as an HTML page (step 7, 8). The result is the
   metadata answer's: its final URL, status and content type (``application/json``), with the
   compliance findings of both URLs. Since it is no HTML answer, ``escalation.needs_browser``
   never sends it to the browser (B5), not even in unblock mode. A JSON answer of the API is no
   bot challenge, soft 404 or login wall, whatever the node's words: those findings of the page
   are cleared before it is judged.
4. The node's file is followed as an attachment (``AttachmentSteps.follow_attachments``,
   whatever the page's words: the file is the material) when its URL is on the render URL's host
   and ``ATTACHMENTS_MAX`` is not 0; a file on another host, or with attachments switched off,
   is listed in the page, never fetched. A kind that is not converted (an archive, for example)
   is listed only. Its text is appended under the file's name as for step 7b, with
   ``attachment_read`` or ``attachment_failed``; a failure that may pass leaves the result
   uncached (``attachments_retry``).
5. An audio or video file (by its declared ``mimetype``) is no attachment: it is listed in
   ``media.embeds`` as well, and on the render URL's host a request with ``transcribe: true``
   hands it to the media steps as a thin page's one media (the pipeline's step 8b, ruling M22),
   whatever ``ATTACHMENTS_MAX`` says: the node's page is the stand-in, and its text comes
   before the transcript.

The answer is cached and fingerprinted as a page's is (``_finish``), without validators: a stale
entry is read through the API again. ``mode_used`` stays ``fast``.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Final, Protocol
from urllib.parse import urlsplit

import structlog

from text_extraction.compliance import ComplianceReport, compliance_block, with_redirect_findings
from text_extraction.conversion_pool import ConversionPool
from text_extraction.convert.attachments import Attachment
from text_extraction.edusharing import metadata_url, node_page, parse_metadata, render_node
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractResult
from text_extraction.fetch import FetchResult
from text_extraction.media.sniff import is_declared_media
from text_extraction.media.types import MediaEmbed, MediaInfo
from text_extraction.pipeline_attachments import AttachmentRun, AttachmentSteps
from text_extraction.settings import Settings

_log = structlog.get_logger(__name__)

# The metadata request gets at most this share of the time left before the request's deadline:
# a repository that hangs still leaves today's path its time.
_TIME_SHARE: Final = 0.5
_JSON_TYPE: Final = "application/json"


class NodeRun(AttachmentRun, Protocol):
    """The part of the pipeline's run state the edu-sharing step uses besides the attachment
    steps': the URL the request fetches, and what the answer reports of its fetch."""

    target: str
    final_url: str | None
    upstream_status: int | None
    content_type: str | None


class EduSharingSteps[R: NodeRun]:
    """Answers the render URLs of edu-sharing repositories from the node's metadata and file.

    ``sub_run`` makes the run of one extra request of a run (the metadata URL, its deadline),
    ``check`` runs step 4 for the run's own URL (a report, or the failed envelope of a refusal of
    the gate), ``fetch`` fetches a sub-run's URL as JSON within ``CONVERT_MAX_BYTES`` (a
    ``FetchResult`` of any status, or the failed envelope of a block, a refusal or a fetch error),
    and ``page`` converts a node's page as a fetched HTML page (its result, or the failed
    envelope of a page that broke the converter), and ``media`` takes an audio or video file on the
    render URL's host to the media steps (the pipeline's step 8b: its subtitles or transcription,
    or None when the request does not transcribe it)."""

    def __init__(
        self,
        settings: Settings,
        threads: ConversionPool,
        attachments: AttachmentSteps[R],
        *,
        sub_run: Callable[[R, str, asyncio.Timeout], R],
        check: Callable[[R], Awaitable[ComplianceReport | Envelope[ExtractResult]]],
        fetch: Callable[[R], Awaitable[FetchResult | Envelope[ExtractResult]]],
        page: Callable[[R, FetchResult], Awaitable[ExtractResult | Envelope[ExtractResult]]],
        media: Callable[[R, ExtractResult, MediaEmbed], Awaitable[ExtractResult | None]],
    ) -> None:
        self._settings = settings
        self._threads = threads
        self._attachments = attachments
        self._sub_run = sub_run
        self._check = check
        self._fetch = fetch
        self._page = page
        self._media = media

    async def node_result(self, run: R) -> ExtractResult | None:
        """The result of ``run``'s render URL from its node's metadata and file, not yet judged
        or stored; None when the URL is no render URL or the API gave no node (today's path)."""
        found = render_node(run.target)
        if found is None or run.site_links:
            return None
        host = urlsplit(run.target).hostname or ""
        checked = await self._check(run)
        if isinstance(checked, Envelope):  # a refusal of the gate
            _fallback(host, checked.error.code.value if checked.error else "failed")
            return None
        if compliance_block(checked) is not None:
            _fallback(host, "blocked")
            return None
        run.report = checked
        fetched = await self._metadata(run, metadata_url(*found))
        if fetched is None:
            return None
        try:  # an extra: a bug in reading the node keeps today's path
            built = await self._threads.run(_built_page, fetched.content, run.target)
        except Exception as error:
            _log.warning("edusharing_node_failed", host=host, error_type=type(error).__name__)
            built = None
        if built is None:
            _fallback(host, "not_a_node")
            return None
        html, attachment = built
        page = FetchResult(
            url=run.target,
            final_url=run.target,  # the page's links are resolved against the render URL
            status=200,
            headers={},
            content=html.encode(),
            content_type="text/html",
            charset="utf-8",
            elapsed_ms=fetched.elapsed_ms,
            redirects=[],
            not_modified=False,
            raw_sha256=fetched.raw_sha256,  # the metadata's bytes
        )
        # The answer reports the fetch it came from: the metadata's URL, status and type.
        run.final_url = fetched.final_url
        run.upstream_status = fetched.status
        run.content_type = fetched.content_type
        result = await self._page(run, page)
        if isinstance(result, Envelope):  # a converter bug: today's path, which may do better
            run.final_url = run.upstream_status = run.content_type = None
            _fallback(host, "conversion_failed")
            return None
        if attachment is None:
            return result
        if is_declared_media(attachment.mime):
            # Listed as the node's media (D4 a); on the render URL's host, a ``transcribe``
            # request hands it on as a page's one media (M22, R3), whatever ATTACHMENTS_MAX says.
            embed = MediaEmbed(url=attachment.url, kind="file")
            info = MediaInfo(platform="html5", transcript_source=None, embeds=[embed])
            result = result.model_copy(update={"media": info})
            if _same_host(attachment, run.target):
                return await self._media(run, result, embed) or result
            return result
        if not _same_host(attachment, run.target) or self._settings.attachments_max == 0:
            return result  # listed in the page, never fetched
        followed, warnings = await self._attachments.follow_attachments(run, [attachment], 1)
        return await self._attachments.appended(
            run, result, followed, warnings, raw_sha256=fetched.raw_sha256
        )

    async def _metadata(self, run: R, url: str) -> FetchResult | None:
        """The metadata answer at ``url`` when it is a 2xx JSON answer, its compliance findings
        added to ``run``'s report; else None (logged)."""
        host = urlsplit(url).hostname or ""
        loop = asyncio.get_running_loop()
        when = run.deadline.when() if run.deadline is not None else None
        if when is None:  # pragma: no cover - the run always has a deadline
            when = loop.time() + self._settings.extract_timeout_seconds
        now = loop.time()
        cutoff = asyncio.timeout_at(now + (when - now) * _TIME_SHARE)
        sub = self._sub_run(run, url, cutoff)
        outcome: FetchResult | Envelope[ExtractResult] | None = None
        try:
            async with cutoff:
                outcome = await self._fetch(sub)
        except TimeoutError:
            if not cutoff.expired():
                raise
        finally:
            run.attempts += sub.attempts
            run.fetch_ms += sub.fetch_ms
            run.compliance_ms += sub.compliance_ms
        if outcome is None:
            _fallback(host, "timeout")
            return None
        if isinstance(outcome, Envelope):
            _fallback(host, outcome.error.code.value if outcome.error else "failed")
            return None
        if not 200 <= outcome.status < 300:
            _fallback(host, f"status_{outcome.status}")
            return None
        declared = outcome.content_type or ""
        if declared != _JSON_TYPE and not declared.endswith("+json"):
            _fallback(host, "not_json")
            return None
        run.report = with_redirect_findings(run.report, sub.report)
        return outcome


def _built_page(body: bytes, base: str) -> tuple[str, Attachment | None] | None:
    """The node's page and file of the metadata answer ``body``, or None when it holds no node.
    CPU-bound: runs on the conversion pool."""
    meta = parse_metadata(body)
    return node_page(meta, base=base) if meta is not None else None


def _same_host(attachment: Attachment, url: str) -> bool:
    found = urlsplit(attachment.url).hostname
    return found is not None and found == urlsplit(url).hostname


def _fallback(host: str, reason: str) -> None:
    _log.info("edusharing_fallback", host=host, reason=reason)
