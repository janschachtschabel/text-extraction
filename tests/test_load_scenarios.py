"""The load profile (`tests/load/scenarios.py`, ruling P11-S4): every scenario sends load to the
local site farm only, and the profile holds every part of the ruling."""

import io
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import docx
import pytest
from pydantic import BaseModel

from tests.e2e.site_farm import SITEMAP_SITES, SITEMAPS, related
from tests.load.scenarios import (
    GENERATED_DOCX,
    REPO_ROOT,
    SCENARIOS,
    Scenario,
    farm_site,
    upload_bytes,
)
from text_extraction.extract_models import ChunkRequest, ExtractRequest, StructuredRequest
from text_extraction.file_models import FileForm
from text_extraction.job_models import BatchRequest
from text_extraction.settings import Settings
from text_extraction.site.types import SiteStructureRequest

FARM = "http://farm.load.test:18000"
# The farm's own port and the ports of its sitemap sites (``site_farm.SITEMAP_SITES``).
FARM_PORTS = {18000 + offset for offset in (0, *SITEMAP_SITES)}
DEFAULTS = Settings.model_fields


def strings(value: object) -> Iterator[str]:
    """Every string anywhere in a JSON value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def urls(body: dict[str, object]) -> list[str]:
    return [text for text in strings(body) if "://" in text]


def by_name(name: str) -> Scenario:
    return next(scenario for scenario in SCENARIOS if scenario.name == name)


def test_every_scenario_targets_the_farm() -> None:
    for scenario in SCENARIOS:
        for index in (0, scenario.count - 1):
            body = scenario.body(index, FARM)
            found = urls(body)
            if scenario.kind != "upload":  # an upload sends a file of the repo, no URL
                assert found, scenario.name
            for url in found:
                parts = urlsplit(url)
                assert (parts.scheme, parts.hostname) == ("http", "farm.load.test"), url
                assert parts.port in FARM_PORTS, (scenario.name, url)
                assert parts.username is None, url
                assert parts.password is None, url


def test_scenario_names_are_unique_and_the_plan_is_sane() -> None:
    names = Counter(scenario.name for scenario in SCENARIOS)
    assert all(count == 1 for count in names.values()), names
    for scenario in SCENARIOS:
        assert scenario.count >= 1, scenario.name
        assert 1 <= scenario.concurrency <= scenario.count, scenario.name
        assert scenario.settle_seconds >= 0, scenario.name
        assert scenario.endpoint.startswith("/"), scenario.name


def test_the_profile_holds_the_parts_of_p11_s4() -> None:
    plan = {scenario.name: scenario for scenario in SCENARIOS}

    assert {plan["sync-fast-c10"].concurrency, plan["sync-fast-c30"].concurrency} == {10, 30}
    assert len(urls(plan["batch-1000"].body(0, FARM))) == 1000
    assert plan["browser-100"].count == 100
    assert plan["upload-docs-50"].count == 50
    assert plan["upload-ocr-10"].count == 10
    assert plan["pii-200"].count == 200
    assert plan["asr-10"].count == 10
    assert plan["structured-selectors-200"].count == 200
    assert plan["structured-llm-50"].count == 50
    assert plan["chunk-semantic-50"].count == 50  # the semantic-chunk cost note (P11-S6)
    assert plan["site-crawl"].count == 1
    assert plan["mcp-20"].count == 20


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.name)
def test_each_body_is_a_valid_request_of_its_route(scenario: Scenario) -> None:
    """The bodies are checked against the service's own request models, so a renamed field
    fails here and not in a load run."""
    body = scenario.body(scenario.count - 1, FARM)
    models: dict[str, type[BaseModel]] = {
        "/v1/extract": ExtractRequest,
        "/v1/structured": StructuredRequest,
        "/v1/chunk": ChunkRequest,
        "/v1/batch": BatchRequest,
        "/v1/site-structure": SiteStructureRequest,
    }
    if scenario.kind == "upload":
        assert scenario.endpoint == "/v1/extract/file"
        fields = dict(body)
        file = fields.pop("file")
        assert isinstance(file, str)
        assert not Path(file).is_absolute()
        assert file == GENERATED_DOCX or (REPO_ROOT / file).is_file(), file
        assert upload_bytes(file)
        assert all(isinstance(value, str) for value in fields.values())
        FileForm.model_validate(fields)
    elif scenario.kind == "mcp":
        assert scenario.endpoint == "/mcp"
        assert body["method"] == "tools/call"
        params = body["params"]
        assert isinstance(params, dict)
        assert params["name"] == "extract_url"
    else:
        models[scenario.endpoint].model_validate(body)


def test_document_uploads_mix_a_long_pdf_and_a_long_docx() -> None:
    """Both MarkItDown paths get load: a 120-page PDF and a DOCX of farm articles, one after
    the other. Both are long enough to pass the quality gate, so every upload succeeds."""
    scenario = by_name("upload-docs-50")
    files = {str(scenario.body(index, FARM)["file"]) for index in range(scenario.count)}

    assert files == {"tests/documents/corpus/long.pdf", GENERATED_DOCX}
    report = docx.Document(io.BytesIO(upload_bytes(GENERATED_DOCX)))
    words = sum(len(paragraph.text.split()) for paragraph in report.paragraphs)
    assert words >= 10 * Settings.model_fields["quality_min_words"].default


def test_every_scenario_expects_its_outcomes() -> None:
    """A scenario names the outcomes it expects (success unless said otherwise); an error is
    never expected."""
    for scenario in SCENARIOS:
        assert scenario.expected, scenario.name
        assert scenario.expected <= {"success", "partial", "failed"}, scenario.name
    assert by_name("upload-docs-50").expected == {"success"}
    assert by_name("sync-fast-c10").expected == {"success"}
    # The farm's pages link only to farm pages, so a crawl has no failed item.
    assert by_name("site-crawl").expected == {"success"}


def test_pages_differ_between_requests_of_a_scenario() -> None:
    """Every request asks for its own page, so no answer is a copy of another."""
    scenario = by_name("sync-fast-c30")

    assert urls(scenario.body(0, FARM)) != urls(scenario.body(1, FARM))


# --- the site structure items (ruling P11-S7) -----------------------------------------------------


def site_request(name: str) -> SiteStructureRequest:
    return SiteStructureRequest.model_validate(by_name(name).body(0, FARM))


def sitemaps_of(name: str) -> tuple[str, ...]:
    """The sitemaps robots.txt lists for the scenario's start page (by its port)."""
    port = urlsplit(site_request(name).url).port
    assert port is not None
    return SITEMAP_SITES.get(port - 18000, ())


def pages_within(start_url: str, depth: int) -> int:
    path = urlsplit(start_url).path
    start = int(path.removeprefix("/p/").removesuffix(".html"))
    found, level = {start}, {start}
    for _ in range(depth):
        level = {page for number in level for page in related(number)} - found
        found |= level
    return len(found)


def test_the_default_crawl_reads_the_main_site_at_the_request_defaults() -> None:
    request = site_request("site-crawl")
    assert (request.depth, request.max_pages, request.sitemaps) == (2, 100, True)
    assert sitemaps_of("site-crawl") == ()  # robots.txt answers 404: one guess, also 404
    assert pages_within(request.url, request.depth) > request.max_pages


def test_a_crawl_runs_at_site_max_pages() -> None:
    """The crawl duration at SITE_MAX_PAGES, against JOB_TTL_SECONDS."""
    scenario = by_name("site-crawl-max-pages")
    request = site_request(scenario.name)

    assert scenario.kind == "site"
    assert scenario.count == 1
    assert request.max_pages == DEFAULTS["site_max_pages"].default
    assert request.depth <= DEFAULTS["site_max_depth"].default
    assert pages_within(request.url, request.depth) > request.max_pages
    assert sitemaps_of(scenario.name) == ()
    assert scenario.expected == {"success"}


def test_one_crawl_reads_a_sitemap_of_200000_entries() -> None:
    """Packing and encoding SITE_MAX_SITEMAP_URLS entries on the event loop."""
    scenario = by_name("sitemap-200k")
    request = site_request(scenario.name)

    assert (scenario.count, scenario.concurrency) == (1, 1)
    assert request.depth == 0  # the start page and the sitemaps only
    assert request.sitemaps
    assert sitemaps_of(scenario.name) == ("200k",)
    _, pages, _ = SITEMAPS["200k"]
    assert pages == DEFAULTS["site_max_sitemap_urls"].default
    assert scenario.expected == {"success"}


def test_three_50m_sitemaps_are_in_flight_at_once_after_one_alone() -> None:
    """The extract worker's peak memory with three 50 MiB sitemaps in flight, and the wall time
    of one alone, which the SITE_PARSE_CONCURRENCY ruling compares it with."""
    one = by_name("sitemap-50m-1")
    three = by_name("sitemaps-50m-3")

    assert sitemaps_of(one.name) == ("50m-1",)
    assert sitemaps_of(three.name) == ("50m-1", "50m-2", "50m-3")
    for scenario in (one, three):
        request = site_request(scenario.name)
        assert (scenario.count, scenario.concurrency) == (1, 1)
        assert request.depth == 0
        assert scenario.expected == {"success"}
    # One crawl's entry budget holds all three files' entries, so none is read to its first
    # entry only.
    entries = sum(SITEMAPS[name][1] for name in sitemaps_of(three.name))
    assert entries <= DEFAULTS["site_max_sitemap_urls"].default
    names = [scenario.name for scenario in SCENARIOS]
    assert names.index(one.name) < names.index(three.name)


def test_the_site_scenarios_start_on_pages_of_their_own() -> None:
    starts = [
        site_request(name).url
        for name in (
            "site-crawl",
            "site-crawl-max-pages",
            "sitemap-200k",
            "sitemap-50m-1",
            "sitemaps-50m-3",
        )
    ]
    assert len(set(starts)) == len(starts)


@pytest.mark.parametrize(
    ("farm", "offset", "site"),
    [
        ("http://farm:8000", 1, "http://farm:8001"),
        ("http://farm:8000/", 3, "http://farm:8003"),
        ("http://127.0.0.2:18000", 2, "http://127.0.0.2:18002"),
        ("http://[fd00::1]:18000", 1, "http://[fd00::1]:18001"),
        ("http://farm", 1, "http://farm:81"),
        ("https://farm", 0, "https://farm:443"),
    ],
)
def test_farm_site_is_the_farm_host_on_the_sites_port(farm: str, offset: int, site: str) -> None:
    assert farm_site(farm, offset) == site
