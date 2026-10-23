"""Fakes and pages shared by the pipeline and API tests."""

import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any

from text_extraction.fetch import FetchError, FetchResult, Validators

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
    ``fetch`` call they belong to)."""

    def __init__(self, answers: dict[str, Answer]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def fetch(self, url: str, **options: Any) -> FetchResult:
        current = url
        hops: list[str] = []
        while True:
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
