"""Rolling upgrades (ruling AU3-R1): what new pods store stays readable by the pods of the release
before, and readers map result values of a later release to the type's catch-all."""

import json
from typing import Any, get_args

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from pydantic import TypeAdapter, ValidationError

from text_extraction.app import create_public_app
from text_extraction.compliance import ComplianceReport
from text_extraction.convert import Link
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractResult, Timings
from text_extraction.job_models import JobOptions
from text_extraction.jobs import _options_json
from text_extraction.page_types import (
    LinkCategory,
    PageType,
    PageTypeInfo,
    TolerantLinkCategory,
    TolerantPageType,
)
from text_extraction.settings import Settings
from text_extraction.site.types import (
    Crawl,
    SiteInfo,
    SiteLink,
    SitemapChild,
    SitemapEntry,
    SitemapInfo,
    SiteOptions,
    SiteStructureRequest,
    TolerantCrawl,
)

URL = "https://a.example/"

# The fields of ``SiteOptions`` in release 0.1.5 (commit 1b133a5, ``git show
# 1b133a5:src/text_extraction/site/types.py``). That model forbids unknown fields, so a 0.1.5 pod
# reads a stored site job only if its ``site`` object has no other key.
SITE_OPTIONS_0_1_5 = frozenset({"depth", "max_pages", "sitemaps", "scope", "metadata", "cache"})


def test_default_site_options_stay_readable_by_0_1_5() -> None:
    """Every option that came after 0.1.5 is left out of the stored form at its default."""
    stored = json.loads(_options_json(JobOptions(site=SiteOptions(), compliance="enforce")))

    assert set(stored["site"]) <= SITE_OPTIONS_0_1_5


def _result_json() -> dict[str, Any]:
    """A page result of a site job with every field that carries a page type, a link category or
    a crawl value, as JSON."""
    result = ExtractResult(
        url=URL,
        final_url=URL,
        upstream_status=200,
        content_type="text/html",
        mode_used="fast",
        attempts=1,
        language="en",
        word_count=2,
        title=None,
        format="text",
        content="Hello world",
        quality=None,
        timings=Timings(total_ms=1, compliance_ms=0, fetch_ms=0, convert_ms=1),
        compliance=ComplianceReport(mode="report"),
        metadata=None,
        links=[Link(url=URL + "x", text="x", kind="internal", category="content")],
        fingerprint=None,
        cache="miss",
        warnings=[],
        page_type=PageTypeInfo(type="content", signals=["word_count"]),
        site=SiteInfo(
            role="page",
            depth=0,
            parent=None,
            links=[
                SiteLink(
                    url=URL + "x",
                    text="x",
                    kind="internal",
                    source="a",
                    rel=[],
                    crawl="queued",
                    category="content",
                )
            ],
            sitemap=SitemapInfo(
                format="index",
                type_hint="content",
                entries=[SitemapEntry(loc=URL + "y", type_guess="content")],
                children=[SitemapChild(url=URL + "s.xml", crawl="queued")],
                entry_count=1,
            ),
        ),
    )
    data: dict[str, Any] = json.loads(
        Envelope[ExtractResult](status="success", data=result).model_dump_json()
    )
    return data


def _read(raw: dict[str, Any]) -> ExtractResult:
    data = Envelope[ExtractResult].model_validate_json(json.dumps(raw)).data
    assert data is not None
    return data


def test_unknown_page_type_reads_as_other() -> None:
    raw = _result_json()
    raw["data"]["page_type"]["type"] = "future_type"
    raw["data"]["site"]["sitemap"]["type_hint"] = "future_type"
    raw["data"]["site"]["sitemap"]["entries"][0]["type_guess"] = "future_type"

    data = _read(raw)

    assert data.page_type is not None
    assert data.page_type.type == "other"
    assert data.site is not None
    assert data.site.sitemap is not None
    assert data.site.sitemap.type_hint == "other"
    assert data.site.sitemap.entries[0].type_guess == "other"


def test_unknown_link_category_reads_as_content() -> None:
    """``content`` is the catch-all of ``link_category``: any link no rule names."""
    raw = _result_json()
    raw["data"]["links"][0]["category"] = "future_category"
    raw["data"]["site"]["links"][0]["category"] = "future_category"

    data = _read(raw)

    assert data.links is not None
    assert data.links[0].category == "content"
    assert data.site is not None
    assert data.site.links is not None
    assert data.site.links[0].category == "content"


def test_unknown_crawl_reads_as_listed() -> None:
    raw = _result_json()
    raw["data"]["site"]["links"][0]["crawl"] = "future_crawl"
    raw["data"]["site"]["sitemap"]["children"][0]["crawl"] = "future_crawl"

    data = _read(raw)

    assert data.site is not None
    assert data.site.links is not None
    assert data.site.links[0].crawl == "listed"
    assert data.site.sitemap is not None
    assert data.site.sitemap.children[0].crawl == "listed"


def test_known_values_and_nulls_read_unchanged() -> None:
    raw = _result_json()
    raw["data"]["links"][0]["category"] = None

    data = _read(raw)

    assert data.links is not None
    assert data.links[0].category is None
    assert data.page_type is not None
    assert data.page_type.type == "content"
    assert data.site is not None
    assert data.site.links is not None
    assert data.site.links[0].crawl == "queued"


@pytest.mark.parametrize(
    ("tolerant", "value"),
    [(TolerantPageType, 3), (TolerantLinkCategory, ["home"]), (TolerantCrawl, None)],
)
def test_a_value_that_is_no_string_still_fails(tolerant: Any, value: object) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(tolerant).validate_python(value)


def test_result_schemas_keep_their_enums() -> None:
    """The tolerant readers do not widen the OpenAPI spec: the enums stay the Literals."""
    spec = create_public_app(
        Settings(auth_mode="none"), FakeAsyncRedis(), CollectorRegistry()
    ).openapi()
    schemas = spec["components"]["schemas"]

    assert schemas["PageTypeInfo"]["properties"]["type"]["enum"] == list(get_args(PageType))
    assert schemas["SiteLink"]["properties"]["crawl"]["enum"] == list(get_args(Crawl))
    assert schemas["SitemapChild"]["properties"]["crawl"]["enum"] == list(get_args(Crawl))
    category = schemas["SiteLink"]["properties"]["category"]["anyOf"][0]
    assert category["enum"] == list(get_args(LinkCategory))


def test_requests_stay_strict() -> None:
    """A request with a value the service does not know is still refused."""
    with pytest.raises(ValidationError):
        SiteStructureRequest.model_validate({"url": URL, "discovery": "future_discovery"})

    app = create_public_app(Settings(auth_mode="none"), FakeAsyncRedis(), CollectorRegistry())
    with TestClient(app) as client:
        response = client.post(
            "/v1/site-structure", json={"url": URL, "discovery": "future_discovery"}
        )

    assert response.status_code == 422
