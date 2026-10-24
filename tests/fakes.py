"""Fakes and pages shared by the pipeline and API tests."""

import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from text_extraction.browser_options import ArtifactRef
from text_extraction.fetch import FetchError, FetchResult, Validators
from text_extraction.outbound import DomainGate
from text_extraction.render import (
    ArtifactUnavailable,
    CookieData,
    MainResponse,
    NavigationCheck,
    Rendered,
    RenderOptions,
)

ORIGIN = "https://example.org"
PAGE = f"{ORIGIN}/learn/photosynthesis.html?session=secret-token"
ROBOTS = f"{ORIGIN}/robots.txt"

PARAGRAPH = (
    "Photosynthesis is the process by which green plants and some other organisms use "
    "sunlight to synthesize foods from carbon dioxide and water. It generally involves the "
    "green pigment chlorophyll and generates oxygen as a byproduct of the reaction. "
)
ARTICLE = f"""<!doctype html>
<html lang="en">
<head>
<title>Photosynthesis explained</title>
<meta name="description" content="How plants turn light into food.">
<link rel="license" href="https://creativecommons.org/licenses/by-sa/4.0/">
</head>
<body>
<nav><a href="/">Home</a></nav>
<article>
<h1>Photosynthesis explained</h1>
<p>{PARAGRAPH * 3}</p>
<p>{PARAGRAPH * 2} Read <a href="/learn/respiration.html">respiration</a>,
the <a href="https://other.example/plants">plant atlas</a> and the
<a href="/files/worksheet.pdf">worksheet</a>.</p>
</article>
</body>
</html>
"""


def fetched(
    url: str = PAGE,
    body: str | bytes = ARTICLE,
    *,
    status: int = 200,
    content_type: str | None = "text/html",
    headers: dict[str, str] | None = None,
) -> FetchResult:
    content = body.encode() if isinstance(body, str) else body
    return FetchResult(
        url=url,
        final_url=url,
        status=status,
        headers=headers or {},
        content=content,
        content_type=content_type,
        charset="utf-8" if content_type else None,
        elapsed_ms=3,
        redirects=[],
        not_modified=status == 304,
        raw_sha256=hashlib.sha256(content).hexdigest(),
    )


@dataclass(frozen=True)
class Redirect:
    """A redirect answer: the fake fetcher follows it like the real one, hook included."""

    location: str


Answer = FetchResult | FetchError | Redirect | Callable[[Validators | None], Awaitable[FetchResult]]


class FakeFetcher:
    """Canned answers by URL; anything else (robots.txt, tdmrep.json, ai.txt) is a 404.

    ``calls`` has one entry per request, redirect hops included (with the options of the
    ``fetch`` call they belong to). ``before_request`` and ``on_request_sent``, when set, are
    awaited with each request's host as the real fetcher does, before the request is noted."""

    def __init__(self, answers: dict[str, Answer]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.before_request: Callable[[str], Awaitable[None]] | None = None
        self.on_request_sent: Callable[[str], Awaitable[None]] | None = None

    async def fetch(self, url: str, **options: Any) -> FetchResult:
        current = url
        hops: list[str] = []
        while True:
            host = urlsplit(current).hostname or ""
            if self.before_request is not None:
                await self.before_request(host)
            if self.on_request_sent is not None:
                await self.on_request_sent(host)
            self.calls.append((current, options))
            answer = self.answers.get(current, fetched(current, "Not found", status=404))
            if not isinstance(answer, Redirect):
                break
            current = answer.location
            hops.append(current)
            if options.get("on_redirect") is not None:
                await options["on_redirect"](current)
        if isinstance(answer, FetchError):
            raise answer
        if not isinstance(answer, FetchResult):
            answer = await answer(options.get("validators"))
        if hops:
            answer = replace(answer, url=url, final_url=current, redirects=hops)
        return answer

    def page_calls(self) -> list[dict[str, Any]]:
        return [options for url, options in self.calls if url == PAGE]


class RefusingGate:
    """A domain gate that refuses every slot with ``error``."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    @asynccontextmanager
    async def slot(self, host: str, crawl_delay: float | None = None) -> AsyncIterator[None]:
        raise self.error
        yield  # pragma: no cover - makes this a generator

    async def record(self, host: str, *, status: int | None, retry_after: float | None) -> None:
        raise AssertionError("nothing was fetched")


class RecordingGate:
    """A real DomainGate that notes each turn taken and given back, and each outcome."""

    def __init__(self, gate: DomainGate) -> None:
        self.gate = gate
        self.events: list[tuple[str, str]] = []

    @asynccontextmanager
    async def slot(self, host: str, crawl_delay: float | None = None) -> AsyncIterator[None]:
        async with self.gate.slot(host, crawl_delay):
            self.events.append(("take", host))
            try:
                yield
            finally:
                self.events.append(("give back", host))

    async def record(self, host: str, *, status: int | None, retry_after: float | None) -> None:
        self.events.append(("record", host))
        await self.gate.record(host, status=status, retry_after=retry_after)

    async def before_request(self, host: str) -> None:
        await self.gate.before_request(host)

    async def mark_sent(self, host: str) -> None:
        await self.gate.mark_sent(host)

    def held(self) -> list[str]:
        """The hosts whose turn is held now, checking that turns were held one at a time."""
        holding: list[str] = []
        for event, host in self.events:
            if event == "take":
                assert not holding, f"two turns at once: {holding[0]} and {host}"
                holding.append(host)
            elif event == "give back":
                assert holding == [host]
                holding.clear()
        return holding


# --- browser ------------------------------------------------------------------------------------


def response(
    url: str = PAGE,
    *,
    status: int = 200,
    content_type: str | None = "text/html",
    headers: dict[str, str] | None = None,
    redirects: Sequence[str] = (),
) -> MainResponse:
    return MainResponse(
        url=url,
        status=status,
        headers=headers or {},
        content_type=content_type,
        charset="utf-8" if content_type else None,
        redirects=tuple(redirects),
    )


def rendered(
    html: str = ARTICLE,
    *,
    url: str = PAGE,
    responses: Sequence[MainResponse] | None = None,
    screenshot: bytes | None = None,
    screenshot_size: tuple[int, int] | None = None,
    warnings: Sequence[str] = (),
    hosts: Sequence[str] = (),
    cookies: Sequence[CookieData] = (),
) -> Rendered:
    answers = tuple(responses) if responses is not None else (response(url),)
    return Rendered(
        html=html,
        final_url=answers[-1].url if answers else url,
        responses=answers,
        screenshot=screenshot,
        screenshot_size=screenshot_size,
        warnings=tuple(warnings),
        hosts=frozenset(hosts) | {urlsplit(url).hostname or ""},
        cookies=tuple(cookies),
    )


@dataclass(frozen=True)
class RenderCall:
    url: str
    options: RenderOptions
    deadline: float


class FakeRenderer:
    """Returns scripted renders in order (the last one again when they run out).

    Before it returns, it asks ``check`` for each URL of ``navigations`` as a browser would for
    a later main-frame navigation; ``sent`` lists the ones the check let through. An exception
    in place of a render is raised; one from ``check`` propagates. ``during`` runs inside the
    render, where tests can look at the gate."""

    def __init__(
        self,
        *results: Rendered | BaseException,
        navigations: Sequence[str] = (),
        during: Callable[[], Awaitable[None]] | None = None,
        user_agent: str = "FakeBrowser/1.0 text-extraction",
    ) -> None:
        self.results = list(results) or [rendered()]
        self.navigations = list(navigations)
        self.during = during
        self.user_agent = user_agent
        self.calls: list[RenderCall] = []
        self.sent: list[str] = []

    async def render(
        self, url: str, check: NavigationCheck, options: RenderOptions, *, deadline: float
    ) -> Rendered:
        self.calls.append(RenderCall(url, options, deadline))
        if self.during is not None:
            await self.during()
        for target in self.navigations:
            await check(target)
            self.sent.append(target)
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, BaseException):
            raise result
        return result


class FakeArtifacts:
    """Keeps the screenshots it is given; ``refuse`` makes it raise ``ArtifactUnavailable``."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self.screenshots: list[tuple[bytes, int, int]] = []

    async def put_screenshot(self, png: bytes, width: int, height: int) -> ArtifactRef:
        if self.refuse:
            raise ArtifactUnavailable("memory is high")
        self.screenshots.append((png, width, height))
        return ArtifactRef(
            name="screenshot.png",
            url="/v1/jobs/job-1/artifacts/screenshot.png",
            content_type="image/png",
            bytes=len(png),
            width=width,
            height=height,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )


class FakeClearance:
    """Clearance cookies by host, in memory; notes each ``get`` and ``put``."""

    def __init__(self, cookies: dict[str, list[CookieData]] | None = None) -> None:
        self.cookies = dict(cookies or {})
        self.gets: list[str] = []
        self.puts: list[tuple[str, list[CookieData]]] = []

    async def get(self, host: str) -> list[CookieData]:
        self.gets.append(host)
        return list(self.cookies.get(host, []))

    async def put(self, host: str, cookies: list[CookieData]) -> None:
        self.puts.append((host, list(cookies)))
        self.cookies[host] = list(cookies)
