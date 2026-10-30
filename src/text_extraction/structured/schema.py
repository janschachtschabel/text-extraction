"""The JSON schema subset of ``structured.schema`` (ruling S2), compiled once per request.

A caller describes the fields it wants with a small subset of JSON schema. ``compile_schema``
checks the subset and returns a ``CompiledSchema``: a Pydantic model that validates what the
strategies found, the strict JSON schema for the LLM (OpenAI structured output), a stable id of
the schema and the field names. ``coerce`` makes a found raw value fit a field's type.

The subset: the root is an ``object`` with ``properties``; a property is a ``string``,
``number``, ``integer``, ``boolean``, an ``array`` (``items``: any of those or an ``object``) or an
``object`` (``properties``); ``enum`` on strings, numbers and integers; ``description`` anywhere;
``required`` is accepted and ignored (every field may be missing). At most 3 levels of objects
(the root is 1), at most 50 properties in all, names ``[A-Za-z_][A-Za-z0-9_]{0,63}``. Anything
else is refused with the path of the node.

Compiling builds Pydantic models (about 4 ms for 50 properties), and options are validated again
on every read of a job (status polls, item claims, the llm worker per item). ``compile_schema``
therefore keeps the last ``COMPILED_CACHE_SIZE`` compiled schemas, keyed by the schema's exact JSON
(the order of its properties included, since it is the order of ``fields``): an equal schema gets
the same ``CompiledSchema``, which is immutable. A schema that is refused is never kept.
"""

import copy
import dataclasses
import hashlib
import json
import math
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

MAX_DEPTH = 3
MAX_PROPERTIES = 50
COMPILED_CACHE_SIZE = 256  # compiled schemas kept (``compile_schema``)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_SCALARS = ("string", "number", "integer", "boolean")
_ENUM_TYPES = ("string", "number", "integer")
_KEYWORDS_BY_TYPE: dict[str, frozenset[str]] = {
    "string": frozenset({"type", "description", "enum"}),
    "number": frozenset({"type", "description", "enum"}),
    "integer": frozenset({"type", "description", "enum"}),
    "boolean": frozenset({"type", "description"}),
    "array": frozenset({"type", "description", "items"}),
    "object": frozenset({"type", "description", "properties", "required", "additionalProperties"}),
}


class SchemaError(ValueError):
    """The schema is not in the subset. ``path`` names the node (``$`` is the root, ``a.b`` a
    property of an object, ``a[]`` the items of an array)."""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message


@dataclasses.dataclass(frozen=True)
class CompiledSchema:
    """A schema of the subset, compiled. Two are equal when their schemas are (the same id), so
    that options compare equal after a round trip."""

    # Every field ``T | None = None``, unknown keys ignored.
    model: type[BaseModel] = dataclasses.field(compare=False)
    fields: tuple[str, ...]  # the top-level names, in the schema's order
    schema_id: str  # 12 hex characters of the SHA-256 of the canonical JSON
    # The top-level property schemas.
    properties: Mapping[str, Any] = dataclasses.field(repr=False, compare=False)

    def field_schema(self, name: str) -> dict[str, Any]:
        """The schema of one top-level field, as the caller wrote it."""
        return copy.deepcopy(dict(self.properties[name]))

    def strict_json_schema(self, fields: Sequence[str]) -> dict[str, Any]:
        """The schema of an LLM answer for ``fields`` in OpenAI strict mode: every property
        required and nullable (null: not found), ``additionalProperties: false`` at every
        level."""
        names = list(fields)
        return {
            "type": "object",
            "properties": {name: _strict(self.properties[name], nullable=True) for name in names},
            "required": names,
            "additionalProperties": False,
        }


# The compiled schemas by the exact JSON of their schema, least recently used first.
_COMPILED: OrderedDict[str, CompiledSchema] = OrderedDict()
_COMPILED_LOCK = threading.Lock()  # options are validated on the event loop and in threads


def compile_schema(schema: Mapping[str, Any]) -> CompiledSchema:
    """Check ``schema`` against the subset and compile it, or return the ``CompiledSchema`` of an
    equal schema compiled before (the module docstring). Raises ``SchemaError``."""
    try:
        exact = json.dumps(schema, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):  # not JSON: the check names what is wrong
        return _compile(schema)
    with _COMPILED_LOCK:
        compiled = _COMPILED.get(exact)
        if compiled is not None:
            _COMPILED.move_to_end(exact)
            return compiled
    compiled = _compile(schema)
    with _COMPILED_LOCK:
        _COMPILED[exact] = compiled
        _COMPILED.move_to_end(exact)
        while len(_COMPILED) > COMPILED_CACHE_SIZE:
            _COMPILED.popitem(last=False)
    return compiled


def _compile(schema: Mapping[str, Any]) -> CompiledSchema:
    if schema.get("type") != "object":
        raise SchemaError("$", "the root must have type 'object'")
    count = _check_object(schema, "$", 1, root=True)
    if count > MAX_PROPERTIES:
        raise SchemaError("$", f"at most {MAX_PROPERTIES} properties in all, found {count}")
    properties = schema["properties"]
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return CompiledSchema(
        model=_model("Structured", properties),
        fields=tuple(properties),
        schema_id=hashlib.sha256(canonical.encode()).hexdigest()[:12],
        properties=copy.deepcopy(dict(properties)),
    )


# --- checking ---------------------------------------------------------------------------------


def _child(path: str, name: str) -> str:
    return name if path == "$" else f"{path}.{name}"


def _check_keywords(node: Mapping[str, Any], path: str, kind: str, allowed: frozenset[str]) -> None:
    for keyword in node:
        if keyword not in allowed:
            hint = "is not supported" if keyword in _KNOWN_UNSUPPORTED else "is not allowed"
            raise SchemaError(path, f"{keyword!r} {hint} for type {kind!r}")
    if "description" in node and not isinstance(node["description"], str):
        raise SchemaError(path, "'description' must be a string")


_KNOWN_UNSUPPORTED = frozenset(
    {"$ref", "$defs", "definitions", "oneOf", "anyOf", "allOf", "not", "pattern", "format"}
)


def _check_object(node: Mapping[str, Any], path: str, level: int, *, root: bool = False) -> int:
    """Check an object node; returns the number of properties in it and below."""
    _check_keywords(node, path, "object", _KEYWORDS_BY_TYPE["object"])
    if node.get("additionalProperties", False) is not False:
        raise SchemaError(path, "'additionalProperties' must be false or absent")
    properties = node.get("properties")
    if not isinstance(properties, Mapping) or not properties:
        raise SchemaError(path, "an object needs 'properties' with at least one property")
    required = node.get("required", [])
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise SchemaError(path, "'required' must be a list of names")
    count = 0
    for name, sub in properties.items():
        sub_path = _child(path, str(name))
        if not isinstance(name, str) or _NAME.fullmatch(name) is None:
            raise SchemaError(sub_path, "a property name must match [A-Za-z_][A-Za-z0-9_]{0,63}")
        count += 1 + _check_property(sub, sub_path, level)
    return count


def _check_property(node: Any, path: str, level: int) -> int:
    """Check a property (or the items of an array) one level below ``level``; returns the number
    of properties it contributes below itself."""
    if not isinstance(node, Mapping):
        raise SchemaError(path, "a property must be an object")
    kind = node.get("type")
    if not isinstance(kind, str):
        # Without a usable type, name the keyword that stands in its place.
        for keyword in node:
            if keyword in _KNOWN_UNSUPPORTED:
                raise SchemaError(path, f"{keyword!r} is not supported")
        raise SchemaError(path, "'type' must be one type name")
    if kind not in _KEYWORDS_BY_TYPE:
        raise SchemaError(path, f"type {kind!r} is not supported")
    if kind == "object":
        if level + 1 > MAX_DEPTH:
            raise SchemaError(path, f"objects may be nested at most {MAX_DEPTH} levels deep")
        return _check_object(node, path, level + 1)
    _check_keywords(node, path, kind, _KEYWORDS_BY_TYPE[kind])
    if kind == "array":
        items = node.get("items")
        item_path = f"{path}[]"
        if not isinstance(items, Mapping):
            raise SchemaError(path, "an array needs 'items'")
        if items.get("type") == "array":
            raise SchemaError(item_path, "arrays of arrays are not supported")
        return _check_property(items, item_path, level)
    if "enum" in node:
        _check_enum(node["enum"], kind, path)
    return 0


def _check_enum(values: Any, kind: str, path: str) -> None:
    if not isinstance(values, list) or not values:
        raise SchemaError(path, "'enum' must be a non-empty list")
    for value in values:
        fits = (
            isinstance(value, str)
            if kind == "string"
            else isinstance(value, int | float) and not isinstance(value, bool)
            if kind == "number"
            else isinstance(value, int) and not isinstance(value, bool)
        )
        if not fits:
            raise SchemaError(path, f"'enum' values must fit the type {kind!r}")


# --- the model --------------------------------------------------------------------------------


def _annotation(node: Mapping[str, Any], name: str) -> Any:
    kind = node["type"]
    if kind == "object":
        return _model(name, node["properties"])
    if kind == "array":
        return list[_annotation(node["items"], f"{name}Item")]  # type: ignore[misc]
    if "enum" in node:
        return Literal[tuple(node["enum"])]
    return {"string": str, "number": float, "integer": int, "boolean": bool}[kind]


def _model(name: str, properties: Mapping[str, Any]) -> type[BaseModel]:
    # Fields are named f0, f1, ... with the property name as the alias: a property may be called
    # `json`, `copy` or `model_dump`, which a field name must not be.
    fields: dict[str, Any] = {}
    for index, (prop, node) in enumerate(properties.items()):
        annotation = _annotation(node, f"{name}{index}")
        fields[f"f{index}"] = (annotation | None, Field(default=None, alias=prop))
    return create_model(
        name,
        __config__=ConfigDict(extra="ignore", validate_by_alias=True, serialize_by_alias=True),
        **fields,
    )


# --- the strict schema ------------------------------------------------------------------------


def _strict(node: Mapping[str, Any], *, nullable: bool) -> dict[str, Any]:
    kind = node["type"]
    out: dict[str, Any] = {"type": [kind, "null"] if nullable else kind}
    if "description" in node:
        out["description"] = node["description"]
    if kind == "object":
        names = list(node["properties"])
        out["properties"] = {
            name: _strict(node["properties"][name], nullable=True) for name in names
        }
        out["required"] = names
        out["additionalProperties"] = False
    elif kind == "array":
        out["items"] = _strict(node["items"], nullable=False)
    elif "enum" in node:
        out["enum"] = [*node["enum"], None] if nullable else list(node["enum"])
    return out


# --- coercion ---------------------------------------------------------------------------------


def coerce(compiled: CompiledSchema, field: str, value: Any) -> Any | None:
    """Make a found raw ``value`` fit the type of top-level ``field``; None when it does not
    fit. Raises ``KeyError`` for a field that is not in the schema."""
    return _coerce(compiled.properties[field], value)


def _coerce(node: Mapping[str, Any], value: Any) -> Any | None:
    kind = node["type"]
    if kind == "object":
        return _coerce_object(node["properties"], value)
    if kind == "array":
        return _coerce_array(node["items"], value)
    result = _SCALAR[kind](value)
    if result is None or "enum" not in node:
        return result
    return _in_enum(node["enum"], result)


def _coerce_object(properties: Mapping[str, Any], value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    out = {name: _coerce(node, value.get(name)) for name, node in properties.items()}
    return out if any(item is not None for item in out.values()) else None


def _coerce_array(items: Mapping[str, Any], value: Any) -> list[Any] | None:
    if value is None:
        return None
    raw = value if isinstance(value, list | tuple) else [value]
    out = [fitted for item in raw if (fitted := _coerce(items, item)) is not None]
    return out if out or not raw else None


def _in_enum(values: list[Any], value: Any) -> Any | None:
    if value in values:
        return value
    if isinstance(value, str):
        folded = value.strip().casefold()
        for candidate in values:
            if candidate.casefold() == folded:
                return candidate
    return None


def _string(value: Any) -> str | None:
    if isinstance(value, list | tuple):
        parts = [part for item in value if (part := _string(item)) is not None]
        return ", ".join(parts) if parts else None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return None if isinstance(value, float) and not math.isfinite(value) else str(value)
    if isinstance(value, str):
        return value if value.strip() else None
    return None


# Characters that group digits besides the point and the comma: an apostrophe (Swiss), a
# no-break space and a narrow no-break space (French, German).
_GROUPING = "'\N{RIGHT SINGLE QUOTATION MARK}\N{NO-BREAK SPACE}\N{NARROW NO-BREAK SPACE}"
_MINUS = "-\N{MINUS SIGN}"
# An optional currency or unit, then the digits with their separators.
_NUMBER = re.compile(
    r"(?:[A-Za-z]{1,3}\s?|[€$£¥]\s?)?"
    rf"(?P<sign>[{_MINUS}+]?)\s?(?:[€$£¥]\s?)?"
    rf"(?P<digits>\d[\d.,{_GROUPING}]*)"
    r"\s?(?:[€$£¥%]|[^\W\d_]{1,12}\.?)?"
)


_MAX_NUMBER_DIGITS = 60  # longer is no number a page states (and int() refuses 4,300 digits)


def _grouped(text: str, separator: str) -> bool:
    return re.fullmatch(rf"\d{{1,3}}(?:{re.escape(separator)}\d{{3}})+", text) is not None


def _parse_number(text: str, *, integer: bool) -> int | float | None:
    """``1.234,5`` and ``1,234.5`` are both 1234.5. A single separator followed by exactly three
    digits is a thousands separator for a comma and, for an integer, for a point (``1,234``,
    ``1.234`` for an integer); otherwise it is the decimal separator (``1,5``, ``3.14``)."""
    match = _NUMBER.fullmatch(text.strip())
    if match is None:
        return None
    token = re.sub(f"[{_GROUPING}]", "", match["digits"])
    if token[-1] in ".," or len(token) > _MAX_NUMBER_DIGITS:
        return None
    dots, commas = token.count("."), token.count(",")
    if dots and commas:
        decimal = "." if token.rfind(".") > token.rfind(",") else ","
        thousands = "," if decimal == "." else "."
        whole, _, fraction = token.rpartition(decimal)
        if thousands in fraction or not (_grouped(whole, thousands) or whole.isdigit()):
            return None
        digits = f"{whole.replace(thousands, '')}.{fraction}"
    elif not dots and not commas:
        digits = token
    else:
        separator = "." if dots else ","
        head, _, tail = token.rpartition(separator)
        if dots + commas > 1:
            if not _grouped(token, separator):
                return None
            digits = token.replace(separator, "")
        elif (
            len(tail) == 3
            and head.isdigit()
            and 1 <= len(head) <= 3
            and head != "0"
            and (separator == "," or integer)
        ):
            digits = head + tail
        else:
            digits = f"{head}.{tail}"
    negative = match["sign"] in tuple(_MINUS)
    whole, point, fraction = digits.partition(".")
    # Exact for an integer: no detour through float, which loses digits above 2**53. A fraction
    # of zeros is still an integer (``1.234,0``).
    if not point or (integer and not fraction.strip("0")):
        return -int(whole) if negative else int(whole)
    return -float(digits) if negative else float(digits)


def _number(value: Any, *, integer: bool) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = _parse_number(value, integer=integer)
        if value is None:
            return None
    if not isinstance(value, int | float) or (
        isinstance(value, float) and not math.isfinite(value)
    ):
        return None
    if integer and not isinstance(value, int):
        return int(value) if value.is_integer() else None
    return value


_TRUE = frozenset({"true", "yes", "ja"})
_FALSE = frozenset({"false", "no", "nein"})


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        word = value.strip().casefold()
        if word in _TRUE:
            return True
        if word in _FALSE:
            return False
    return None


_SCALAR = {
    "string": _string,
    "number": lambda value: _number(value, integer=False),
    "integer": lambda value: _number(value, integer=True),
    "boolean": _boolean,
}
