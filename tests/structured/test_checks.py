"""Request checks of ``structured`` that depend on the caller (ruling R4)."""

from typing import Any

import pytest

from text_extraction.auth import Principal
from text_extraction.envelope import ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.structured.checks import (
    HAS_LIMIT_MESSAGE,
    XPATH_NEEDS_ADMIN_MESSAGE,
    check_selector_access,
)
from text_extraction.structured.types import StructuredOptions

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
    ],
)
def test_selector_access_refused_for_users(rule: dict[str, Any], message: str) -> None:
    with pytest.raises(ServiceError) as raised:
        check_selector_access(options(rule), USER)
    assert raised.value.http_status == 422
    assert raised.value.code == ErrorCode.INVALID_REQUEST
    assert str(raised.value) == message
    assert "h1" not in str(raised.value)  # never the selector

    check_selector_access(options(rule), ADMIN)  # admins may


@pytest.mark.parametrize("rule", [{"css": "h1"}, {"css": "div:has(> p)"}])
def test_selector_access_allows_css(rule: dict[str, Any]) -> None:
    check_selector_access(options(rule), USER)
    check_selector_access(None, USER)
    check_selector_access(StructuredOptions(tables=True), USER)
