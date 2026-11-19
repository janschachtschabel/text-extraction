"""The hints of the ``options`` argument (package 24, gap 1).

An agent cannot read /docs, so each tool's ``options`` description names the further fields of
its route's REST body, each with a hint of at most 70 characters. The fields come from the REST
model (``option_fields``), so a new field shows up at once; ``tests/mcp_server/test_option_hints``
fails until it has a hint here.
"""

from typing import Final

from pydantic import BaseModel

OPTION_HINTS: Final[dict[str, str]] = {
    "metadata": "DOM metadata: title, licence, images; default true",
    "links": "the page's links; default false",
    "compliance": "site rules: enforce|report|ignore (ignore: admin keys)",
    "unblock": "false: no unblock browser under compliance ignore; default true",
    "cache": "use the result cache; default true",
    "actions": "[{type: click|scroll|wait_for|type|press|select|expand_all|...}]",
    "cookies": '[{"name", "value", "domain"}] set before the page loads',
    "headers": "extra request headers, e.g. Authorization",
    "screenshot": "also capture a screenshot (attached as an image)",
    "full_page": "screenshot of the whole page; default true",
    "screenshot_options": '{"viewport", "format": png|jpeg|webp, "quality", "width"}',
    "ocr": "auto|never|always",
    "transcribe": "speech recognition for audio/video without subtitles",
    "languages": 'subtitle/transcription languages, e.g. ["de", "en"]',
    "pii": 'personal data: mask|redact|hash|annotate or {"mode": ...}',
    "structured": '{"schema": {...}, "strategy": metadata|selectors|patterns|llm|auto}',
    "chunking": '{"strategy": fixed|recursive|markdown|sentence|semantic, "size"}',
    "describe_images": '{"max_images": n}: describe images with the vision model',
    "mode": "fast|auto|browser; default auto",
    "format": "markdown|text|html; default markdown",
    "quality": "jpeg/webp quality 1-100",
    "viewport": '{"width", "height"}; default 1920 x 1080',
    "width": "scale the image down to this width in px",
    "sitemaps": "read robots.txt and the sitemaps; default true",
}
# A field whose meaning differs on one route.
ROUTE_HINTS: Final[dict[str, dict[str, str]]] = {
    "POST /v1/screenshot": {"format": "png|jpeg|webp; default png"},
    "POST /v1/chunk": {"format": "markdown|text; default markdown"},
    "POST /v1/extract/file": {"metadata": "the document's metadata; default true"},
}
# A sentence after the field list of one route.
ROUTE_NOTES: Final[dict[str, str]] = {
    "POST /v1/chunk": " With `text` only format, metadata, pii and structured apply; "
    "the others need `url`.",
}


def option_hint(route: str, name: str) -> str | None:
    """The hint of the field ``name`` on ``route``; None for a field without one."""
    return ROUTE_HINTS.get(route, {}).get(name, OPTION_HINTS.get(name))


def option_fields(rest_model: type[BaseModel], args_model: type[BaseModel]) -> list[str]:
    """The fields of ``rest_model`` that are not an argument of the tool (``args_model``), in
    the REST model's order: the fields ``options`` carries."""
    own = args_model.model_fields
    return [name for name in rest_model.model_fields if name not in own]


def options_description(route: str, fields: list[str]) -> str:
    """The description of a tool's ``options``: its fields with their hints."""
    listed = ", ".join(
        name if (hint := option_hint(route, name)) is None else f"{name} ({hint})"
        for name in fields
    )
    note = ROUTE_NOTES.get(route, "")
    return f"Further fields of the REST body of {route} (details: /docs): {listed}.{note}"
