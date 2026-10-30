"""The options and result models of structured output and chunking (ruling S1, S11): what they
accept, what they refuse at request time and how they serialise."""

import json
from typing import Any

import pytest
from pydantic import ValidationError

from text_extraction.chunking.types import Chunk, ChunkingOptions
from text_extraction.extract_models import ExtractResult
from text_extraction.llm.client import LlmUsage
from text_extraction.structured.types import (
    LearnReport,
    PatternRule,
    SelectorRule,
    StructuredOptions,
    StructuredResult,
    Table,
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "price": {"type": "number"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
}


def _loc(error: ValidationError) -> list[tuple[Any, ...]]:
    return [tuple(item["loc"]) for item in error.errors()]


def test_options_need_schemaunless_tables_only() -> None:
    with pytest.raises(ValidationError, match="schema"):
        StructuredOptions()
    with pytest.raises(ValidationError, match="schema"):
        StructuredOptions(strategy="llm")
    with pytest.raises(ValidationError, match="schema"):
        StructuredOptions(learn_selectors=True)

    tables_only = StructuredOptions(tables=True)
    assert tables_only.schema is None
    assert tables_only.compiled is None
    assert StructuredOptions(tables=True, strategy="metadata").schema is None

    options = StructuredOptions(schema=SCHEMA)
    assert options.strategy == "auto"
    assert options.selectors == {}
    assert options.patterns == {}
    assert options.tables is False
    assert options.learn_selectors is False
    assert options.instructions is None
    assert options.compiled is not None
    assert options.compiled.fields == ("price", "tags")


def test_options_compile_the_schemaonce() -> None:
    options = StructuredOptions(schema=SCHEMA)

    assert options.compiled is options.compiled


def test_options_schemaerror_is_a_validation_error_naming_the_path() -> None:
    bad = {"type": "object", "properties": {"a": {"$ref": "#/x"}}}

    with pytest.raises(ValidationError) as caught:
        StructuredOptions(schema=bad)

    assert "schema" in str(caught.value)
    assert "a" in str(caught.value)


def test_options_instructions_are_bounded_and_for_llm_strategies() -> None:
    assert StructuredOptions(schema=SCHEMA, instructions="x" * 2000, strategy="llm")
    assert StructuredOptions(schema=SCHEMA, instructions="Prices in EUR").strategy == "auto"
    with pytest.raises(ValidationError):
        StructuredOptions(schema=SCHEMA, instructions="x" * 2001)
    with pytest.raises(ValidationError, match="instructions"):
        StructuredOptions(schema=SCHEMA, instructions="x", strategy="selectors")


def test_options_unknown_strategy_and_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        StructuredOptions(schema=SCHEMA, strategy="magic")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        StructuredOptions.model_validate({"schema": SCHEMA, "unknown": 1})


def test_selector_rule_exactly_one_of_css_xpath_and_broken_selector_rejected() -> None:
    assert SelectorRule(css="span.price").attr is None
    assert SelectorRule(css="a.buy", attr="href", all=False).all is False
    assert SelectorRule(xpath="//a/@href").xpath == "//a/@href"
    for neither_or_both in ({}, {"css": "a", "xpath": "//a"}, {"attr": "href"}):
        with pytest.raises(ValidationError, match="exactly one"):
            SelectorRule.model_validate(neither_or_both)
    with pytest.raises(ValidationError) as css_error:
        SelectorRule(css="div[")
    assert ("css",) in _loc(css_error.value)
    with pytest.raises(ValidationError) as xpath_error:
        SelectorRule(xpath="//div[")
    assert ("xpath",) in _loc(xpath_error.value)
    with pytest.raises(ValidationError):
        SelectorRule(css="a" * 501)
    with pytest.raises(ValidationError):
        SelectorRule(xpath="/" + "a" * 500)
    with pytest.raises(ValidationError):
        SelectorRule.model_validate({"css": "a", "unknown": 1})


def test_selector_error_does_not_echo_the_selector() -> None:
    with pytest.raises(ValidationError) as caught:
        SelectorRule(css="div[secret-token")

    assert "secret-token" not in str(caught.value)


def test_pattern_rule_broken_regex_rejected() -> None:
    rule = PatternRule(regex=r"(\d+) EUR", group=1)
    assert rule.flags == []
    assert rule.all is None
    assert PatternRule(regex=r"(?P<n>\d+)", group="n", flags=["i", "m", "s"]).group == "n"
    with pytest.raises(ValidationError) as caught:
        PatternRule(regex="(unclosed")
    assert ("regex",) in _loc(caught.value)
    assert "unclosed" not in str(caught.value)
    with pytest.raises(ValidationError):
        PatternRule(regex="a" * 501)
    with pytest.raises(ValidationError):
        PatternRule(regex="a", flags=["x"])  # type: ignore[list-item]


def test_pattern_rule_group_must_exist() -> None:
    with pytest.raises(ValidationError, match="group"):
        PatternRule(regex=r"(\d+)", group=2)
    with pytest.raises(ValidationError, match="group"):
        PatternRule(regex=r"(?P<n>\d+)", group="m")
    assert PatternRule(regex=r"\d+").group == 0


def test_selector_and_pattern_keys_must_be_schemafields() -> None:
    options = StructuredOptions(
        schema=SCHEMA,
        selectors={"price": SelectorRule(css=".price")},
        patterns={"tags": PatternRule(regex=r"#\w+")},
    )
    assert set(options.selectors) == {"price"}

    with pytest.raises(ValidationError, match=r"selectors.*cost"):
        StructuredOptions(schema=SCHEMA, selectors={"cost": SelectorRule(css=".price")})
    with pytest.raises(ValidationError, match=r"patterns.*cost"):
        StructuredOptions(schema=SCHEMA, patterns={"cost": PatternRule(regex=r"\d")})
    with pytest.raises(ValidationError, match="need a schema"):
        StructuredOptions(tables=True, selectors={"price": SelectorRule(css=".price")})


def test_options_from_json_compile_their_rules() -> None:
    options = StructuredOptions.model_validate_json(
        json.dumps(
            {
                "schema": SCHEMA,
                "strategy": "selectors",
                "selectors": {"price": {"css": ".price", "attr": "content"}},
                "patterns": {"price": {"regex": "(\\d+) EUR", "group": 1}},
            }
        )
    )

    assert options.selectors["price"].attr == "content"
    assert options.patterns["price"].group == 1
    rebuilt = StructuredOptions.model_validate(options.model_dump())
    assert rebuilt == options
    with pytest.raises(ValidationError):
        StructuredOptions.model_validate_json(
            json.dumps({"schema": SCHEMA, "selectors": {"price": {"css": "a[["}}})
        )


def test_chunking_bounds() -> None:
    defaults = ChunkingOptions()
    assert (defaults.strategy, defaults.size, defaults.overlap) == ("recursive", 1000, 100)
    assert defaults.breakpoint_percentile == 90
    assert ChunkingOptions(size=200, overlap=100)
    assert ChunkingOptions(size=20_000, overlap=10_000)
    assert ChunkingOptions(size=200, overlap=0)
    for bad in ({"size": 199}, {"size": 20_001}, {"size": 1000, "overlap": 501}, {"overlap": -1}):
        with pytest.raises(ValidationError):
            ChunkingOptions.model_validate(bad)
    with pytest.raises(ValidationError, match="overlap"):
        ChunkingOptions(size=300, overlap=151)
    for percentile in (49, 100):
        with pytest.raises(ValidationError):
            ChunkingOptions(breakpoint_percentile=percentile)
    assert ChunkingOptions(breakpoint_percentile=50).strategy == "recursive"
    for strategy in ("fixed", "recursive", "markdown", "sentence", "semantic"):
        assert ChunkingOptions.model_validate({"strategy": strategy}).strategy == strategy
    with pytest.raises(ValidationError):
        ChunkingOptions(strategy="paragraph")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ChunkingOptions.model_validate({"size": 300, "unknown": 1})


def test_chunking_default_overlap_fits_the_smallest_size() -> None:
    assert ChunkingOptions(size=200).overlap == 100


def test_result_models_round_trip() -> None:
    result = StructuredResult(
        values={"price": 9.5, "tags": None},
        sources={"price": "metadata"},
        missing=["tags"],
        tables=[Table(index=0, caption=None, headers=["a"], rows=[["1"], ["2"]])],
        learned=LearnReport(stored=["price"], rejected=[]),
        llm=LlmUsage(model="m", calls=1, prompt_tokens=10, completion_tokens=2),
        stage="done",
    )

    again = StructuredResult.model_validate_json(result.model_dump_json())

    assert again == result
    chunk = Chunk(index=0, text="abc", start=0, end=3, headings=["Topic"])
    assert Chunk.model_validate(chunk.model_dump()) == chunk
    with pytest.raises(ValidationError):
        StructuredResult(values={}, stage="later")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        StructuredResult(values={}, sources={"price": "guess"}, stage="dom")  # type: ignore[dict-item]


def test_learn_html_hidden_from_openapi_schema() -> None:
    schema = StructuredResult.model_json_schema()
    assert "learn_html" not in schema["properties"]
    assert "learn_html" not in json.dumps(ExtractResult.model_json_schema())

    # Drafts in Valkey need it: it stays a serialised field.
    result = StructuredResult(values={}, stage="dom", learn_html="<p>x</p>")
    assert result.model_dump()["learn_html"] == "<p>x</p>"
    assert StructuredResult.model_validate_json(result.model_dump_json()).learn_html == "<p>x</p>"
