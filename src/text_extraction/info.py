"""The core of ``GET /v1/info``: name and version of the service, who the caller is, which
features the caller's key can use and which limits apply (package 24, gap 2). A module of its
own, so that the MCP tool ``service_info`` does not import the app module."""

from typing import Final, Literal

from pydantic import BaseModel, Field

from text_extraction import __version__
from text_extraction.auth import Principal, Role
from text_extraction.compliance import ComplianceMode
from text_extraction.documents.routing import ocr_available
from text_extraction.envelope import Envelope
from text_extraction.feature_access import Feature, allowed
from text_extraction.file_api import upload_file_limit
from text_extraction.pii_dispatch import pii_available
from text_extraction.pipeline_media import asr_available
from text_extraction.settings import Settings, llm_available
from text_extraction.tracing import current_trace_id

SERVICE_NAME = "text-extraction"

OfferedFeature = Literal["ocr", "pii", "transcribe", "llm"]
# The switch (``feature_access``) behind each offered feature.
_SWITCH: Final[dict[OfferedFeature, Feature]] = {
    "ocr": "ocr",
    "pii": "pii",
    "transcribe": "asr",
    "llm": "llm",
}


def offered_features(
    role: str | None, settings: Settings
) -> tuple[dict[OfferedFeature, bool], frozenset[str]]:
    """The optional features a key of ``role`` can use (the service has them and the role may
    use them, ``feature_access.allowed``), and those the service has but keeps for admin keys.
    The UI's forms (``ui.forms.feature_offer``) and ``GET /v1/info`` both read it."""
    has: dict[OfferedFeature, bool] = {
        "ocr": ocr_available(settings),
        "pii": pii_available(settings),
        "transcribe": asr_available(settings) and settings.media_enabled,
        "llm": llm_available(settings),
    }
    admin_only = frozenset(
        name for name, on in has.items() if on and not allowed(_SWITCH[name], role, settings)
    )
    return {name: on and name not in admin_only for name, on in has.items()}, admin_only


class InfoFeatures(BaseModel):
    """What the caller's key can use on this service."""

    jobs: bool = Field(description="Jobs: `/v1/batch`, `/v1/site-structure`, slow requests.")
    browser: bool = Field(description="Browser mode (`mode: browser`, `auto` escalation).")
    screenshots: bool = Field(description="`/v1/screenshot` and `screenshot: true`.")
    unblock: bool = Field(
        description="Unblock mode under `compliance: ignore` (admin keys, "
        "`BROWSER_UNBLOCK_ENABLED`)."
    )
    media: bool = Field(description="Video pages: subtitles and details.")
    ocr: bool = Field(description="OCR of documents (Docling).")
    pii: bool = Field(description="`pii`: finding and handling personal data.")
    transcribe: bool = Field(description="`transcribe` and audio/video uploads.")
    llm: bool = Field(description="The LLM: strategy `llm`, `learn_selectors`.")
    llm_needs_key: bool = Field(
        description="The LLM needs the caller's `X-LLM-Api-Key` (the service has no key)."
    )
    semantic_chunking: bool = Field(description="Chunking strategy `semantic`.")
    describe_images: bool = Field(description="`describe_images` with the vision model.")
    site_structure: bool = Field(description="`/v1/site-structure`.")
    compliance_choice: bool = Field(
        description="The key may choose `compliance` (`ignore` stays for admin keys)."
    )
    compliance_default: ComplianceMode = Field(description="The mode without a request.")


class InfoLimits(BaseModel):
    """The limits that apply to the caller's requests."""

    batch_max_urls: int = Field(description="Most URLs of one batch (`BATCH_MAX_URLS`).")
    site_max_depth: int = Field(description="Most `depth` of a site job (`SITE_MAX_DEPTH`).")
    site_max_pages: int = Field(description="Most `max_pages` of a site job (`SITE_MAX_PAGES`).")
    upload_max_bytes: int = Field(description="Most bytes of an uploaded file.")
    request_max_body_bytes: int = Field(
        description="Most bytes of a JSON body (`REQUEST_MAX_BODY_BYTES`; batches and chunk "
        "texts have larger limits)."
    )
    rate_limit_per_minute: int | None = Field(
        description="Requests per minute of the key; null while rate limiting is off."
    )


class InfoData(BaseModel):
    name: str
    version: str
    principal: str
    role: Role
    features: InfoFeatures
    limits: InfoLimits


def _features(principal: Principal, settings: Settings) -> InfoFeatures:
    offered, _ = offered_features(principal.role, settings)
    admin = principal.role == "admin"
    browser = settings.browser_enabled
    return InfoFeatures(
        jobs=settings.jobs_enabled,
        browser=browser,
        screenshots=browser and settings.jobs_enabled,
        unblock=browser and settings.browser_unblock_enabled and admin,
        media=settings.media_enabled,
        ocr=offered["ocr"],
        pii=offered["pii"],
        transcribe=offered["transcribe"],
        llm=offered["llm"],
        llm_needs_key=offered["llm"] and settings.llm_api_key is None,
        semantic_chunking=offered["llm"] and settings.llm_embedding_model is not None,
        describe_images=offered["llm"],
        site_structure=settings.jobs_enabled,
        compliance_choice=admin or settings.compliance_allow_override,
        compliance_default=settings.compliance_default,
    )


def _limits(principal: Principal, settings: Settings) -> InfoLimits:
    rate = (
        principal.rate_limit_per_minute
        if principal.rate_limit_per_minute is not None
        else settings.rate_limit_per_minute
    )
    return InfoLimits(
        batch_max_urls=settings.batch_max_urls,
        site_max_depth=settings.site_max_depth,
        site_max_pages=settings.site_max_pages,
        upload_max_bytes=upload_file_limit(settings),
        request_max_body_bytes=settings.request_max_body_bytes,
        rate_limit_per_minute=rate if settings.rate_limit_enabled else None,
    )


def service_info(principal: Principal, settings: Settings) -> Envelope[InfoData]:
    """The core of ``GET /v1/info`` (ruling P10-S1): name and version of the service, who the
    caller is, the features its key can use and the limits.

    Caller's checks (this core runs none of them): ``principal_for`` (the user role, which every
    key has), then one ``check_rate`` hit."""
    data = InfoData(
        name=SERVICE_NAME,
        version=__version__,
        principal=principal.name,
        role=principal.role,
        features=_features(principal, settings),
        limits=_limits(principal, settings),
    )
    return Envelope(status="success", data=data, trace_id=current_trace_id())
