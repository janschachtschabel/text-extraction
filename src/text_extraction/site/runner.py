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
- **robots** and **sitemap**: ``SitemapItems._robots`` and ``SitemapItems._sitemap``
  (``site/sitemap_items.py``), the base class of ``SiteRunner``.

``extend`` takes at most ``EXTEND_BATCH`` candidates a call: an item sends batches 0, 1, ... in
order, each with the same keys in the same order on a rerun (the keys depend on the page, the scope
and the depth only; whether a candidate may be appended also depends on the memory guard, which the
stored answers make moot), and queues each batch's new items as it is answered. A rerun of an item
(a lost lease) gets the stored answers, appends nothing and queues those items again (a copy of a
finished one is skipped by the worker), the items of stored batches it no longer sends included. A
batch whose stored answer has another length than the batch (the page changed) leaves its links
with their preliminary ``crawl`` and no ``target``. ``requeue_children`` queues the children again
for an exhausted item before it fails. While the memory guard is high an item appends nothing to
crawl (its candidates are looked up only): its links (or children) say ``memory_guard`` and it has
the warning ``site_memory_guard``. A sitemap's further parts are results, which are stored as every
result is (the job store's writes are not guarded, so accepted jobs complete).

Metrics: ``text_extraction_site_items_total{role, outcome}``,
``text_extraction_site_links_total{crawl}``, ``text_extraction_site_caps_total{cap}``. The log
line ``site_item_done`` has the role, the host and counts, never a path or a query.
"""

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal
from weakref import WeakKeyDictionary

import structlog
from prometheus_client import CollectorRegistry, Counter

from text_extraction.compliance import ComplianceReport
from text_extraction.conversion_pool import ConversionPool
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.extract_models import ExtractRequest, ExtractResult, failed_extract
from text_extraction.jobs import JobRecord, JobStore, job_principal
from text_extraction.pipeline import LINKS_TRUNCATED_WARNING, Pipeline
from text_extraction.result_cache import MemoryPressure
from text_extraction.scheduler import FairQueue, QueueItem
from text_extraction.settings import Settings
from text_extraction.site.follow import judge_links, unique_candidates
from text_extraction.site.sitemap_items import (
    _SIZE_MARGIN,
    _WIDEST_CRAWL,
    _WIDEST_TARGET,
    MAX_SITEMAP_NESTING,
    SITE_MEMORY_GUARD_WARNING,
    SITEMAP_FILES_TRUNCATED_WARNING,
    SITEMAP_NOT_FOUND_WARNING,
    SITEMAP_TRUNCATED_WARNING,
    SITEMAP_URLS_TRUNCATED_WARNING,
    SitemapItems,
    _decided,
    _Extended,
    _fitting,
    _json_size,
    _no_timings,
    _origin,
    _Outcome,
    site_result,
)
from text_extraction.site.store import EXTEND_BATCH, Candidate, Finished, SiteContext, SiteStore
from text_extraction.site.types import (
    SiteInfo,
    SiteLink,
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

MAX_PAGES_REACHED_WARNING = "max_pages_reached"

_UNKNOWN_ITEM_MESSAGE = "The crawl has no record of this item."

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


def _robots_url(url: str) -> str:
    robots = _origin(url) + "/robots.txt"
    return normalize_url(robots) or robots


def _with_warnings(result: ExtractResult, warnings: Sequence[str]) -> list[str]:
    merged = list(result.warnings)
    merged += [warning for warning in warnings if warning not in merged]
    return merged


class SiteRunner(SitemapItems):
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
