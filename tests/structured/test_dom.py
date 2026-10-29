"""The DOM stage of structured output (ruling S3, S7's ``learn_html``): the ``metadata`` and
``selectors`` strategies, tables, the cleaned HTML for learning, and the cache key part (S4)."""

import json
import re
from typing import Any

import pytest

from text_extraction.metadata import extract_metadata, structured_items
from text_extraction.structured.dom import (
    MAX_TABLE_ROWS,
    MAX_TABLES,
    MAX_VALUE_CHARS,
    clean_html_for_learning,
    dom_stage,
    runs_dom_stage,
    structured_cache_part,
)
from text_extraction.structured.types import SelectorRule, StructuredOptions, StructuredResult

URL = "https://example.org/dir/page.html"


def stage(
    html: str,
    options: dict[str, Any],
    *,
    learned: dict[str, SelectorRule] | None = None,
    learn: bool = False,
    learn_max_chars: int = 128_000,
    url: str = URL,
) -> StructuredResult:
    return dom_stage(
        html,
        url,
        StructuredOptions(**options),
        extract_metadata(html, url, {}),
        structured_items(html, url),
        learn=learn,
        learn_max_chars=learn_max_chars,
        learned=learned or {},
    )


def schema(**properties: Any) -> dict[str, Any]:
    return {"type": "object", "properties": properties}


STRING = {"type": "string"}
NUMBER = {"type": "number"}
STRINGS = {"type": "array", "items": {"type": "string"}}

# --- metadata --------------------------------------------------------------------------------

META_PAGE = """<!doctype html>
<html lang="de"><head>
<title>The page title</title>
<meta property="og:image" content="https://example.org/img/cover.png">
<meta property="og:title" content="OG title">
<meta property="og:site_name" content="Example Site">
<meta name="keywords" content="plants, light">
<meta name="description" content="Meta description">
<script type="application/ld+json">
{"@context": "https://schema.org", "@graph": [
  {"@type": "WebSite", "name": "Site name in JSON-LD"},
  {"@type": "Article", "datePublished": "2026-10-01", "headline": "JSON-LD headline",
   "description": "JSON-LD description", "price": "on request",
   "category": "not-in-enum"}
]}
</script>
</head><body>
<div itemscope itemtype="https://schema.org/CreativeWork">
  <span itemprop="author">Ada Lovelace</span>
  <span itemprop="price">12,50</span>
  <span itemprop="category">science</span>
</div>
<p>Text.</p>
</body></html>"""


def test_metadata_strategy_matches_jsonld_microdata_og_meta_by_normalised_name() -> None:
    result = stage(
        META_PAGE,
        {
            "schema": schema(
                date_published=STRING,  # JSON-LD datePublished
                Author=STRING,  # microdata itemprop="author"
                image=STRING,  # og:image
                keywords=STRINGS,  # <meta name="keywords">
                site_name=STRING,  # og:site_name
                language=STRING,  # PageMetadata.language (the <html lang>)
                isbn=STRING,  # nowhere
            ),
            "strategy": "metadata",
        },
    )

    assert result.stage == "dom"
    assert result.values == {
        "date_published": "2026-10-01",
        "Author": "Ada Lovelace",
        "image": "https://example.org/img/cover.png",
        "keywords": ["plants", "light"],
        "site_name": "Example Site",
        "language": "de",
        "isbn": None,
    }
    assert result.sources == {
        name: "metadata"
        for name in ("date_published", "Author", "image", "keywords", "site_name", "language")
    }
    assert result.missing == ["isbn"]
    assert result.tables is None
    assert result.learn_html is None


def test_metadata_first_source_wins_and_misfit_skipped() -> None:
    result = stage(
        META_PAGE,
        {
            "schema": schema(
                description=STRING,  # JSON-LD before <meta name="description">
                title=STRING,  # og:title before PageMetadata.title
                name=STRING,  # the WebSite node's JSON-LD name before PageMetadata
                price=NUMBER,  # JSON-LD "on request" does not fit: microdata 12,50
                category={"type": "string", "enum": ["science", "art"]},  # likewise
            ),
            "strategy": "metadata",
        },
    )

    assert result.values == {
        "description": "JSON-LD description",
        "title": "OG title",
        "name": "Site name in JSON-LD",
        "price": 12.5,
        "category": "science",
    }
    assert result.missing == []


def test_metadata_strategy_without_page_metadata_and_on_broken_markup() -> None:
    options = StructuredOptions(schema=schema(title=STRING), strategy="metadata")
    page = "<html><head><title>Only a title</title></head><body>x</body></html>"
    result = dom_stage(
        page,
        URL,
        options,
        None,
        structured_items(page, URL),
        learn=False,
        learn_max_chars=1000,
    )
    assert result.values == {"title": None}
    assert result.missing == ["title"]

    broken = '<html><script type="application/ld+json">{"headline": </script><p>x'
    result = stage(broken, {"schema": schema(headline=STRING), "strategy": "metadata"})
    assert result.values == {"headline": None}


# --- selectors -------------------------------------------------------------------------------

SELECTOR_PAGE = f"""<html><head><title>Selectors</title></head><body>
<h1>  Big
   title </h1>
<ul class="tags"><li>alpha</li><li>beta</li><li>   </li></ul>
<a class="more" href="/more?x=1">More</a>
<img class="pic" src="pic.png">
<span class="price" data-value="12.50">12,50 €</span>
<p class="long">{"x" * 20_000}</p>
<p class="classes a b">c</p>
</body></html>"""


def test_css_selector_text_attr_all_and_absolute_urls() -> None:
    result = stage(
        SELECTOR_PAGE,
        {
            "schema": schema(
                title=STRING,
                tags=STRINGS,
                first_tag=STRING,
                joined_tags=STRING,
                one_tag=STRINGS,
                more=STRING,
                picture=STRING,
                price=NUMBER,
                long=STRING,
                classes=STRING,
                nothing=STRING,
            ),
            "strategy": "selectors",
            "selectors": {
                "title": {"css": "h1"},
                "tags": {"css": ".tags li"},  # an array: all, blanks skipped
                "first_tag": {"css": ".tags li"},  # a string: the first
                "joined_tags": {"css": ".tags li", "all": True},
                "one_tag": {"css": ".tags li", "all": False},
                "more": {"css": "a.more", "attr": "href"},
                "picture": {"css": "img.pic", "attr": "src"},
                "price": {"css": ".price", "attr": "data-value"},
                "long": {"css": "p.long"},
                "classes": {"css": "p.classes", "attr": "class"},
                "nothing": {"css": ".nothing"},
            },
        },
    )

    assert result.values == {
        "title": "Big title",
        "tags": ["alpha", "beta"],
        "first_tag": "alpha",
        "joined_tags": "alpha, beta",
        "one_tag": ["alpha"],
        "more": "https://example.org/more?x=1",
        "picture": "https://example.org/dir/pic.png",
        "price": 12.5,
        "long": "x" * MAX_VALUE_CHARS,
        "classes": "classes a b",
        "nothing": None,
    }
    assert result.sources["title"] == "selectors"
    assert "nothing" not in result.sources
    assert result.missing == ["nothing"]


def test_xpath_string_result() -> None:
    result = stage(
        SELECTOR_PAGE,
        {
            "schema": schema(
                raw=STRING,
                more=STRING,
                count={"type": "integer"},
                texts=STRINGS,
                heading=STRING,
                flag={"type": "boolean"},
            ),
            "strategy": "selectors",
            "selectors": {
                "raw": {"xpath": "string(//h1)"},  # a string result: as it is
                "more": {"xpath": "//a[@class='more']/@href"},  # made absolute
                "count": {"xpath": "count(//ul[@class='tags']/li)"},
                "texts": {"xpath": "//ul/li/text()"},
                "heading": {"xpath": "/html/body/h1"},  # an element: collapsed text
                "flag": {"xpath": "boolean(//h1)"},
            },
        },
    )

    assert result.values == {
        "raw": "  Big\n   title ",
        "more": "https://example.org/more?x=1",
        "count": 3,
        "texts": ["alpha", "beta"],
        "heading": "Big title",
        "flag": True,
    }


def test_xpath_evaluation_error_leaves_field_missing() -> None:
    """Carried ruling M2.5: an XPath that compiles but fails at evaluation (an unknown
    function, variable or namespace prefix) costs its field, nothing else."""
    for broken in ("foo()", "$v", "//svg:a"):
        SelectorRule(xpath=broken)  # accepted at validation
    result = stage(
        SELECTOR_PAGE,
        {
            "schema": schema(a=STRING, b=STRING, c=STRING, title=STRING),
            "strategy": "selectors",
            "selectors": {
                "a": {"xpath": "foo()"},
                "b": {"xpath": "$v"},
                "c": {"xpath": "//svg:a"},
                "title": {"css": "h1"},
            },
        },
    )

    assert result.values == {"a": None, "b": None, "c": None, "title": "Big title"}
    assert result.missing == ["a", "b", "c"]


def test_learned_rules_fill_fields_without_a_caller_rule() -> None:
    result = stage(
        SELECTOR_PAGE,
        {
            "schema": schema(title=STRING, more=STRING),
            "strategy": "selectors",
            "selectors": {"title": {"css": "h1"}},
        },
        learned={"more": SelectorRule(css="a.more", attr="href")},
    )

    assert result.values == {"title": "Big title", "more": "https://example.org/more?x=1"}
    assert result.sources == {"title": "selectors", "more": "learned"}


def test_auto_order_learned_metadata_selectors() -> None:
    """``auto`` on the DOM: learned selectors, then metadata, then the caller's selectors."""
    page = META_PAGE.replace("<p>Text.</p>", '<h2 class="h">Heading</h2><b>Bold</b>')
    result = stage(
        page,
        {
            "schema": schema(headline=STRING, title=STRING, bold=STRING, other=STRING),
            "selectors": {"title": {"css": "h2.h"}, "bold": {"css": "b"}},
        },
        learned={"headline": SelectorRule(css="h2.h")},
    )

    assert result.values == {
        "headline": "Heading",  # learned, before the JSON-LD headline
        "title": "OG title",  # metadata, before the caller's rule
        "bold": "Bold",  # the caller's rule, where metadata has nothing
        "other": None,
    }
    assert result.sources == {"headline": "learned", "title": "metadata", "bold": "selectors"}


# --- tables ----------------------------------------------------------------------------------


def test_tables_headers_colspan_and_limits() -> None:
    first = """<table><caption> Prices  2026 </caption>
<thead><tr><th>Name</th><th>Value</th></tr></thead>
<tbody><tr><td>a</td><td> 1 </td></tr><tr><td colspan="2">wide</td></tr>
<tr><td>nested</td><td><table><tr><td>in</td></tr><tr><td>side</td></tr></table></td></tr>
</tbody></table>"""
    single_row = "<table><tr><td>alone</td></tr></table>"
    long = "<table>" + "<tr><td>r</td></tr>" * (MAX_TABLE_ROWS + 500) + "</table>"
    many = "<table><tr><td>1</td></tr><tr><td>2</td></tr></table>" * 200
    page = f"<html><body>{first}{single_row}{long}{many}</body></html>"

    result = stage(page, {"tables": True})

    assert result.values == {}
    assert result.missing == []
    assert result.tables is not None
    assert len(result.tables) == MAX_TABLES == 50
    table = result.tables[0]
    assert table.index == 0
    assert table.caption == "Prices 2026"
    assert table.headers == ["Name", "Value"]
    assert table.rows == [["a", "1"], ["wide", "wide"], ["nested", "in side"]]
    assert result.tables[1].rows == [["in"], ["side"]]  # the nested table is a table too
    assert result.tables[2].headers == []
    assert len(result.tables[2].rows) == MAX_TABLE_ROWS
    assert [t.index for t in result.tables] == list(range(50))

    # Without `tables`, no tables.
    assert stage(page, {"schema": schema(a=STRING), "strategy": "metadata"}).tables is None


# --- learn_html ------------------------------------------------------------------------------

LEARN_PAGE = (
    """<!doctype html><html lang="en"><head><title>Learn &amp; page</title>
<meta name="x" content="y"><style>.a { color: red }</style><script>var a = 1;</script>
<link rel="stylesheet" href="/s.css"></head>
<body><!-- a comment --><div id="main" class="c d" data-track="1" onclick="x()" style="color:red">
<h1 itemprop="name">Heading &lt;1&gt;</h1><svg><path d="M0 0"/></svg><script>evil()</script>
<a href="/x" rel="nofollow" title="t">Link</a><time datetime="2026-10-01">1 Oct</time>
<meta property="og:x" name="n" content="v"><img src="/i.png" alt="pic" width="3"><br>
</div>
"""
    + "".join(f'<p class="p">Paragraph {index} with some words.</p>\n' for index in range(200))
    + ("</body></html>")
)


def test_learn_html_cleaned_and_cut_at_element_border() -> None:
    full = clean_html_for_learning(LEARN_PAGE, 1_000_000)

    for gone in (
        "<script",
        "<style",
        "<!--",
        "<svg",
        "<path",
        "data-track",
        "onclick",
        "style=",
        'rel="',
        "title=",
        "alt=",
        "width=",
        "/s.css",
        'name="x"',
        "var a",
        "evil",
    ):
        assert gone not in full, gone
    for kept in (
        "<title>Learn &amp; page</title>",
        'id="main"',
        'class="c d"',
        'itemprop="name"',
        'href="/x"',
        'datetime="2026-10-01"',
        'src="/i.png"',
        'property="og:x"',
        'name="n"',
        'content="v"',
        "Heading &lt;1&gt;",
        "<br>",
    ):
        assert kept in full, kept
    assert "Paragraph 199" in full

    cut = clean_html_for_learning(LEARN_PAGE, 600)
    assert 400 < len(cut) <= 600
    assert full.startswith(cut)
    assert cut.endswith(">")
    assert cut.rfind("<") < cut.rfind(">")  # no tag cut in half

    with_learn = stage(
        LEARN_PAGE, {"schema": schema(a=STRING), "strategy": "llm"}, learn=True, learn_max_chars=600
    )
    assert with_learn.learn_html == cut
    assert with_learn.values == {"a": None}
    assert with_learn.missing == ["a"]
    assert with_learn.sources == {}


# --- when the stage runs, and the cache key part ---------------------------------------------


def _options(**values: Any) -> StructuredOptions:
    values.setdefault("schema", schema(a=STRING, b=STRING))
    return StructuredOptions(**values)


@pytest.mark.parametrize(
    ("options", "runs"),
    [
        ({"strategy": "metadata"}, True),
        ({"strategy": "selectors"}, True),
        ({"strategy": "auto"}, True),
        ({"strategy": "patterns"}, False),
        ({"strategy": "llm"}, False),
        ({"strategy": "llm", "learn_selectors": True}, True),
        ({"strategy": "patterns", "tables": True}, True),
        ({"schema": None, "tables": True}, True),
    ],
)
def test_runs_dom_stage(options: dict[str, Any], runs: bool) -> None:
    assert runs_dom_stage(_options(**options)) is runs


def test_structured_cache_part() -> None:
    assert structured_cache_part(_options(strategy="patterns"), {}) is None
    assert structured_cache_part(_options(strategy="llm", learn_selectors=True), {}) is None

    options = _options(selectors={"a": {"css": "h1"}, "b": {"xpath": "//p"}})
    part = structured_cache_part(options, {})
    assert part is not None
    assert part.keys() == {"dom", "schema", "selectors", "tables"}
    assert part["dom"] == "auto"
    assert options.compiled is not None
    assert part["schema"] == options.compiled.schema_id
    assert re.fullmatch(r"[0-9a-f]{12}", part["selectors"])
    assert part["tables"] is False
    json.dumps(part)

    # The order of the rules does not matter; their content does.
    same = _options(selectors={"b": {"xpath": "//p"}, "a": {"css": "h1"}})
    assert structured_cache_part(same, {}) == part
    other = _options(selectors={"a": {"css": "h2"}, "b": {"xpath": "//p"}})
    assert structured_cache_part(other, {}) != part

    learned = structured_cache_part(options, {"a": SelectorRule(css="h3")})
    assert learned is not None
    assert re.fullmatch(r"[0-9a-f]{12}", learned["learned"])
    assert {key: value for key, value in learned.items() if key != "learned"} == part

    tables_only = structured_cache_part(StructuredOptions(tables=True), {})
    assert tables_only is not None
    assert tables_only["schema"] is None
    assert tables_only["tables"] is True
    assert tables_only["dom"] == "auto"
