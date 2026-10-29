"""The ``structured`` option of a request and the structured part of a result (ruling S1, S11).

The options are checked when the request is parsed: the schema against the subset of S2, the
selectors with soupsieve and lxml, the patterns with ``regex``. A broken one is a validation
error (422) that names the field and never repeats the selector or the pattern.
"""

import warnings
from typing import Any, Literal, Self

import regex
import soupsieve
from lxml import etree
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from text_extraction.llm.usage import LlmUsage
from text_extraction.structured.schema import CompiledSchema, SchemaError, compile_schema

StructuredStrategy = Literal["metadata", "selectors", "patterns", "llm", "auto"]
Source = Literal["metadata", "selectors", "learned", "patterns", "llm"]

MAX_RULE_LENGTH = 500
MAX_INSTRUCTIONS_LENGTH = 2000

_FLAGS = {"i": regex.IGNORECASE, "m": regex.MULTILINE, "s": regex.DOTALL}


def _compile(pattern: str, flags: int) -> "regex.Pattern[str]":
    try:
        return regex.compile(pattern, flags)
    except (regex.error, ValueError, OverflowError):
        raise ValueError("regex is not a valid regular expression") from None


class SelectorRule(BaseModel, extra="forbid", hide_input_in_errors=True):
    """Where on the page a field's value is: a CSS selector or an XPath expression."""

    css: str | None = Field(
        default=None,
        max_length=MAX_RULE_LENGTH,
        description="A CSS selector; without an admin key it may use `:has()` at most once. "
        "Exactly one of `css` and `xpath`.",
    )
    xpath: str | None = Field(
        default=None,
        max_length=MAX_RULE_LENGTH,
        description="An XPath 1.0 expression, for admin keys only (422 for others: use CSS); a "
        "string result (`.../@href`, `string(...)`) is taken as it is. Exactly one of `css` and "
        "`xpath`.",
    )
    attr: str | None = Field(
        default=None,
        max_length=100,
        description="The attribute to read (`href`, `content`, ...); default: the element's text "
        "with whitespace collapsed.",
    )
    all: bool | None = Field(
        default=None,
        description="Take every match; default: all for an array field, else the first.",
    )

    @field_validator("css")
    @classmethod
    def _css_compiles(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                soupsieve.compile(value)
            except Exception:
                raise ValueError("css is not a valid CSS selector") from None
        return value

    @field_validator("xpath")
    @classmethod
    def _xpath_compiles(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                etree.XPath(value)
            except Exception:
                raise ValueError("xpath is not a valid XPath expression") from None
        return value

    @model_validator(mode="after")
    def _exactly_one_selector(self) -> Self:
        if (self.css is None) == (self.xpath is None):
            raise ValueError("a selector rule needs exactly one of css and xpath")
        return self


class PatternRule(BaseModel, extra="forbid", hide_input_in_errors=True):
    """A regular expression that finds a field's value in the content."""

    regex: str = Field(
        max_length=MAX_RULE_LENGTH,
        description="A regular expression (the `regex` module's syntax, a superset of Python's "
        "`re`); it is run on the content with a time limit.",
    )
    group: int | str = Field(
        default=0,
        description="The group to take: its number (0: the whole match) or its name.",
    )
    all: bool | None = Field(
        default=None,
        description="Take every match; default: all for an array field, else the first.",
    )
    flags: list[Literal["i", "m", "s"]] = Field(
        default=[],
        description="`i`: ignore case. `m`: `^` and `$` match at lines. `s`: `.` matches a "
        "newline.",
    )

    @field_validator("regex")
    @classmethod
    def _regex_compiles(cls, value: str) -> str:
        _compile(value, 0)
        return value

    @model_validator(mode="after")
    def _has_the_group(self) -> Self:
        flags = 0
        for flag in self.flags:
            flags |= _FLAGS[flag]
        compiled = _compile(self.regex, flags)
        if isinstance(self.group, str):
            known = self.group in compiled.groupindex
        else:
            known = 0 <= self.group <= compiled.groups
        if not known:
            raise ValueError("group is not a group of the regex")
        return self


# The API calls the field `schema`, which shadows a deprecated method of BaseModel; Pydantic warns
# about that when the class is made.
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message='Field name "schema"')

    class StructuredOptions(BaseModel, extra="forbid", hide_input_in_errors=True):
        """The ``structured`` option of a request."""

        schema: dict[str, Any] | None = Field(  # type: ignore[assignment]
            default=None,
            description="The fields to extract, as a JSON schema of a subset: the root is an "
            "`object` with `properties` of the types `string`, `number`, `integer`, `boolean`, "
            "`array` (of those or of an `object`) and `object`; `enum` on strings and numbers; "
            "`description` anywhere; at most 3 levels of objects, at most 50 properties in "
            "all. `$ref`, `oneOf`, `pattern`, formats and the like are refused. `required` is "
            "ignored: every field may be missing. Needed unless the request asks for `tables` "
            "only.",
        )
        strategy: StructuredStrategy = Field(
            default="auto",
            description="`metadata`: the page's JSON-LD, microdata, RDFa, OpenGraph and meta tags. "
            "`selectors`: your CSS or XPath rules. `patterns`: your regular expressions on the "
            "content. `llm`: the LLM reads the content. `auto`: every field is looked up in this "
            "order and the first strategy that finds a value wins: `selectors` (your rules, else "
            "the selectors learned for this API key and site), `metadata`, `patterns`, `llm` for "
            "what is still missing.",
        )
        selectors: dict[str, SelectorRule] = Field(
            default={},
            description="Selector rules by field name; the names must be properties of `schema`. "
            "They run on pages of at most 50,000 elements (else they are skipped with the warning "
            "`selectors_skipped_large_page`); a field takes at most 1,000 values and 100,000 "
            "characters (else `structured_truncated`).",
        )
        patterns: dict[str, PatternRule] = Field(
            default={},
            description="Pattern rules by field name; the names must be properties of `schema`.",
        )
        tables: bool = Field(
            default=False,
            description="Also return the tables of the page (`data.structured.tables`).",
        )
        learn_selectors: bool = Field(
            default=False,
            description="After the LLM found values, ask it for one CSS selector per field, check "
            "that each reproduces the value and keep it for this API key and this page layout, so "
            "that later pages of the site need no LLM.",
        )
        instructions: str | None = Field(
            default=None,
            max_length=MAX_INSTRUCTIONS_LENGTH,
            description="Free text for the LLM (what a field means, the currency, the language). "
            "`strategy` `llm` and `auto` only.",
        )
        _compiled: CompiledSchema | None = PrivateAttr(default=None)

        @model_validator(mode="after")
        def _check(self) -> Self:
            if self.schema is None:
                if not self.tables:
                    raise ValueError("schema is required unless tables is true")
                if self.selectors or self.patterns or self.learn_selectors:
                    raise ValueError("selectors, patterns and learn_selectors need a schema")
            else:
                try:
                    self._compiled = compile_schema(self.schema)
                except SchemaError as error:
                    raise ValueError(
                        f"schema is not valid at {error.path}: {error.message}"
                    ) from None
                for option, rules in (("selectors", self.selectors), ("patterns", self.patterns)):
                    unknown = [name for name in rules if name not in self._compiled.fields]
                    if unknown:
                        raise ValueError(
                            f"{option} names fields that are not properties of schema: "
                            + ", ".join(unknown)
                        )
            if self.instructions is not None and self.strategy not in ("llm", "auto"):
                raise ValueError("instructions need strategy llm or auto")
            return self

        @property
        def compiled(self) -> CompiledSchema | None:
            """The schema, compiled once when the options were validated; None without a schema."""
            return self._compiled


class Table(BaseModel):
    """One table of the page."""

    index: int = Field(description="Position among the page's tables, from 0.")
    caption: str | None
    headers: list[str] = Field(description="The `<th>` cells of the first row; else empty.")
    rows: list[list[str]]


class LearnReport(BaseModel):
    """What `learn_selectors` did, by field name."""

    stored: list[str]
    rejected: list[str]


class StructuredResult(BaseModel):
    """The structured part of a result."""

    values: dict[str, Any] = Field(
        description="Every top-level field of the schema; null when it was not found."
    )
    sources: dict[str, Source] = Field(default={}, description="Where each found value came from.")
    missing: list[str] = Field(default=[], description="The fields without a value.")
    tables: list[Table] | None = Field(
        default=None, description="The page's tables; only when `tables` was asked for."
    )
    learned: LearnReport | None = Field(default=None, description="With `learn_selectors` only.")
    llm: LlmUsage | None = Field(
        default=None, description="The LLM calls made; null when no LLM was used."
    )
    stage: Literal["dom", "done"] = Field(
        description="`dom`: the strategies that read the page's HTML have run; `done`: all have."
    )
    # What the LLM selector learning reads; the post stage clears it, so that no answer carries
    # it. A draft in Valkey keeps it, and the schema of the API does not show it.
    learn_html: SkipJsonSchema[str | None] = None
