import asyncio
import json
import threading
from dataclasses import replace
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis
from protego import Protego
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from text_extraction import compliance
from text_extraction.auth import Principal
from text_extraction.compliance import (
    ComplianceChecker,
    ComplianceMode,
    ComplianceReport,
    blocking_code,
    resolve_mode,
)
from text_extraction.envelope import ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.fetch import Fetcher, FetchError, FetchResult
from text_extraction.settings import Settings

ORIGIN = "https://example.org"
PAGE = f"{ORIGIN}/docs/page.html"
ROBOTS = f"{ORIGIN}/robots.txt"
TDMREP = f"{ORIGIN}/.well-known/tdmrep.json"
AI_TXT = f"{ORIGIN}/ai.txt"

USER = Principal(name="alice", role="user", rate_limit_per_minute=None)
ADMIN = Principal(name="root", role="admin", rate_limit_per_minute=None)


def result(url: str, body: str | bytes = "", status: int = 200) -> FetchResult:
    content = body.encode() if isinstance(body, str) else body
    return FetchResult(
        url=url,
        final_url=url,
        status=status,
        headers={},
        content=content,
        content_type="text/plain",
        charset=None,
        elapsed_ms=1,
        redirects=[],
        not_modified=False,
        raw_sha256="0" * 64,
    )


def page_result(
    headers: dict[str, str] | None = None, content_type: str = "text/html"
) -> FetchResult:
    fetched = result(PAGE, "<html></html>")
    return replace(fetched, headers=headers or {}, content_type=content_type)


class FakeFetcher:
    """Canned answers by URL; any URL without an answer is a 404."""

    def __init__(self, answers: dict[str, FetchResult | FetchError]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def fetch(self, url: str, **options: Any) -> FetchResult:
        self.calls.append((url, options))
        answer = self.answers.get(url, result(url, "Not found", 404))
        if isinstance(answer, FetchError):
            raise answer
        return answer

    def urls(self) -> list[str]:
        return [url for url, _ in self.calls]


def make_checker(
    answers: dict[str, FetchResult | FetchError],
    valkey: FakeAsyncRedis | None = None,
    **overrides: Any,
) -> tuple[ComplianceChecker, FakeFetcher, FakeAsyncRedis]:
    fetcher = FakeFetcher(answers)
    store = valkey if valkey is not None else FakeAsyncRedis()
    settings = Settings(auth_mode="none", **overrides)
    return ComplianceChecker(cast(Fetcher, fetcher), store, settings), fetcher, store


def check(
    answers: dict[str, FetchResult | FetchError],
    mode: ComplianceMode = "enforce",
    url: str = PAGE,
    **overrides: Any,
) -> ComplianceReport:
    checker, _, _ = make_checker(answers, **overrides)
    return asyncio.run(checker.before_fetch(url, mode))


def robots_answer(body: str, status: int = 200) -> dict[str, FetchResult | FetchError]:
    return {ROBOTS: result(ROBOTS, body, status)}


# --- robots.txt -------------------------------------------------------------------------------


def test_robots_disallow_blocks_in_enforce() -> None:
    report = check(robots_answer("User-agent: *\nDisallow: /docs/\n"))

    assert report.robots == "disallowed"
    assert report.decision == "blocked"
    assert blocking_code(report) is ErrorCode.BLOCKED_BY_ROBOTS
    assert report.reasons


def test_robots_disallow_only_reported_in_report_mode() -> None:
    report = check(robots_answer("User-agent: *\nDisallow: /docs/\n"), mode="report")

    assert report.robots == "disallowed"
    assert report.decision == "reported"
    assert blocking_code(report) is None


def test_robots_allowed_path() -> None:
    report = check(robots_answer("User-agent: *\nDisallow: /private/\n"))

    assert report.robots == "allowed"
    assert report.decision == "allowed"
    assert report.reasons == []
    assert blocking_code(report) is None


def test_robots_404_allows_all() -> None:
    report = check(robots_answer("nope", status=404))

    assert report.robots == "allowed"
    assert report.decision == "allowed"


def test_robots_403_allows_all() -> None:
    report = check(robots_answer("denied", status=403))

    assert report.robots == "allowed"


def test_robots_503_is_unavailable_and_blocks_in_enforce() -> None:
    report = check(robots_answer("down", status=503))

    assert report.robots == "unavailable"
    assert report.decision == "blocked"
    assert blocking_code(report) is ErrorCode.BLOCKED_BY_ROBOTS


def test_robots_429_is_unavailable() -> None:
    report = check(robots_answer("slow down", status=429))

    assert report.robots == "unavailable"


def test_robots_fetch_error_is_unavailable_and_only_reported_in_report_mode() -> None:
    answers: dict[str, FetchResult | FetchError] = {
        ROBOTS: FetchError(ErrorCode.TIMEOUT, "timed out", retryable=True)
    }

    enforced = check(answers)
    reported = check(answers, mode="report")

    assert enforced.robots == "unavailable"
    assert blocking_code(enforced) is ErrorCode.BLOCKED_BY_ROBOTS
    assert reported.decision == "reported"
    assert blocking_code(reported) is None


def test_robots_too_large_404_page_is_absent_not_unavailable() -> None:
    oversized_404 = FetchError(ErrorCode.TOO_LARGE, "too large", upstream_status=404)
    answers: dict[str, FetchResult | FetchError] = {ROBOTS: oversized_404, AI_TXT: oversized_404}

    report = check(answers)

    assert report.robots == "allowed"
    assert report.ai_txt == "absent"
    assert report.decision == "allowed"


def test_robots_too_large_429_or_5xx_or_200_stays_unavailable() -> None:
    for status in (429, 503, 200):
        answers: dict[str, FetchResult | FetchError] = {
            ROBOTS: FetchError(ErrorCode.TOO_LARGE, "too large", upstream_status=status)
        }

        assert check(answers).robots == "unavailable", status


def test_robots_final_3xx_is_absent() -> None:
    stray_304 = check(robots_answer("", status=304))
    no_location = check(robots_answer("moved", status=301))

    assert stray_304.robots == "allowed"
    assert no_location.robots == "allowed"
    assert no_location.decision == "allowed"


def test_robots_too_many_redirects_is_absent() -> None:
    answers: dict[str, FetchResult | FetchError] = {
        ROBOTS: FetchError(
            ErrorCode.TARGET_HTTP_ERROR,
            "The target redirected too many times.",
            upstream_status=302,
        ),
        AI_TXT: FetchError(
            ErrorCode.TARGET_HTTP_ERROR,
            "The target redirected too many times.",
            upstream_status=302,
        ),
    }

    report = check(answers)

    assert report.robots == "allowed"
    assert report.ai_txt == "absent"


def test_robots_too_large_is_unavailable() -> None:
    answers: dict[str, FetchResult | FetchError] = {
        ROBOTS: FetchError(ErrorCode.TOO_LARGE, "too large")
    }

    assert check(answers).robots == "unavailable"


def test_robots_crawl_delay_reported() -> None:
    report = check(robots_answer("User-agent: *\nCrawl-delay: 2.5\nDisallow: /private/\n"))

    assert report.robots == "allowed"
    assert report.crawl_delay == 2.5


def test_no_crawl_delay_is_none() -> None:
    assert check(robots_answer("User-agent: *\nDisallow:\n")).crawl_delay is None


def test_wildcard_and_user_agent_group() -> None:
    robots = (
        "User-agent: *\nDisallow: /\n\n"
        "User-agent: text-extraction\nDisallow: /private/\n\n"
        "User-agent: badbot\nDisallow: /\n"
    )

    assert check(robots_answer(robots)).robots == "allowed"
    assert check(robots_answer(robots), url=f"{ORIGIN}/private/x").robots == "disallowed"


def test_robots_wildcards_and_end_anchor() -> None:
    robots = "User-agent: *\nDisallow: /*.pdf$\nAllow: /public/*.pdf$\n"

    assert check(robots_answer(robots), url=f"{ORIGIN}/a/b.pdf").robots == "disallowed"
    assert check(robots_answer(robots), url=f"{ORIGIN}/a/b.pdf?x=1").robots == "allowed"
    assert check(robots_answer(robots), url=f"{ORIGIN}/public/b.pdf").robots == "allowed"


def test_malformed_robots_does_not_crash() -> None:
    garbage = b"\x00\xff\xfe<html>not robots</html>\nDisallow\n:::\nUser-agent\n" + b"\x80" * 50
    answers: dict[str, FetchResult | FetchError] = {ROBOTS: result(ROBOTS, garbage)}

    report = check(answers)

    assert report.robots == "allowed"
    assert report.decision == "allowed"


def test_robots_with_bom_is_parsed() -> None:
    body = "﻿User-agent: *\nDisallow: /docs/\n"

    assert check(robots_answer(body)).robots == "disallowed"


def test_robots_requested_with_limits() -> None:
    checker, fetcher, _ = make_checker(robots_answer(""))

    asyncio.run(checker.before_fetch(PAGE, "enforce"))

    assert set(fetcher.urls()) == {ROBOTS, TDMREP, AI_TXT}
    for url, options in fetcher.calls:
        # robots.txt is read from its first 500 KiB (RFC 9309); the others must fit.
        truncating = {"truncate": True} if url == ROBOTS else {}
        assert options == {"max_bytes": 512_000, "timeout_seconds": 10, **truncating}


def test_robots_origin_includes_scheme_and_port() -> None:
    checker, fetcher, _ = make_checker({})

    asyncio.run(checker.before_fetch("http://Example.ORG:8080/a?b=1#c", "enforce"))

    assert "http://example.org:8080/robots.txt" in fetcher.urls()


# --- caching ----------------------------------------------------------------------------------


def test_robots_cached_in_valkey() -> None:
    checker, fetcher, valkey = make_checker(robots_answer("User-agent: *\nDisallow: /docs/\n"))

    async def scenario() -> tuple[ComplianceReport, ComplianceReport, list[str]]:
        first = await checker.before_fetch(PAGE, "enforce")
        calls_after_first = len(fetcher.calls)
        second = await checker.before_fetch(f"{ORIGIN}/other", "enforce")
        keys = sorted(
            str(key, "utf-8") if isinstance(key, bytes) else key
            for key in await valkey.keys("cmp:*")
        )
        assert len(fetcher.calls) == calls_after_first  # nothing fetched the second time
        return first, second, keys

    first, second, keys = asyncio.run(scenario())

    assert first.robots == "disallowed"
    assert second.robots == "allowed"
    assert keys == [
        "cmp:ai:{https://example.org:443}",
        "cmp:robots:{https://example.org:443}",
        "cmp:tdmrep:{https://example.org:443}",
    ]


def test_cached_entry_is_text_not_a_python_object() -> None:
    checker, _, valkey = make_checker(robots_answer("User-agent: *\nDisallow: /x\n"))

    async def scenario() -> tuple[bytes | str | None, int]:
        await checker.before_fetch(PAGE, "enforce")
        key = "cmp:robots:{https://example.org:443}"
        return await valkey.get(key), await valkey.ttl(key)

    raw, ttl = asyncio.run(scenario())

    assert raw is not None
    assert json.loads(raw) == {"state": "found", "body": "User-agent: *\nDisallow: /x\n"}
    assert 3590 <= ttl <= 3600


def test_unavailable_robots_is_cached_for_a_short_time() -> None:
    checker, fetcher, valkey = make_checker(robots_answer("down", status=503))

    async def scenario() -> tuple[int, int]:
        await checker.before_fetch(PAGE, "enforce")
        await checker.before_fetch(PAGE, "enforce")
        return await valkey.ttl("cmp:robots:{https://example.org:443}"), len(fetcher.calls)

    ttl, calls = asyncio.run(scenario())

    assert 290 <= ttl <= 300
    assert calls == 3  # robots, tdmrep and ai.txt once; the second check was served from cache


def test_short_ttl_never_exceeds_the_configured_cache_time() -> None:
    checker, _, valkey = make_checker(
        robots_answer("down", status=503), compliance_cache_seconds=60
    )

    async def scenario() -> int:
        await checker.before_fetch(PAGE, "enforce")
        return await valkey.ttl("cmp:robots:{https://example.org:443}")

    assert 55 <= asyncio.run(scenario()) <= 60


def test_cache_disabled_with_zero_seconds() -> None:
    checker, fetcher, valkey = make_checker(robots_answer(""), compliance_cache_seconds=0)

    async def scenario() -> list[bytes | str]:
        await checker.before_fetch(PAGE, "enforce")
        await checker.before_fetch(PAGE, "enforce")
        return await valkey.keys("cmp:*")

    assert asyncio.run(scenario()) == []
    assert len(fetcher.calls) == 6


def test_parsed_rules_are_reused_between_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    parsed: list[str] = []
    real_parse = Protego.parse

    def counting_parse(text: str) -> Any:
        parsed.append(text)
        return real_parse(text)

    monkeypatch.setattr(Protego, "parse", counting_parse)
    checker, _, _ = make_checker(robots_answer("User-agent: *\nDisallow: /docs/\n"))

    async def scenario() -> list[ComplianceReport]:
        return [await checker.before_fetch(PAGE, "enforce") for _ in range(3)]

    reports = asyncio.run(scenario())

    assert {report.robots for report in reports} == {"disallowed"}
    assert parsed == ["User-agent: *\nDisallow: /docs/\n"]  # robots.txt once; the 404s have no text


def test_parsed_rules_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compliance, "_RULES_CACHE_SIZE", 2)
    checker, _, _ = make_checker({})

    async def scenario() -> None:
        for number in range(5):
            await checker._rules(
                compliance._Entry("found", f"User-agent: *\nDisallow: /{number}\n")
            )

    asyncio.run(scenario())

    assert len(checker._rules_cache) == 2


def test_garbled_cache_entry_is_refetched() -> None:
    checker, fetcher, valkey = make_checker(robots_answer("User-agent: *\nDisallow: /docs/\n"))

    async def scenario() -> ComplianceReport:
        await valkey.set("cmp:robots:{https://example.org:443}", "not json")
        return await checker.before_fetch(PAGE, "enforce")

    report = asyncio.run(scenario())

    assert report.robots == "disallowed"
    assert ROBOTS in fetcher.urls()


def test_valkey_outage_does_not_stop_the_checks() -> None:
    class BrokenValkey:
        async def get(self, key: str) -> None:
            raise RedisConnectionError("down")

        async def set(self, key: str, value: str, **options: Any) -> None:
            raise RedisConnectionError("down")

    fetcher = FakeFetcher(robots_answer("User-agent: *\nDisallow: /docs/\n"))
    checker = ComplianceChecker(
        cast(Fetcher, fetcher), cast(Redis, BrokenValkey()), Settings(auth_mode="none")
    )

    report = asyncio.run(checker.before_fetch(PAGE, "enforce"))

    assert report.robots == "disallowed"


# --- TDMRep ----------------------------------------------------------------------------------


def tdmrep_answer(entries: object) -> dict[str, FetchResult | FetchError]:
    return {TDMREP: result(TDMREP, json.dumps(entries))}


def test_tdmrep_json_location_match() -> None:
    entries = [
        {"location": "/open/*", "tdm-reservation": 0},
        {
            "location": "/docs/*",
            "tdm-reservation": 1,
            "tdm-policy": "https://example.org/policy.json",
        },
        {"location": "/*", "tdm-reservation": 0},
    ]

    reserved = check(tdmrep_answer(entries))
    open_page = check(tdmrep_answer(entries), url=f"{ORIGIN}/open/a")
    other = check(tdmrep_answer(entries), url=f"{ORIGIN}/elsewhere")

    assert reserved.tdm_reserved is True
    assert reserved.tdm_policy == "https://example.org/policy.json"
    assert reserved.tdm_source == "tdmrep.json"
    assert reserved.decision == "blocked"
    assert blocking_code(reserved) is ErrorCode.TDM_RESERVED
    assert open_page.tdm_reserved is False
    assert open_page.tdm_source == "tdmrep.json"
    assert blocking_code(open_page) is None
    assert other.tdm_reserved is False


@pytest.mark.parametrize(
    ("location", "path", "expected"),
    [
        ("/", "/anything", True),
        ("/docs", "/docs/page.html", True),
        ("/docs/*", "/docs/page.html", True),
        ("/docs/*", "/doc", False),
        ("/*.pdf$", "/a/b.pdf", True),
        ("/*.pdf$", "/a/b.pdf?x=1", False),
        ("/a$", "/a", True),
        ("/a$", "/ab", False),
        ("/a*b*c", "/a-x-b-y-c", True),
        ("/a*b*c", "/a-c-b", False),
        ("/ab*b$", "/ab", False),
        ("*", "/x", True),
    ],
)
def test_tdmrep_location_patterns(location: str, path: str, expected: bool) -> None:
    entries = [{"location": location, "tdm-reservation": 1}]

    report = check(tdmrep_answer(entries), url=f"{ORIGIN}{path}")

    assert (report.tdm_reserved is True) is expected


def test_tdmrep_first_matching_entry_wins() -> None:
    entries = [
        {"location": "/*", "tdm-reservation": 0},
        {"location": "/docs/*", "tdm-reservation": 1},
    ]

    assert check(tdmrep_answer(entries)).tdm_reserved is False


def test_tdmrep_accepts_string_values_and_skips_entries_without_a_decision() -> None:
    entries = [
        {"location": "/docs/*"},
        {"location": "/docs/*", "tdm-reservation": "1"},
    ]

    assert check(tdmrep_answer(entries)).tdm_reserved is True


def test_tdmrep_no_match_leaves_unknown() -> None:
    report = check(tdmrep_answer([{"location": "/other/*", "tdm-reservation": 1}]))

    assert report.tdm_reserved is None
    assert report.tdm_source is None


def test_tdmrep_reserved_only_reported_in_report_mode() -> None:
    report = check(tdmrep_answer([{"location": "/*", "tdm-reservation": 1}]), mode="report")

    assert report.tdm_reserved is True
    assert report.decision == "reported"
    assert blocking_code(report) is None


@pytest.mark.parametrize(
    "body",
    ["not json", "{}", '"text"', "[1, 2, null]", '[{"location": 5, "tdm-reservation": 1}]'],
)
def test_malformed_tdmrep_is_ignored(body: str) -> None:
    report = check({TDMREP: result(TDMREP, body)})

    assert report.tdm_reserved is None
    assert report.decision == "allowed"


def test_tdmrep_server_error_is_not_a_block() -> None:
    report = check({TDMREP: result(TDMREP, "down", 500)})

    assert report.tdm_reserved is None
    assert report.decision == "allowed"


def test_tdm_header_and_meta() -> None:
    base = check({})
    checker, _, _ = make_checker({})

    by_header = checker.after_fetch(
        base, page_result({"tdm-reservation": "1", "tdm-policy": "https://example.org/p"}), None
    )
    html = '<html><head><meta name="TDM-Reservation" content="1">'
    html += '<meta name="tdm-policy" content="https://example.org/meta-policy"></head></html>'
    by_meta = checker.after_fetch(base, page_result(), html)
    open_header = checker.after_fetch(base, page_result({"tdm-reservation": "0"}), None)

    assert by_header.tdm_reserved is True
    assert by_header.tdm_source == "header"
    assert by_header.tdm_policy == "https://example.org/p"
    assert by_header.decision == "blocked"
    assert blocking_code(by_header) is ErrorCode.TDM_RESERVED
    assert by_meta.tdm_reserved is True
    assert by_meta.tdm_source == "meta"
    assert by_meta.tdm_policy == "https://example.org/meta-policy"
    assert open_header.tdm_reserved is False
    assert open_header.tdm_source == "header"
    assert open_header.decision == "allowed"
    assert base.tdm_reserved is None  # after_fetch returns a new report


def test_repeated_tdm_reservation_header_uses_the_first_value() -> None:
    checker, _, _ = make_checker({})

    reserved = checker.after_fetch(check({}), page_result({"tdm-reservation": "1, 1"}), None)
    open_first = checker.after_fetch(check({}), page_result({"tdm-reservation": "0, 1"}), None)

    assert reserved.tdm_reserved is True
    assert open_first.tdm_reserved is False


def test_header_reservation_overrides_an_open_tdmrep_entry() -> None:
    checker, _, _ = make_checker(tdmrep_answer([{"location": "/*", "tdm-reservation": 0}]))
    before = asyncio.run(checker.before_fetch(PAGE, "enforce"))

    after = checker.after_fetch(before, page_result({"tdm-reservation": "1"}), None)

    assert before.tdm_reserved is False
    assert after.tdm_reserved is True
    assert after.tdm_source == "header"


def test_tdmrep_reservation_is_kept_when_the_page_says_nothing() -> None:
    before = check(tdmrep_answer([{"location": "/*", "tdm-reservation": 1}]), mode="report")
    checker, _, _ = make_checker({})

    after = checker.after_fetch(before, page_result(), "<html><head></head></html>")

    assert after.tdm_reserved is True
    assert after.tdm_source == "tdmrep.json"
    assert after.decision == "reported"
    assert len(after.reasons) == len(before.reasons)  # no duplicate reason


def test_meta_after_body_start_is_ignored() -> None:
    checker, _, _ = make_checker({})
    html = '<html><head></head><body><meta name="tdm-reservation" content="1"></body></html>'

    after = checker.after_fetch(check({}), page_result(), html)

    assert after.tdm_reserved is None


# --- noai ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["noai", "noindex, noai", "NOML", "text-extraction: noai", "googlebot: noindex, robots: noml"],
)
def test_x_robots_tag_noai(value: str) -> None:
    checker, _, _ = make_checker({})

    after = checker.after_fetch(check({}), page_result({"x-robots-tag": value}), None)

    assert after.noai is True
    assert after.decision == "blocked"
    assert blocking_code(after) is ErrorCode.TDM_RESERVED


@pytest.mark.parametrize(
    "value",
    [
        "noindex, nofollow",
        "googlebot: noai",
        "otherbot: noml, noindex",
        "unavailable_after: 25 Jun",
    ],
)
def test_x_robots_tag_without_noai_for_us(value: str) -> None:
    checker, _, _ = make_checker({})

    after = checker.after_fetch(check({}), page_result({"x-robots-tag": value}), None)

    assert after.noai is False
    assert after.decision == "allowed"


def test_meta_robots_noai() -> None:
    checker, _, _ = make_checker({})
    html = '<html><head><meta name="robots" content="index, NoAI, noimageai"></head></html>'

    after = checker.after_fetch(check({}), page_result(), html)

    assert after.noai is True
    assert blocking_code(after) is ErrorCode.TDM_RESERVED


def test_meta_robots_for_another_bot_is_ignored() -> None:
    checker, _, _ = make_checker({})
    html = '<html><head><meta name="googlebot" content="noai"></head></html>'

    assert checker.after_fetch(check({}), page_result(), html).noai is False


def test_noai_only_reported_in_report_mode() -> None:
    checker, _, _ = make_checker({})

    after = checker.after_fetch(
        check({}, mode="report"), page_result({"x-robots-tag": "noai"}), None
    )

    assert after.noai is True
    assert after.decision == "reported"
    assert blocking_code(after) is None


def test_robots_block_survives_after_fetch() -> None:
    checker, _, _ = make_checker({})
    before = check(robots_answer("User-agent: *\nDisallow: /\n"), mode="report")

    after = checker.after_fetch(before, page_result(), "<html></html>")

    assert after.decision == "reported"
    assert after.robots == "disallowed"


# --- ai.txt ----------------------------------------------------------------------------------


def test_ai_txt_disallow() -> None:
    answers: dict[str, FetchResult | FetchError] = {
        AI_TXT: result(AI_TXT, "User-Agent: *\nDisallow: /\n")
    }

    enforced = check(answers)
    reported = check(answers, mode="report")

    assert enforced.ai_txt == "disallowed"
    assert blocking_code(enforced) is ErrorCode.TDM_RESERVED
    assert reported.ai_txt == "disallowed"
    assert reported.decision == "reported"
    assert blocking_code(reported) is None


def test_ai_txt_allowing_and_absent() -> None:
    allowing = check({AI_TXT: result(AI_TXT, "User-Agent: *\nDisallow: /private/\n")})
    absent = check({})

    assert allowing.ai_txt == "allowed"
    assert absent.ai_txt == "absent"
    assert blocking_code(allowing) is None
    assert blocking_code(absent) is None


def test_ai_txt_outage_is_not_a_block() -> None:
    server_error = check({AI_TXT: result(AI_TXT, "down", 503)})
    fetch_error = check({AI_TXT: FetchError(ErrorCode.CONNECTION_ERROR, "refused")})

    assert server_error.ai_txt == "absent"
    assert fetch_error.ai_txt == "absent"
    assert fetch_error.decision == "allowed"


def test_llms_txt_is_not_checked() -> None:
    checker, fetcher, _ = make_checker({})

    asyncio.run(checker.before_fetch(PAGE, "enforce"))

    assert not any("llms" in url for url in fetcher.urls())


# --- ignore, modes and the blocking code ------------------------------------------------------


def test_ignore_skips_all_fetches() -> None:
    checker, fetcher, valkey = make_checker(robots_answer("User-agent: *\nDisallow: /\n"))

    async def scenario() -> tuple[ComplianceReport, ComplianceReport, list[bytes | str]]:
        before = await checker.before_fetch(PAGE, "ignore")
        after = checker.after_fetch(
            before, page_result({"tdm-reservation": "1", "x-robots-tag": "noai"}), None
        )
        return before, after, await valkey.keys("*")

    before, after, keys = asyncio.run(scenario())

    assert fetcher.calls == []
    assert keys == []
    assert before.mode == "ignore"
    assert before.decision == "skipped"
    assert before.robots == "not_checked"
    assert before.ai_txt == "not_checked"
    assert after.decision == "skipped"
    assert blocking_code(before) is None
    assert blocking_code(after) is None


def test_blocking_code_prefers_robots() -> None:
    report = check(
        {
            **robots_answer("User-agent: *\nDisallow: /\n"),
            AI_TXT: result(AI_TXT, "User-Agent: *\nDisallow: /\n"),
        }
    )

    assert blocking_code(report) is ErrorCode.BLOCKED_BY_ROBOTS


def test_report_serialises_to_json() -> None:
    report = check(tdmrep_answer([{"location": "/*", "tdm-reservation": 1}]))

    dumped = report.model_dump(mode="json")

    assert dumped["mode"] == "enforce"
    assert dumped["decision"] == "blocked"
    assert dumped["tdm_source"] == "tdmrep.json"
    assert isinstance(dumped["reasons"], list)


# --- resolve_mode ----------------------------------------------------------------------------


def settings_for(**overrides: Any) -> Settings:
    return Settings(auth_mode="none", **overrides)


def test_resolve_mode_none_gives_the_default() -> None:
    assert resolve_mode(None, USER, settings_for()) == "enforce"
    assert resolve_mode(None, USER, settings_for(compliance_default="report")) == "report"


def test_resolve_mode_user_may_pick_report_when_allowed() -> None:
    assert resolve_mode("report", USER, settings_for()) == "report"
    assert resolve_mode("enforce", USER, settings_for(compliance_default="report")) == "enforce"


def test_resolve_mode_user_cannot_ignore_403() -> None:
    with pytest.raises(ServiceError) as raised:
        resolve_mode("ignore", USER, settings_for())

    assert raised.value.code is ErrorCode.FORBIDDEN
    assert raised.value.http_status == 403


def test_resolve_mode_override_disabled() -> None:
    settings = settings_for(compliance_allow_override=False)

    with pytest.raises(ServiceError) as raised:
        resolve_mode("report", USER, settings)

    assert raised.value.code is ErrorCode.FORBIDDEN
    assert raised.value.http_status == 403
    assert resolve_mode("enforce", USER, settings) == "enforce"  # the default is always fine
    assert resolve_mode(None, USER, settings) == "enforce"


def test_resolve_mode_user_asking_for_the_default_ignore_is_fine() -> None:
    assert resolve_mode("ignore", USER, settings_for(compliance_default="ignore")) == "ignore"


def test_admin_may_ignore() -> None:
    assert resolve_mode("ignore", ADMIN, settings_for()) == "ignore"
    assert resolve_mode("ignore", ADMIN, settings_for(compliance_allow_override=False)) == "ignore"
    assert resolve_mode("report", ADMIN, settings_for(compliance_allow_override=False)) == "report"


# --- settings --------------------------------------------------------------------------------


def test_compliance_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("COMPLIANCE_DEFAULT", "COMPLIANCE_ALLOW_OVERRIDE", "COMPLIANCE_CACHE_SECONDS"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(auth_mode="none")

    assert settings.compliance_default == "enforce"
    assert settings.compliance_allow_override is True
    assert settings.compliance_cache_seconds == 3600


def test_compliance_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPLIANCE_DEFAULT", "report")
    monkeypatch.setenv("COMPLIANCE_ALLOW_OVERRIDE", "false")
    monkeypatch.setenv("COMPLIANCE_CACHE_SECONDS", "120")

    settings = Settings(auth_mode="none")

    assert settings.compliance_default == "report"
    assert settings.compliance_allow_override is False
    assert settings.compliance_cache_seconds == 120


@pytest.mark.parametrize(
    ("name", "value"),
    [("COMPLIANCE_DEFAULT", "strict"), ("COMPLIANCE_CACHE_SECONDS", "-1")],
)
def test_invalid_compliance_settings_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name.lower()):
        Settings(auth_mode="none")


def test_with_page_findings_carries_the_stored_pages_statements() -> None:
    fresh = ComplianceReport(mode="report", robots="allowed", ai_txt="absent")
    previous = ComplianceReport(
        mode="report",
        decision="reported",
        robots="disallowed",  # an old origin-file finding: never carried
        tdm_reserved=True,
        tdm_policy="https://example.org/policy",
        tdm_source="meta",
        noai=True,
    )

    merged = compliance.with_page_findings(fresh, previous)

    assert merged.robots == "allowed"
    assert merged.tdm_reserved is True
    assert merged.tdm_policy == "https://example.org/policy"
    assert merged.tdm_source == "meta"
    assert merged.noai is True
    assert merged.decision == "reported"
    assert len(merged.reasons) == 2


def test_with_page_findings_keeps_origin_reservations_and_ignores_tdmrep() -> None:
    fresh = ComplianceReport(
        mode="enforce", robots="allowed", tdm_reserved=True, tdm_source="tdmrep.json"
    )
    previous = ComplianceReport(mode="enforce", tdm_reserved=False, tdm_source="meta")
    assert compliance.with_page_findings(fresh, previous) == fresh

    stale_origin = ComplianceReport(mode="enforce", tdm_reserved=True, tdm_source="tdmrep.json")
    plain = ComplianceReport(mode="enforce", robots="allowed")
    assert compliance.with_page_findings(plain, stale_origin) == plain


def test_truncated_robots_is_parsed_from_its_first_part() -> None:
    head = "User-agent: *\nDisallow: /private/\n"
    filler = "".join(f"Disallow: /old/{number}/\n" for number in range(30_000))
    lines = (head + filler)[:500_000]
    body = lines[: lines.rindex("\n") + 1] + "Disallow: /d"  # cut inside "/docs-archive/"
    truncated = replace(result(ROBOTS, body), truncated=True)
    answers: dict[str, FetchResult | FetchError] = {ROBOTS: truncated}

    private = check(answers, url=f"{ORIGIN}/private/x")
    docs = check(answers)

    assert private.robots == "disallowed"
    assert private.decision == "blocked"
    assert docs.robots == "allowed"  # the cut-off last line is not a rule


def test_concurrent_checks_of_one_origin_fetch_each_file_once() -> None:
    checker, fetcher, _ = make_checker(robots_answer("User-agent: *\nDisallow: /docs/\n"))
    answer_fetch = fetcher.fetch

    async def slow_fetch(url: str, **options: Any) -> FetchResult:
        await asyncio.sleep(0.05)
        return await answer_fetch(url, **options)

    fetcher.fetch = slow_fetch  # type: ignore[method-assign]

    async def scenario() -> list[ComplianceReport]:
        return await asyncio.gather(*(checker.before_fetch(PAGE, "enforce") for _ in range(10)))

    reports = asyncio.run(scenario())

    assert {report.robots for report in reports} == {"disallowed"}
    assert sorted(fetcher.urls()) == sorted([ROBOTS, TDMREP, AI_TXT])


def test_a_failed_shared_load_fails_every_waiter_and_is_retried() -> None:
    checker, fetcher, _ = make_checker({})
    answer_fetch = fetcher.fetch
    failing = True

    attempts: list[str] = []

    async def flaky_fetch(url: str, **options: Any) -> FetchResult:
        attempts.append(url)
        await asyncio.sleep(0.05)
        if failing and url == ROBOTS:
            raise RuntimeError("fetcher broke")
        return await answer_fetch(url, **options)

    fetcher.fetch = flaky_fetch  # type: ignore[method-assign]

    async def scenario() -> list[ComplianceReport | BaseException]:
        return await asyncio.gather(
            *(checker.before_fetch(PAGE, "enforce") for _ in range(3)), return_exceptions=True
        )

    first = asyncio.run(scenario())
    failing = False
    second = asyncio.run(scenario())

    # The three file loads run in a task group, so the error arrives in an exception group.
    assert all(
        isinstance(outcome, ExceptionGroup) and outcome.subgroup(RuntimeError) is not None
        for outcome in first
    )
    assert attempts.count(ROBOTS) == 2  # once for the failed round, once for the retry
    assert all(isinstance(outcome, ComplianceReport) for outcome in second)


def test_rules_are_parsed_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    threads: list[str] = []
    real_parse = Protego.parse

    def recording_parse(text: str) -> Any:
        threads.append(threading.current_thread().name)
        return real_parse(text)

    monkeypatch.setattr(Protego, "parse", recording_parse)
    answers = robots_answer("User-agent: *\nDisallow: /docs/\n")
    answers[AI_TXT] = result(AI_TXT, "User-agent: *\nDisallow: /\n")

    report = check(answers)

    assert report.robots == "disallowed"
    assert report.ai_txt == "disallowed"
    assert len(threads) == 2
    assert threading.main_thread().name not in threads
