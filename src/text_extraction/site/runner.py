"""The runner of a ``site`` job's items (rulings P9-S7, P9-S11, P9-S12, P9-S15).

A ``site`` job grows while it runs (D1 a): each item is a page, the site's robots.txt, a sitemap
file or a further part of a large sitemap, and an item appends what it found as new items of the
same job (``SiteStore.extend``) before the worker writes its own result, so the job completes
only with its last item. ``ExtractRunner`` hands every item of a ``site`` job to
``SiteRunner.run``, which reads the item's node and runs its role:

- **page**: ``Pipeline.run`` in fast mode with ``site_links`` (every check of an extraction:
  SSRF guard, compliance, the host's turn, the cache, quality); its links (of a result with
  ``site.links``: success, partial, or failed by a quality verdict) are judged
  (``follow.judge_links``) and sent to ``extend``, and the new items are queued. The start page
  (item 0) sets the crawl's scope from its ``final_url`` and first appends the robots item (when
  ``sitemaps`` is asked), whatever its own outcome. Its answer keeps title, status, metadata and
  quality: ``content`` and ``links`` are null (D5 a), ``site`` has its depth, parent and typed
  links with ``crawl`` and ``target``. A page whose links do not all fit ``JOB_ITEM_MAX_BYTES``
  keeps the first ones that do (``links_truncated``), and only those are followed. A link
  deeper than ``depth`` says ``depth`` without its compliance verdict (none is read for it).
  A platform video page (YouTube and the like) is answered by the media path, which reads no
  links: its item is a page with ``links`` null and nothing is followed from it.
- **robots**: robots.txt's ``Sitemap:`` lines through ``Pipeline.robots_sitemaps`` (cached, or
  loaded in the host's turn, in every compliance mode), within ``EXTRACT_TIMEOUT_SECONDS``. Its
  sitemaps (at most ``SITE_MAX_SITEMAP_FILES``, else ``sitemap_files_truncated``) become items at
  depth 0, on any host (the fetch checks the address); none listed means one guess,
  ``<origin>/sitemap.xml``. ``unavailable`` fails ``target_http_error``, retryable, no guess.
- **sitemap**: ``Pipeline.fetch_raw`` (at most ``SITE_SITEMAP_MAX_BYTES``, cut there), then the
  inflate and the parse on the runner's own pool (one thread per pod, so sitemap parses never
  hold the threads that convert pages). The parse has a deadline (the item's budget,
  ``EXTRACT_TIMEOUT_SECONDS * 2`` from its start, inside the lease of
  ``EXTRACT_TIMEOUT_SECONDS * 2 + 10``) that it checks itself: a parse cut by it answers what it
  read, ``partial`` with ``sitemap_truncated`` and the code ``timeout``; only a wait for the
  pool's thread that outlasts the deadline by 5 s fails ``timeout``, retryable. Before the
  parse the item reads how many entries it may keep (``SiteStore.entries_left``: its grant on
  a rerun, else what is left of ``SITE_MAX_SITEMAP_URLS``), and the parse keeps at most that
  many; with none left a file is read only to its first entry (an index is read whole for its
  children). The item then reserves as many as it holds (``SiteStore.reserve_entries``, the
  same grant on a rerun) and keeps that many (``sitemap_urls_truncated`` for the rest). Its
  entries are packed into parts of at most half of ``JOB_ITEM_MAX_BYTES``: part 1 is its own
  result, parts 2 and up are finished ``sitemap_part`` items appended with its children. A
  rerun whose parse was cut at another point than the first run's (the deadline) may answer a
  part 1 that does not match the parts stored by the first run; the grant keeps the total
  bounded. An index's children become sitemap items one level deeper when that is at most 3
  and they are on the index's host or the crawl's; otherwise they say ``depth`` (looked up) or
  ``out_of_scope``. A children list that does not fit the item is cut (``sitemap_truncated``),
  and only the kept ones are followed. A child refused by ``SITE_MAX_SITEMAP_FILES`` says
  ``max_files``, with ``sitemap_files_truncated``.

Results of robots and sitemap items are built by ``site_result``: ``mode_used`` fast, ``format``
text, ``content`` null. ``extend`` takes at most ``EXTEND_BATCH`` candidates a call: an item
sends batches 0, 1, ... in order, each with the same keys in the same order on a rerun (the keys
depend on the page, the scope and the depth only; whether a candidate may be appended also
depends on the memory guard, which the stored answers make moot), and queues each batch's new
items as it is answered. A rerun of an item (a lost lease) gets the stored answers, appends
nothing and queues those items again (a copy of a finished one is skipped by the worker), the
items of stored batches it no longer sends included. A batch whose stored answer has another
length than the batch (the page changed) leaves its links with their preliminary ``crawl`` and
no ``target``. ``requeue_children`` queues the children again for an exhausted item before it
fails. While the memory guard is high an item appends nothing to crawl (its candidates are
looked up only): its links (or children) say ``memory_guard`` and it has the warning
``site_memory_guard``. A sitemap's further parts are results, which are stored as every result
is (the job store's writes are not guarded, so accepted jobs complete).

Metrics: ``text_extraction_site_items_total{role, outcome}``,
``text_extraction_site_links_total{crawl}``, ``text_extraction_site_caps_total{cap}``. The log
line ``site_item_done`` has the role, the host and counts, never a path or a query.
"""

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

import structlog
from prometheus_client import CollectorRegistry, Counter
from pydantic import BaseModel

from text_extraction.compliance import ComplianceReport
from text_extraction.conversion_pool import ConversionPool
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.extract_models import ExtractRequest, ExtractResult, Timings, failed_extract
from text_extraction.jobs import JobRecord, JobStore, job_principal
from text_extraction.pipeline import LINKS_TRUNCATED_WARNING, Pipeline
from text_extraction.result_cache import MemoryPressure
from text_extraction.scheduler import FairQueue, QueueItem
from text_extraction.settings import Settings
from text_extraction.site.follow import judge_links, unique_candidates
from text_extraction.site.sitemaps import ParsedSitemap, inflate_bounded, pack_parts, parse_sitemap
from text_extraction.site.store import EXTEND_BATCH, Candidate, Finished, SiteContext, SiteStore
from text_extraction.site.types import (
    Crawl,
    SiteInfo,
    SiteLink,
    SitemapChild,
    SitemapInfo,
    SiteNode,
    SiteOptions,
    SiteScope,
)
from text_extraction.site.urls import (
    in_scope,
    normalize_url,
    scope_for,
    seen_key,
    site_host,
)

__all__ = [
    "MAX_PAGES_REACHED_WARNING",
    "MAX_SITEMAP_NESTING",
    "SITEMAP_FILES_TRUNCATED_WARNING",
    "SITEMAP_NOT_FOUND_WARNING",
    "SITEMAP_TRUNCATED_WARNING",
    "SITEMAP_URLS_TRUNCATED_WARNING",
    "SITE_MEMORY_GUARD_WARNING",
    "SiteRunner",
    "site_result",
]

_log = structlog.get_logger(__name__)

# The deepest sitemap a crawl reads: robots.txt lists depth 0, an index's children are one deeper.
MAX_SITEMAP_NESTING = 3

SITEMAP_NOT_FOUND_WARNING = "sitemap_not_found"
SITEMAP_TRUNCATED_WARNING = "sitemap_truncated"
SITEMAP_URLS_TRUNCATED_WARNING = "sitemap_urls_truncated"
SITEMAP_FILES_TRUNCATED_WARNING = "sitemap_files_truncated"
MAX_PAGES_REACHED_WARNING = "max_pages_reached"
SITE_MEMORY_GUARD_WARNING = "site_memory_guard"

_UNKNOWN_ITEM_MESSAGE = "The crawl has no record of this item."
_ROBOTS_UNAVAILABLE_MESSAGE = "robots.txt could not be retrieved."
_ROBOTS_TIMEOUT_MESSAGE = "robots.txt could not be retrieved in time."
_SITEMAP_INVALID_MESSAGE = "The sitemap could not be read."
_SITEMAP_UNSUPPORTED_MESSAGE = "The file is no sitemap (an HTML page, for example)."
_SITEMAP_SLOT_MESSAGE = "The sitemap could not be read in time: the worker was busy."
_SITEMAP_NO_ROOT_MESSAGE = "The sitemap was read until its time limit without reaching a sitemap."
_SITEMAP_TIMEOUT_MESSAGE = "The sitemap was read until its time limit: the entries before it."
_SITEMAP_CUT_MESSAGE = "The sitemap was cut: the entries before the cut."

# How long past the item's deadline the runner waits for a parse that stops itself at it.
_PARSE_GRACE_SECONDS = 5.0
# Room in an item's JSON for the warnings and error the runner adds after it sized the item.
_SIZE_MARGIN = 512
# The widest ``crawl`` and ``target`` a link or a child can get, for sizing before they are set.
_WIDEST_CRAWL: Crawl = "out_of_scope"
_WIDEST_TARGET = 9_999_999
# Cached compliance verdicts read at a time for one page's links.
_VERDICTS_AT_ONCE = 50


@dataclass(frozen=True)
class _Metrics:
    items: Counter
    links: Counter
    caps: Counter


_METRICS: WeakKeyDictionary[CollectorRegistry, _Metrics] = WeakKeyDictionary()


def _metrics_for(registry: CollectorRegistry) -> _Metrics:
    metrics = _METRICS.get(registry)
    if metrics is None:
        metrics = _Metrics(
            items=Counter(
                "text_extraction_site_items_total",
                "Items of site jobs run by this worker, by role (page, robots, sitemap) and "
                "outcome (success, or the error code of the result).",
                ["role", "outcome"],
                registry=registry,
            ),
            links=Counter(
                "text_extraction_site_links_total",
                "Links of crawled pages, by what the crawl did with them.",
                ["crawl"],
                registry=registry,
            ),
            caps=Counter(
                "text_extraction_site_caps_total",
                "Site job items that ran into a cap: pages (max_pages), files "
                "(SITE_MAX_SITEMAP_FILES), urls (SITE_MAX_SITEMAP_URLS), links "
                "(SITE_MAX_LINKS_PER_PAGE or the item size) or memory (the memory guard).",
                ["cap"],
                registry=registry,
            ),
        )
        _METRICS[registry] = metrics
    return metrics


def site_result(
    url: str,
    *,
    site: SiteInfo,
    report: ComplianceReport,
    timings: Timings,
    attempts: int,
    final_url: str | None = None,
    upstream_status: int | None = None,
    content_type: str | None = None,
    warnings: Sequence[str] = (),
) -> ExtractResult:
    """The result of a robots or sitemap item (or a sitemap part): no content, fast mode."""
    return ExtractResult(
        url=url,
        final_url=final_url,
        upstream_status=upstream_status,
        content_type=content_type,
        mode_used="fast",
        attempts=attempts,
        language=None,
        word_count=0,
        title=None,
        format="text",
        content=None,
        quality=None,
        timings=timings,
        compliance=report,
        metadata=None,
        links=None,
        fingerprint=None,
        cache="bypass",
        warnings=list(warnings),
        site=site,
    )


@dataclass(frozen=True)
class _Extended:
    """What the batches of one item's ``extend`` answered, by candidate in order: its index,
    and whether the answer is known (False for a batch whose stored answer belongs to other
    candidates: a rerun of a page that changed)."""

    indexes: list[int | None]
    known: list[bool]
    new: set[int]
    capped: bool

    def answer(self, position: int) -> tuple[int | None, bool]:
        return self.indexes[position], self.known[position]


@dataclass
class _Outcome:
    """What an item counts in the metrics and the log line, besides its envelope."""

    links: list[SiteLink]
    entries: int = 0
    appended: int = 0
    caps: set[str] | None = None


def _no_timings() -> Timings:
    return Timings(total_ms=0, compliance_ms=0, fetch_ms=0, convert_ms=0)


def _ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _json_size(model: BaseModel) -> int:
    return len(model.model_dump_json().encode())


def _fitting(base: int, sizes: Sequence[int], budget: int) -> int:
    """How many of the leading items fit ``budget`` next to ``base`` bytes, at one comma each."""
    total = base
    for count, size in enumerate(sizes):
        total += size + 1
        if total > budget:
            return count
    return len(sizes)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _robots_url(url: str) -> str:
    robots = _origin(url) + "/robots.txt"
    return normalize_url(robots) or robots


def _decided(
    preliminary: Crawl,
    answer: tuple[int | None, bool],
    extended: _Extended,
    cap: Literal["max_pages", "max_files"],
) -> Crawl:
    """A followed link's (or child's) final ``crawl`` from its ``extend`` answer; ``cap`` is
    what a refusal by the cap says (``max_pages`` for pages, ``max_files`` for sitemaps). An
    unknown answer keeps the preliminary ``crawl``."""
    index, known = answer
    if not known:
        return preliminary
    if preliminary == "queued":
        if index is None:
            # Not appended and not seen: over the cap, or nothing was appended (memory guard).
            return cap if extended.capped else "memory_guard"
        new = extended.new
        return "queued" if index in new else "seen"
    if preliminary == "depth" and index is not None:
        return "seen"
    return preliminary


def _with_warnings(result: ExtractResult, warnings: Sequence[str]) -> list[str]:
    merged = list(result.warnings)
    merged += [warning for warning in warnings if warning not in merged]
    return merged


class SiteRunner:
    """Runs the items of ``site`` jobs (see the module docstring). ``pool`` runs the sitemap
    parses (``build_worker`` gives it a pool of one thread of its own); ``guard`` tells whether
    Valkey is nearly full; ``registry`` gets the site metrics."""

    def __init__(
        self,
        pipeline: Pipeline,
        store: JobStore,
        sites: SiteStore,
        queue: FairQueue,
        settings: Settings,
        pool: ConversionPool,
        *,
        guard: MemoryPressure | None = None,
        registry: CollectorRegistry | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._store = store
        self._sites = sites
        self._queue = queue
        self._settings = settings
        self._pool = pool
        self._guard = guard
        self._metrics = _metrics_for(registry) if registry is not None else None
        self._item_max_bytes = settings.job_item_max_bytes
        # The time an item may take, inside the lease of ExtractRunner (``* 2 + 10``).
        self._item_budget = settings.extract_timeout_seconds * 2

    async def run(
        self,
        job: JobRecord,
        item: QueueItem,
        url: str,
        *,
        gate_refusals: Literal["fail", "raise"],
    ) -> Envelope[ExtractResult]:
        """The item's result. A gate refusal with ``gate_refusals="raise"``, a Valkey error and
        ``JobLost`` propagate, as from ``ExtractRunner``."""
        started = time.monotonic()
        context = await self._sites.context(job.id, item.index)
        node = context.node
        if node is None:
            envelope = failed_extract(
                ErrorCode.INTERNAL_ERROR, _UNKNOWN_ITEM_MESSAGE, retryable=False
            )
            outcome = _Outcome(links=[])
            role = "unknown"
        else:
            role = node.role
            if node.role == "page":
                envelope, outcome = await self._page(job, item, url, node, context, gate_refusals)
            elif node.role == "robots":
                envelope, outcome = await self._robots(job, item, url, node, gate_refusals)
            elif node.role == "sitemap":
                envelope, outcome = await self._sitemap(
                    job, item, url, node, context, gate_refusals, started
                )
            else:  # a sitemap part is appended finished and never queued
                envelope = failed_extract(
                    ErrorCode.INTERNAL_ERROR, _UNKNOWN_ITEM_MESSAGE, retryable=False
                )
                outcome = _Outcome(links=[])
        self._count(role, envelope, outcome)
        _log.info(
            "site_item_done",
            role=role,
            host=site_host(url),
            links=len(outcome.links),
            entries=outcome.entries,
            appended=outcome.appended,
            outcome=_outcome_label(envelope),
        )
        return envelope

    async def requeue_children(self, job: JobRecord, item: QueueItem) -> None:
        """Queue again the items that ``item`` appended (its stored ``extend`` answers), for an
        item that will not run again (exhausted): a crash after its ``extend`` and before its
        queueing must not leave them unqueued."""
        children = await self._sites.stored_children(job.id, item.index)
        if children:
            await self._queue.enqueue_items(job.id, item.client, children)

    # --- the roles ---------------------------------------------------------------------------

    async def _page(
        self,
        job: JobRecord,
        item: QueueItem,
        url: str,
        node: SiteNode,
        context: SiteContext,
        gate_refusals: Literal["fail", "raise"],
    ) -> tuple[Envelope[ExtractResult], _Outcome]:
        options = job.options.site or SiteOptions()
        request = ExtractRequest(
            url=url,
            mode="fast",
            format="text",
            metadata=options.metadata,
            links=False,
            compliance=job.options.compliance,
            cache=options.cache,
            ocr="never",
        )
        envelope = await self._pipeline.run(
            request, job_principal(job), gate_refusals=gate_refusals, site_links=True
        )
        data = envelope.data
        found = data.site if data is not None else None
        final_url = data.final_url if data is not None else None
        nofollow = found.nofollow if found is not None else False
        if data is None:
            data = site_result(
                url,
                site=SiteInfo(role="page", depth=node.depth, parent=node.parent),
                report=ComplianceReport(mode=job.options.compliance, decision="skipped"),
                timings=_no_timings(),
                attempts=0,
            )
        else:
            data = data.model_copy(update={"content": None, "links": None})
        site = SiteInfo(
            role="page", depth=node.depth, parent=node.parent, links=None, nofollow=nofollow
        )
        warnings: list[str] = []
        caps: set[str] = set()
        links = found.links if found is not None else None
        if links is not None:
            if LINKS_TRUNCATED_WARNING in data.warnings:
                caps.add("links")
            links = self._fit_links(envelope, data, site, links)
            if found is not None and found.links is not None and len(links) < len(found.links):
                warnings.append(LINKS_TRUNCATED_WARNING)
                caps.add("links")
        scope = context.scope
        if scope is None:
            scope = await self._scope(job, item, url, final_url, options)
        guard_high = self._guard is not None and self._guard.high

        candidates: list[Candidate] = []
        if item.index == 0 and options.sitemaps:
            robots = _robots_url(final_url or url)
            candidates.append(
                Candidate(
                    key=seen_key(robots),
                    url=robots,
                    node=SiteNode(role="robots", depth=0, parent=item.index),
                    add=not guard_high,
                )
            )
        judged: list[tuple[SiteLink, Candidate | None]] = []
        if links is not None:
            child_depth = node.depth + 1
            blocked = (
                await self._verdicts(links, scope, nofollow, job.options.compliance)
                if child_depth <= options.depth
                else {}
            )
            judged = judge_links(
                links,
                page_nofollow=nofollow,
                scope=scope,
                child_depth=child_depth,
                max_depth=options.depth,
                blocked=blocked,
                parent=item.index,
            )
        page_candidates = unique_candidates(candidate for _, candidate in judged)
        if guard_high:
            page_candidates = [replace(candidate, add=False) for candidate in page_candidates]
        candidates += page_candidates
        aliases = [final_url] if final_url and seen_key(final_url) != seen_key(url) else []

        appended = 0
        if candidates or aliases or item.index == 0:
            extended = await self._extend(
                job,
                item,
                candidates,
                aliases=aliases,
                scope=scope if item.index == 0 else None,
                max_pages=options.max_pages,
            )
            appended = len(extended.new)
            robots_kept = 1 if item.index == 0 and options.sitemaps else 0
            if robots_kept and extended.answer(0) == (None, True) and guard_high:
                warnings.append(SITE_MEMORY_GUARD_WARNING)
                caps.add("memory")
            by_key = {
                candidate.key: extended.answer(position)
                for position, candidate in enumerate(candidates)
                if position >= robots_kept
            }
            decided: list[SiteLink] = []
            for link, candidate in judged:
                if candidate is None:
                    decided.append(link)
                    continue
                answer = by_key[candidate.key]
                crawl = _decided(link.crawl, answer, extended, "max_pages")
                decided.append(link.model_copy(update={"crawl": crawl, "target": answer[0]}))
            if any(link.crawl == "max_pages" for link in decided):
                warnings.append(MAX_PAGES_REACHED_WARNING)
                caps.add("pages")
            if any(link.crawl == "memory_guard" for link in decided):
                warnings.append(SITE_MEMORY_GUARD_WARNING)
                caps.add("memory")
            judged_links = decided
        else:
            judged_links = [link for link, _ in judged]
        if links is not None:
            site = site.model_copy(update={"links": judged_links})
        data = data.model_copy(update={"site": site, "warnings": _with_warnings(data, warnings)})
        envelope = envelope.model_copy(update={"data": data})
        return envelope, _Outcome(links=judged_links, appended=appended, caps=caps)

    async def _robots(
        self,
        job: JobRecord,
        item: QueueItem,
        url: str,
        node: SiteNode,
        gate_refusals: Literal["fail", "raise"],
    ) -> tuple[Envelope[ExtractResult], _Outcome]:
        started = time.monotonic()
        report = ComplianceReport(mode=job.options.compliance, decision="skipped")
        site = SiteInfo(role="robots", depth=node.depth, parent=node.parent)

        def result(warnings: Sequence[str] = (), **fields: object) -> ExtractResult:
            return site_result(
                url,
                site=site.model_copy(update=fields),
                report=report,
                timings=Timings(total_ms=_ms(started), compliance_ms=0, fetch_ms=0, convert_ms=0),
                attempts=0,
                warnings=warnings,
            )

        deadline = asyncio.timeout(self._settings.extract_timeout_seconds)
        try:
            async with deadline:
                found = await self._pipeline.robots_sitemaps(url, gate_refusals=gate_refusals)
        except TimeoutError:
            if not deadline.expired():
                raise
            error = ErrorInfo(
                code=ErrorCode.TIMEOUT, message=_ROBOTS_TIMEOUT_MESSAGE, retryable=True
            )
            return Envelope(status="failed", data=result(), error=error), _Outcome(links=[])
        if found.state == "unavailable":
            error = ErrorInfo(
                code=ErrorCode.TARGET_HTTP_ERROR,
                message=_ROBOTS_UNAVAILABLE_MESSAGE,
                retryable=True,
            )
            return Envelope(status="failed", data=result(), error=error), _Outcome(links=[])
        max_files = self._settings.site_max_sitemap_files
        warnings: list[str] = []
        caps: set[str] = set()
        if found.state == "found" and found.urls:
            urls, guessed = found.urls[:max_files], False
            if len(found.urls) > max_files:
                warnings.append(SITEMAP_FILES_TRUNCATED_WARNING)
                caps.add("files")
        else:
            guess = _origin(url) + "/sitemap.xml"
            urls, guessed = [normalize_url(guess) or guess], True
        guard_high = self._guard is not None and self._guard.high
        candidates = unique_candidates(
            Candidate(
                key=seen_key(sitemap),
                url=sitemap,
                node=SiteNode(role="sitemap", depth=0, parent=item.index, guessed=guessed),
                add=not guard_high,
            )
            for sitemap in urls
        )
        extended = await self._extend(job, item, candidates, max_pages=0)
        listed = [
            candidate.url
            for position, candidate in enumerate(candidates)
            if extended.indexes[position] is not None
        ]
        refused = [
            position
            for position in range(len(candidates))
            if extended.answer(position) == (None, True)
        ]
        if refused:
            if extended.capped:
                warnings.append(SITEMAP_FILES_TRUNCATED_WARNING)
                caps.add("files")
            else:
                warnings.append(SITE_MEMORY_GUARD_WARNING)
                caps.add("memory")
        envelope = Envelope(status="success", data=result(_dedup(warnings), sitemaps=listed))
        return envelope, _Outcome(links=[], appended=len(extended.new), caps=caps)

    async def _sitemap(
        self,
        job: JobRecord,
        item: QueueItem,
        url: str,
        node: SiteNode,
        context: SiteContext,
        gate_refusals: Literal["fail", "raise"],
        started: float,
    ) -> tuple[Envelope[ExtractResult], _Outcome]:
        settings = self._settings
        site = SiteInfo(role="sitemap", depth=node.depth, parent=node.parent)
        raw = await self._pipeline.fetch_raw(
            url,
            job_principal(job),
            job.options.compliance,
            max_bytes=settings.site_sitemap_max_bytes,
            truncate=True,
            gate_refusals=gate_refusals,
        )
        if raw.failure is not None:
            return _with_site(raw.failure, url, site, job), _Outcome(links=[])
        fetched = raw.fetched
        if fetched is None:  # pragma: no cover - RawFetch holds one of both
            raise RuntimeError("fetch_raw answered neither a fetch nor a failure")

        def result(info: SitemapInfo | None, warnings: Sequence[str] = ()) -> ExtractResult:
            return site_result(
                url,
                site=site.model_copy(update={"sitemap": info}),
                report=raw.report,
                timings=raw.timings,
                attempts=raw.attempts,
                final_url=fetched.final_url,
                upstream_status=fetched.status,
                content_type=fetched.content_type,
                warnings=warnings,
            )

        def failed(code: ErrorCode, message: str, *, retryable: bool) -> Envelope[ExtractResult]:
            error = ErrorInfo(code=code, message=message, retryable=retryable)
            return Envelope(status="failed", data=result(None), error=error)

        if fetched.status in (404, 410) and node.guessed:
            info = SitemapInfo(format="absent", guessed=True)
            envelope = Envelope(status="success", data=result(info, [SITEMAP_NOT_FOUND_WARNING]))
            return envelope, _Outcome(links=[])
        if not 200 <= fetched.status < 300:
            retryable = fetched.status >= 500 or fetched.status == 429
            message = f"The sitemap answered with HTTP status {fetched.status}."
            return failed(ErrorCode.TARGET_HTTP_ERROR, message, retryable=retryable), _Outcome(
                links=[]
            )

        # What this item may keep: its grant on a rerun, else what is left of the crawl's. With
        # nothing left a file is read only to its first entry (an index is read whole).
        left = await self._sites.entries_left(job.id, item.index)
        deadline = started + self._item_budget
        try:
            # The parse stops itself at the deadline and answers what it read; this timeout,
            # later by a grace, ends only a wait for the pool's thread that outlasts it.
            seconds = max(deadline - time.monotonic(), 0.0) + _PARSE_GRACE_SECONDS
            async with asyncio.timeout(seconds):
                parsed, inflate_cut = await self._pool.run(
                    _read_sitemap,
                    fetched.content,
                    fetched.final_url,
                    settings.site_sitemap_max_bytes,
                    left,
                    deadline,
                )
        except TimeoutError:
            return failed(ErrorCode.TIMEOUT, _SITEMAP_SLOT_MESSAGE, retryable=True), _Outcome(
                links=[]
            )
        if parsed.timed_out and parsed.format is None:
            return failed(ErrorCode.TIMEOUT, _SITEMAP_NO_ROOT_MESSAGE, retryable=False), _Outcome(
                links=[]
            )
        if parsed.error == "invalid" or (parsed.format is None and parsed.error is None):
            return failed(
                ErrorCode.CONVERSION_FAILED, _SITEMAP_INVALID_MESSAGE, retryable=False
            ), _Outcome(links=[])
        if parsed.error == "unsupported":
            return failed(
                ErrorCode.UNSUPPORTED_CONTENT_TYPE, _SITEMAP_UNSUPPORTED_MESSAGE, retryable=False
            ), _Outcome(links=[])
        sitemap_format = parsed.format
        if sitemap_format is None:  # pragma: no cover - answered above
            raise RuntimeError("a parsed sitemap without a format")

        granted = await self._sites.reserve_entries(job.id, item.index, len(parsed.entries))
        entries = parsed.entries[:granted]
        warnings: list[str] = []
        caps: set[str] = set()
        codes: list[tuple[ErrorCode, str]] = []
        if parsed.timed_out:
            warnings.append(SITEMAP_TRUNCATED_WARNING)
            codes.append((ErrorCode.TIMEOUT, _SITEMAP_TIMEOUT_MESSAGE))
        elif (
            fetched.truncated
            or inflate_cut
            or (parsed.truncated and parsed.entry_count == len(parsed.entries))
        ):
            warnings.append(SITEMAP_TRUNCATED_WARNING)
            codes.append((ErrorCode.TOO_LARGE, _SITEMAP_CUT_MESSAGE))
        if parsed.entry_count > len(entries) and not parsed.timed_out:
            warnings.append(SITEMAP_URLS_TRUNCATED_WARNING)
            codes.append((ErrorCode.TOO_LARGE, _SITEMAP_CUT_MESSAGE))
            caps.add("urls")

        parts = pack_parts(entries, self._item_max_bytes // 2)
        count = len(parts)
        finished = [
            Finished(
                url=url,
                node=SiteNode(
                    role="sitemap_part", depth=node.depth, parent=item.index, part=number
                ),
                envelope=Envelope(
                    status="success",
                    data=site_result(
                        url,
                        site=SiteInfo(
                            role="sitemap_part",
                            depth=node.depth,
                            parent=item.index,
                            sitemap=SitemapInfo(
                                format=sitemap_format,
                                guessed=node.guessed,
                                entries=part,
                                entry_count=parsed.entry_count,
                                part=number,
                                parts=count,
                            ),
                        ),
                        report=raw.report,
                        timings=_no_timings(),
                        attempts=0,
                        final_url=fetched.final_url,
                        upstream_status=fetched.status,
                        content_type=fetched.content_type,
                    ),
                ),
            )
            for number, part in enumerate(parts[1:], start=2)
        ]
        info = SitemapInfo(
            format=sitemap_format,
            guessed=node.guessed,
            entries=parts[0],
            entry_count=parsed.entry_count,
            part=1,
            parts=count,
        )
        children = self._fit_children(result(info, [*warnings, SITEMAP_TRUNCATED_WARNING]), parsed)
        if len(children) < len(parsed.children) and SITEMAP_TRUNCATED_WARNING not in warnings:
            warnings.append(SITEMAP_TRUNCATED_WARNING)
            codes.append((ErrorCode.TOO_LARGE, _SITEMAP_CUT_MESSAGE))
        judged = self._judge_children(children, url, node, item.index, context.scope)
        candidates = unique_candidates(candidate for _, candidate in judged)
        extended = await self._extend(job, item, candidates, finished=finished, max_pages=0)
        by_key = {
            candidate.key: extended.answer(position)
            for position, candidate in enumerate(candidates)
        }
        decided: list[SitemapChild] = []
        for child, candidate in judged:
            if candidate is None:
                decided.append(child)
                continue
            answer = by_key[candidate.key]
            crawl = _decided(child.crawl, answer, extended, "max_files")
            decided.append(child.model_copy(update={"crawl": crawl, "target": answer[0]}))
        if any(child.crawl == "max_files" for child in decided):
            warnings.append(SITEMAP_FILES_TRUNCATED_WARNING)
            caps.add("files")
        if any(child.crawl == "memory_guard" for child in decided):
            warnings.append(SITE_MEMORY_GUARD_WARNING)
            caps.add("memory")
        info = info.model_copy(update={"children": decided})
        data = result(info, _dedup(warnings))
        if codes:
            code, message = codes[0]
            error = ErrorInfo(code=code, message=message, retryable=False)
            envelope = Envelope(status="partial", data=data, error=error)
        else:
            envelope = Envelope(status="success", data=data)
        outcome = _Outcome(links=[], entries=len(entries), appended=len(extended.new), caps=caps)
        return envelope, outcome

    # --- helpers -----------------------------------------------------------------------------

    async def _extend(
        self,
        job: JobRecord,
        item: QueueItem,
        candidates: Sequence[Candidate],
        *,
        finished: Sequence[Finished] = (),
        aliases: Sequence[str] = (),
        scope: SiteScope | None = None,
        max_pages: int,
    ) -> _Extended:
        """``SiteStore.extend`` in batches of at most ``EXTEND_BATCH`` candidates, in order,
        ``finished``, ``aliases`` and ``scope`` with batch 0; each batch's new items are queued
        as soon as it is answered (again on a rerun, whose answers are the stored ones).

        A rerun (batch 0's answer is a repeat) may send fewer batches than the first run did
        (the page changed, or a cut moved): the items of the stored batches it did not send are
        queued again too (``SiteStore.stored_children``), so none is left unqueued."""
        indexes: list[int | None] = []
        known: list[bool] = []
        new: set[int] = set()
        capped = False
        repeat = False
        batches = [
            candidates[start : start + EXTEND_BATCH]
            for start in range(0, len(candidates), EXTEND_BATCH)
        ] or [[]]
        for number, batch in enumerate(batches):
            first = number == 0
            answer = await self._sites.extend(
                job.id,
                item.index,
                batch,
                finished=finished if first else (),
                aliases=aliases if first else (),
                max_pages=max_pages,
                scope=scope if first else None,
                batch=number,
            )
            repeat = repeat or (first and answer.repeat)
            batch_indexes = answer.indexes
            batch_known = len(batch_indexes) == len(batch)
            if not batch_known:
                # A rerun whose page changed since its first run: the stored answer belongs to
                # other candidates. Its new items are still queued; no target is claimed.
                _log.warning(
                    "site_extend_answer_mismatch",
                    batch=number,
                    stored=len(batch_indexes),
                    sent=len(batch),
                )
                batch_indexes = [None] * len(batch)
            indexes += batch_indexes
            known += [batch_known] * len(batch)
            capped = capped or answer.capped
            if answer.new:
                await self._queue.enqueue_items(job.id, item.client, answer.new)
                new.update(index for index, _ in answer.new)
        if repeat:
            stored = await self._sites.stored_children(job.id, item.index)
            missing = [(index, url) for index, url in stored if index not in new]
            if missing:
                await self._queue.enqueue_items(job.id, item.client, missing)
        return _Extended(indexes=indexes, known=known, new=new, capped=capped)

    async def _scope(
        self,
        job: JobRecord,
        item: QueueItem,
        url: str,
        final_url: str | None,
        options: SiteOptions,
    ) -> SiteScope:
        """The scope item 0 sets (from its ``final_url``); for another item without one stored
        (none should be), the start URL's."""
        if item.index == 0:
            return scope_for(url, final_url, options.scope)
        start = await self._store.url(job.id, 0)
        return scope_for(start or url, None, options.scope)

    async def _verdicts(
        self,
        links: Sequence[SiteLink],
        scope: SiteScope,
        nofollow: bool,
        mode: str,
    ) -> dict[str, bool | None]:
        """The cached compliance verdict of each link that would be followed; only ``enforce``
        blocks, so other modes read none."""
        if mode != "enforce" or nofollow:
            return {}
        urls = list(
            dict.fromkeys(
                link.url
                for link in links
                if link.source in ("a", "area")
                and link.kind != "file"
                and "nofollow" not in link.rel
                and in_scope(link.url, scope)
            )
        )
        verdicts: dict[str, bool | None] = {}
        for start in range(0, len(urls), _VERDICTS_AT_ONCE):
            chunk = urls[start : start + _VERDICTS_AT_ONCE]
            answers = await asyncio.gather(
                *(self._pipeline.blocked_from_cache(url, "enforce") for url in chunk)
            )
            verdicts.update(zip(chunk, answers, strict=True))
        return verdicts

    def _fit_links(
        self,
        envelope: Envelope[ExtractResult],
        data: ExtractResult,
        site: SiteInfo,
        links: list[SiteLink],
    ) -> list[SiteLink]:
        """The leading links that let the page's item fit ``JOB_ITEM_MAX_BYTES``, sized with
        the widest ``crawl`` and ``target`` they can get."""
        budget = self._item_max_bytes - _SIZE_MARGIN
        shell = envelope.model_copy(
            update={"data": data.model_copy(update={"site": site.model_copy(update={"links": []})})}
        )
        base = _json_size(shell)
        # Cheap first: the links as they are, plus the most the crawl decisions can add.
        quick = sum(len(link.model_dump_json().encode()) + 24 for link in links)
        if base + quick + len(links) <= budget:
            return links
        sizes = [
            _json_size(link.model_copy(update={"crawl": _WIDEST_CRAWL, "target": _WIDEST_TARGET}))
            for link in links
        ]
        return links[: _fitting(base, sizes, budget)]

    def _fit_children(self, shell: ExtractResult, parsed: ParsedSitemap) -> list[SitemapChild]:
        """The leading children of an index that let its item fit ``JOB_ITEM_MAX_BYTES``."""
        children = parsed.children
        if not children:
            return []
        budget = self._item_max_bytes - _SIZE_MARGIN
        base = _json_size(Envelope(status="partial", data=shell))
        sizes = [
            _json_size(child.model_copy(update={"crawl": _WIDEST_CRAWL, "target": _WIDEST_TARGET}))
            for child in children
        ]
        return children[: _fitting(base, sizes, budget)]

    def _judge_children(
        self,
        children: Sequence[SitemapChild],
        url: str,
        node: SiteNode,
        parent: int,
        scope: SiteScope | None,
    ) -> list[tuple[SitemapChild, Candidate | None]]:
        """An index's children with their preliminary ``crawl`` and candidates: one level
        deeper, on the index's host or the crawl's; deeper than ``MAX_SITEMAP_NESTING`` looked
        up only."""
        depth = node.depth + 1
        hosts = {site_host(url)}
        if scope is not None:
            hosts.add(scope.host)
        guard_high = self._guard is not None and self._guard.high
        judged: list[tuple[SitemapChild, Candidate | None]] = []
        for child in children:
            if site_host(child.url) not in hosts:
                judged.append((child.model_copy(update={"crawl": "out_of_scope"}), None))
                continue
            deep = depth > MAX_SITEMAP_NESTING
            crawl: Crawl = "depth" if deep else "queued"
            candidate = Candidate(
                key=seen_key(child.url),
                url=child.url,
                node=SiteNode(role="sitemap", depth=depth, parent=parent),
                add=not deep and not guard_high,
            )
            judged.append((child.model_copy(update={"crawl": crawl}), candidate))
        return judged

    def _count(self, role: str, envelope: Envelope[ExtractResult], outcome: _Outcome) -> None:
        if self._metrics is None:
            return
        self._metrics.items.labels(role=role, outcome=_outcome_label(envelope)).inc()
        for link in outcome.links:
            self._metrics.links.labels(crawl=link.crawl).inc()
        for cap in sorted(outcome.caps or ()):
            self._metrics.caps.labels(cap=cap).inc()


def _outcome_label(envelope: Envelope[ExtractResult]) -> str:
    return envelope.error.code.value if envelope.error is not None else envelope.status


def _dedup(warnings: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(warnings))


def _with_site(
    envelope: Envelope[ExtractResult], url: str, site: SiteInfo, job: JobRecord
) -> Envelope[ExtractResult]:
    """A failure envelope with the item's ``site``."""
    data = envelope.data
    if data is None:
        data = site_result(
            url,
            site=site,
            report=ComplianceReport(mode=job.options.compliance, decision="skipped"),
            timings=_no_timings(),
            attempts=0,
        )
    else:
        data = data.model_copy(update={"site": site})
    return envelope.model_copy(update={"data": data})


def _read_sitemap(
    body: bytes, base_url: str, max_bytes: int, max_entries: int, deadline: float
) -> tuple[ParsedSitemap, bool]:
    """Inflate (a gzip body) and parse one sitemap file, on a pool thread: at most
    ``max_entries`` kept, and none left stops the parse at its first entry."""
    inflated, cut = inflate_bounded(body, max_bytes)
    parsed = parse_sitemap(
        inflated,
        base_url,
        max_entries=max_entries,
        deadline=deadline,
        stop_at_cap=max_entries <= 0,
    )
    return parsed, cut
