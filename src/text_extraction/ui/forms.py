"""The UI's operations and the REST bodies of its forms (rulings P10-S14, P10-S15).

Each operation names one REST route. A form's fields build the route's body; the "Options
(JSON)" textarea is merged under them: a field left at its default yields to the same key in
Options (so ``format: html``, which the form does not offer, goes through Options), while a key
the form sets (a typed value, a box or choice changed from its default) and Options both give is
refused with "<key> is given twice.". Nothing is forwarded for a refused form. The REST route
validates the merged body as for any client.

"Site rules" (``access``, rulings P12-S3, package 16) is a form field that is no body field: the
radio of a step (``enforce``, ``report`` or ``ignore``) sends that ``compliance`` as set by the
user; the deployment's default step (radio value ``default``) sends nothing. The steps a caller
sees come from `access_offer`; REST refuses what a key may not choose (``ignore`` for a user)."""

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Literal

from text_extraction.compliance import ComplianceMode
from text_extraction.documents.routing import ocr_available
from text_extraction.file_api import PATH as FILE_PATH
from text_extraction.pii_dispatch import pii_available
from text_extraction.pipeline_media import asr_available
from text_extraction.settings import Settings, llm_available
from text_extraction.site_api import PATH as SITE_PATH
from text_extraction.structured_api import CHUNK_PATH, STRUCTURED_PATH
from text_extraction.ui.i18n import Message, MessageError

DEFAULT_WAIT_SECONDS: Final = 25
DEFAULT_MAX_IMAGES: Final = 5
DEFAULT_SITE_DEPTH: Final = 2
DEFAULT_SITE_PAGES: Final = 100
CHECKED: Final = "on"
# The "Site rules" choice: its form field and the radio value of the deployment's default step.
ACCESS_FIELD: Final = "access"
ACCESS_DEFAULT: Final = "default"
_ACCESS_ORDER: Final[tuple[ComplianceMode, ...]] = ("enforce", "report", "ignore")
# The choices of the optional features (package 19, P19-R3), each with the value that sends
# nothing (the API's default): OCR, personal data, the structured strategy, the chunking method.
OCR_CHOICES: Final = ("auto", "never", "always")
PII_CHOICES: Final = ("", "mask", "redact", "hash", "annotate")
STRATEGY_CHOICES: Final = ("auto", "metadata", "selectors", "patterns", "llm")
METHOD_CHOICES: Final = ("recursive", "fixed", "markdown", "sentence", "semantic")
# The strategy and the method that need the LLM.
LLM_STRATEGIES: Final = frozenset({"llm"})
LLM_METHODS: Final = frozenset({"semantic"})

# The screenshot form's "Capture" choice: each preset is the REST fields it sets (a field it does
# not name stays at its default); ``custom`` reads the form's own fields instead.
_Preset = Mapping[str, Any]
SCREENSHOT_PRESETS: Final[Mapping[str, _Preset]] = MappingProxyType(
    {
        "page": {"full_page": True},
        "viewport": {"full_page": False, "viewport": {"width": 1920, "height": 1080}},
        "social": {
            "full_page": False,
            "viewport": {"width": 1200, "height": 630},
            "format": "webp",
            "quality": 80,
        },
        "thumb": {
            "full_page": False,
            "viewport": {"width": 1280, "height": 800},
            "format": "webp",
            "width": 400,
        },
        "mobile": {"full_page": True, "viewport": {"width": 390, "height": 844}},
    }
)
DEFAULT_PRESET: Final = "page"
CUSTOM_PRESET: Final = "custom"
DEFAULT_VIEWPORT: Final = (1920, 1080)
DEFAULT_IMAGE_FORMAT: Final = "png"


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


class OptionsError(MessageError):
    """A form that cannot become a REST body; its ``message`` is shown, nothing is forwarded.
    ``field`` names the form field at fault (None: the whole form), so the page can tie the
    message to its control (`error_control`)."""

    def __init__(self, message: Message, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


# The forms whose fields an error can name, by the prefix of their ids: a form's id is
# ``<prefix>-form`` (htmx sends it as ``HX-Trigger``), a control's ``<prefix>-<field>`` with
# each ``_`` of the field's name as ``-`` (ruling P12-S8).
FORM_PREFIXES: Final = frozenset(
    {"extract", "file", "screenshot", "structured", "chunk", "batch", "site", "tree", "preview"}
)
FORM_ID_SUFFIX: Final = "-form"


def error_control(form_id: str | None, field: str | None) -> str | None:
    """The id of the control of ``field`` in the form whose id is ``form_id``; None for an
    error of the whole form, or a form this page does not have."""
    if field is None or form_id is None or not form_id.endswith(FORM_ID_SUFFIX):
        return None
    prefix = form_id.removesuffix(FORM_ID_SUFFIX)
    if prefix not in FORM_PREFIXES:
        return None
    return f"{prefix}-{field.replace('_', '-')}"


# The body fields of a form that a REST 422 can name, by operation: the answer's ``details`` then
# tie to the control of the first of them (`rest_error_control`). The screenshot form's are its
# "Custom capture" fields.
_NAMED_FIELDS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {"screenshot": ("full_page", "format", "quality", "width", "viewport")}
)
_MODEL_ERROR_PREFIX: Final = "Value error, "


def _detail_field(detail: Mapping[str, Any], names: tuple[str, ...]) -> str | None:
    """The form field a 422 detail names: by its ``loc`` (``body``, the field, for a viewport
    its side) or, for an error of the whole body, by the first word of its message."""
    loc = [str(part) for part in detail.get("loc") or [] if part != "body"]
    if loc and loc[0] in names:
        if loc[0] != "viewport":
            return loc[0]
        side = loc[1] if len(loc) > 1 and loc[1] in ("width", "height") else "width"
        return f"viewport_{side}"
    words = str(detail.get("msg", "")).removeprefix(_MODEL_ERROR_PREFIX).split(maxsplit=1)
    if not loc and words and words[0] in names:
        return "viewport_width" if words[0] == "viewport" else words[0]
    return None


def _options_keys(form: Mapping[str, str]) -> frozenset[str]:
    """The keys of the form's "Options (JSON)" object; none when it is empty or no object."""
    try:
        options = _json_object(form, "options", Message("options.label.options"))
    except OptionsError:
        return frozenset()
    return frozenset(options or ())


def rest_error_control(op: str, details: object, form: Mapping[str, str]) -> str | None:
    """The id of the control at fault in a REST 422 to ``op``'s form, posted as ``form``, for
    the first of its fields the ``details`` name that can be tied: the "Options (JSON)" field
    when the field's key came from there, else the field's own control, but only with the
    preset ``custom`` (ui.js disables the custom fields for any other). None for an operation
    whose fields are not named, or details that tie to no control (the status line still shows
    the error)."""
    names = _NAMED_FIELDS.get(op)
    if names is None or not isinstance(details, list):
        return None
    form_id = f"{op}{FORM_ID_SUFFIX}"
    from_options = _options_keys(form)
    custom = _text(form, "preset") == CUSTOM_PRESET
    for detail in details:
        field = _detail_field(detail, names) if isinstance(detail, Mapping) else None
        if field is None:
            continue
        key = "viewport" if field.startswith("viewport_") else field
        if key in from_options:
            return error_control(form_id, "options")
        if custom:
            return error_control(form_id, field)
    return None


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


def _picked(form: Mapping[str, str], name: str, choices: tuple[str, ...]) -> str:
    """The value of the select ``name``: one of ``choices`` (the first, its default, when the
    form sends none)."""
    value = _text(form, name) or choices[0]
    if value not in choices:
        raise OptionsError(Message("options.unknown_choice", {"name": name, "value": value}), name)
    return value


def _optional_choice(
    fields: Fields, form: Mapping[str, str], name: str, choices: tuple[str, ...]
) -> None:
    """A select that sends nothing at its default (the first of ``choices``)."""
    value = _picked(form, name, choices)
    if value != choices[0]:
        fields[name] = _Field(value, set_by_user=True)


def _features(fields: Fields, form: Mapping[str, str], *, transcribe: bool) -> None:
    """OCR, personal data and (``transcribe``) "Transcribe": each only when chosen."""
    _optional_choice(fields, form, "ocr", OCR_CHOICES)
    _optional_choice(fields, form, "pii", PII_CHOICES)
    if transcribe and _text(form, "transcribe") == CHECKED:
        fields["transcribe"] = _Field(True, set_by_user=True)


def _with_choice(
    fields: Fields, form: Mapping[str, str], name: str, select: str, choices: tuple[str, ...]
) -> None:
    """The select ``select`` sets ``strategy`` in the JSON object ``name``: at its default it
    leaves a strategy the JSON names, otherwise the JSON must name none or the same one."""
    value = _picked(form, select, choices)
    given = fields.get(name)
    if given is None:
        if value != choices[0]:
            fields[name] = _Field({"strategy": value}, set_by_user=True)
        return
    named = given.value.get("strategy")
    if value == choices[0] or named == value:
        return
    if named is not None:
        raise OptionsError(Message("options.given_twice", {"name": "strategy"}), select)
    given.value["strategy"] = value


def _whole_number(form: Mapping[str, str], name: str, default: int) -> int:
    value = _text(form, name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise OptionsError(Message("options.whole_number", {"name": name}), name) from None


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
            raise OptionsError(Message("options.number", {"name": "wait_seconds"}), "wait_seconds")
    fields["wait_seconds"] = _Field(value, set_by_user=value != DEFAULT_WAIT_SECONDS)


def _access(fields: Fields, form: Mapping[str, str]) -> None:
    """The "Site rules" choice: ``enforce``, ``report`` or ``ignore`` sends that ``compliance``
    as set by the user (so Options cannot give ``compliance`` too); ``default``, empty or any
    other value sends nothing."""
    value = _text(form, ACCESS_FIELD)
    if value in _ACCESS_ORDER:
        fields["compliance"] = _Field(value, set_by_user=True)


@dataclass(frozen=True)
class AccessOffer:
    """The "Site rules" steps a caller is offered: ``modes`` in the order enforce, report,
    ignore; ``default`` is the deployment's default step; ``unblock`` tells that ``ignore`` also
    unlocks the unblock browser for this caller."""

    modes: tuple[ComplianceMode, ...]
    default: ComplianceMode
    unblock: bool

    @property
    def shown(self) -> bool:
        """Whether there is a choice to show."""
        return len(self.modes) > 1


def access_offer(role: str, settings: Settings) -> AccessOffer:
    """The steps the forms offer to a caller of ``role``: an admin all three; anyone else
    ``enforce`` and ``report`` while ``compliance_allow_override`` is on, plus ``ignore`` only
    when it is the default; without override only the default (as REST's ``resolve_mode``)."""
    default = settings.compliance_default
    if role == "admin":
        allowed = set(_ACCESS_ORDER)
    elif settings.compliance_allow_override:
        allowed = {"enforce", "report", default}
    else:
        allowed = {default}
    unblock = role == "admin" and settings.browser_enabled and settings.browser_unblock_enabled
    return AccessOffer(tuple(m for m in _ACCESS_ORDER if m in allowed), default, unblock)


def access_value(offer: AccessOffer, mode: ComplianceMode | None) -> str:
    """The radio value that presets ``mode``: ``default`` for None, the default step or a mode
    the caller is not offered, else the mode's name."""
    if mode is None or mode == offer.default or mode not in offer.modes:
        return ACCESS_DEFAULT
    return mode


@dataclass(frozen=True)
class FeatureOffer:
    """The optional features the forms offer to a caller (package 19): each is True while the
    service has it. ``llm_needs_key``: the service has no ``LLM_API_KEY``, so LLM work needs the
    caller's key (the key field)."""

    ocr: bool
    pii: bool
    transcribe: bool
    llm: bool
    llm_needs_key: bool


def feature_offer(role: str, settings: Settings) -> FeatureOffer:
    """The features the forms offer to a caller of ``role``."""
    del role  # every role is offered the same features (P19-D1)
    llm = llm_available(settings)
    return FeatureOffer(
        ocr=ocr_available(settings),
        pii=pii_available(settings),
        transcribe=asr_available(settings) and settings.media_enabled,
        llm=llm,
        llm_needs_key=llm and settings.llm_api_key is None,
    )


def _no_constant(name: str) -> Any:
    """``NaN`` and ``Infinity`` are not JSON."""
    raise ValueError(name)


def _json_object(form: Mapping[str, str], name: str, label: Message) -> dict[str, Any] | None:
    """The JSON object of the textarea ``name`` (None when it is empty); ``label`` names it in
    the messages."""
    value = _text(form, name)
    if not value:
        return None
    try:
        parsed = json.loads(value, parse_constant=_no_constant)
    except (ValueError, RecursionError):  # RecursionError: nested too deep
        raise OptionsError(Message("options.not_json", {"label": label}), name) from None
    if not isinstance(parsed, dict):
        raise OptionsError(Message("options.not_object", {"label": label}), name)
    return parsed


def _object(fields: Fields, form: Mapping[str, str], name: str, label: Message) -> None:
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
    if _text(form, "screenshot") == CHECKED and _text(form, "mode") != "fast":
        fields["screenshot"] = _Field(True, set_by_user=True)  # the API refuses it with mode fast
    if _text(form, "metadata_structured") == CHECKED:
        structured = {"strategy": "metadata", "schema": UI_METADATA_SCHEMA}
        fields["structured"] = _Field(structured, set_by_user=True)
    if _text(form, "describe_images") == CHECKED:
        max_images = _whole_number(form, "max_images", DEFAULT_MAX_IMAGES)
        fields["describe_images"] = _Field({"max_images": max_images}, set_by_user=True)
    _features(fields, form, transcribe=True)
    _wait(fields, form)
    return fields


def _file_fields(form: Mapping[str, str]) -> Fields:
    fields: Fields = {}
    _choice(fields, form, "format", "markdown")
    _box(fields, form, "metadata", default=True)
    _features(fields, form, transcribe=False)  # audio and video uploads are always transcribed
    _wait(fields, form)
    return fields


def _preset_fields(fields: Fields, preset: _Preset) -> None:
    """The fields a preset sets; the REST fields it leaves out stay at their defaults."""
    full_page = preset.get("full_page", True)
    fields["full_page"] = _Field(full_page, set_by_user=full_page is not True)
    for name in ("viewport", "format", "quality", "width"):
        if name in preset:
            fields[name] = _Field(preset[name], set_by_user=True)


def _custom_fields(fields: Fields, form: Mapping[str, str]) -> None:
    """The "Custom" capture: the fields as typed, each left out at its default. A viewport side
    left empty takes the default of the viewport (1920x1080)."""
    sides = (
        _whole_number(form, "viewport_width", DEFAULT_VIEWPORT[0]),
        _whole_number(form, "viewport_height", DEFAULT_VIEWPORT[1]),
    )
    if sides != DEFAULT_VIEWPORT:
        viewport = {"width": sides[0], "height": sides[1]}
        fields["viewport"] = _Field(viewport, set_by_user=True)
    _box(fields, form, "full_page", default=True)
    image_format = _text(form, "format") or DEFAULT_IMAGE_FORMAT
    if image_format != DEFAULT_IMAGE_FORMAT:
        fields["format"] = _Field(image_format, set_by_user=True)
    for name in ("quality", "width"):
        if _text(form, name):
            fields[name] = _Field(_whole_number(form, name, 0), set_by_user=True)


def _screenshot_fields(form: Mapping[str, str]) -> Fields:
    """The "Capture" choice (`SCREENSHOT_PRESETS`) sets ``full_page``, ``viewport``, ``format``,
    ``quality`` and ``width``; only ``custom`` reads the fields below it (a form without the
    choice is the default preset)."""
    fields: Fields = {}
    _typed(fields, form, "url")
    _access(fields, form)
    name = _text(form, "preset") or DEFAULT_PRESET
    if name == CUSTOM_PRESET:
        _custom_fields(fields, form)
    elif name in SCREENSHOT_PRESETS:
        _preset_fields(fields, SCREENSHOT_PRESETS[name])
    else:
        raise OptionsError(Message("options.unknown_preset", {"name": name}), "preset")
    _wait(fields, form)
    return fields


def _structured_fields(form: Mapping[str, str]) -> Fields:
    fields: Fields = {}
    _typed(fields, form, "url")
    _choice(fields, form, "format", "markdown")
    _choice(fields, form, "mode", "auto")
    _access(fields, form)
    _object(fields, form, "structured", Message("options.label.structured"))
    _with_choice(fields, form, "structured", "strategy", STRATEGY_CHOICES)
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
    _object(fields, form, "chunking", Message("options.label.chunking"))
    _with_choice(fields, form, "chunking", "method", METHOD_CHOICES)
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
    _features(fields, form, transcribe=True)
    return fields


def _site_fields(form: Mapping[str, str]) -> Fields:
    fields: Fields = {}
    _typed(fields, form, "url")
    for name, default in (("depth", DEFAULT_SITE_DEPTH), ("max_pages", DEFAULT_SITE_PAGES)):
        value = _whole_number(form, name, default)
        fields[name] = _Field(value, set_by_user=value != default)
    _choice(fields, form, "scope", "host")
    _box(fields, form, "sitemaps", default=True)
    # The form's radios always post a choice (its preset is "Fast: sitemap first"); a post without
    # one is not from the form and reads as the API's default.
    _choice(fields, form, "discovery", "crawl")
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
    options = _json_object(form, "options", Message("options.label.options")) or {}
    body: dict[str, Any] = {}
    for name, value in options.items():
        given = fields.get(name)
        if given is not None and given.set_by_user:
            raise OptionsError(Message("options.given_twice", {"name": name}), "options")
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
