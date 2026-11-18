"""Package 24, gap 1: each tool's ``options`` description names the further fields of its REST
body with a short hint, so an agent knows them without reading /docs."""

import pytest

from text_extraction.mcp_server.option_hints import (
    OPTION_HINTS,
    ROUTE_HINTS,
    option_fields,
    option_hint,
)
from text_extraction.mcp_server.tools_extract import ExtractionTool, extraction_tools
from text_extraction.mcp_server.tools_jobs import job_tools
from text_extraction.settings import Settings

SETTINGS = Settings(auth_mode="none", mcp_enabled=True)
TOOLS: dict[str, ExtractionTool] = {
    tool.name: tool
    for tool in [*extraction_tools(SETTINGS), *job_tools()]
    if isinstance(tool, ExtractionTool)
}


def description(name: str) -> str:
    text: str = TOOLS[name].parameters["properties"]["options"]["description"]
    return text


def test_the_seven_tools_with_options() -> None:
    assert set(TOOLS) == {
        "extract_url",
        "extract_file",
        "screenshot_url",
        "extract_structured",
        "chunk",
        "batch_extract",
        "site_structure",
    }


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_every_option_field_has_a_hint(name: str) -> None:
    tool = TOOLS[name]
    fields = option_fields(tool.rest_model, tool.args_model)

    assert fields
    assert [field for field in fields if option_hint(tool.route, field) is None] == []
    for field in fields:
        assert f"{field} ({option_hint(tool.route, field)})" in description(name)


def test_options_description_lists_the_fields() -> None:
    text = description("extract_url")

    for part in ("pii (", "ocr (auto|never|always)", "compliance (", "screenshot (", "/docs"):
        assert part in text
    for argument in ("url (", "mode (", "wait_seconds (", " format ("):
        assert argument not in text


def test_site_options_description() -> None:
    fields = option_fields(TOOLS["site_structure"].rest_model, TOOLS["site_structure"].args_model)

    assert sorted(fields) == ["cache", "compliance", "metadata", "sitemaps"]


def test_hints_are_short() -> None:
    hints = [*OPTION_HINTS.values(), *(h for route in ROUTE_HINTS.values() for h in route.values())]
    assert [hint for hint in hints if len(hint) > 70] == []


def test_the_screenshot_format_is_an_image_format() -> None:
    assert "format (png|jpeg|webp; default png)" in description("screenshot_url")
    assert "format (markdown|text|html; default markdown)" in description("chunk")
