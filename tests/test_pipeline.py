import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis
from prometheus_client import CollectorRegistry
from structlog.testing import capture_logs

from text_extraction.auth import Principal
from text_extraction.compliance import ComplianceChecker
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.fetch import Fetcher, FetchError, FetchResult, Validators
from text_extraction.fingerprint import FingerprintStore
from text_extraction.outbound import CircuitOpen, DomainGate, OutboundBusy
from text_extraction.pipeline import ExtractRequest, ExtractResult, Pipeline, create_pipeline
from text_extraction.result_cache import ResultCache
from text_extraction.settings import Settings

ORIGIN = "https://example.org"
PAGE = f"{ORIGIN}/learn/photosynthesis.html?session=secret-token"
ROBOTS = f"{ORIGIN}/robots.txt"

USER = Principal(name="alice", role="user", rate_limit_per_minute=None)

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
SHORT_PAGE = """<!doctype html><html lang="en"><head><title>Short</title></head>
<body><article><h1>Short</h1><p>Only a few words of text are on this page today.</p>
</article></body></html>"""
CHALLENGE_PAGE = """<!doctype html><html><head><title>Just a moment...</title></head>
<body><p>Checking your browser before accessing the site.</p></body></html>"""


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


Answer = FetchResult | FetchError | Callable[[Validators | None], Awaitable[FetchResult]]


class FakeFetcher:
    """Canned answers by URL; anything else (robots.txt, tdmrep.json, ai.txt) is a 404."""

    def __init__(self, answers: dict[str, Answer]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def fetch(self, url: str, **options: Any) -> FetchResult:
        self.calls.append((url, options))
        answer = self.answers.get(url, fetched(url, "Not found", status=404))
        if isinstance(answer, FetchError):
            raise answer
        if isinstance(answer, FetchResult):
            return answer
        return await answer(options.get("validators"))

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


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"auth_mode": "none", "outbound_min_delay_ms": 0}
    values.update(overrides)
    return Settings(**values)


class Harness:
    def __init__(
        self,
        answers: dict[str, Answer],
        *,
        gate: object | None = None,
        **overrides: Any,
    ) -> None:
        self.fetcher = FakeFetcher(answers)
        self.valkey = FakeAsyncRedis()
        self.registry = CollectorRegistry()
        self.settings = make_settings(**overrides)
        fetcher = cast(Fetcher, self.fetcher)
        self.pipeline = Pipeline(
            fetcher,
            cast(DomainGate, gate) if gate is not None else DomainGate(self.valkey, self.settings),
            ComplianceChecker(fetcher, self.valkey, self.settings),
            ResultCache(self.valkey, self.settings),
            FingerprintStore(self.valkey, self.settings),
            self.settings,
            self.registry,
        )

    async def run(self, **request: Any) -> Envelope[ExtractResult]:
        request.setdefault("url", PAGE)
        return await self.pipeline.run(ExtractRequest(**request), USER)

    def run_once(self, **request: Any) -> Envelope[ExtractResult]:
        return asyncio.run(self.run(**request))

    def run_twice(
        self, first: dict[str, Any] | None = None, second: dict[str, Any] | None = None
    ) -> tuple[Envelope[ExtractResult], Envelope[ExtractResult]]:
        async def both() -> tuple[Envelope[ExtractResult], Envelope[ExtractResult]]:
            return await self.run(**(first or {})), await self.run(**(second or {}))

        return asyncio.run(both())

    def keys(self, pattern: str) -> list[bytes]:
        async def scan() -> list[bytes]:
            return [key async for key in self.valkey.scan_iter(pattern)]

        return asyncio.run(scan())


def data_of(envelope: Envelope[ExtractResult]) -> ExtractResult:
    assert envelope.data is not None
    return envelope.data


def test_success() -> None:
    harness = Harness({PAGE: fetched()})

    envelope = harness.run_once()

    assert envelope.status == "success"
    assert envelope.error is None
    data = data_of(envelope)
    assert data.url == PAGE
    assert data.final_url == PAGE
    assert data.upstream_status == 200
    assert data.content_type == "text/html"
    assert data.mode_used == "fast"
    assert data.attempts == 1
    assert data.format == "markdown"
    assert data.content is not None
    assert "Photosynthesis is the process" in data.content
    assert data.word_count >= 100
    assert data.language == "en"
    assert data.title == "Photosynthesis explained"
    assert data.quality is not None
    assert data.quality.word_count == data.word_count
    assert data.compliance.decision == "allowed"
    assert data.compliance.robots == "allowed"
    assert data.metadata is not None
    assert data.metadata.license is not None
    assert data.metadata.license.id == "CC-BY-SA-4.0"
    assert data.links is None
    assert data.fingerprint is not None
    assert data.fingerprint.changed is None
    assert data.cache == "miss"
    assert data.timings.total_ms >= data.timings.fetch_ms


def test_robots_block_fails_without_fetching_the_page() -> None:
    harness = Harness({PAGE: fetched(), ROBOTS: fetched(ROBOTS, "User-agent: *\nDisallow: /")})

    envelope = harness.run_once()

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.BLOCKED_BY_ROBOTS
    assert envelope.error.retryable is False
    data = data_of(envelope)
    assert data.compliance.decision == "blocked"
    assert data.compliance.robots == "disallowed"
    assert data.content is None
    assert data.attempts == 0
    assert data.upstream_status is None
    assert harness.fetcher.page_calls() == []


def test_report_mode_fetches_and_reports() -> None:
    harness = Harness({PAGE: fetched(), ROBOTS: fetched(ROBOTS, "User-agent: *\nDisallow: /")})

    envelope = harness.run_once(compliance="report")

    assert envelope.status == "success"
    data = data_of(envelope)
    assert data.compliance.mode == "report"
    assert data.compliance.decision == "reported"
    assert data.content is not None


def test_tdm_reservation_header_blocks_after_the_fetch() -> None:
    harness = Harness({PAGE: fetched(headers={"tdm-reservation": "1"})})

    envelope = harness.run_once()

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.TDM_RESERVED
    data = data_of(envelope)
    assert data.compliance.decision == "blocked"
    assert data.compliance.tdm_source == "header"
    assert data.content is None
    assert data.upstream_status == 200
    assert data.attempts == 1


def test_404_is_target_http_error() -> None:
    harness = Harness({PAGE: fetched(body="<html><body>gone</body></html>", status=404)})

    envelope = harness.run_once()

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.TARGET_HTTP_ERROR
    assert envelope.error.retryable is False
    data = data_of(envelope)
    assert data.upstream_status == 404
    assert data.content is None


def test_429_is_retryable_and_pauses_the_host() -> None:
    harness = Harness({PAGE: fetched(status=429, headers={"retry-after": "120"})})

    envelope = harness.run_once()

    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.TARGET_HTTP_ERROR
    assert envelope.error.retryable is True
    assert harness.keys("out:cooldown:*") == [b"out:cooldown:{example.org}"]


def test_unsupported_content_type() -> None:
    harness = Harness({PAGE: fetched(body=b"%PDF-1.7", content_type="application/pdf")})

    envelope = harness.run_once()

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.UNSUPPORTED_CONTENT_TYPE
    data = data_of(envelope)
    assert data.content_type == "application/pdf"
    assert data.upstream_status == 200
    assert data.content is None


def test_plain_text_is_converted() -> None:
    text = (PARAGRAPH * 3).replace(". ", ".\r\n")
    harness = Harness({PAGE: fetched(body=text, content_type="text/plain")})

    envelope = harness.run_once()

    assert envelope.status == "success"
    data = data_of(envelope)
    assert data.content is not None
    assert "\r" not in data.content
    assert data.content.startswith("Photosynthesis is the process")
    assert data.metadata is None


def test_too_short_is_partial_with_content() -> None:
    harness = Harness({PAGE: fetched(body=SHORT_PAGE)})

    envelope = harness.run_once()

    assert envelope.status == "partial"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.CONTENT_TOO_SHORT
    data = data_of(envelope)
    assert data.content is not None
    assert "Only a few words" in data.content
    assert 0 < data.word_count < 50


def test_quality_failure_keeps_content_but_is_not_cached() -> None:
    harness = Harness({PAGE: fetched(body=CHALLENGE_PAGE)})

    first, second = harness.run_twice()

    assert first.status == "failed"
    assert first.error is not None
    assert first.error.code == ErrorCode.BOT_PROTECTION
    assert data_of(first).content is not None
    assert data_of(first).fingerprint is None
    assert data_of(second).cache == "miss"
    assert len(harness.fetcher.page_calls()) == 2
    assert harness.keys("fp:*") == []


def test_cache_hit_serves_the_stored_result() -> None:
    harness = Harness({PAGE: fetched()})

    first, second = harness.run_twice()

    assert second.status == "success"
    assert data_of(second).cache == "hit"
    assert data_of(second).content == data_of(first).content
    assert data_of(second).attempts == 0
    assert len(harness.fetcher.page_calls()) == 1


def test_cache_key_covers_the_options() -> None:
    harness = Harness({PAGE: fetched()})

    _, second = harness.run_twice({"format": "markdown"}, {"format": "text"})

    assert data_of(second).cache == "miss"
    assert len(harness.fetcher.page_calls()) == 2


def test_revalidated_by_304() -> None:
    async def answer(validators: Validators | None) -> FetchResult:
        if validators is not None and validators.etag == '"v1"':
            return fetched(body=b"", status=304, headers={"etag": '"v1"'})
        return fetched(headers={"etag": '"v1"'})

    harness = Harness({PAGE: answer}, result_cache_ttl_seconds=0)

    first, second = harness.run_twice()

    assert data_of(first).cache == "miss"
    assert second.status == "success"
    data = data_of(second)
    assert data.cache == "revalidated"
    assert data.content == data_of(first).content
    assert data.upstream_status == 200
    assert data.fingerprint is not None
    assert data.fingerprint.changed is False
    assert data.fingerprint.similarity_to_previous == 1.0
    assert harness.fetcher.page_calls()[1]["validators"] == Validators(etag='"v1"')


def test_304_keeps_the_page_findings_of_the_stored_result() -> None:
    page = ARTICLE.replace("<head>", '<head><meta name="robots" content="noai">')

    async def answer(validators: Validators | None) -> FetchResult:
        if validators is not None:
            return fetched(body=b"", status=304)
        return fetched(body=page, headers={"etag": '"v1"'})

    harness = Harness({PAGE: answer}, result_cache_ttl_seconds=0)

    first, second = harness.run_twice({"compliance": "report"}, {"compliance": "report"})

    assert data_of(first).compliance.noai is True
    assert data_of(second).cache == "revalidated"
    assert data_of(second).compliance.noai is True
    assert data_of(second).compliance.decision == "reported"


def test_revalidated_by_same_hash() -> None:
    harness = Harness({PAGE: fetched()}, result_cache_ttl_seconds=0)

    first, second = harness.run_twice()

    data = data_of(second)
    assert data.cache == "revalidated"
    assert data.content == data_of(first).content
    assert data.fingerprint is not None
    assert data.fingerprint.changed is False


def test_changed_page_is_extracted_again() -> None:
    pages = iter([ARTICLE, ARTICLE.replace("green plants", "blue plants")])

    async def answer(validators: Validators | None) -> FetchResult:
        return fetched(body=next(pages))

    harness = Harness({PAGE: answer}, result_cache_ttl_seconds=0)

    _, second = harness.run_twice()

    data = data_of(second)
    assert data.cache == "miss"
    assert data.content is not None
    assert "blue plants" in data.content
    assert data.fingerprint is not None
    assert data.fingerprint.changed is True


def test_cache_false_bypasses_the_cache_but_compares_fingerprints() -> None:
    harness = Harness({PAGE: fetched()})

    first, second = harness.run_twice({"cache": False}, {"cache": False})

    assert data_of(first).cache == "bypass"
    assert data_of(second).cache == "bypass"
    assert len(harness.fetcher.page_calls()) == 2
    assert harness.keys("res:*") == []
    fingerprint = data_of(second).fingerprint
    assert fingerprint is not None
    assert fingerprint.changed is False


def test_timeout() -> None:
    async def slow(validators: Validators | None) -> FetchResult:
        await asyncio.sleep(5)
        raise AssertionError("not reached")

    harness = Harness({PAGE: slow}, extract_timeout_seconds=0.2)

    envelope = harness.run_once()

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.TIMEOUT
    assert envelope.error.retryable is True
    assert data_of(envelope).compliance.robots == "allowed"


def test_fetch_gets_at_most_the_time_left() -> None:
    harness = Harness({PAGE: fetched()}, extract_timeout_seconds=5, fetch_timeout_seconds=20)

    harness.run_once()

    timeout = harness.fetcher.page_calls()[0]["timeout_seconds"]
    assert 0 < timeout <= 5


def test_fetch_error_maps_to_its_code_and_counts_a_failure() -> None:
    error = FetchError(ErrorCode.DNS_ERROR, "The host name could not be resolved.", retryable=True)
    harness = Harness({PAGE: error})

    envelope = harness.run_once()

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.DNS_ERROR
    assert envelope.error.retryable is True
    assert envelope.error.message == "The host name could not be resolved."
    assert data_of(envelope).attempts == 1
    assert harness.keys("out:fail:*") == [b"out:fail:{example.org}"]


def test_too_large_keeps_the_upstream_status() -> None:
    error = FetchError(ErrorCode.TOO_LARGE, "too large", upstream_status=200)
    harness = Harness({PAGE: error})

    envelope = harness.run_once()

    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.TOO_LARGE
    assert data_of(envelope).upstream_status == 200


def test_busy() -> None:
    harness = Harness({PAGE: fetched()}, gate=RefusingGate(OutboundBusy(3.0)))

    envelope = harness.run_once()

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.RATE_LIMITED
    assert envelope.error.retryable is True
    assert data_of(envelope).attempts == 0


def test_circuit_open() -> None:
    harness = Harness({PAGE: fetched()}, gate=RefusingGate(CircuitOpen(30.0)))

    envelope = harness.run_once()

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.CONNECTION_ERROR
    assert envelope.error.retryable is True


def test_invalid_url_is_a_failed_envelope() -> None:
    harness = Harness({})

    envelope = harness.run_once(url="ftp://example.org/file.txt")

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.INVALID_URL
    assert data_of(envelope).attempts == 0
    assert harness.fetcher.calls == []


def test_links_on_request() -> None:
    harness = Harness({PAGE: fetched()})

    envelope = harness.run_once(links=True)

    links = data_of(envelope).links
    assert links is not None
    kinds = {link.url: link.kind for link in links}
    assert kinds[f"{ORIGIN}/learn/respiration.html"] == "internal"
    assert kinds["https://other.example/plants"] == "external"
    assert kinds[f"{ORIGIN}/files/worksheet.pdf"] == "file"


def test_metadata_off() -> None:
    harness = Harness({PAGE: fetched()})

    envelope = harness.run_once(metadata=False)

    assert envelope.status == "success"
    assert data_of(envelope).metadata is None
    assert data_of(envelope).language == "en"


def test_user_cannot_ignore_compliance() -> None:
    harness = Harness({PAGE: fetched()})

    with pytest.raises(ServiceError) as raised:
        harness.run_once(compliance="ignore")

    assert raised.value.http_status == 403


def test_metrics_and_log_line_without_the_url() -> None:
    harness = Harness({PAGE: fetched(), f"{ORIGIN}/missing": fetched(status=404)})

    with capture_logs() as logs:
        harness.run_once()
        harness.run_once(url=f"{ORIGIN}/missing", mode="fast")

    sample = harness.registry.get_sample_value
    assert sample("text_extraction_extract_total", {"mode": "auto", "outcome": "success"}) == 1
    assert (
        sample("text_extraction_extract_total", {"mode": "fast", "outcome": "target_http_error"})
        == 1
    )
    assert sample("text_extraction_extract_duration_seconds_count", {"mode": "auto"}) == 1
    done = [line for line in logs if line["event"] == "extract_done"]
    assert len(done) == 2
    assert done[0]["host"] == "example.org"
    assert done[0]["outcome"] == "success"
    assert done[0]["upstream_status"] == 200
    assert len(done[0]["path_hash"]) == 12
    assert isinstance(done[0]["total_ms"], int)
    assert "secret-token" not in repr(logs)


def test_two_pipelines_share_one_registry() -> None:
    registry = CollectorRegistry()
    settings = make_settings()
    for _ in range(2):
        create_pipeline(settings, FakeAsyncRedis(), cast(Fetcher, FakeFetcher({})), registry)


def test_redirect_target_is_the_final_url() -> None:
    final = f"{ORIGIN}/learn/moved.html"
    harness = Harness({PAGE: replace(fetched(), final_url=final, redirects=[final])})

    envelope = harness.run_once()

    assert data_of(envelope).final_url == final
