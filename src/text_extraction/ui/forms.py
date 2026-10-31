"""The UI's operations and the REST bodies of its forms (rulings P10-S14, P10-S15).

Each operation names one REST route. A form's fields build the route's body; the "Options
(JSON)" textarea is merged under them: a field left at its default yields to the same key in
Options (so ``format: html``, which the form does not offer, goes through Options), while a key
the form sets (a typed value, a box or choice changed from its default) and Options both give is
refused with "<key> is given twice.". Nothing is forwarded for a refused form. The REST route
validates the merged body as for any client.

"Site rules" (``access``, ruling P12-S3) is a form field that is no body field: ``respect`` (the
default) sends no ``compliance``, ``maximum`` sends ``compliance: "ignore"`` as set by the user.
Only admins see the choice (`access_choice`); REST refuses ``ignore`` from anyone else."""

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Literal

from text_extraction.file_api import PATH as FILE_PATH
from text_extraction.settings import Settings
from text_extraction.site_api import PATH as SITE_PATH
from text_extraction.structured_api import CHUNK_PATH, STRUCTURED_PATH

DEFAULT_WAIT_SECONDS: Final = 25
DEFAULT_MAX_IMAGES: Final = 5
DEFAULT_SITE_DEPTH: Final = 2
DEFAULT_SITE_PAGES: Final = 100
CHECKED: Final = "on"
# The "Site rules" choice: its form field and its two values.
ACCESS_FIELD: Final = "access"
ACCESS_RESPECT: Final = "respect"
ACCESS_MAXIMUM: Final = "maximum"
AccessChoice = Literal["hidden", "enabled", "disabled"]


@dataclass(frozen=True)
class Operation:
    """One REST call of the UI: its method, its path (with ``{job_id}`` and ``{name}`` as in the
    route) and the template of its form ("" for an operation without a form of its own)."""

    method: Literal["GET", "POST", "DELETE"]
    path: str
    form: str


OPERATIONS: Final[Mapping[str, Operation]] = MappingProxyType(
    {
        "login": Operation("GET", "/v1/info", "login.html"),
        "info": Operation("GET", "/v1/info", "forms/info.html"),
        "extract": Operation("POST", "/v1/extract", "forms/extract.html"),
        "file": Operation("POST", FILE_PATH, "forms/file.html"),
        "screenshot": Operation("POST", "/v1/screenshot", "forms/screenshot.html"),
        "structured": Operation("POST", STRUCTURED_PATH, "forms/structured.html"),
        "chunk": Operation("POST", CHUNK_PATH, "forms/chunk.html"),
        "batch": Operation("POST", "/v1/batch", "forms/batch.html"),
        "site-structure": Operation("POST", SITE_PATH, "forms/site_structure.html"),
        "job": Operation("GET", "/v1/jobs/{job_id}", ""),
        "artifact": Operation("GET", "/v1/jobs/{job_id}/artifacts/{name}", ""),
        "cancel": Operation("DELETE", "/v1/jobs/{job_id}", ""),
    }
)

# The fields "Metadata, structured" asks for: the `metadata` strategy reads them by name from the
# page's JSON-LD, microdata, RDFa, OpenGraph and meta tags (schema.org and LRMI names).
UI_METADATA_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "headline": {"type": "string"},
        "description": {"type": "string"},
        "author": {"type": "array", "items": {"type": "string"}},
        "datePublished": {"type": "string"},
        "dateModified": {"type": "string"},
        "license": {"type": "string"},
        "inLanguage": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "publisher": {"type": "string"},
        "educationalLevel": {"type": "array", "items": {"type": "string"}},
        "learningResourceType": {"type": "array", "items": {"type": "string"}},
        "isAccessibleForFree": {"type": "boolean"},
    },
}


class OptionsError(ValueError):
    """A form that cannot become a REST body; the message is shown, nothing is forwarded."""


@dataclass(frozen=True)
class _Field:
    """A body field of a form: its value, and whether the user set it (else Options may give
    the key instead)."""

    value: Any
    set_by_user: bool


Fields = dict[str, _Field]


def _text(form: Mapping[str, str], name: str) -> str:
    value = form.get(name)
    return value.strip() if isinstance(value, str) else ""


def _typed(fields: Fields, form: Mapping[str, str], name: str) -> None:
    """A typed text field: in the body when it is not empty."""
    value = _text(form, name)
    if value:
        fields[name] = _Field(value, set_by_user=True)


def _choice(fields: Fields, form: Mapping[str, str], name: str, default: str) -> None:
    value = _text(form, name) or default
    fields[name] = _Field(value, set_by_user=value != default)


def _box(fields: Fields, form: Mapping[str, str], name: str, *, default: bool) -> None:
    """A checkbox: checked is true, absent is false (a browser sends no unchecked box)."""
    value = _text(form, name) == CHECKED
    fields[name] = _Field(value, set_by_user=value != default)


def _whole_number(form: Mapping[str, str], name: str, default: int) -> int:
    value = _text(form, name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise OptionsError(f"{name} must be a whole number.") from None


def _wait(fields: Fields, form: Mapping[str, str]) -> None:
    """``wait_seconds``: a whole or decimal number of seconds (REST checks its range)."""
    text = _text(form, "wait_seconds")
    value: float = DEFAULT_WAIT_SECONDS
    if text:
        try:
            value = int(text) if text.lstrip("-").isdigit() else float(text)
        except ValueError:
            value = math.nan
        if not math.isfinite(value):
            raise OptionsError("wait_seconds must be a number.")
    fields["wait_seconds"] = _Field(value, set_by_user=value != DEFAULT_WAIT_SECONDS)


def _access(fields: Fields, form: Mapping[str, str]) -> None:
    """The "Site rules" choice: ``maximum`` sends ``compliance: "ignore"`` as set by the user
    (so Options cannot give ``compliance`` too); ``respect`` or any other value sends nothing."""
    if _text(form, ACCESS_FIELD) == ACCESS_MAXIMUM:
        fields["compliance"] = _Field("ignore", set_by_user=True)


def access_choice(role: str, settings: Settings) -> AccessChoice:
    """How the forms offer "Site rules" to a caller of ``role``: ``hidden`` for anyone but an
    admin; for an admin ``enabled`` while browser mode and unblock mode are both on
    (``BROWSER_ENABLED``, ``BROWSER_UNBLOCK_ENABLED``), else ``disabled``."""
    if role != "admin":
        return "hidden"
    if settings.browser_enabled and settings.browser_unblock_enabled:
        return "enabled"
    return "disabled"


def _no_constant(name: str) -> Any:
    """``NaN`` and ``Infinity`` are not JSON."""
    raise ValueError(name)


def _json_object(form: Mapping[str, str], name: str, label: str) -> dict[str, Any] | None:
    value = _text(form, name)
    if not value:
        return None
    try:
        parsed = json.loads(value, parse_constant=_no_constant)
    except (ValueError, RecursionError):  # RecursionError: nested too deep
        raise OptionsError(f"{label} is not valid JSON.") from None
    if not isinstance(parsed, dict):
        raise OptionsError(f"{label} must be a JSON object.")
    return parsed


def _object(fields: Fields, form: Mapping[str, str], name: str, label: str) -> None:
    parsed = _json_object(form, name, label)
    if parsed is not None:
        fields[name] = _Field(parsed, set_by_user=True)


def _extract_fields(form: Mapping[str, str]) -> Fields:
    fields: Fields = {}
    _typed(fields, form, "url")
    _choice(fields, form, "format", "markdown")
    _choice(fields, form, "mode", "auto")
    _access(fields, form)
    _box(fields, form, "metadata", default=True)
    if _text(form, "metadata_structured") == CHECKED:
        structured = {"strategy": "metadata", "schema": UI_METADATA_SCHEMA}
        fields["structured"] = _Field(structured, set_by_user=True)
    if _text(form, "describe_images") == CHECKED:
        max_images = _whole_number(form, "max_images", DEFAULT_MAX_IMAGES)
        fields["describe_images"] = _Field({"max_images": max_images}, set_by_user=True)
    _wait(fields, form)
    return fields


def _file_fields(form: Mapping[str, str]) -> Fields:
    fields: Fields = {}
    _choice(fields, form, "format", "markdown")
    _box(fields, form, "metadata", default=True)
    _wait(fields, form)
    return fields


def _screenshot_fields(form: Mapping[str, str]) -> Fields:
    fields: Fields = {}
    _typed(fields, form, "url")
    _access(fields, form)
    _box(fields, form, "full_page", default=True)
    _wait(fields, form)
    return fields


def _structured_fields(form: Mapping[str, str]) -> Fields:
    fields: Fields = {}
    _typed(fields, form, "url")
    _choice(fields, form, "format", "markdown")
    _choice(fields, form, "mode", "auto")
    _access(fields, form)
    _object(fields, form, "structured", "Structured")
    _wait(fields, form)
    return fields


def _chunk_fields(form: Mapping[str, str]) -> Fields:
    """``url`` or ``text``: a text has no page, so no ``mode`` and no "Site rules" (REST
    refuses page options with a text)."""
    fields: Fields = {}
    _typed(fields, form, "url")
    if form.get("text"):  # kept as typed: its whitespace is part of the text
        fields["text"] = _Field(form["text"], set_by_user=True)
        _choice(fields, form, "text_type", "markdown")
    else:
        _access(fields, form)
    _choice(fields, form, "format", "markdown")
    _object(fields, form, "chunking", "Chunking")
    _wait(fields, form)
    return fields


def _no_fields(form: Mapping[str, str]) -> Fields:
    return {}


def _batch_fields(form: Mapping[str, str]) -> Fields:
    """The options of a batch; its ``urls`` are the route's (`batch_body` leaves them empty), so
    Options cannot give them."""
    fields: Fields = {"urls": _Field([], set_by_user=True)}
    _choice(fields, form, "format", "markdown")
    _choice(fields, form, "mode", "auto")
    _access(fields, form)
    _box(fields, form, "metadata", default=True)
    return fields


def _site_fields(form: Mapping[str, str]) -> Fields:
    fields: Fields = {}
    _typed(fields, form, "url")
    for name, default in (("depth", DEFAULT_SITE_DEPTH), ("max_pages", DEFAULT_SITE_PAGES)):
        value = _whole_number(form, name, default)
        fields[name] = _Field(value, set_by_user=value != default)
    _choice(fields, form, "scope", "host")
    _box(fields, form, "sitemaps", default=True)
    _access(fields, form)
    return fields


_FORMS: Final[Mapping[str, Callable[[Mapping[str, str]], Fields]]] = MappingProxyType(
    {
        "info": _no_fields,
        "extract": _extract_fields,
        "file": _file_fields,
        "screenshot": _screenshot_fields,
        "structured": _structured_fields,
        "chunk": _chunk_fields,
        "batch": _batch_fields,
        "site-structure": _site_fields,
    }
)
# The operations whose form is one REST call shown as a result (``POST /ui/run/<op>``).
SINGLE_OPERATIONS: Final = ("info", "extract", "file", "screenshot", "structured", "chunk")


def build_body(op: str, form: Mapping[str, str]) -> dict[str, Any]:
    """The REST body of the form of ``op`` (one of ``SINGLE_OPERATIONS``, ``batch`` with empty
    ``urls``, or ``site-structure``): its fields, with the "Options (JSON)" textarea
    (``options``) merged under them. Raises `OptionsError` for invalid JSON, an Options value
    that is not an object, a key given twice or a malformed number."""
    fields = _FORMS[op](form)
    options = _json_object(form, "options", "Options") or {}
    body: dict[str, Any] = {}
    for name, value in options.items():
        given = fields.get(name)
        if given is not None and given.set_by_user:
            raise OptionsError(f"{name} is given twice.")
        body[name] = value
    for name, field in fields.items():
        if field.set_by_user or name not in options:
            body[name] = field.value
    return _ordered(body, fields)


def _ordered(body: dict[str, Any], fields: Fields) -> dict[str, Any]:
    """The form's fields first, in form order, then the Options keys (for the curl line)."""
    first = {name: body[name] for name in fields if name in body}
    return first | {name: value for name, value in body.items() if name not in first}


def multipart_fields(body: Mapping[str, Any]) -> dict[str, str]:
    """``body`` as the text fields of an upload form (``POST /v1/extract/file``): a boolean is
    ``true``/``false``, an object or a list its JSON, anything else its text."""
    fields: dict[str, str] = {}
    for name, value in body.items():
        if isinstance(value, bool):
            fields[name] = "true" if value else "false"
        elif isinstance(value, dict | list):
            fields[name] = json.dumps(value)
        else:
            fields[name] = str(value)
    return fields
