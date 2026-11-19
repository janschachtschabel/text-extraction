"""Live web search check (package 25): ``POST /v1/search`` and the MCP tool ``web_search`` of a
started service whose SearXNG reaches the real sources.

    uv run python -m tests.live.run_search --expect hits --summary-out results/summary_web_search.md
    uv run python -m tests.live.run_search --expect failed --query Chlorophyll \
        --summary-out results/summary_web_search_down.md

``--expect hits`` (SearXNG up): REST answers HTTP 200 and MCP ``isError: false``, both with
``status`` success or partial, at least one hit from ``wikipedia`` on de.wikipedia.org, at least
one hit from another source, and only http(s) URLs. A failed try is repeated up to ``--retries``
times (hits only): each try asks for one more hit (``max_results`` 10, 11, ...), so it is a new
cache key and never the cached answer of the try before. Then a second identical REST call must be
a cache hit (``data.cache: "hit"``). Only ``success`` answers are cached, so when the answer was
``partial`` the check uses a Wikipedia-only search instead.

``--expect failed`` (SearXNG stopped): a query the service has not cached answers, through REST
and MCP, HTTP 200 with ``status: failed``, ``error.code: search_failed`` and
``error.retryable: true``, and MCP ``isError: false``. Tried once, never repeated; a 5xx is
always a failure.

The exit code is 1 when a check fails. The Markdown summary lists each call with its hits per
source and ``sources_failed``. See ``tests/live/README.md``.
"""

import argparse
import asyncio
import os
import sys
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

DEFAULT_QUERY = "Photosynthese"
DEFAULT_LANGUAGE = "de"
DEFAULT_RETRIES = 3
DEFAULT_PAUSE_SECONDS = 10.0
# One search takes at most SEARCH_TIMEOUT_SECONDS (10 s) at the service.
DEFAULT_CLIENT_TIMEOUT_SECONDS = 60.0
FIRST_MAX_RESULTS = 10
MCP_ACCEPT = "application/json, text/event-stream"
WIKIPEDIA_HOST = "de.wikipedia.org"


@dataclass
class Call:
    """One search as sent and answered: the envelope (REST's body or MCP's structuredContent)."""

    label: str
    http_status: int
    envelope: dict[str, Any] | None
    is_error: bool | None = None  # MCP only
    problems: list[str] = field(default_factory=list)


@dataclass
class Report:
    calls: list[Call] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _hits(envelope: dict[str, Any] | None) -> list[dict[str, Any]]:
    data = (envelope or {}).get("data")
    results = data.get("results") if isinstance(data, dict) else None
    return [hit for hit in results or [] if isinstance(hit, dict)]


def _is_web_url(url: object) -> bool:
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def hit_problems(envelope: dict[str, Any] | None) -> list[str]:
    """What keeps ``envelope`` from being an answer with the expected hits."""
    if not isinstance(envelope, dict):
        return ["no envelope"]
    problems: list[str] = []
    if envelope.get("status") not in ("success", "partial"):
        error = envelope.get("error") or {}
        problems.append(f"status {envelope.get('status')} ({error.get('code')})")
    hits = _hits(envelope)
    wikipedia = [
        hit
        for hit in hits
        if "wikipedia" in (hit.get("sources") or [])
        and urlsplit(str(hit.get("url"))).hostname == WIKIPEDIA_HOST
    ]
    if not wikipedia:
        problems.append(f"no hit from wikipedia on {WIKIPEDIA_HOST}")
    if not [hit for hit in hits if set(hit.get("sources") or []) - {"wikipedia"}]:
        problems.append("no hit from a source other than wikipedia")
    bad = [hit.get("url") for hit in hits if not _is_web_url(hit.get("url"))]
    if bad:
        problems.append(f"URLs that are not http(s): {bad}")
    return problems


def failure_problems(envelope: dict[str, Any] | None) -> list[str]:
    """What keeps ``envelope`` from being the retryable ``search_failed`` of a SearXNG that is
    down."""
    if not isinstance(envelope, dict):
        return ["no envelope"]
    problems: list[str] = []
    error = envelope.get("error") or {}
    if envelope.get("status") != "failed":
        problems.append(f"status {envelope.get('status')}, expected failed")
    if error.get("code") != "search_failed":
        problems.append(f"error.code {error.get('code')}, expected search_failed")
    if error.get("retryable") is not True:
        problems.append("error.retryable is not true")
    return problems


async def rest_search(client: httpx.AsyncClient, body: dict[str, Any], label: str) -> Call:
    response = await client.post("/v1/search", json=body)
    try:
        envelope = response.json()
    except ValueError:
        envelope = None
    call = Call(label, response.status_code, envelope if isinstance(envelope, dict) else None)
    if response.status_code != 200:
        call.problems.append(f"HTTP {response.status_code}, expected 200")
    return call


async def mcp_search(client: httpx.AsyncClient, arguments: dict[str, Any], label: str) -> Call:
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "web_search", "arguments": arguments},
    }
    response = await client.post("/mcp", json=message, headers={"Accept": MCP_ACCEPT})
    try:
        answer = response.json()
    except ValueError:
        answer = None
    result = answer.get("result") if isinstance(answer, dict) else None
    envelope = result.get("structuredContent") if isinstance(result, dict) else None
    is_error = result.get("isError") if isinstance(result, dict) else None
    call = Call(
        label, response.status_code, envelope if isinstance(envelope, dict) else None, is_error
    )
    if response.status_code != 200:
        call.problems.append(f"HTTP {response.status_code}, expected 200")
    if not isinstance(result, dict):
        error = answer.get("error") if isinstance(answer, dict) else None
        call.problems.append(f"no tools/call result ({error})")
    elif is_error is not False:
        call.problems.append(f"isError {is_error}, expected false")
    return call


Sleep = Callable[[float], Awaitable[None]]


async def check_hits(
    client: httpx.AsyncClient,
    query: str,
    language: str,
    retries: int = DEFAULT_RETRIES,
    pause_seconds: float = DEFAULT_PAUSE_SECONDS,
    sleep: Sleep = asyncio.sleep,
) -> Report:
    report = Report()
    rest: Call | None = None
    for attempt in range(retries + 1):
        if attempt:
            await sleep(pause_seconds)
        body = {"query": query, "language": language, "max_results": FIRST_MAX_RESULTS + attempt}
        rest = await rest_search(client, body, f"REST try {attempt + 1}")
        rest.problems += hit_problems(rest.envelope)
        mcp = await mcp_search(client, body, f"MCP try {attempt + 1}")
        mcp.problems += hit_problems(mcp.envelope)
        report.calls += [rest, mcp]
        if not rest.problems and not mcp.problems:
            break
    else:
        report.problems.append(f"no try of {retries + 1} had the expected hits")
        return report
    assert rest is not None  # the loop ran at least once
    if rest.envelope and rest.envelope.get("status") == "success":
        body = {"query": query, "language": language, "max_results": FIRST_MAX_RESULTS + attempt}
    else:
        body = {"query": query, "language": language, "sources": ["wikipedia"]}
        report.calls.append(await rest_search(client, body, "REST Wikipedia only"))
    again = await rest_search(client, body, "REST again (cache)")
    data = (again.envelope or {}).get("data") or {}
    if data.get("cache") != "hit":
        again.problems.append(f"data.cache {data.get('cache')}, expected hit")
    report.calls.append(again)
    report.problems += [f"{again.label}: {problem}" for problem in again.problems]
    return report


async def check_failure(client: httpx.AsyncClient, query: str, language: str) -> Report:
    report = Report()
    body = {"query": query, "language": language}
    for call in (
        await rest_search(client, body, "REST, SearXNG stopped"),
        await mcp_search(client, body, "MCP, SearXNG stopped"),
    ):
        call.problems += failure_problems(call.envelope)
        report.calls.append(call)
        report.problems += [f"{call.label}: {problem}" for problem in call.problems]
    return report


def summarize(title: str, report: Report) -> str:
    """The Markdown summary: one row per call, then the hits per source of each call."""
    lines = [
        f"#### {title}",
        "",
        "| Call | HTTP | Status | Cache | Hits | Hits per source | Sources failed | Problems |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for call in report.calls:
        envelope = call.envelope or {}
        data = envelope.get("data") or {}
        error = envelope.get("error") or {}
        per_source = Counter(
            source for hit in _hits(envelope) for source in hit.get("sources") or []
        )
        failed = data.get("sources_failed") or error.get("details") or []
        status = str(envelope.get("status"))
        if error:
            status += f" ({error.get('code')})"
        lines.append(
            "| "
            + " | ".join(
                [
                    call.label,
                    str(call.http_status),
                    status,
                    str(data.get("cache", "")),
                    str(len(_hits(envelope))),
                    ", ".join(f"{name} {count}" for name, count in sorted(per_source.items()))
                    or "-",
                    ", ".join(f"{f.get('source')}: {f.get('reason')}" for f in failed) or "-",
                    "; ".join(call.problems).replace("|", "/") or "-",
                ]
            )
            + " |"
        )
    lines += ["", "**Result:** " + ("ok" if not report.problems else "FAILED"), ""]
    lines += [f"- {problem}" for problem in report.problems]
    return "\n".join(lines) + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--api-key", default=os.environ.get("LIVE_API_KEY"))
    parser.add_argument("--expect", choices=["hits", "failed"], required=True)
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--language", default=DEFAULT_LANGUAGE)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--pause", type=float, default=DEFAULT_PAUSE_SECONDS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_CLIENT_TIMEOUT_SECONDS)
    parser.add_argument("--summary-out", type=Path, default=None)
    return parser.parse_args(argv)


def _transport() -> httpx.AsyncBaseTransport | None:
    """The client's transport: the network (None); the tests put a fake service here."""
    return None


async def amain(args: argparse.Namespace) -> int:
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    async with httpx.AsyncClient(
        base_url=args.base_url, headers=headers, timeout=args.timeout, transport=_transport()
    ) as client:
        if args.expect == "hits":
            report = await check_hits(client, args.query, args.language, args.retries, args.pause)
            title = f"Web search: {args.query!r} ({args.language})"
        else:
            report = await check_failure(client, args.query, args.language)
            title = f"Web search with SearXNG stopped: {args.query!r}"
    summary = summarize(title, report)
    print(summary)
    if args.summary_out is not None:
        args.summary_out.parent.mkdir(parents=True, exist_ok=True)
        args.summary_out.write_text(summary, encoding="utf-8")
    return 1 if report.problems else 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(amain(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
