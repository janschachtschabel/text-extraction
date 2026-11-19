"""The SearXNG client: one JSON search, mapped to an ``Envelope[SearchResult]``.

SearXNG is the operator's service (``SEARCH_URL``), not a caller's target, so the SSRF guard does
not apply. A finished ``success`` answer is cached in Valkey for
``SEARCH_CACHE_SECONDS`` under ``search:{<sha256>}``; the hash covers every parameter that
changes the answer. A Valkey problem never fails a search: a read acts as a miss, a write is
dropped, both with one log line that carries no query. Errors never echo the query or the URL."""

import hashlib
import html
import json
import math
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
import structlog
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from text_extraction import __version__
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.search.types import SearchHit, SearchRequest, SearchResult, SourceFailure
from text_extraction.settings import Settings
from text_extraction.tracing import outbound_trace_headers

_log = structlog.get_logger(__name__)


def _engines(request: SearchRequest, settings: Settings) -> list[str]:
    """The engines to ask: the request's (deduplicated, in lower case) or the configured ones."""
    if request.sources:
        return list(dict.fromkeys(name.strip().lower() for name in request.sources))
    return list(settings.search_sources)


def cache_key(request: SearchRequest, engines: list[str]) -> str:
    """Key of the answer to ``request`` asked of ``engines``. Spacing of the query and the order
    of the engines do not matter; every other parameter does."""
    canonical = json.dumps(
        {
            "q": " ".join(request.query.split()),
            "language": request.language,
            "page": request.page,
            "time_range": request.time_range,
            "engines": sorted(engines),
            "max_results": request.max_results,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    digest = hashlib.sha256(f"{__version__}\0{canonical}".encode()).hexdigest()
    return f"search:{{{digest}}}"


def _failed(
    message: str, *, retryable: bool = True, details: list[dict[str, Any]] | None = None
) -> Envelope[SearchResult]:
    return Envelope[SearchResult](
        status="failed",
        error=ErrorInfo(
            code=ErrorCode.SEARCH_FAILED, message=message, retryable=retryable, details=details
        ),
    )


def _is_web_url(url: object) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url.strip())
        return parts.scheme.lower() in {"http", "https"} and bool(parts.hostname)
    except ValueError:
        return False


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


# A tag-like token: Crossref and OpenAlex hand on JATS XML (<jats:p>), Wikipedia marks the match
# with <span>. "a < b" is no tag: a letter must follow the bracket.
_TAG = re.compile(r"</?([A-Za-z][\w:.-]*)(?:\s[^<>]*)?/?>")
_SPACE = re.compile(r"\s+")
# Tags that end a block of text and so stand for a space; any other tag (<span>, <i>) is dropped
# without one, so a highlighted part of a word stays in the word.
_BLOCK_TAGS = frozenset({"p", "br", "div", "li", "title", "sec", "list-item", "abstract", "label"})


def _tag_gap(match: re.Match[str]) -> str:
    return " " if match.group(1).rsplit(":", 1)[-1].lower() in _BLOCK_TAGS else ""


def _plain(value: object) -> str | None:
    """A title or snippet as plain text: tags dropped, entities decoded, white space collapsed."""
    if not isinstance(value, str):
        return None
    return _SPACE.sub(" ", html.unescape(_TAG.sub(_tag_gap, value))).strip() or None


def _hit(raw: Any) -> SearchHit | None:
    if not isinstance(raw, dict) or not _is_web_url(raw.get("url")):
        return None
    url = raw["url"].strip()
    engines = raw.get("engines")
    names = [e for e in engines if isinstance(e, str)] if isinstance(engines, list) else []
    if not names and isinstance(raw.get("engine"), str):
        names = [raw["engine"]]
    score = raw.get("score")
    return SearchHit(
        url=url,
        title=_plain(raw.get("title")) or url,
        snippet=_plain(raw.get("content")),
        sources=names,
        published=_text(raw.get("publishedDate")),
        score=float(score)
        if isinstance(score, int | float) and not isinstance(score, bool)
        else None,
    )


def _rank(raw: Any) -> float:
    """The best position the hit had in any source's own list (1 = a source's top hit); a hit
    without positions ranks after every hit that has one."""
    positions = raw.get("positions") if isinstance(raw, dict) else None
    ranks = [
        p
        for p in (positions if isinstance(positions, list) else [])
        if isinstance(p, int) and not isinstance(p, bool) and p > 0
    ]
    return float(min(ranks)) if ranks else math.inf


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _page_key(url: str) -> str:
    """One key per page: the scheme, its default port, ``www.``, the case of the host, a trailing
    slash, the order of the query, ``utm_`` parameters and a plain fragment (``#top``) do not
    matter. A fragment that is a route of a single-page app (``#/p``, ``#!p``) does."""
    parts = urlsplit(url)
    try:
        port_number = parts.port
        port = (
            ""
            if port_number in (None, _DEFAULT_PORTS.get(parts.scheme.lower()))
            else f":{port_number}"
        )
    except ValueError:  # a port that is no number: kept as written
        port = ":" + parts.netloc.rpartition(":")[2]
    host = (parts.hostname or "").lower().removeprefix("www.")
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_")
        )
    )
    route = f"#{parts.fragment}" if parts.fragment.startswith(("/", "!")) else ""
    return f"{host}{port}{parts.path.rstrip('/')}?{query}{route}"


def _address_rank(url: str) -> tuple[bool, bool]:
    """Lower is better: an https address before an http one, one without credentials first."""
    parts = urlsplit(url)
    return (parts.scheme.lower() != "https", "@" in parts.netloc)


def _merge(first: SearchHit, other: SearchHit) -> SearchHit:
    """``first`` (the better ranked) with the sources of ``other`` added and its gaps filled; the
    address is the better one of the two (``first``'s when they are equally good)."""
    scores = [s for s in (first.score, other.score) if s is not None]
    return first.model_copy(
        update={
            "url": min((first.url, other.url), key=_address_rank),
            "sources": list(dict.fromkeys([*first.sources, *other.sources])),
            "snippet": first.snippet or other.snippet,
            "published": first.published or other.published,
            "score": max(scores) if scores else None,
        }
    )


def _ranked_hits(raw_results: list[Any]) -> list[SearchHit]:
    """The hits in round-robin order across the sources (every source's first hit, then every
    source's second, ...; SearXNG's order within a rank) with duplicates of a page merged."""
    ranked: dict[str, tuple[float, int, SearchHit]] = {}
    for index, raw in enumerate(raw_results):
        hit = _hit(raw)
        if hit is None:
            continue
        rank = _rank(raw)
        key = _page_key(hit.url)
        seen = ranked.get(key)
        if seen is None:
            ranked[key] = (rank, index, hit)
        elif (rank, index) < seen[:2]:
            ranked[key] = (rank, index, _merge(hit, seen[2]))
        else:
            ranked[key] = (seen[0], seen[1], _merge(seen[2], hit))
    return [hit for _rank_, _index, hit in sorted(ranked.values(), key=lambda e: e[:2])]


def _failures(raw: Any) -> list[SourceFailure]:
    failures: list[SourceFailure] = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, list | tuple) and entry and isinstance(entry[0], str):
                reason = entry[1] if len(entry) > 1 and isinstance(entry[1], str) else "failed"
                failures.append(SourceFailure(source=entry[0], reason=reason))
    return failures


def _map(body: Any, request: SearchRequest, engines: list[str]) -> Envelope[SearchResult]:
    if not isinstance(body, dict):
        return _failed("The search service answered in an unexpected format.")
    raw_results = body.get("results")
    hits = _ranked_hits(raw_results if isinstance(raw_results, list) else [])[: request.max_results]
    failures = _failures(body.get("unresponsive_engines"))
    suggestions = body.get("suggestions")
    # SearXNG reports an engine by the name it was asked for (``engines=``); compared in lower
    # case. Only when every asked engine failed is the search failed; the failures then travel in
    # error.details, as a failed answer has no data. No hit with some failures is an empty
    # partial answer: the query may simply have no hits.
    failed_names = {failure.source.strip().lower() for failure in failures}
    if not hits and failures and failed_names.issuperset(engines):
        return _failed(
            "No search source answered.",
            details=[failure.model_dump() for failure in failures],
        )
    result = SearchResult(
        query=request.query,
        results=hits,
        suggestions=[s for s in suggestions if isinstance(s, str)]
        if isinstance(suggestions, list)
        else [],
        sources_failed=failures,
    )
    return Envelope[SearchResult](status="partial" if failures else "success", data=result)


async def _read_cache(valkey: Redis | None, key: str) -> Envelope[SearchResult] | None:
    if valkey is None:
        return None
    try:
        raw = await valkey.get(key)
    except (RedisError, OSError) as error:
        _log.warning("search_cache_read_failed", error=type(error).__name__)
        return None
    if raw is None:
        return None
    try:
        envelope = Envelope[SearchResult].model_validate_json(raw)
    except ValidationError:
        _log.warning("search_cache_entry_unreadable")
        return None
    if envelope.data is None or envelope.status == "failed":
        return None
    envelope.data.cache = "hit"
    return envelope


async def _write_cache(
    valkey: Redis | None, key: str, envelope: Envelope[SearchResult], seconds: int
) -> None:
    if valkey is None:
        return
    try:
        await valkey.set(key, envelope.model_dump_json(), ex=seconds)
    except (RedisError, OSError) as error:
        _log.warning("search_cache_write_failed", error=type(error).__name__)


async def search(
    request: SearchRequest,
    settings: Settings,
    http: httpx.AsyncClient,
    valkey: Redis | None,
) -> Envelope[SearchResult]:
    """Ask SearXNG, or answer from the cache. Never raises for a failure of the backend."""
    engines = _engines(request, settings)
    caching = settings.search_cache_seconds > 0 and valkey is not None
    key = cache_key(request, engines)
    if caching and (cached := await _read_cache(valkey, key)) is not None:
        return cached

    params = {
        "q": request.query,
        "format": "json",
        "pageno": str(request.page),
        "engines": ",".join(engines),
    }
    if request.language:
        params["language"] = request.language
    if request.time_range:
        params["time_range"] = request.time_range
    try:
        response = await http.get(
            f"{settings.search_url}/search",
            params=params,
            headers={**outbound_trace_headers(), "Accept": "application/json"},
            timeout=settings.search_timeout_seconds,
        )
    except httpx.TimeoutException:
        return _failed("The search service did not answer in time.")
    except httpx.HTTPError as error:
        _log.warning("search_backend_unreachable", error=type(error).__name__)
        return _failed("The search service is not reachable.")

    if response.status_code == 400:
        # SearXNG refuses a parameter it does not accept; asking again changes nothing.
        return _failed("The search service refused the request (HTTP 400).", retryable=False)
    if response.status_code == 403:
        return _failed(
            "The search service refused the request: enable the json format in SearXNG "
            "(`search.formats` in its settings.yml).",
            retryable=False,
        )
    if response.status_code != 200:
        _log.warning("search_backend_http_error", status=response.status_code)
        return _failed(f"The search service answered HTTP {response.status_code}.")
    try:
        body = response.json()
    except ValueError:
        return _failed("The search service did not answer with JSON.")

    envelope = _map(body, request, engines)
    if envelope.data is None:
        return envelope
    if not caching:
        envelope.data.cache = "bypass"
        return envelope
    envelope.data.cache = "miss"
    if envelope.status == "success":  # a partial answer is asked again, not kept for the TTL
        await _write_cache(valkey, key, envelope, settings.search_cache_seconds)
    return envelope
