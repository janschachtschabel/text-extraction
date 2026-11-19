"""``POST /v1/search`` (package 25): a web search through the operator's SearXNG.

The route checks the request against the deployment's bounds (``SEARCH_MAX_RESULTS``,
``SEARCH_SOURCES``) and asks SearXNG through ``search.client.search``, which answers from the
Valkey cache when it can. A SearXNG that is down, slow or refusing is the search's failure, not
ours: 200 with ``status: failed`` and ``search_failed``. The app keeps one ``httpx.AsyncClient``
for SearXNG in ``app.state.search_http`` (``create_search_client``; the lifespan closes it).
"""

from typing import Annotated, Any, Final

import httpx
from fastapi import APIRouter, Body, Depends, Request
from fastapi.exceptions import RequestValidationError
from starlette.datastructures import State

from text_extraction.auth import Principal, authenticate, require_role
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.feature_access import check_feature_access
from text_extraction.ratelimit import rate_limited
from text_extraction.search.client import search
from text_extraction.search.types import SearchRequest, SearchResult
from text_extraction.settings import Settings
from text_extraction.tracing import current_trace_id

__all__ = [
    "PATH",
    "SEARCH_DISABLED_MESSAGE",
    "check_search_enabled",
    "create_search_client",
    "router",
    "run_search",
]

PATH = "/v1/search"
SEARCH_DISABLED_MESSAGE = "Web search is not enabled on this service."

router = APIRouter()

_EXAMPLES: Final[dict[str, Any]] = {
    "simple": {
        "summary": "Search all configured sources",
        "value": {"query": "photosynthesis"},
    },
    "narrow": {
        "summary": "Recent German hits from two sources",
        "value": {
            "query": "Photosynthese",
            "language": "de",
            "max_results": 20,
            "time_range": "year",
            "sources": ["wikipedia", "wikibooks"],
        },
    },
}


def create_search_client(settings: Settings) -> httpx.AsyncClient | None:
    """The client for SearXNG while ``SEARCH_ENABLED``, else None. ``SEARCH_URL`` is the
    operator's internal service, not a caller's target, so the SSRF guard of the fetcher does
    not apply to it; nor do proxies from the environment (``trust_env=False``) or redirects."""
    if not settings.search_enabled:
        return None
    return httpx.AsyncClient(
        timeout=httpx.Timeout(settings.search_timeout_seconds),
        follow_redirects=False,
        trust_env=False,
    )


def check_search_enabled(settings: Settings) -> None:
    """400 ``invalid_request`` while ``SEARCH_ENABLED`` is false, as for every switch that is off
    (C12)."""
    if not settings.search_enabled:
        raise ServiceError(ErrorCode.INVALID_REQUEST, SEARCH_DISABLED_MESSAGE, 400)


async def require_search_enabled(
    request: Request, _principal: Annotated[Principal, Depends(authenticate)]
) -> None:
    """Dependency: 400 while ``SEARCH_ENABLED`` is false (after authentication)."""
    check_search_enabled(request.app.state.settings)


def _invalid(field: str, message: str) -> RequestValidationError:
    return RequestValidationError([{"type": "value_error", "loc": ("body", field), "msg": message}])


def _check(body: SearchRequest, settings: Settings) -> None:
    """422 for a ``max_results`` over ``SEARCH_MAX_RESULTS``, for an empty ``sources`` list (it
    would read as every source) and for a source that is not in ``SEARCH_SOURCES`` (compared in
    lower case, around blanks). A name with a comma or an empty one is never in
    ``SEARCH_SOURCES``, so it cannot split SearXNG's comma list ``engines``."""
    if body.max_results > settings.search_max_results:
        raise _invalid(
            "max_results",
            f"max_results may be at most {settings.search_max_results} (SEARCH_MAX_RESULTS).",
        )
    allowed = settings.search_sources
    if body.sources == []:
        raise _invalid("sources", "Name at least one source in sources, or leave sources out.")
    if body.sources and any(name.strip().lower() not in allowed for name in body.sources):
        raise _invalid(
            "sources",
            f"sources may only name these sources (SEARCH_SOURCES): {', '.join(allowed)}.",
        )


@router.post(
    PATH,
    summary="Search the web",
    name="search",  # the operation id (C13); `search` here is the client's function
    dependencies=[Depends(require_search_enabled), Depends(rate_limited)],
)
async def search_web(
    body: Annotated[SearchRequest, Body(openapi_examples=_EXAMPLES)],
    request: Request,
    principal: Annotated[Principal, Depends(require_role("user"))],
) -> Envelope[SearchResult]:
    """Search the web through the service's SearXNG and answer the hits: `url`, `title`,
    `snippet`, the `sources` that found each, `published` and `score`, besides `suggestions`.

    Without `sources` every source of `SEARCH_SOURCES` is asked; `max_results` (at most
    `SEARCH_MAX_RESULTS`), `language`, `page` and `time_range` narrow the search. Answers are
    cached for `SEARCH_CACHE_SECONDS` (`data.cache` says `hit`, `miss` or `bypass`). To read a
    hit's text, send its `url` to `POST /v1/extract` or several to `POST /v1/batch`.

    A search whose sources did not all answer is `partial` (also without a hit) and lists them
    in `data.sources_failed`; when the search service is unreachable or slow, or no source asked
    answered (`error.details` lists each), the answer is 200 with `status: failed` and
    `search_failed` (retryable; not when SearXNG refuses JSON answers, a setting of the
    service).

    4xx answers: an invalid body, a `max_results` over `SEARCH_MAX_RESULTS`, an empty `sources`
    list or a source outside `SEARCH_SOURCES` (422; for a source outside, the message lists the
    allowed names), a user key while `SEARCH_ALLOW_USERS` is false (403), web search not enabled
    (400).
    """
    return await run_search(request.app.state, principal, body)


async def run_search(
    state: State, principal: Principal, body: SearchRequest
) -> Envelope[SearchResult]:
    """The core of ``POST /v1/search`` (ruling P10-S1): the switch (400 while
    ``SEARCH_ENABLED`` is false, so a caller that skips its own check never reaches the missing
    client), the feature access (403 for a user key while ``SEARCH_ALLOW_USERS`` is false), the
    deployment's bounds (422), then the search.

    Caller's checks (this core runs none of them): ``principal_for`` (the user role, which every
    key has) and one ``check_rate`` hit. The REST route also runs ``check_search_enabled`` as a
    dependency, so its 400 comes before the rate hit; the MCP call guard counts the hit first."""
    settings: Settings = state.settings
    check_search_enabled(settings)
    check_feature_access(principal, settings, search=True)
    _check(body, settings)
    http: httpx.AsyncClient = state.search_http
    envelope = await search(body, settings, http, state.valkey)
    envelope.trace_id = current_trace_id()
    return envelope
