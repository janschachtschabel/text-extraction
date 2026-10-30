"""Request checks of ``structured`` that depend on the caller (ruling R4)."""

from typing import Any

import pytest

from text_extraction.auth import Principal
from text_extraction.chunking.types import ChunkingOptions
from text_extraction.envelope import ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.pii.types import PiiOptions
from text_extraction.settings import Settings
from text_extraction.structured.checks import (
    BACKSLASH_MESSAGE,
    HAS_LIMIT_MESSAGE,
    LLM_DISABLED_MESSAGE,
    LLM_KEY_HEADER,
    LLM_KEY_MISSING_MESSAGE,
    SEMANTIC_MODEL_MESSAGE,
    XPATH_NEEDS_ADMIN_MESSAGE,
    check_selector_access,
    check_structured_request,
    llm_key_of,
    positional,
    wants_llm,
)
from text_extraction.structured.types import SelectorRule, StructuredOptions

USER = Principal(name="alice", role="user", rate_limit_per_minute=None)
ADMIN = Principal(name="root", role="admin", rate_limit_per_minute=None)
SCHEMA: dict[str, Any] = {"type": "object", "properties": {"a": {"type": "string"}}}


def options(rule: dict[str, Any]) -> StructuredOptions:
    return StructuredOptions.model_validate({"schema": SCHEMA, "selectors": {"a": rule}})


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        ({"xpath": "//h1"}, XPATH_NEEDS_ADMIN_MESSAGE),
        ({"css": "div:has(p):has(a)"}, HAS_LIMIT_MESSAGE),
        ({"css": "div:HAS(p) span:has(a)"}, HAS_LIMIT_MESSAGE),
        # N4.1: an escape spells `:has(` past the count, so a user's CSS has no backslash.
        ({"css": r"div:h\61 s(a):has(b)"}, BACKSLASH_MESSAGE),
        ({"css": r"h\31"}, BACKSLASH_MESSAGE),
    ],
)
def test_selector_access_refused_for_users(rule: dict[str, Any], message: str) -> None:
    with pytest.raises(ServiceError) as raised:
        check_selector_access(options(rule), USER)
    assert raised.value.http_status == 422
    assert raised.value.code == ErrorCode.INVALID_REQUEST
    assert str(raised.value) == message
    assert "h1" not in str(raised.value)  # never the selector
    assert "61" not in str(raised.value)

    check_selector_access(options(rule), ADMIN)  # admins may


@pytest.mark.parametrize("rule", [{"css": "h1"}, {"css": "div:has(> p)"}])
def test_selector_access_allows_css(rule: dict[str, Any]) -> None:
    check_selector_access(options(rule), USER)
    check_selector_access(None, USER)
    check_selector_access(StructuredOptions(tables=True), USER)


# --- the request checks (ruling S12) -----------------------------------------------------------

LLM_ON: dict[str, Any] = {"llm_enabled": True, "llm_api_key": "sk-env", "jobs_enabled": True}
MASK = PiiOptions(mode="mask")


def settings(**overrides: Any) -> Settings:
    return Settings(auth_mode="none", **overrides)


def structured(**changes: Any) -> StructuredOptions:
    return StructuredOptions.model_validate({"schema": SCHEMA, **changes})


def check(
    structured_options: StructuredOptions | None = None,
    chunking: ChunkingOptions | None = None,
    pii: PiiOptions | None = None,
    req_format: Any = "markdown",
    *,
    llm_key: str | None = None,
    **overrides: Any,
) -> None:
    check_structured_request(
        structured_options, chunking, pii, req_format, settings(**overrides), llm_key=llm_key
    )


def refused(status: int, message: str, **kwargs: Any) -> None:
    with pytest.raises(ServiceError) as raised:
        check(**kwargs)
    assert raised.value.http_status == status
    assert raised.value.code == ErrorCode.INVALID_REQUEST
    assert str(raised.value) == message


def test_the_messages_are_s12s() -> None:
    assert LLM_DISABLED_MESSAGE == "LLM features are not enabled on this service."
    assert SEMANTIC_MODEL_MESSAGE == "Semantic chunking needs LLM_EMBEDDING_MODEL."
    assert LLM_KEY_MISSING_MESSAGE == "No LLM key: send X-LLM-Api-Key."
    assert LLM_KEY_HEADER == "X-LLM-Api-Key"


@pytest.mark.parametrize(
    "case",
    [
        {"structured_options": structured(strategy="llm")},
        {"structured_options": structured(strategy="auto", learn_selectors=True)},
        {"structured_options": structured(strategy="llm", learn_selectors=True)},
        {"chunking": ChunkingOptions(strategy="semantic")},
    ],
    ids=["llm", "learn-with-auto", "learn-with-llm", "semantic"],
)
def test_llm_features_400_without_llm(case: dict[str, Any]) -> None:
    refused(400, LLM_DISABLED_MESSAGE, llm_key="sk-caller", **case)  # LLM_ENABLED is off


def test_auto_without_llm_is_fine() -> None:
    check(structured(), llm_key=None)  # the LLM is only one of auto's strategies
    check(structured(strategy="patterns"), ChunkingOptions(strategy="recursive"))


def test_semantic_400_without_embedding_model() -> None:
    refused(
        400,
        SEMANTIC_MODEL_MESSAGE,
        chunking=ChunkingOptions(strategy="semantic"),
        llm_key="sk-caller",
        **LLM_ON,
    )
    check(
        chunking=ChunkingOptions(strategy="semantic"),
        llm_key="sk-caller",
        llm_embedding_model="embed-1",
        **LLM_ON,
    )


@pytest.mark.parametrize(
    "case",
    [
        {"structured_options": structured(strategy="llm")},
        {"structured_options": structured(strategy="auto")},
        {"chunking": ChunkingOptions(strategy="semantic")},
    ],
    ids=["llm", "auto", "semantic"],
)
def test_llm_work_400_without_any_key(case: dict[str, Any]) -> None:
    overrides = {**LLM_ON, "llm_api_key": None, "llm_embedding_model": "embed-1"}
    refused(400, LLM_KEY_MISSING_MESSAGE, **case, **overrides)
    assert "sk-" not in LLM_KEY_MISSING_MESSAGE


@pytest.mark.parametrize(
    ("llm_key", "env_key"), [("sk-caller", None), (None, "sk-env")], ids=["header", "env"]
)
def test_header_key_accepted_without_env_key(llm_key: str | None, env_key: str | None) -> None:
    overrides = {**LLM_ON, "llm_api_key": env_key, "llm_embedding_model": "embed-1"}
    check(structured(strategy="llm"), llm_key=llm_key, **overrides)
    check(structured(), llm_key=llm_key, **overrides)
    check(chunking=ChunkingOptions(strategy="semantic"), llm_key=llm_key, **overrides)


def test_work_that_needs_no_llm_needs_no_key() -> None:
    overrides = {**LLM_ON, "llm_api_key": None}
    check(structured(strategy="patterns"), **overrides)
    check(structured(strategy="selectors"), ChunkingOptions(strategy="markdown"), **overrides)
    check(StructuredOptions(tables=True), **overrides)  # tables only: the strategy does not matter
    check(**overrides)
    # Without LLM on the service, auto simply has no LLM step and so needs no key.
    check(structured(), llm_api_key=None)


@pytest.mark.parametrize("value", ["has space", "x" * 1025, "tab\there", "", "café"])
def test_bad_llm_key_header_400(value: str) -> None:
    with pytest.raises(ServiceError) as raised:
        llm_key_of(value)
    assert raised.value.http_status == 400
    assert raised.value.code == ErrorCode.INVALID_REQUEST
    assert LLM_KEY_HEADER in str(raised.value)
    assert value not in str(raised.value) or value == ""


def test_llm_key_of_passes_a_good_value_and_none() -> None:
    assert llm_key_of(None) is None
    assert llm_key_of("sk-abc_123") == "sk-abc_123"
    assert llm_key_of("x" * 1024) == "x" * 1024


@pytest.mark.parametrize("req_format", ["html"])
def test_chunking_html_422(req_format: str) -> None:
    refused(
        422,
        "Chunking needs format markdown or text.",
        chunking=ChunkingOptions(),
        req_format=req_format,
    )
    for allowed in ("markdown", "text"):
        check(chunking=ChunkingOptions(), req_format=allowed)
    check(structured(strategy="patterns"), req_format="html")  # structured takes HTML


@pytest.mark.parametrize(
    ("case", "what"),
    [
        ({"strategy": "metadata"}, "Strategy metadata"),
        ({"strategy": "selectors", "selectors": {"a": {"css": "h1"}}}, "Strategy selectors"),
        ({"strategy": "patterns", "tables": True}, "tables"),
        ({"strategy": "llm", "learn_selectors": True}, "learn_selectors"),
        ({"strategy": "auto", "learn_selectors": True}, "learn_selectors"),
        ({"strategy": "auto", "selectors": {"a": {"css": "h1"}}}, "selectors"),
        ({"strategy": "patterns", "selectors": {"a": {"css": "h1"}}}, "selectors"),
    ],
    ids=[
        "metadata",
        "selectors",
        "tables",
        "learn-llm",
        "learn-auto",
        "auto-rules",
        "pattern-rules",
    ],
)
def test_pii_with_dom_strategies_422(case: dict[str, Any], what: str) -> None:
    refused(
        422,
        f"{what} reads the raw page and cannot be combined with pii.",
        structured_options=structured(**case),
        pii=MASK,
        llm_key="sk-caller",
        **LLM_ON,
    )


def test_pii_with_tables_only_422() -> None:
    refused(
        422,
        "tables reads the raw page and cannot be combined with pii.",
        structured_options=StructuredOptions(tables=True),
        pii=MASK,
    )


def test_pii_with_patterns_llm_and_chunking_accepted() -> None:
    patterns = {"a": {"regex": r"\d+"}}
    for options in (
        structured(strategy="patterns", patterns=patterns),
        structured(strategy="llm"),
        structured(strategy="auto"),
        structured(strategy="auto", patterns=patterns),
    ):
        check(options, ChunkingOptions(), MASK, llm_key="sk-caller", **LLM_ON)
    check(chunking=ChunkingOptions(strategy="markdown"), pii=MASK)
    check(pii=MASK)


@pytest.mark.parametrize("strategy", ["metadata", "selectors", "patterns"])
def test_learn_needs_llm_or_auto_422(strategy: str) -> None:
    refused(
        422,
        "learn_selectors needs strategy llm or auto.",
        structured_options=structured(strategy=strategy, learn_selectors=True),
        llm_key="sk-caller",
        **LLM_ON,
    )
    check(structured(strategy="llm", learn_selectors=True), llm_key="sk-caller", **LLM_ON)
    check(structured(strategy="auto", learn_selectors=True), llm_key="sk-caller", **LLM_ON)


def test_wants_llm() -> None:
    on = settings(**LLM_ON)
    off = settings()
    semantic = ChunkingOptions(strategy="semantic")
    assert wants_llm(structured(strategy="llm"), None, on)
    assert wants_llm(structured(), None, on)  # auto may need it
    assert wants_llm(None, semantic, on)
    assert wants_llm(structured(strategy="patterns"), semantic, on)
    assert not wants_llm(structured(strategy="metadata"), None, on)
    assert not wants_llm(structured(strategy="selectors"), ChunkingOptions(), on)
    assert not wants_llm(StructuredOptions(tables=True), None, on)
    assert not wants_llm(None, ChunkingOptions(), on)
    assert not wants_llm(None, None, on)
    for options in (structured(strategy="llm"), structured(), None):
        assert not wants_llm(options, semantic, off)


@pytest.mark.parametrize(
    "rule",
    [
        {"css": "ul li:nth-child(2)"},
        {"css": "ul li:NTH-OF-TYPE(1)"},
        {"css": "li:nth-last-child(1)"},
        {"css": "li:nth-last-of-type(1)"},
        {"css": "li:first-child"},
        {"css": "li:first-of-type"},
        {"css": "li:last-child"},
        {"css": "li:last-of-type"},
        {"css": "li:only-child"},
        {"css": "li:only-of-type"},
        {"css": "h1 + span"},
        {"css": "h1~span"},
        {"xpath": "//li[position() = 2]"},
        {"xpath": "//li[2]"},
        {"xpath": "(//li)[ 1 ]"},
        {"xpath": "//li[last()]"},
        {"xpath": "//h1/following-sibling::span"},
        {"xpath": "//h1/preceding-sibling::span"},
    ],
)
def test_positional_rules(rule: dict[str, Any]) -> None:
    """M-d: rules whose match depends on sibling positions, which the cleaned learn_html shifts
    (scripts, styles, SVG and comments removed)."""
    assert positional(SelectorRule.model_validate(rule))


@pytest.mark.parametrize(
    "rule",
    [
        {"css": "h1.title"},
        {"css": "a[href~='x'] span"},  # `~=` is an attribute operator, not a combinator
        {"css": "a[title='a + b']"},
        {"css": "div > p span"},
        {"css": "div:has(> p)"},
        {"xpath": "//h1[@class='title']"},
        {"xpath": "//a[contains(@title, '[2]')]"},
    ],
)
def test_not_positional_rules(rule: dict[str, Any]) -> None:
    assert not positional(SelectorRule.model_validate(rule))
