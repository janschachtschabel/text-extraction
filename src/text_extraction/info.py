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
from text_extraction.pii_dispatch import pii_available
from text_extraction.pipeline_media import asr_available
from text_extraction.settings import Settings, llm_available
from text_extraction.tracing import current_trace_id

SERVICE_NAME = "text-extraction"

OfferedFeature = Literal["ocr", "pii", "transcribe", "llm", "llm_needs_key", "semantic", "search"]
# The switch (``feature_access``) behind each offered feature.
_SWITCH: Final[dict[str, Feature]] = {
    "ocr": "ocr",
    "pii": "pii",
    "transcribe": "asr",
    "llm": "llm",
    "search": "search",
}


def offered_features(
    role: str | None, settings: Settings
) -> tuple[dict[OfferedFeature, bool], frozenset[str]]:
    """The optional features a key of ``role`` can use (the service has them and the role may
    use them, ``feature_access.allowed``), and those the service has but keeps for admin keys
    (``search`` too: ``SEARCH_ALLOW_USERS``, package 25); ``llm_needs_key`` (the LLM is offered
    and the service has no key) and ``semantic`` (semantic chunking: the LLM and an embedding
    model) follow from them. The UI's forms (``ui.forms.feature_offer``) and ``GET /v1/info``
    both read it."""
    has: dict[OfferedFeature, bool] = {
        "ocr": ocr_available(settings),
        "pii": pii_available(settings),
        "transcribe": asr_available(settings) and settings.media_enabled,
        "llm": llm_available(settings),
        "search": settings.search_enabled,
    }
    admin_only = frozenset(
        name for name, on in has.items() if on and not allowed(_SWITCH[name], role, settings)
    )
    offered: dict[OfferedFeature, bool] = {
        name: on and name not in admin_only for name, on in has.items()
    }
    offered["llm_needs_key"] = offered["llm"] and settings.llm_api_key is None
    offered["semantic"] = offered["llm"] and settings.llm_embedding_model is not None
    return offered, admin_only


def search_offered(role: str | None, settings: Settings) -> bool:
    """Whether a key of ``role`` can search: ``SEARCH_ENABLED`` and, for a user key,
    ``SEARCH_ALLOW_USERS``."""
    return settings.search_enabled and allowed("search", role, settings)


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
    site_apis: bool = Field(
        description="A site crawl's `apis`: feeds, WordPress REST API and Drupal JSON:API "
        "as sitemaps (`SITE_APIS_ENABLED`)."
    )
    compliance_choice: bool = Field(
        description="The key may ask for another `compliance` mode than the default (asking "
        "for `ignore` needs an admin key)."
    )
    compliance_default: ComplianceMode = Field(description="The mode without a request.")
    search: bool = Field(
        description="Web search: `POST /v1/search` (`SEARCH_ENABLED`; user keys while "
        "`SEARCH_ALLOW_USERS`)."
    )


class InfoLimits(BaseModel):
    """The limits that apply to the caller's requests."""

    batch_max_urls: int = Field(description="Most URLs of one batch (`BATCH_MAX_URLS`).")
    site_max_depth: int = Field(description="Most `depth` of a site job (`SITE_MAX_DEPTH`).")
    site_max_pages: int = Field(description="Most `max_pages` of a site job (`SITE_MAX_PAGES`).")
    document_max_bytes: int = Field(
        description="Most bytes of a document, uploaded or fetched (`DOCUMENT_MAX_BYTES`)."
    )
    media_upload_max_bytes: int | None = Field(
        description="Most bytes of an uploaded audio or video file (`MEDIA_UPLOAD_MAX_BYTES`); "
        "null when the key cannot have uploads transcribed."
    )
    request_max_body_bytes: int = Field(
        description="Most bytes of a JSON body (`REQUEST_MAX_BODY_BYTES`; batches and chunk "
        "texts have larger limits)."
    )
    rate_limit_per_minute: int | None = Field(
        description="Requests per minute of the key; null while rate limiting is off."
    )
    search_max_results: int | None = Field(
        description="Most `max_results` of a web search (`SEARCH_MAX_RESULTS`); null when the "
        "key cannot search."
    )


class InfoData(BaseModel):
    name: str
    version: str
    principal: str
    role: Role
    features: InfoFeatures
    limits: InfoLimits


def _features(
    principal: Principal, settings: Settings, offered: dict[OfferedFeature, bool]
) -> InfoFeatures:
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
        llm_needs_key=offered["llm_needs_key"],
        semantic_chunking=offered["semantic"],
        describe_images=offered["llm"],
        site_structure=settings.jobs_enabled,
        site_apis=settings.site_apis_enabled,
        compliance_choice=admin or settings.compliance_allow_override,
        compliance_default=settings.compliance_default,
        search=search_offered(principal.role, settings),
    )


def _limits(
    principal: Principal, settings: Settings, offered: dict[OfferedFeature, bool]
) -> InfoLimits:
    rate = (
        principal.rate_limit_per_minute
        if principal.rate_limit_per_minute is not None
        else settings.rate_limit_per_minute
    )
    return InfoLimits(
        batch_max_urls=settings.batch_max_urls,
        site_max_depth=settings.site_max_depth,
        site_max_pages=settings.site_max_pages,
        document_max_bytes=settings.document_max_bytes,
        media_upload_max_bytes=(settings.media_upload_max_bytes if offered["transcribe"] else None),
        request_max_body_bytes=settings.request_max_body_bytes,
        rate_limit_per_minute=rate if settings.rate_limit_enabled else None,
        search_max_results=(
            settings.search_max_results if search_offered(principal.role, settings) else None
        ),
    )


def service_info(principal: Principal, settings: Settings) -> Envelope[InfoData]:
    """The core of ``GET /v1/info`` (ruling P10-S1): name and version of the service, who the
    caller is, the features its key can use and the limits.

    Caller's checks (this core runs none of them): ``principal_for`` (the user role, which every
    key has), then one ``check_rate`` hit."""
    offered, _ = offered_features(principal.role, settings)
    data = InfoData(
        name=SERVICE_NAME,
        version=__version__,
        principal=principal.name,
        role=principal.role,
        features=_features(principal, settings, offered),
        limits=_limits(principal, settings, offered),
    )
    return Envelope(status="success", data=data, trace_id=current_trace_id())
