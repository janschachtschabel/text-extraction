"""The site structure models (rulings P9-S1, P9-S2)."""

import json
from typing import Any

import pytest
from pydantic import ValidationError

from text_extraction.site.types import (
    SiteInfo,
    SiteLink,
    SitemapChild,
    SitemapEntry,
    SitemapInfo,
    SiteNode,
    SiteOptions,
    SiteScope,
    SiteStructureRequest,
)

URL = "https://lernen.example.org/"


def test_request_defaults() -> None:
    req = SiteStructureRequest(url=URL)

    assert req.depth == 2
    assert req.max_pages == 100
    assert req.sitemaps is True
    assert req.scope == "host"
    assert req.metadata is True
    assert req.compliance is None
    assert req.cache is True


@pytest.mark.parametrize(
    "body",
    [
        {"url": URL, "depth": 11},
        {"url": URL, "depth": -1},
        {"url": URL, "max_pages": 0},
        {"url": URL, "max_pages": 100_001},
        {"url": URL, "scope": "domain"},
        {"url": URL, "content": True},
        {"url": "https://a.example/" + "x" * 2048},
        {},
    ],
)
def test_request_rejects_extra_and_bounds(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SiteStructureRequest.model_validate(body)


def test_request_bounds_accepted() -> None:
    low = SiteStructureRequest(url=URL, depth=0, max_pages=1, scope="path")
    high = SiteStructureRequest(url=URL, depth=10, max_pages=100_000, compliance="ignore")

    assert (low.depth, low.max_pages, low.scope) == (0, 1, "path")
    assert (high.depth, high.max_pages, high.compliance) == (10, 100_000, "ignore")


def test_request_options_drop_url_and_compliance() -> None:
    req = SiteStructureRequest(
        url=URL,
        depth=3,
        max_pages=50,
        sitemaps=False,
        scope="path",
        metadata=False,
        compliance="report",
        cache=False,
    )

    options = req.options()

    assert options == SiteOptions(
        depth=3, max_pages=50, sitemaps=False, scope="path", metadata=False, cache=False
    )
    assert set(SiteOptions.model_fields) == set(SiteStructureRequest.model_fields) - {
        "url",
        "compliance",
    }
    with pytest.raises(ValidationError):
        SiteOptions.model_validate({"url": URL})


def test_sitemap_entry_json_drops_nulls() -> None:
    assert (
        SitemapEntry(loc="https://a.example/").model_dump_json() == '{"loc":"https://a.example/"}'
    )
    full = SitemapEntry(
        loc="https://a.example/", lastmod="2026-10-01", changefreq="weekly", priority=0.8
    )
    assert json.loads(full.model_dump_json()) == {
        "loc": "https://a.example/",
        "lastmod": "2026-10-01",
        "changefreq": "weekly",
        "priority": 0.8,
    }
    assert SitemapEntry.model_validate_json(full.model_dump_json()) == full


def test_sitemap_entry_nulls_dropped_inside_a_result() -> None:
    info = SiteInfo(
        role="sitemap",
        depth=0,
        parent=1,
        sitemap=SitemapInfo(
            format="urlset",
            entries=[SitemapEntry(loc="https://a.example/x")],
            entry_count=1,
        ),
    )

    dumped = json.loads(info.model_dump_json())

    assert dumped["sitemap"]["entries"] == [{"loc": "https://a.example/x"}]
    assert SiteInfo.model_validate(dumped) == info


@pytest.mark.parametrize("value", [-0.1, 1.5, float("nan"), float("inf"), "high", ""])
def test_priority_outside_0_1_is_none(value: object) -> None:
    assert SitemapEntry.model_validate({"loc": URL, "priority": value}).priority is None


@pytest.mark.parametrize(("value", "expected"), [(0, 0.0), (1, 1.0), ("0.5", 0.5), (" 0.8 ", 0.8)])
def test_priority_in_0_1_is_kept(value: object, expected: float) -> None:
    assert SitemapEntry.model_validate({"loc": URL, "priority": value}).priority == expected


def test_changefreq_unknown_is_none_and_case_folded() -> None:
    assert SitemapEntry.model_validate({"loc": URL, "changefreq": "sometimes"}).changefreq is None
    assert SitemapEntry.model_validate({"loc": URL, "changefreq": " Weekly "}).changefreq == (
        "weekly"
    )


def test_lastmod_over_64_characters_is_none() -> None:
    assert SitemapEntry.model_validate({"loc": URL, "lastmod": "x" * 65}).lastmod is None
    assert SitemapEntry.model_validate({"loc": URL, "lastmod": " 2026-10-30 "}).lastmod == (
        "2026-10-30"
    )
    assert SitemapEntry.model_validate({"loc": URL, "lastmod": "  "}).lastmod is None


def test_node_defaults_and_round_trip() -> None:
    node = SiteNode(role="page", depth=0, parent=None)

    assert node.guessed is False
    assert node.part is None
    part = SiteNode(role="sitemap_part", depth=1, parent=4, part=2)
    assert SiteNode.model_validate_json(part.model_dump_json()) == part
    with pytest.raises(ValidationError):
        SiteNode.model_validate({"role": "image", "depth": 0, "parent": None})


def test_scope_and_link_and_child() -> None:
    scope = SiteScope(host="a.example", path_prefix="/mathe/")
    link = SiteLink(
        url="https://a.example/b",
        text="B",
        kind="internal",
        source="a",
        rel=["nofollow"],
        hreflang=None,
        region="nav",
        crawl="listed",
    )
    child = SitemapChild(url="https://a.example/s.xml", crawl="queued", target=3)

    assert SiteScope.model_validate_json(scope.model_dump_json()) == scope
    assert link.target is None
    assert child.lastmod is None
    with pytest.raises(ValidationError):
        SiteLink.model_validate({**link.model_dump(), "crawl": "maybe"})
    with pytest.raises(ValidationError):
        SiteLink.model_validate({**link.model_dump(), "region": "sidebar"})


def test_site_info_defaults() -> None:
    info = SiteInfo(role="page", depth=1, parent=0)

    assert info.links is None
    assert info.nofollow is False
    assert info.sitemaps is None
    assert info.sitemap is None


def test_sitemap_entry_schema_keeps_its_fields() -> None:
    """Dropping nulls must not hide the entry's fields from the OpenAPI spec."""
    schema = SitemapEntry.model_json_schema(mode="serialization")

    assert set(schema["properties"]) == {"loc", "lastmod", "changefreq", "priority"}
