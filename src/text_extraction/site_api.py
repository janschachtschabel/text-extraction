"""``POST /v1/site-structure`` (ruling P9-S13): queue the crawl of a site as a ``site`` job.

The route checks the request, opens the job as ``POST /v1/batch`` does (``jobs_api.open_job``:
the memory guard, ``BATCH_MAX_OPEN_PER_CLIENT`` shared with batches), makes item 0 the start
page in the crawl store and queues it on the extract pool. It never crawls inline: the workers
run the items (``site/runner.py``) and append what they find to the job, so its ``total``
grows while it runs. The body is bounded by ``REQUEST_MAX_BODY_BYTES`` like any other.
"""

from typing import Annotated, Any, Final

import structlog
from fastapi import APIRouter, Body, Depends, Request
from fastapi.exceptions import RequestValidationError
from redis.exceptions import RedisError
from starlette.datastructures import State

from text_extraction.auth import Principal, require_role
from text_extraction.compliance import resolve_mode
from text_extraction.envelope import Envelope
from text_extraction.job_models import JobAccepted, JobOptions
from text_extraction.jobs import JobLost
from text_extraction.jobs_api import (
    backend_unavailable,
    discard_job,
    open_job,
    require_jobs_enabled,
)
from text_extraction.netguard import InvalidUrlError, validate_url
from text_extraction.ratelimit import rate_limited
from text_extraction.scheduler import FairQueue, domain_of
from text_extraction.settings import Settings
from text_extraction.site.store import SiteStore
from text_extraction.site.types import SiteStructureRequest
from text_extraction.tracing import current_trace_id

__all__ = ["PATH", "create_site_structure", "router", "start_site"]

_log = structlog.get_logger(__name__)

PATH = "/v1/site-structure"

router = APIRouter()

_EXAMPLES: Final[dict[str, Any]] = {
    "site": {
        "summary": "Map a site: links two clicks deep, robots.txt and sitemaps",
        "value": {"url": "https://www.example.org/", "depth": 2, "max_pages": 100},
    },
    "section": {
        "summary": "Only the pages under one directory, without sitemaps",
        "value": {
            "url": "https://www.example.org/docs/",
            "depth": 3,
            "max_pages": 500,
            "scope": "path",
            "sitemaps": False,
        },
    },
    "sitemaps": {
        "summary": "Only the start page and the sitemaps",
        "value": {"url": "https://www.example.org/", "depth": 0},
    },
}


def _invalid(field: str, message: str) -> RequestValidationError:
    return RequestValidationError([{"type": "value_error", "loc": ("body", field), "msg": message}])


def _check(body: SiteStructureRequest, settings: Settings) -> None:
    """422 for a URL that is no fetch target and for a depth or page count over the
    deployment's bounds (the model only knows the widest ones)."""
    try:
        validate_url(body.url)
    except InvalidUrlError as error:
        raise _invalid("url", str(error)) from None
    if body.depth > settings.site_max_depth:
        raise _invalid("depth", f"depth may be at most {settings.site_max_depth} (SITE_MAX_DEPTH).")
    if body.max_pages > settings.site_max_pages:
        raise _invalid(
            "max_pages", f"max_pages may be at most {settings.site_max_pages} (SITE_MAX_PAGES)."
        )


@router.post(
    PATH,
    status_code=202,
    summary="Map the structure of a site as a job",
    dependencies=[Depends(require_jobs_enabled), Depends(rate_limited)],
)
async def create_site_structure(
    body: Annotated[SiteStructureRequest, Body(openapi_examples=_EXAMPLES)],
    request: Request,
    principal: Annotated[Principal, Depends(require_role("user"))],
) -> Envelope[JobAccepted]:
    """Crawl a site's links, robots.txt and sitemaps as a `site` job; answers 202 with its id
    and `status_url`.

    The job starts with one item, the start page; every page, robots.txt and sitemap file the
    crawl finds becomes an item of its own, so the job's `total` grows while it runs. Poll
    `GET /v1/jobs/{id}` for progress and the items: each result has `data.site` with its `role`
    (`page`, `robots`, `sitemap`, `sitemap_part`), `depth`, `parent`, and the page's typed
    `links` (each with what the crawl did with it, `crawl`, and the item it led to, `target`)
    or the sitemap's entries. Results hold the structure only: `content` is null; send the URLs
    you want as text to `POST /v1/batch`.

    Links are followed `depth` clicks deep (at most `SITE_MAX_DEPTH`) on the start page's host
    (a leading `www.` ignored), with `scope: path` also only under the start URL's directory,
    for at most `max_pages` page items (at most `SITE_MAX_PAGES`). Pages are fetched in fast
    mode, with the service's user agent, within the per-host limits, and with the compliance
    checks of `compliance`: under `enforce` what robots.txt disallows or whose TDM rights are
    reserved is not fetched (its link says `blocked`).

    4xx answers: an invalid body, a URL that is no http(s) URL, or a `depth` or `max_pages` over
    the deployment's bounds (422), a compliance mode the caller may not choose (403),
    `BATCH_MAX_OPEN_PER_CLIENT` unfinished batches and site jobs together (429, retryable).
    503 when Valkey is unavailable, jobs are disabled, or Valkey's memory is at
    `VALKEY_MEMORY_GUARD_RATIO` of its maximum (retryable).
    """
    return await start_site(request.app.state, principal, body)


async def start_site(
    state: State, principal: Principal, body: SiteStructureRequest
) -> Envelope[JobAccepted]:
    """The core of ``POST /v1/site-structure`` (ruling P10-S1).

    Caller's checks (this core runs none of them): ``principal_for`` (the user role, which every
    key has), ``check_jobs_enabled`` (503), then one ``check_rate`` hit."""
    settings: Settings = state.settings
    sites: SiteStore = state.site_store
    queue: FairQueue = state.job_queue

    _check(body, settings)
    site = body.options()
    options = JobOptions(
        mode="fast",
        format="text",
        metadata=site.metadata,
        compliance=resolve_mode(body.compliance, principal, settings),
        cache=site.cache,
        ocr="never",
        site=site,
    )
    try:
        job_id = await open_job(state, principal, "site", options, [body.url])
        try:
            await sites.init(job_id, body.url)
            # Enqueued exactly once and never retried, as a batch.
            await queue.enqueue_items(job_id, principal.name, [(0, body.url)])
        except BaseException:
            await discard_job(state, principal, job_id)
            raise
    except (RedisError, OSError, JobLost) as error:
        raise backend_unavailable(error) from None
    # Counted only here: a job that was discarded above never reaches a worker.
    state.job_metrics.created.labels(kind="site").inc()
    _log.info(
        "site_accepted",
        job_id=job_id,
        host=domain_of(body.url),
        depth=site.depth,
        max_pages=site.max_pages,
    )
    data = JobAccepted(
        id=job_id, kind="site", state="queued", total=1, status_url=f"/v1/jobs/{job_id}"
    )
    return Envelope(status="success", data=data, trace_id=current_trace_id())
