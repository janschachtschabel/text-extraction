import asyncio
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis
from prometheus_client import CollectorRegistry
from pytest_httpserver import HTTPServer
from structlog.testing import capture_logs

from tests.fakes import (
    ARTICLE,
    ORIGIN,
    PAGE,
    PARAGRAPH,
    ROBOTS,
    Answer,
    FakeFetcher,
    Redirect,
    RefusingGate,
    fetched,
)
from text_extraction.app import create_public_app
from text_extraction.auth import Principal
from text_extraction.compliance import ComplianceChecker
from text_extraction.convert import convert_html
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.extract_models import ExtractRequest, ExtractResult
from text_extraction.fetch import Fetcher, FetchError, FetchResult, Validators
from text_extraction.fingerprint import FingerprintStore
from text_extraction.outbound import CircuitOpen, DomainGate, OutboundBusy
from text_extraction.pipeline import Pipeline, create_outbound, create_pipeline
from text_extraction.result_cache import ResultCache
from text_extraction.settings import Settings

USER = Principal(name="alice", role="user", rate_limit_per_minute=None)

SHORT_PAGE = """<!doctype html><html lang="en"><head><title>Short</title></head>
<body><article><h1>Short</h1><p>Only a few words of text are on this page today.</p>
</article></body></html>"""
CHALLENGE_PAGE = """<!doctype html><html><head><title>Just a moment...</title></head>
<body><p>Checking your browser before accessing the site.</p></body></html>"""


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
        the_gate = gate if gate is not None else DomainGate(self.valkey, self.settings)
        # As create_outbound wires the real fetcher: every request is checked and reported.
        self.fetcher.before_request = getattr(the_gate, "before_request", None)
        self.fetcher.on_request_sent = getattr(the_gate, "mark_sent", None)
        self.pipeline = Pipeline(
            fetcher,
            cast(DomainGate, the_gate),
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

    def run_all(self, *requests: dict[str, Any]) -> list[Envelope[ExtractResult]]:
        async def all_of() -> list[Envelope[ExtractResult]]:
            return [await self.run(**request) for request in requests]

        return asyncio.run(all_of())

    def ttl(self, key: bytes) -> int:
        async def read() -> int:
            return int(await self.valkey.ttl(key))

        return asyncio.run(read())

    def expire(self, key: bytes, seconds: int) -> None:
        async def shorten() -> None:
            await self.valkey.expire(key, seconds)

        asyncio.run(shorten())

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


def test_a_cold_origin_that_blocks_the_page_does_not_wait_for_the_page_spacing() -> None:
    harness = Harness(
        {PAGE: fetched(), ROBOTS: fetched(ROBOTS, "User-agent: *\nDisallow: /")},
        outbound_min_delay_ms=3000,
    )

    started = time.monotonic()
    envelope = harness.run_once()

    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.BLOCKED_BY_ROBOTS
    assert time.monotonic() - started < 2  # no page follows, so no spacing to keep before it


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

    _, second, third = harness.run_all(
        {"format": "markdown"}, {"format": "text"}, {"format": "text", "mode": "fast"}
    )

    assert data_of(second).cache == "miss"
    assert data_of(third).cache == "miss"
    assert len(harness.fetcher.page_calls()) == 3


def test_browser_recommendation_is_reported_in_both_modes() -> None:
    shell = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'
    harness = Harness({PAGE: fetched(body=shell)})

    fast, auto = harness.run_all({"mode": "fast"}, {"mode": "auto"})

    for envelope in (fast, auto):
        quality = data_of(envelope).quality
        assert quality is not None
        assert quality.browser_recommended is True
        assert data_of(envelope).mode_used == "fast"


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

    harness = Harness({PAGE: slow}, extract_timeout_seconds=2)

    envelope = harness.run_once()

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.TIMEOUT
    assert envelope.error.retryable is True
    assert data_of(envelope).compliance.robots == "allowed"
    assert data_of(envelope).attempts == 1
    assert harness.keys("out:inflight:*") == []  # the lease was released on cancellation
    assert harness.keys("out:fail:*") == []  # our deadline, not the host's fault


def test_too_little_time_left_fails_without_fetching() -> None:
    async def slow_robots(validators: Validators | None) -> FetchResult:
        await asyncio.sleep(1.2)  # leaves at most 2 - 1.2 - 0.25 s, under the 1 s minimum
        return fetched(ROBOTS, "Not found", status=404)

    harness = Harness({PAGE: fetched(), ROBOTS: slow_robots}, extract_timeout_seconds=2)

    envelope = harness.run_once()

    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.TIMEOUT
    assert envelope.error.retryable is True
    assert data_of(envelope).attempts == 0
    assert harness.fetcher.page_calls() == []
    assert harness.keys("out:fail:*") == []


def test_timeout_on_a_short_budget_is_not_the_hosts_fault() -> None:
    error = FetchError(ErrorCode.TIMEOUT, "The fetch did not finish in time.", retryable=True)
    harness = Harness({PAGE: error}, extract_timeout_seconds=3, fetch_timeout_seconds=20)

    envelope = harness.run_once()

    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.TIMEOUT
    assert harness.keys("out:fail:*") == []


def test_timeout_on_a_full_budget_counts_against_the_host() -> None:
    error = FetchError(ErrorCode.TIMEOUT, "The fetch did not finish in time.", retryable=True)
    harness = Harness({PAGE: error}, fetch_timeout_seconds=2)

    harness.run_once()

    assert harness.keys("out:fail:*") == [b"out:fail:{example.org}"]


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


@pytest.mark.parametrize(
    ("refusal", "code"),
    [(OutboundBusy(3.0), ErrorCode.RATE_LIMITED), (CircuitOpen(30.0), ErrorCode.CONNECTION_ERROR)],
)
def test_gate_refusals_raise_when_asked(refusal: Exception, code: ErrorCode) -> None:
    harness = Harness({PAGE: fetched()}, gate=RefusingGate(refusal))

    async def raised() -> BaseException:
        with pytest.raises(type(refusal)) as caught:
            await harness.pipeline.run(ExtractRequest(url=PAGE), USER, gate_refusals="raise")
        return caught.value

    assert asyncio.run(raised()) is refusal  # unchanged, with its retry_after

    envelope = harness.run_once()  # the default still answers with an envelope
    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == code
    # Both runs count as extractions that the gate refused.
    assert harness.registry.get_sample_value(
        "text_extraction_extract_total", {"mode": "auto", "outcome": code.value}
    ) == pytest.approx(2)


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
    pipelines = [
        create_pipeline(settings, FakeAsyncRedis(), cast(Fetcher, FakeFetcher({})), registry)
        for _ in range(2)
    ]

    async def run_each() -> None:
        for pipeline in pipelines:
            await pipeline.run(ExtractRequest(url="ftp://example.org/"), USER)

    asyncio.run(run_each())

    labels = {"mode": "auto", "outcome": "invalid_url"}
    assert registry.get_sample_value("text_extraction_extract_total", labels) == 2


def test_the_outbound_fetcher_reports_sent_requests_to_its_gate() -> None:
    valkey = FakeAsyncRedis()
    gate, fetcher = create_outbound(make_settings(), valkey)

    async def send() -> object:
        hook = fetcher.on_request_sent
        assert hook is not None
        await hook("Example.ORG")
        value: object = await valkey.zcard("out:starts:{example.org}")
        await fetcher.aclose()
        return value

    assert fetcher.before_request == gate.before_request
    assert fetcher.on_request_sent == gate.mark_sent
    assert asyncio.run(send()) == 1


def test_the_api_pipeline_uses_the_gate_its_fetcher_reports_to() -> None:
    app = create_public_app(make_settings(), FakeAsyncRedis(), CollectorRegistry())

    gate = app.state.pipeline._gate
    assert app.state.fetcher.before_request == gate.before_request
    assert app.state.fetcher.on_request_sent == gate.mark_sent


def test_a_cold_origins_files_and_page_go_out_back_to_back(httpserver: HTTPServer) -> None:
    for path in ("/robots.txt", "/ai.txt", "/.well-known/tdmrep.json"):
        httpserver.expect_request(path).respond_with_data("", status=404)
    httpserver.expect_request("/page").respond_with_data(ARTICLE, content_type="text/html")
    settings = make_settings(fetch_allow_private_networks=True, outbound_min_delay_ms=1000)
    valkey = FakeAsyncRedis()
    gate = DomainGate(valkey, settings)
    confirmed: list[float] = []  # the turn's start as checked before each send
    sent: list[float] = []  # the turn's start once the send was reported
    sent_at: list[float] = []

    async def latest() -> float:
        key = "out:starts:{127.0.0.1}"
        members = cast(list[tuple[bytes, float]], await valkey.zrange(key, 0, -1, withscores=True))
        (score,) = [value for _, value in members]  # one turn: one token
        return float(score)

    async def on_sent(host: str) -> None:
        sent_at.append(time.monotonic())
        confirmed.append(await latest())
        await gate.mark_sent(host)
        sent.append(await latest())

    async def scenario() -> Envelope[ExtractResult]:
        fetcher = Fetcher(settings, before_request=gate.before_request, on_request_sent=on_sent)
        pipeline = create_pipeline(settings, valkey, fetcher, CollectorRegistry(), gate=gate)
        try:
            return await pipeline.run(ExtractRequest(url=httpserver.url_for("/page")), USER)
        finally:
            await fetcher.aclose()

    envelope = asyncio.run(scenario())

    assert envelope.status == "success"
    assert len(sent) == 4  # three compliance files and the page, all in one turn
    # A send never records a start earlier than the one its check recorded.
    assert all(after >= before for before, after in zip(confirmed, sent, strict=True))
    # First contact costs no spacing: the turn's own requests follow each other at once.
    assert sent_at[-1] - sent_at[0] < 0.5


def test_redirect_target_is_the_final_url() -> None:
    final = f"{ORIGIN}/learn/moved.html"
    harness = Harness({PAGE: replace(fetched(), final_url=final, redirects=[final])})

    envelope = harness.run_once()

    assert data_of(envelope).final_url == final


def test_304_with_its_own_tdm_reservation_header_blocks() -> None:
    async def answer(validators: Validators | None) -> FetchResult:
        if validators is not None:
            return fetched(body=b"", status=304, headers={"tdm-reservation": "1"})
        return fetched(headers={"etag": '"v1"'})

    harness = Harness({PAGE: answer}, result_cache_ttl_seconds=0)

    _, second = harness.run_twice()

    assert second.status == "failed"
    assert second.error is not None
    assert second.error.code == ErrorCode.TDM_RESERVED
    assert data_of(second).compliance.tdm_source == "header"
    assert data_of(second).content is None


@pytest.mark.parametrize("status", [300, 302, 305])
def test_a_final_status_other_than_2xx_or_304_is_target_http_error(status: int) -> None:
    harness = Harness({PAGE: fetched(status=status)})

    envelope = harness.run_once()

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.TARGET_HTTP_ERROR
    assert envelope.error.retryable is False
    assert data_of(envelope).upstream_status == status
    assert data_of(envelope).content is None


def test_switching_format_does_not_report_a_change() -> None:
    harness = Harness({PAGE: fetched()})

    first, second, third, fourth = harness.run_all(
        {"cache": False, "format": "markdown"},
        {"cache": False, "format": "html"},
        {"cache": False, "format": "markdown"},
        {"cache": False, "format": "html"},
    )

    fingerprints = [data_of(envelope).fingerprint for envelope in (first, second, third, fourth)]
    assert [fp.changed if fp else "missing" for fp in fingerprints] == [None, None, False, False]


def test_revalidation_keeps_the_fingerprint_baseline_alive() -> None:
    harness = Harness({PAGE: fetched()}, result_cache_ttl_seconds=0, fingerprint_ttl_seconds=1000)
    harness.run_once()
    (key,) = harness.keys("fp:*")
    harness.expire(key, 5)

    envelope = harness.run_once()

    assert data_of(envelope).cache == "revalidated"
    assert harness.ttl(key) > 900


def test_gate_keys_use_the_normalised_host() -> None:
    url = "https://B\u00fccher.Example./learn"
    harness = Harness({url: fetched(url, status=429)})

    harness.run_once(url=url)

    assert harness.keys("out:cooldown:*") == [b"out:cooldown:{xn--bcher-kva.example}"]


def test_conversion_failure_is_logged_with_traceback_and_without_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(html: str, url: str, fmt: str) -> None:
        raise RuntimeError("converter broke")

    monkeypatch.setattr("text_extraction.pipeline.convert_html", broken)
    harness = Harness({PAGE: fetched()})

    with capture_logs() as logs:
        envelope = harness.run_once()

    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.CONVERSION_FAILED
    (line,) = [entry for entry in logs if entry["event"] == "conversion_failed"]
    assert line["exc_info"] is True
    assert line["host"] == "example.org"
    assert "secret-token" not in repr(line)


def test_non_ascii_validators_are_not_sent_back(httpserver: HTTPServer) -> None:
    body = ARTICLE.encode()
    for path in ("/robots.txt", "/ai.txt", "/.well-known/tdmrep.json"):
        httpserver.expect_request(path).respond_with_data("", status=404)
    httpserver.expect_request("/page").respond_with_data(
        body,
        content_type="text/html; charset=utf-8",
        headers={"ETag": '"caf\u00e9"', "Last-Modified": "Mon, 05 Oct 2026 \u00e9"},
    )
    settings = make_settings(fetch_allow_private_networks=True, result_cache_ttl_seconds=0)

    async def scenario() -> list[Envelope[ExtractResult]]:
        fetcher = Fetcher(settings)
        pipeline = create_pipeline(settings, FakeAsyncRedis(), fetcher, CollectorRegistry())
        try:
            request = ExtractRequest(url=httpserver.url_for("/page"))
            return [await pipeline.run(request, USER), await pipeline.run(request, USER)]
        finally:
            await fetcher.aclose()

    first, second = asyncio.run(scenario())

    assert first.status == "success"
    assert second.status == "success"
    assert data_of(second).cache == "revalidated"  # same bytes, not a stored bad validator
    page_requests = [request for request, _ in httpserver.log if request.path == "/page"]
    assert len(page_requests) == 2
    assert "If-None-Match" not in page_requests[1].headers
    assert "If-Modified-Since" not in page_requests[1].headers


# --- redirects: compliance and the outbound gate per hop ---------------------------------------

OTHER = "https://other.example"
MOVED = f"{ORIGIN}/private/moved.html"
DISALLOW_PRIVATE = "User-agent: *\nDisallow: /private/\n"


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


def recording_harness(
    answers: dict[str, Answer], **overrides: Any
) -> tuple[Harness, RecordingGate]:
    valkey = FakeAsyncRedis()
    gate = RecordingGate(DomainGate(valkey, make_settings(**overrides)))
    harness = Harness(answers, gate=gate, **overrides)
    harness.valkey = valkey
    return harness, gate


def leases(harness: Harness) -> int:
    async def count() -> int:
        return sum(
            [
                await harness.valkey.zcard(key)
                async for key in harness.valkey.scan_iter("out:inflight:*")
            ]
        )

    return asyncio.run(count())


def test_same_origin_redirect_to_a_disallowed_path_is_blocked() -> None:
    harness = Harness(
        {PAGE: Redirect(MOVED), MOVED: fetched(MOVED), ROBOTS: fetched(ROBOTS, DISALLOW_PRIVATE)}
    )

    envelope = harness.run_once()

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.BLOCKED_BY_ROBOTS
    assert envelope.error.retryable is False
    data = data_of(envelope)
    assert data.compliance.robots == "disallowed"
    assert data.compliance.decision == "blocked"
    assert data.content is None
    assert MOVED not in [url for url, _ in harness.fetcher.calls]


def test_same_origin_redirect_to_a_disallowed_path_is_reported() -> None:
    harness = Harness(
        {PAGE: Redirect(MOVED), MOVED: fetched(MOVED), ROBOTS: fetched(ROBOTS, DISALLOW_PRIVATE)}
    )

    envelope = harness.run_once(compliance="report")

    assert envelope.status == "success"
    data = data_of(envelope)
    assert data.final_url == MOVED
    assert data.compliance.robots == "disallowed"
    assert data.compliance.decision == "reported"
    assert any("robots.txt disallows" in reason for reason in data.compliance.reasons)


def test_cross_origin_redirect_checks_the_targets_files_and_charges_its_gate() -> None:
    target = f"{OTHER}/learn/page.html"
    harness, gate = recording_harness({PAGE: Redirect(target), target: fetched(target)})

    envelope = harness.run_once()

    assert envelope.status == "success"
    assert f"{OTHER}/robots.txt" in [url for url, _ in harness.fetcher.calls]
    assert gate.events == [
        ("take", "example.org"),
        ("give back", "example.org"),
        ("take", "other.example"),
        ("record", "other.example"),
        ("give back", "other.example"),
    ]
    assert leases(harness) == 0


def test_cross_origin_redirect_blocked_by_the_targets_robots() -> None:
    target = f"{OTHER}/private/page.html"
    other_robots = f"{OTHER}/robots.txt"
    harness, gate = recording_harness(
        {
            PAGE: Redirect(target),
            target: fetched(target),
            other_robots: fetched(other_robots, DISALLOW_PRIVATE),
        }
    )

    envelope = harness.run_once()

    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.BLOCKED_BY_ROBOTS
    assert target not in [url for url, _ in harness.fetcher.calls]
    # The target's robots.txt is fetched in the target host's turn; the block gives it back.
    assert gate.events[-2:] == [("take", "other.example"), ("give back", "other.example")]
    assert leases(harness) == 0


def test_redirect_to_a_paused_host_gives_every_lease_back() -> None:
    target = f"{OTHER}/learn/page.html"
    harness, gate = recording_harness({PAGE: Redirect(target), target: fetched(target)})

    async def pause_other() -> None:
        await harness.valkey.set("out:cooldown:{other.example}", f"{time.time() + 60:.6f}")

    asyncio.run(pause_other())

    envelope = harness.run_once()

    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.CONNECTION_ERROR  # the circuit-open answer
    assert target not in [url for url, _ in harness.fetcher.calls]
    assert gate.events == [("take", "example.org"), ("give back", "example.org")]
    assert leases(harness) == 0


# --- conversion budget ------------------------------------------------------------------------


def test_a_page_over_the_conversion_limit_fails_before_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_decoding(data: bytes, charset: str | None) -> str:
        raise AssertionError("an oversized page is not decoded")

    monkeypatch.setattr("text_extraction.pipeline.decode_html", no_decoding)
    body = ARTICLE + "<!--" + "x" * 3000 + "-->"
    harness = Harness({PAGE: fetched(body=body)}, convert_max_bytes=2048)

    envelope = harness.run_once()

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.TOO_LARGE
    assert envelope.error.retryable is False
    data = data_of(envelope)
    assert data.upstream_status == 200
    assert data.content is None


def test_a_page_at_the_conversion_limit_is_converted() -> None:
    harness = Harness({PAGE: fetched()}, convert_max_bytes=len(ARTICLE.encode()))

    assert harness.run_once().status == "success"


def test_conversion_runs_on_the_conversion_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    threads: list[str] = []
    real_convert = convert_html

    def recording_convert(*args: Any) -> Any:
        threads.append(threading.current_thread().name)
        return real_convert(*args)

    monkeypatch.setattr("text_extraction.pipeline.convert_html", recording_convert)
    harness = Harness({PAGE: fetched()})

    assert harness.run_once().status == "success"
    assert len(threads) == 1
    assert threads[0].startswith("convert")


def test_waiting_for_a_conversion_slot_counts_against_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    conversions: list[str] = []
    real_convert = convert_html

    def slow_convert(html: str, url: str, fmt: Any) -> Any:
        conversions.append(url)
        release.wait(30)
        return real_convert(html, url, fmt)

    monkeypatch.setattr("text_extraction.pipeline.convert_html", slow_convert)
    second = f"{ORIGIN}/learn/second.html"
    harness = Harness(
        {PAGE: fetched(), second: fetched(second)},
        convert_concurrency=1,
        extract_timeout_seconds=2,
    )

    async def scenario() -> tuple[Envelope[ExtractResult], Envelope[ExtractResult]]:
        first = asyncio.ensure_future(harness.run())
        await asyncio.sleep(0.5)  # the first request now holds the only slot
        waiting = await harness.run(url=second)
        release.set()
        return await first, waiting

    first, waiting = asyncio.run(scenario())

    assert waiting.error is not None
    assert waiting.error.code == ErrorCode.TIMEOUT
    assert second not in conversions  # timed out waiting, never converted
    assert first.error is not None
    assert first.error.code == ErrorCode.TIMEOUT


def test_a_same_host_redirect_keeps_the_turn_it_already_holds() -> None:
    harness, gate = recording_harness(
        {PAGE: Redirect(MOVED), MOVED: fetched(MOVED)},
        outbound_min_delay_ms=1000,
    )

    started = time.monotonic()
    envelope = harness.run_once()
    elapsed = time.monotonic() - started

    assert envelope.status == "success"
    assert data_of(envelope).final_url == MOVED
    assert gate.events == [
        ("take", "example.org"),
        ("record", "example.org"),
        ("give back", "example.org"),
    ]
    assert elapsed < 0.9  # no second spacing slot at the same host


def test_a_same_host_redirect_does_not_wait_for_the_crawl_delay_again() -> None:
    robots = fetched(ROBOTS, "User-agent: *\nCrawl-delay: 2\n")
    harness, gate = recording_harness(
        {PAGE: Redirect(MOVED), MOVED: fetched(MOVED), ROBOTS: robots},
        outbound_min_delay_ms=1000,
    )

    started = time.monotonic()
    envelope = harness.run_once()
    elapsed = time.monotonic() - started

    assert envelope.status == "success"
    assert [name for name, _ in gate.events] == ["take", "record", "give back"]
    assert elapsed < 1.9


def test_a_cross_host_redirect_still_takes_the_new_hosts_turn() -> None:
    target = f"{OTHER}/learn/page.html"
    harness, gate = recording_harness(
        {PAGE: Redirect(target), target: fetched(target)},
        outbound_min_delay_ms=1000,
    )

    envelope = harness.run_once()

    assert envelope.status == "success"
    assert gate.events == [
        ("take", "example.org"),
        ("give back", "example.org"),
        ("take", "other.example"),
        ("record", "other.example"),
        ("give back", "other.example"),
    ]


def test_a_hop_that_got_little_time_is_not_the_hosts_fault() -> None:
    target = f"{OTHER}/learn/page.html"
    other_robots = f"{OTHER}/robots.txt"
    timed_out = FetchError(ErrorCode.TIMEOUT, "The fetch did not finish in time.", retryable=True)

    async def slow_robots(validators: Validators | None) -> FetchResult:
        await asyncio.sleep(1.4)  # the hop's turn comes with less than half the fetch budget left
        return fetched(other_robots, "Not found", status=404)

    harness = Harness(
        {PAGE: Redirect(target), target: timed_out, other_robots: slow_robots},
        extract_timeout_seconds=2,
        fetch_timeout_seconds=2,
    )

    envelope = harness.run_once()

    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.TIMEOUT
    assert harness.keys("out:fail:*") == []


def test_a_hop_that_got_a_full_budget_counts_against_its_host() -> None:
    target = f"{OTHER}/learn/page.html"
    timed_out = FetchError(ErrorCode.TIMEOUT, "The fetch did not finish in time.", retryable=True)
    harness = Harness({PAGE: Redirect(target), target: timed_out}, fetch_timeout_seconds=2)

    harness.run_once()

    assert harness.keys("out:fail:*") == [b"out:fail:{other.example}"]


def test_a_hop_is_judged_by_what_is_left_of_the_chains_fetch_budget() -> None:
    # The request deadline is far away, but the fetcher gives the whole redirect chain one
    # FETCH_TIMEOUT_SECONDS: a hop that starts late gets only the rest of it.
    target = f"{OTHER}/learn/page.html"
    other_robots = f"{OTHER}/robots.txt"
    timed_out = FetchError(ErrorCode.TIMEOUT, "The fetch did not finish in time.", retryable=True)

    async def slow_robots(validators: Validators | None) -> FetchResult:
        await asyncio.sleep(1.4)  # leaves the hop 0.6 s of the chain's 2 s
        return fetched(other_robots, "Not found", status=404)

    harness = Harness(
        {PAGE: Redirect(target), target: timed_out, other_robots: slow_robots},
        fetch_timeout_seconds=2,
    )

    envelope = harness.run_once()

    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.TIMEOUT
    assert harness.keys("out:fail:*") == []


# --- compliance files inside the page's gate turn ---------------------------------------------


def watch_requests(harness: Harness, gate: RecordingGate) -> list[int]:
    """Note each request in ``gate.events`` as ("fetch", url) and return a one-item list that
    ends up holding the most requests that were in flight at once."""
    answer = harness.fetcher.fetch
    in_flight = 0
    most = [0]

    async def fetch(url: str, **options: Any) -> FetchResult:
        nonlocal in_flight
        in_flight += 1
        most[0] = max(most[0], in_flight)
        gate.events.append(("fetch", url))
        try:
            await asyncio.sleep(0.01)
            return await answer(url, **options)
        finally:
            in_flight -= 1

    harness.fetcher.fetch = fetch  # type: ignore[method-assign]
    return most


@pytest.mark.parametrize("gate_refusals", ["fail", "raise"])  # POST /v1/extract, the worker
def test_a_cold_origins_files_are_fetched_one_by_one_in_the_pages_turn(gate_refusals: str) -> None:
    harness, gate = recording_harness({PAGE: fetched(PAGE)})
    most = watch_requests(harness, gate)

    async def twice() -> list[Envelope[ExtractResult]]:
        request = ExtractRequest(url=PAGE, cache=False)
        return [
            await harness.pipeline.run(request, USER, gate_refusals=gate_refusals)  # type: ignore[arg-type]
            for _ in range(2)
        ]

    first, second = asyncio.run(twice())

    assert first.status == second.status == "success"
    turn = [("take", "example.org")]
    end = [("record", "example.org"), ("give back", "example.org")]
    assert gate.events == [
        *turn,
        ("fetch", ROBOTS),
        ("fetch", f"{ORIGIN}/.well-known/tdmrep.json"),
        ("fetch", f"{ORIGIN}/ai.txt"),
        ("fetch", PAGE),
        *end,
        # The files are cached now: the second turn fetches the page only.
        *turn,
        ("fetch", PAGE),
        *end,
    ]
    assert most[0] == 1
    assert leases(harness) == 0


def test_a_refused_turn_fetches_no_compliance_file() -> None:
    harness = Harness({PAGE: fetched(PAGE)}, gate=RefusingGate(OutboundBusy(3.0)))

    envelope = harness.run_once()

    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.RATE_LIMITED
    assert harness.fetcher.calls == []


def test_a_cross_host_redirects_files_are_fetched_in_the_targets_turn() -> None:
    target = f"{OTHER}/learn/page.html"
    harness, gate = recording_harness({PAGE: Redirect(target), target: fetched(target)})
    most = watch_requests(harness, gate)

    envelope = harness.run_once()

    assert envelope.status == "success"
    assert gate.events == [
        ("take", "example.org"),
        ("fetch", ROBOTS),
        ("fetch", f"{ORIGIN}/.well-known/tdmrep.json"),
        ("fetch", f"{ORIGIN}/ai.txt"),
        ("fetch", PAGE),
        ("give back", "example.org"),
        ("take", "other.example"),
        ("fetch", f"{OTHER}/robots.txt"),
        ("fetch", f"{OTHER}/.well-known/tdmrep.json"),
        ("fetch", f"{OTHER}/ai.txt"),
        ("record", "other.example"),
        ("give back", "other.example"),
    ]
    # The fake fetcher follows the redirect inside the first fetch call, so that call counts as
    # in flight while the target's files load: two, where the real fetcher has one.
    assert most[0] == 2
    assert leases(harness) == 0


def test_two_cold_turns_sharing_the_file_load_still_space_their_pages() -> None:
    page_starts: list[float] = []

    async def page(validators: Validators | None) -> FetchResult:
        page_starts.append(time.monotonic())  # after the gate's check and the send report
        return fetched(PAGE)

    async def slow_file(validators: Validators | None) -> FetchResult:
        await asyncio.sleep(0.3)  # slow compliance files: both turns wait for the one load
        return fetched(ROBOTS, "Not found", status=404)

    files = (ROBOTS, f"{ORIGIN}/.well-known/tdmrep.json", f"{ORIGIN}/ai.txt")
    answers: dict[str, Answer] = {PAGE: page, **dict.fromkeys(files, slow_file)}
    harness = Harness(answers, outbound_min_delay_ms=200)

    async def both() -> list[Envelope[ExtractResult]]:
        return list(await asyncio.gather(harness.run(cache=False), harness.run(cache=False)))

    first, second = asyncio.run(both())

    assert first.status == second.status == "success"
    assert len(page_starts) == 2
    assert abs(page_starts[1] - page_starts[0]) >= 0.19  # 200 ms, minus clock slack


@pytest.mark.parametrize(("gate_wait", "counted"), [(0.0, True), (0.7, False)])
def test_a_timeout_is_judged_by_the_time_the_host_had_after_the_gate(
    httpserver: HTTPServer, gate_wait: float, counted: bool
) -> None:
    """The fetch's budget covers the gate's wait before the request is sent. A long wait
    followed by a slow host is not the host's fault: what counts is the time left once the gate
    let the request go (FETCH_TIMEOUT_SECONDS 1, so a fair share is 0.5 s)."""

    def hang(request: Any) -> Any:
        time.sleep(1.5)
        return "late"

    httpserver.expect_request("/slow").respond_with_handler(hang)
    settings = make_settings(
        fetch_allow_private_networks=True,
        fetch_timeout_seconds=1,
        compliance_default="ignore",
        outbound_min_delay_ms=0,
    )
    valkey = FakeAsyncRedis()
    gate = DomainGate(valkey, settings)

    async def slow_gate(host: str) -> None:
        await gate.before_request(host)
        await asyncio.sleep(gate_wait)

    async def scenario() -> tuple[Envelope[ExtractResult], list[Any]]:
        fetcher = Fetcher(settings, before_request=slow_gate, on_request_sent=gate.mark_sent)
        pipeline = create_pipeline(settings, valkey, fetcher, CollectorRegistry(), gate=gate)
        try:
            envelope = await pipeline.run(ExtractRequest(url=httpserver.url_for("/slow")), USER)
        finally:
            await fetcher.aclose()
        return envelope, await valkey.keys("out:fail:*")

    envelope, failures = asyncio.run(scenario())

    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.TIMEOUT
    assert (failures == [b"out:fail:{127.0.0.1}"]) is counted
