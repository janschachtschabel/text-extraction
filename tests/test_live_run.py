"""Scheduling, output and summary of the live harness (`tests/live/run_live.py`)."""

import asyncio
import io
import json
import time
from collections import defaultdict
from pathlib import Path

import httpx
import pytest

from tests.live import run_live
from tests.live.run_live import Record, domain_of, ok_rate, read_urls, summarize


def ok_body(url: str) -> dict[str, object]:
    return {
        "status": "success",
        "data": {"word_count": 100, "compliance": {"decision": "reported"}},
        "error": None,
    }


def run_with(
    transport: httpx.MockTransport, urls: list[str], concurrency: int = 4, mode: str = "auto"
) -> tuple[list[Record], list[dict[str, object]]]:
    sink = io.StringIO()

    async def scenario() -> list[Record]:
        async with httpx.AsyncClient(transport=transport, base_url="http://service") as client:
            return await run_live.run(
                urls,
                client,
                concurrency=concurrency,
                compliance="report",
                min_words=50,
                sink=sink,
                mode=mode,
            )

    records = asyncio.run(scenario())
    return records, [json.loads(line) for line in sink.getvalue().splitlines()]


def test_read_urls_takes_the_url_column_and_keeps_order(tmp_path: Path) -> None:
    path = tmp_path / "urls.csv"
    path.write_text(
        "url,category\nhttps://b.example/,x\nhttps://a.example/,y\nhttps://b.example/,z\n"
    )
    assert read_urls(path) == ["https://b.example/", "https://a.example/", "https://b.example/"]


def test_read_urls_needs_a_url_header(tmp_path: Path) -> None:
    path = tmp_path / "urls.csv"
    path.write_text("https://a.example/\n")
    with pytest.raises(ValueError, match="url"):
        read_urls(path)


@pytest.mark.parametrize("name", ["smoke", "wlo_50", "wlo_200", "wlo_4000"])
def test_shipped_url_lists_have_a_url_column(name: str) -> None:
    urls = read_urls(Path(run_live.__file__).parent / "urls" / f"{name}.csv")
    assert urls
    assert all(url.startswith(("http://", "https://")) for url in urls)


def test_domain_of_lower_cases_the_host() -> None:
    assert domain_of("https://WWW.Example.org:8443/a?b=1") == "www.example.org"
    assert domain_of("not a url") == ""


def test_run_sends_the_documented_request_and_writes_one_line_per_url() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json=ok_body(body["url"]))

    records, lines = run_with(httpx.MockTransport(handler), ["https://a.example/x"])
    assert seen == [
        {
            "url": "https://a.example/x",
            "mode": "auto",
            "format": "text",
            "cache": False,
            "compliance": "report",
        }
    ]
    assert [r.bucket for r in records] == ["ok"]
    assert lines[0].keys() == {
        "url",
        "domain",
        "bucket",
        "http_status",
        "status",
        "error_code",
        "word_count",
        "compliance_decision",
        "mode_used",
        "warnings",
        "elapsed_ms",
    }


def test_mode_option_is_sent_and_reported() -> None:
    sent: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body["mode"])
        if body["url"].endswith("/failed"):
            return httpx.Response(
                200, json={"status": "failed", "data": None, "error": {"code": "timeout"}}
            )
        used = "browser" if body["url"].endswith("/js") else "fast"
        answer = ok_body(body["url"])
        answer["data"] = {"word_count": 100, "mode_used": used, "compliance": {}}
        return httpx.Response(200, json=answer)

    urls = ["https://a.example/js", "https://a.example/plain", "https://b.example/failed"]
    # The default is auto, the mode of the gate.
    run_with(httpx.MockTransport(handler), urls[:1])
    assert sent == ["auto"]

    sent.clear()
    records, lines = run_with(httpx.MockTransport(handler), urls, mode="browser")
    assert sent == ["browser"] * 3
    by_url = {r.url: r.mode_used for r in records}
    assert by_url == {
        "https://a.example/js": "browser",
        "https://a.example/plain": "fast",
        "https://b.example/failed": None,
    }
    assert {line["url"]: line["mode_used"] for line in lines} == by_url

    text = summarize(records)
    assert "| browser | 1 | 1 |" in text
    assert "| fast | 1 | 1 |" in text
    assert "| (none) | 1 | 0 |" in text

    # The command line sets the same option; the default is auto.
    assert run_live.parse_args([]).mode == "auto"
    assert run_live.parse_args(["--mode", "fast"]).mode == "fast"
    with pytest.raises(SystemExit):
        run_live.parse_args(["--mode", "bogus"])


def test_run_keeps_one_request_per_domain_in_flight_and_respects_concurrency() -> None:
    in_flight: dict[str, int] = defaultdict(int)
    peak_per_domain: dict[str, int] = defaultdict(int)
    total = 0
    peak_total = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal total, peak_total
        url = json.loads(request.content)["url"]
        domain = domain_of(url)
        in_flight[domain] += 1
        total += 1
        peak_per_domain[domain] = max(peak_per_domain[domain], in_flight[domain])
        peak_total = max(peak_total, total)
        await asyncio.sleep(0.01)
        in_flight[domain] -= 1
        total -= 1
        return httpx.Response(200, json=ok_body(url))

    urls = [f"https://{host}.example/{n}" for host in "abc" for n in range(4)]
    records, _ = run_with(httpx.MockTransport(handler), urls, concurrency=2)
    assert len(records) == len(urls)
    assert max(peak_per_domain.values()) == 1
    assert peak_total == 2


def test_run_takes_turns_between_domains() -> None:
    order: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        url = json.loads(request.content)["url"]
        order.append(domain_of(url))
        await asyncio.sleep(0)
        return httpx.Response(200, json=ok_body(url))

    urls = [f"https://{host}.example/{n}" for host in "ab" for n in range(3)]
    run_with(httpx.MockTransport(handler), urls, concurrency=1)
    assert order == ["a.example", "b.example"] * 3


def test_run_records_a_transport_failure_and_a_non_json_body_as_service_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "down" in json.loads(request.content)["url"]:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, text="<html>oops</html>")

    records, _ = run_with(
        httpx.MockTransport(handler), ["https://down.example/", "https://html.example/"]
    )
    by_domain = {r.domain: r for r in records}
    assert by_domain["down.example"].bucket == "service_error"
    assert by_domain["down.example"].error_code == "client_ReadTimeout"
    assert by_domain["down.example"].http_status is None
    assert by_domain["html.example"].bucket == "service_error"
    assert by_domain["html.example"].http_status == 200


def record(
    domain: str,
    bucket: str,
    decision: str | None = "allowed",
    mode_used: str | None = "fast",
    *,
    status: str | None = "success",
    error_code: str | None = None,
    warnings: tuple[str, ...] = (),
) -> Record:
    return Record(
        url=f"https://{domain}/",
        domain=domain,
        bucket=bucket,  # type: ignore[arg-type]
        http_status=200,
        status=status,
        error_code=error_code,
        word_count=100,
        compliance_decision=decision,
        mode_used=mode_used,
        warnings=list(warnings),
        elapsed_ms=1,
    )


def test_run_records_the_warnings_of_the_answer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        answer = ok_body("x")
        answer["data"] = {"word_count": 100, "warnings": ["browser_unavailable", 7]}
        return httpx.Response(200, json=answer)

    (only,), (line,) = run_with(httpx.MockTransport(handler), ["https://a.example/"])

    assert only.warnings == ["browser_unavailable"]  # strings only
    assert line["warnings"] == ["browser_unavailable"]


def test_summary_counts_browser_unavailable_and_error_codes() -> None:
    # M-7: the summary is the only per-cause signal of a CI run.
    records = [
        record("a.example", "ok", warnings=("browser_unavailable",)),
        record("a.example", "too_short", status="partial", error_code="content_too_short"),
        record("b.example", "connection_error", status="failed", error_code="timeout"),
        record("b.example", "connection_error", status="failed", error_code="timeout"),
        record("c.example", "connection_error", status="failed", error_code="dns_error"),
        record("c.example", "ok", status="success", error_code=None),
        record("d.example", "service_error", status=None, error_code="client_ReadTimeout"),
    ]

    text = summarize(records)

    assert "Answers with the warning `browser_unavailable`: 1" in text
    section = text.split("### Error codes")[1].split("### URLs not ok")[0]
    assert "| Code | URLs | Top domains |" in section
    assert "| timeout | 2 | b.example 2 |" in section
    assert "| content_too_short | 1 | a.example 1 |" in section
    assert "| dns_error | 1 | c.example 1 |" in section
    assert section.index("| timeout |") < section.index("| content_too_short |")
    assert "client_ReadTimeout" not in section  # no answer, no envelope: counted as a bucket


def test_summary_without_error_codes_has_no_error_table() -> None:
    text = summarize([record("a.example", "ok")])
    assert "Answers with the warning `browser_unavailable`: 0" in text
    assert "### Error codes" not in text


def test_summary_lists_each_url_that_is_not_ok() -> None:
    # Two runs are compared URL by URL: the domain tables hide which pages flipped.
    records = [
        record(
            "b.example",
            "too_short",
            mode_used="browser",
            status="partial",
            error_code="content_too_short",
        ),
        record("a.example", "ok"),
        record(
            "a.example", "connection_error", mode_used=None, status="failed", error_code="timeout"
        ),
    ]

    text = summarize(records)

    section = text.split("### URLs not ok")[1]
    assert "| URL | Bucket | Code | Mode | Words |" in section
    assert "| https://a.example/ | connection_error | timeout | - | 100 |" in section
    assert "| https://b.example/ | too_short | content_too_short | browser | 100 |" in section
    assert section.index("a.example") < section.index("b.example")
    assert section.count("| https://") == 2


def test_summary_without_failures_has_no_url_table() -> None:
    assert "### URLs not ok" not in summarize([record("a.example", "ok")])


def test_ok_rate_counts_against_all_urls() -> None:
    records = [record("a", "ok"), record("a", "too_short"), record("b", "ok"), record("b", "ok")]
    assert ok_rate(records) == 75.0
    assert ok_rate([]) == 0.0


def test_summary_lists_buckets_rate_would_be_blocked_and_domains() -> None:
    records = [
        record("a.example", "ok", "reported"),
        record("a.example", "ok"),
        record("a.example", "too_short"),
        record("b.example", "blocked", "blocked"),
    ]
    text = summarize(records, top_domains=1)
    assert "Ok rate: 50.0 % (2 of 4)" in text
    assert "Would be blocked under `enforce`: 1" in text
    assert "| ok | 2 | 50.0 % |" in text
    assert "| too_short | 1 | 25.0 % |" in text
    assert "| a.example | 3 | 2 | 1 |" in text
    assert "b.example" not in text.split("### Top")[1].split("### URLs not ok")[0]


@pytest.mark.parametrize(("min_ok_rate", "exit_code"), [(None, 0), (50.0, 0), (80.0, 1)])
def test_main_exit_code_follows_min_ok_rate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    min_ok_rate: float | None,
    exit_code: int,
) -> None:
    urls = tmp_path / "urls.csv"
    urls.write_text("url\nhttps://a.example/ok\nhttps://b.example/short\n")

    def handler(request: httpx.Request) -> httpx.Response:
        url = json.loads(request.content)["url"]
        words = 100 if url.endswith("/ok") else 3
        body = ok_body(url)
        body["data"] = {"word_count": words, "compliance": {"decision": "allowed"}}
        return httpx.Response(200, json=body)

    real_client = httpx.AsyncClient

    def client_with_mock(**kwargs: object) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", client_with_mock)
    out = tmp_path / "nested" / "out.jsonl"
    summary = tmp_path / "summary.md"
    argv = ["--urls", str(urls), "--out", str(out), "--summary-out", str(summary)]
    if min_ok_rate is not None:
        argv += ["--min-ok-rate", str(min_ok_rate)]

    assert run_live.main(argv) == exit_code
    assert len(out.read_text().splitlines()) == 2
    assert "Ok rate: 50.0 % (1 of 2)" in capsys.readouterr().out
    assert "Ok rate: 50.0 % (1 of 2)" in summary.read_text()


def test_read_urls_skips_rows_without_a_url_and_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "urls.csv"
    path.write_text("category,url\nx,https://a.example/\nshort-row\ny,\nz,https://b.example/\n")

    assert read_urls(path) == ["https://a.example/", "https://b.example/"]
    warnings = capsys.readouterr().err
    assert "line 3" in warnings
    assert "line 4" in warnings


def test_run_records_any_client_failure_and_keeps_going() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "broken" in json.loads(request.content)["url"]:
            raise RuntimeError("unexpected")
        return httpx.Response(200, json=ok_body("x"))

    records, lines = run_with(
        httpx.MockTransport(handler), ["https://broken.example/", "https://fine.example/"]
    )

    by_domain = {r.domain: r for r in records}
    assert by_domain["broken.example"].bucket == "service_error"
    assert by_domain["broken.example"].error_code == "client_RuntimeError"
    assert by_domain["fine.example"].bucket == "ok"
    assert len(lines) == 2


def test_run_gives_each_request_a_total_deadline() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)  # a server that trickles: no single phase times out
        return httpx.Response(200, json=ok_body("x"))

    sink = io.StringIO()

    async def scenario() -> list[Record]:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://service") as client:
            return await run_live.run(
                ["https://slow.example/"],
                client,
                concurrency=1,
                compliance="report",
                min_words=50,
                sink=sink,
                deadline_seconds=0.2,
            )

    started = time.monotonic()
    (only,) = asyncio.run(scenario())

    assert time.monotonic() - started < 10
    assert only.bucket == "service_error"
    assert only.error_code == "client_TimeoutError"
