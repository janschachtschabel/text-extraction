"""The schema subset (ruling S2): what compiles, the strict form for the LLM, the schema id and
the coercion of found values."""

import copy
from typing import Any

import pytest
from pydantic import BaseModel

from text_extraction.structured.schema import SchemaError, coerce, compile_schema

SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "A product",
    "required": ["name"],
    "properties": {
        "name": {"type": "string", "description": "The product's name"},
        "price": {"type": "number"},
        "stock": {"type": "integer"},
        "available": {"type": "boolean"},
        "size": {"type": "string", "enum": ["S", "M", "L"]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "maker": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "founded": {"type": "integer"}},
        },
        "offers": {
            "type": "array",
            "items": {"type": "object", "properties": {"shop": {"type": "string"}}},
        },
    },
}


def test_schema_subset_compiles_to_model_and_strict_schema() -> None:
    compiled = compile_schema(SCHEMA)

    assert compiled.fields == (
        "name",
        "price",
        "stock",
        "available",
        "size",
        "tags",
        "maker",
        "offers",
    )
    assert issubclass(compiled.model, BaseModel)
    assert len(compiled.schema_id) == 12
    # Every field may be missing, and what the model does not know is ignored.
    empty = compiled.model.model_validate({})
    assert empty.model_dump() == {
        "name": None,
        "price": None,
        "stock": None,
        "available": None,
        "size": None,
        "tags": None,
        "maker": None,
        "offers": None,
    }
    filled = compiled.model.model_validate(
        {"name": "Lamp", "price": 9.5, "maker": {"name": "Acme", "other": 1}, "unknown": True}
    )
    assert filled.model_dump()["maker"] == {"name": "Acme", "founded": None}
    assert compiled.field_schema("size") == {"type": "string", "enum": ["S", "M", "L"]}
    assert compiled.field_schema("name")["description"] == "The product's name"

    strict = compiled.strict_json_schema(["name", "price", "size", "maker", "offers", "tags"])

    assert strict["type"] == "object"
    assert strict["additionalProperties"] is False
    assert strict["required"] == ["name", "price", "size", "maker", "offers", "tags"]
    props = strict["properties"]
    assert list(props) == ["name", "price", "size", "maker", "offers", "tags"]
    assert props["name"] == {"type": ["string", "null"], "description": "The product's name"}
    assert props["price"] == {"type": ["number", "null"]}
    assert props["size"] == {"type": ["string", "null"], "enum": ["S", "M", "L", None]}
    assert props["tags"] == {"type": ["array", "null"], "items": {"type": "string"}}
    maker = props["maker"]
    assert maker["type"] == ["object", "null"]
    assert maker["additionalProperties"] is False
    assert maker["required"] == ["name", "founded"]
    assert maker["properties"]["founded"] == {"type": ["integer", "null"]}
    offer = props["offers"]["items"]
    assert offer["type"] == "object"
    assert offer["additionalProperties"] is False
    assert offer["required"] == ["shop"]
    assert offer["properties"]["shop"] == {"type": ["string", "null"]}
    # The compiled schema is not touched by building the strict form.
    assert SCHEMA["properties"]["price"] == {"type": "number"}
    assert compiled.field_schema("price") == {"type": "number"}


def _nested(depth: int) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": {"leaf": {"type": "string"}}}
    for level in range(depth - 1):
        schema = {"type": "object", "properties": {f"l{level}": schema}}
    return schema


@pytest.mark.parametrize(
    ("schema", "path"),
    [
        ({"type": "object", "properties": {"a": {"$ref": "#/defs/x"}}}, "a"),
        ({"type": "object", "properties": {"a": {"oneOf": [{"type": "string"}]}}}, "a"),
        (
            {"type": "object", "properties": {"a": {"type": "string", "pattern": "^x"}}},
            "a",
        ),
        (
            {
                "type": "object",
                "properties": {
                    "o": {
                        "type": "object",
                        "properties": {"b": {"type": "string", "format": "uri"}},
                    }
                },
            },
            "o.b",
        ),
        (_nested(4), "l2.l1.l0"),
        (
            {"type": "object", "properties": {f"p{n}": {"type": "string"} for n in range(51)}},
            "$",
        ),
        ({"type": "object", "properties": {"1bad": {"type": "string"}}}, "1bad"),
        (
            {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "additionalProperties": True,
            },
            "$",
        ),
        ({"type": "string"}, "$"),
        ({"type": "object", "properties": {}}, "$"),
        (
            {"type": "object", "properties": {"a": {"type": "array", "items": {"type": "array"}}}},
            "a[]",
        ),
        ({"type": "object", "properties": {"a": {"type": "boolean", "enum": [True]}}}, "a"),
        ({"type": "object", "properties": {"a": {"type": "integer", "enum": ["x"]}}}, "a"),
        ({"type": "object", "properties": {"a": {"type": ["string", "null"]}}}, "a"),
    ],
)
def test_schema_rejects_ref_oneof_pattern_depth_4_and_51_properties(
    schema: dict[str, Any], path: str
) -> None:
    with pytest.raises(SchemaError) as caught:
        compile_schema(schema)

    assert caught.value.path == path
    assert path in str(caught.value)
    assert isinstance(caught.value, ValueError)


def test_schema_limits_are_inclusive() -> None:
    assert compile_schema(_nested(3)).fields == ("l1",)
    fifty = {"type": "object", "properties": {f"p{n}": {"type": "string"} for n in range(50)}}
    assert len(compile_schema(fifty).fields) == 50
    # Properties of nested objects count, too.
    nested = {
        "type": "object",
        "properties": {
            **{f"p{n}": {"type": "string"} for n in range(49)},
            "o": {"type": "object", "properties": {"x": {"type": "string"}}},
        },
    }
    with pytest.raises(SchemaError):
        compile_schema(nested)  # 49 + o + x = 51


def test_schema_id_is_stable_under_key_order() -> None:
    reordered = copy.deepcopy(SCHEMA)
    reordered["properties"]["price"] = {"type": "number"}
    reordered = {
        "properties": dict(reversed(list(reordered["properties"].items()))),
        "required": reordered["required"],
        "type": "object",
        "description": reordered["description"],
    }
    other = copy.deepcopy(SCHEMA)
    other["properties"]["price"] = {"type": "integer"}

    first = compile_schema(SCHEMA)

    assert compile_schema(reordered).schema_id == first.schema_id
    assert compile_schema(other).schema_id != first.schema_id
    # The fields keep the schema's own order.
    assert compile_schema(reordered).fields[0] == "offers"


def test_coerce_string_number_boolean_list_and_misfit() -> None:
    compiled = compile_schema(SCHEMA)

    # string
    assert coerce(compiled, "name", "Lamp") == "Lamp"
    assert coerce(compiled, "name", ["Lamp", "Desk"]) == "Lamp, Desk"
    assert coerce(compiled, "name", 12) == "12"
    assert coerce(compiled, "name", True) == "true"
    assert coerce(compiled, "name", "  ") is None
    assert coerce(compiled, "name", {"a": 1}) is None
    assert coerce(compiled, "name", None) is None
    # number
    assert coerce(compiled, "price", 9.5) == 9.5
    assert coerce(compiled, "price", "1.234,5") == 1234.5
    assert coerce(compiled, "price", "1,234.5") == 1234.5
    assert coerce(compiled, "price", "12,50 €") == 12.5
    assert coerce(compiled, "price", "$ 3.99") == 3.99
    assert coerce(compiled, "price", "1,234") == 1234
    assert coerce(compiled, "price", "3.14") == 3.14
    assert coerce(compiled, "price", "-7") == -7
    assert coerce(compiled, "price", "cheap") is None
    assert coerce(compiled, "price", True) is None
    assert coerce(compiled, "price", float("nan")) is None
    # integer
    assert coerce(compiled, "stock", "1.234") == 1234  # thousands, for an integer
    assert coerce(compiled, "stock", "12") == 12
    assert coerce(compiled, "stock", 12.0) == 12
    assert coerce(compiled, "stock", "1.234,0") == 1234
    assert coerce(compiled, "stock", "12.5") is None
    # Exact, not through float (which would give 12345678901234567168).
    assert coerce(compiled, "stock", "12345678901234567890") == 12345678901234567890
    assert coerce(compiled, "stock", "-12345678901234567891") == -12345678901234567891
    assert coerce(compiled, "stock", 12345678901234567891) == 12345678901234567891
    assert coerce(compiled, "stock", "1.234.567") == 1234567
    assert coerce(compiled, "stock", "1.234.567 Stück") == 1234567
    assert coerce(compiled, "stock", "12,0") == 12
    assert coerce(compiled, "stock", "9" * 61) is None
    assert coerce(compiled, "price", "12345678901234567890") == 12345678901234567890
    assert coerce(compiled, "stock", True) is None
    # boolean
    for yes in ("true", "Yes", "JA", True):
        assert coerce(compiled, "available", yes) is True
    for no in ("false", "no", " Nein ", False):
        assert coerce(compiled, "available", no) is False
    assert coerce(compiled, "available", "maybe") is None
    assert coerce(compiled, "available", 1) is None
    # enum
    assert coerce(compiled, "size", "M") == "M"
    assert coerce(compiled, "size", "m") == "M"
    assert coerce(compiled, "size", "XL") is None
    # list
    assert coerce(compiled, "tags", ["a", "b"]) == ["a", "b"]
    assert coerce(compiled, "tags", "a") == ["a"]
    assert coerce(compiled, "tags", ["a", {"x": 1}, "b"]) == ["a", "b"]
    assert coerce(compiled, "tags", [{"x": 1}]) is None
    assert coerce(compiled, "tags", []) == []
    # object
    assert coerce(compiled, "maker", {"name": "Acme", "founded": "1999", "x": 1}) == {
        "name": "Acme",
        "founded": 1999,
    }
    assert coerce(compiled, "maker", "Acme") is None
    assert coerce(compiled, "offers", [{"shop": "A"}, {"shop": "B"}]) == [
        {"shop": "A"},
        {"shop": "B"},
    ]
    with pytest.raises(KeyError):
        coerce(compiled, "unknown", "x")


# --- the cache of compiled schemas (review M2.3) -------------------------------------------------


def _counting_models(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from text_extraction.structured import schema as schema_module

    made: list[str] = []
    real = schema_module._model

    def counting(name: str, properties: Any) -> type[BaseModel]:
        made.append(name)
        return real(name, properties)

    monkeypatch.setattr(schema_module, "_model", counting)
    return made


def test_equal_schemas_compile_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every read of a job validates its options again (status polls, item claims, the llm
    worker per item): an equal schema reuses the model compiled first."""
    from text_extraction.job_models import JobOptions
    from text_extraction.structured.types import StructuredOptions

    made = _counting_models(monkeypatch)
    unique = {"type": "object", "properties": {f"f_{id(made)}": {"type": "string"}}}
    options = JobOptions(
        compliance="enforce", structured=StructuredOptions.model_validate({"schema": unique})
    )

    again = JobOptions.model_validate_json(options.model_dump_json())
    third = compile_schema(copy.deepcopy(unique))

    assert made == ["Structured"]  # once, though validated three times
    assert options.structured is not None
    assert again.structured is not None
    assert again.structured.compiled is options.structured.compiled
    assert third is options.structured.compiled


def test_a_reordered_schema_compiles_its_own_field_order(monkeypatch: pytest.MonkeyPatch) -> None:
    made = _counting_models(monkeypatch)
    first = {"type": "object", "properties": {"a_order": {"type": "string"}, "b": NUMBER_NODE}}
    second = {"type": "object", "properties": {"b": NUMBER_NODE, "a_order": {"type": "string"}}}

    one, two = compile_schema(first), compile_schema(second)

    assert one.schema_id == two.schema_id  # the same schema
    assert (one.fields, two.fields) == (("a_order", "b"), ("b", "a_order"))
    assert len(made) == 2


def test_an_invalid_schema_is_refused_every_time() -> None:
    bad = {"type": "object", "properties": {"x": {"type": "date"}}}
    for _ in range(2):
        with pytest.raises(SchemaError):
            compile_schema(bad)


def test_the_cache_is_bounded() -> None:
    from text_extraction.structured import schema as schema_module

    for index in range(schema_module.COMPILED_CACHE_SIZE + 10):
        compile_schema({"type": "object", "properties": {f"bounded_{index}": {"type": "string"}}})

    assert len(schema_module._COMPILED) == schema_module.COMPILED_CACHE_SIZE


NUMBER_NODE: dict[str, Any] = {"type": "number"}
