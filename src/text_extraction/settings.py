"""Environment-driven settings. Every setting is an env var with a documented default."""

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from pydantic_settings.exceptions import SettingsError

from text_extraction.pii.types import ENTITY_TYPES, EntityType

# Environment the OpenAI SDK reads whatever ``llm/client.py`` passes it (issue #19): extra
# headers (an ``Authorization`` line would replace the caller's key) and its plain-text log.
# Start-up refuses them while LLM_ENABLED is true. ``OPENAI_ORG_ID`` and ``OPENAI_PROJECT_ID``
# are read too, but the client never sends them.
OPENAI_ENV_REFUSED = ("OPENAI_CUSTOM_HEADERS", "OPENAI_LOG")

# Playwright's resource types (``Request.resource_type``). A blocked type is not loaded.
_RESOURCE_TYPES = (
    "document",
    "stylesheet",
    "image",
    "media",
    "font",
    "script",
    "texttrack",
    "xhr",
    "fetch",
    "eventsource",
    "websocket",
    "manifest",
    "other",
)


class ApiKey(BaseModel):
    """One API key. Only the SHA-256 of the key is stored, never the key itself."""

    model_config = ConfigDict(hide_input_in_errors=True)

    name: str = Field(min_length=1)
    role: Literal["user", "admin"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="Lowercase hex SHA-256 of the key.")
    rate_limit_per_minute: int | None = Field(default=None, ge=1)


_API_KEY_LIST = TypeAdapter(list[ApiKey])

_API_KEYS_HINT = "API_KEYS must be a JSON list of {name, role, sha256} objects"


class ConfigError(Exception):
    """The environment configuration is invalid. The message never contains input values."""


def _describe(error: ValidationError) -> str:
    """Join location and message of each error; input values are left out on purpose."""
    parts = []
    for item in error.errors(include_input=False, include_url=False, include_context=False):
        location = ".".join(str(part) for part in item["loc"])
        parts.append(f"{location}: {item['msg']}" if location else item["msg"])
    return "; ".join(parts)


def _read_api_keys_file(path: Path) -> list[ApiKey]:
    try:
        raw = json.loads(path.read_text())
        return _API_KEY_LIST.validate_python(raw)
    except OSError as error:
        raise ValueError(f"API_KEYS_FILE {path}: cannot be read ({error.strerror})") from None
    except json.JSONDecodeError as error:
        raise ValueError(f"API_KEYS_FILE {path}: invalid JSON ({error.msg})") from None
    except ValidationError as error:
        raise ValueError(
            f"API_KEYS_FILE {path}: expected a JSON list of {{name, role, sha256}} objects "
            f"({_describe(error)})"
        ) from None


class Settings(BaseSettings):
    """Service settings. Secrets: ``API_KEYS`` and ``API_KEYS_FILE`` content and
    ``ASR_EXTERNAL_API_KEY`` and ``PII_HASH_KEY``."""

    model_config = SettingsConfigDict(
        case_sensitive=False, extra="ignore", hide_input_in_errors=True
    )

    public_host: str = "0.0.0.0"  # noqa: S104 - bind inside the container
    public_port: int = Field(default=8080, ge=1, le=65535)
    internal_host: str = "0.0.0.0"  # noqa: S104 - bind inside the container
    internal_port: int = Field(default=9090, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"  # any case
    log_format: Literal["json", "console"] = "json"
    auth_mode: Literal["api_key", "none"] = "api_key"
    api_keys: list[ApiKey] = []  # secret; JSON list in env API_KEYS
    api_keys_file: Path | None = None  # JSON file with the same shape, merged with API_KEYS
    valkey_url: str = "redis://localhost:6379/0"  # secret when it carries a password
    # Share of Valkey's maxmemory from which no new bulk is taken on: POST /v1/batch answers 503,
    # slow extractions are not handed over to jobs, the result cache (API and workers) skips.
    valkey_memory_guard_ratio: float = Field(default=0.8, ge=0.1, le=1.0)
    rate_limit_per_minute: int = Field(default=60, ge=1)
    rate_limit_enabled: bool = True
    # Largest request body of the public API, in bytes; POST /v1/batch has its own bound, from
    # BATCH_MAX_URLS. A larger body answers 413 before it is read whole.
    request_max_body_bytes: int = Field(default=65536, ge=1024)
    # Total time budget of one fetch; at least the pipeline's 1 s minimum for a fetch.
    fetch_timeout_seconds: float = Field(default=20, ge=1)
    fetch_max_bytes: int = Field(default=20_000_000, ge=1024)  # response body limit
    fetch_max_redirects: int = Field(default=5, ge=0, le=20)
    # UNSAFE: lifts the SSRF guard for private networks. Development and tests only.
    fetch_allow_private_networks: bool = False
    # Per-domain outbound gate (shared by all pods through Valkey).
    outbound_max_concurrency_per_host: int = Field(default=2, ge=1)  # requests in flight per host
    outbound_min_delay_ms: int = Field(default=1000, ge=0)  # least gap between request starts
    outbound_max_crawl_delay_seconds: float = Field(default=10, ge=0)  # cap for robots Crawl-delay
    outbound_max_wait_seconds: float = Field(default=10, ge=0)  # longest wait for a slot
    outbound_backoff_seconds: float = Field(default=30, ge=1)  # cooldown after 429/503
    outbound_max_backoff_seconds: float = Field(default=300, ge=1)  # cap for any cooldown
    circuit_failure_threshold: int = Field(default=5, ge=1)  # consecutive failures to open
    circuit_open_seconds: float = Field(default=60, ge=1)  # how long the circuit stays open
    # Connection pool of this pod's fetcher, over all hosts.
    outbound_max_connections: int = Field(default=200, ge=1)  # open connections at most
    outbound_max_keepalive: int = Field(default=50, ge=0)  # idle connections kept for reuse
    # Compliance: robots.txt, TDMRep, ai.txt and noai.
    compliance_default: Literal["enforce", "report", "ignore"] = "enforce"  # mode without a request
    compliance_allow_override: bool = True  # may non-admin callers choose another mode?
    compliance_cache_seconds: int = Field(default=3600, ge=0)  # cache of the origin files; 0 = off
    # Quality gate: extracted content with fewer words than this is `partial`/`content_too_short`.
    quality_min_words: int = Field(default=50, ge=0)
    # Result cache (fresh window and storage time) and change-detection fingerprints; 0 = off.
    result_cache_ttl_seconds: int = Field(default=3600, ge=0)  # stored result is served unchecked
    result_store_ttl_seconds: int = Field(default=86400, ge=0)  # kept for revalidation
    fingerprint_ttl_seconds: int = Field(default=2_592_000, ge=0)  # 30 days
    # Time budget of one POST /v1/extract, from compliance checks to the finished result. At
    # least 2 s, so the fetch can get its 1 s minimum after the 0.25 s margin.
    extract_timeout_seconds: float = Field(default=25, ge=2)
    # Seconds between SIGTERM (readiness off) and the start of the graceful shutdown.
    shutdown_drain_seconds: float = Field(default=5, ge=0)
    # Conversion budget: largest HTML/text body converted, and conversions at once per pod.
    convert_max_bytes: int = Field(default=5_242_880, ge=1024)  # 5 MiB
    convert_concurrency: int = Field(default=2, ge=1)
    # Jobs: how long a job and its results stay in Valkey (renewed when it completes), and the
    # largest stored result of one item as JSON; a bigger one is stored without its content.
    job_ttl_seconds: int = Field(default=86400, ge=60)
    job_item_max_bytes: int = Field(default=2_000_000, ge=10_000)
    # Lost leases plus deferrals one job item may have; one more lost lease fails the item.
    job_item_max_attempts: int = Field(default=5, ge=1)
    # Client-domain pairs one queue claim looks at, at most, before it answers "try again soon".
    scheduler_claim_scan_budget: int = Field(default=200, ge=1, le=5000)
    # Job items one worker pod processes at once (conversions are still capped per pod).
    worker_concurrency: int = Field(default=8, ge=1)
    # Batch API: URLs in one batch, and unfinished batches one API key may have at a time.
    batch_max_urls: int = Field(default=1000, ge=1, le=100_000)
    batch_max_open_per_client: int = Field(default=5, ge=1)
    # False: POST /v1/batch and GET /v1/jobs/{id} answer 503 (API pods without workers).
    jobs_enabled: bool = True
    # Extractions one pod keeps running after their request got a 202 (and requests waiting for
    # theirs); above it a request keeps waiting inline.
    extract_max_background: int = Field(default=32, ge=1)
    # Browser mode (Playwright). Off by default: the image of an API pod has no browser.
    browser_enabled: bool = False
    browser_timeout_seconds: float = Field(default=30, ge=5, le=120)  # one page, navigation to end
    # How long a sync request waits for a browser worker to claim its page (B1).
    browser_queue_wait_seconds: float = Field(default=10, ge=0, le=60)
    browser_concurrency: int = Field(default=4, ge=1)  # pages rendered at once per pod
    browser_idle_wait_seconds: float = Field(default=8, ge=0, le=30)  # longest wait for the network
    browser_recycle_after_pages: int = Field(default=200, ge=1)  # then the browser is restarted
    browser_max_bytes: int = Field(default=50_000_000, ge=1_000_000)  # all responses of one page
    # Comma-separated Playwright resource types that are not loaded; empty: none. Not `document`.
    browser_blocked_resources: str = "image,media,font"
    browser_autoconsent: bool = True  # opt out of cookie banners automatically (B11)
    browser_executable_path: Path | None = None  # development only: a local Chromium
    # Unblock mode: a second browser that clears bot challenges. Only for requests whose
    # compliance mode is `ignore` (admins), and only where the operator switches it on.
    browser_unblock_enabled: bool = False
    browser_unblock_channel: Literal["chrome", "chromium"] = "chrome"
    browser_unblock_headless: bool = False
    browser_unblock_timeout_seconds: float = Field(default=60, ge=10, le=180)
    browser_unblock_challenge_wait_seconds: float = Field(default=20, ge=0, le=60)
    # How long the cookies of a cleared challenge are reused for the host; 0: no reuse.
    browser_unblock_cookie_ttl_seconds: int = Field(default=1800, ge=0)
    # Screenshots: largest PNG/JPEG in bytes, tallest capture in pixels, and how long the file
    # (an artifact) stays available.
    screenshot_max_bytes: int = Field(default=10_485_760, ge=1)
    screenshot_max_height_px: int = Field(default=16384, ge=1080, le=32768)
    artifact_ttl_seconds: int = Field(default=3600, ge=60)
    # Documents (PDF, Office, EPUB, CSV, ODF, images). MarkItDown converts in child processes of
    # the pod that holds the bytes; OCR and ODF go to the Docling worker.
    document_max_bytes: int = Field(default=20_000_000, ge=1024)  # at most FETCH_MAX_BYTES
    document_max_pages: int = Field(default=100, ge=1)  # pages after it are cut
    # Largest total of all entries of a ZIP-based document once unpacked (container bombs).
    document_max_uncompressed_bytes: int = Field(default=200_000_000, ge=1024)
    document_concurrency: int = Field(default=2, ge=1)  # conversion child processes per pod
    document_memory_limit_mb: int = Field(default=2048, ge=256)  # address space of one child
    # Docling worker (OCR). Off by default: the API image has neither Docling nor Tesseract.
    docling_enabled: bool = False
    docling_timeout_seconds: float = Field(default=300, ge=30, le=1800)  # one conversion
    # How long a sync request waits for the Docling worker before it answers without OCR.
    docling_sync_wait_seconds: float = Field(default=20, ge=0, le=25)
    docling_input_ttl_seconds: int = Field(default=3600, ge=600)  # a queued document's bytes
    # Bytes of all jobs' inputs at once in Valkey (queued documents, slow uploads); at least
    # DOCUMENT_MAX_BYTES.
    docling_input_budget_bytes: int = Field(default=200_000_000, ge=1024)
    docling_max_open_per_client: int = Field(default=5, ge=1)  # unfinished Docling jobs per key
    docling_concurrency: int = Field(default=1, ge=1)  # conversions at once per Docling worker
    docling_threads: int = Field(default=4, ge=1)  # CPU threads of one conversion
    docling_max_pages: int = Field(default=100, ge=1)  # pages after it are cut
    docling_memory_limit_mb: int = Field(default=6144, ge=256)  # address space of one child
    # Tesseract language codes, comma-separated (the traineddata files must be on the volume).
    docling_ocr_languages: str = Field(default="deu,eng", pattern=r"^[a-z_]{3,}(,[a-z_]{3,})*$")
    # Volume with the models: Docling's in docling/, Tesseract's data in tessdata/.
    docling_models_path: Path = Path("/models")
    # Audio and video: subtitles of platform videos and HTML5 tracks, in child processes of the pod
    # that holds the request. Speech recognition goes to the ASR worker.
    media_enabled: bool = True
    media_concurrency: int = Field(default=2, ge=1)  # media child processes per pod
    media_memory_limit_mb: int = Field(default=1024, ge=256)  # address space of one child
    # Bytes all requests of one media call may pull through the egress proxy (player APIs,
    # caption files), and the largest subtitle track a child hands back.
    media_extract_max_bytes: int = Field(default=30_000_000, ge=1024)
    media_subtitle_max_bytes: int = Field(default=5_000_000, ge=1024)
    media_max_tracks: int = Field(default=3, ge=0, le=10)  # HTML5 tracks added to one page
    media_max_duration_seconds: int = Field(default=7200, ge=60)  # longer is media_too_long (D6)
    media_max_bytes: int = Field(default=500_000_000, ge=1_000_000)  # a download for ASR
    # Largest uploaded audio or video file; its bytes travel through Valkey, so with ASR_ENABLED
    # at most DOCLING_INPUT_BUDGET_BYTES.
    media_upload_max_bytes: int = Field(default=25_000_000, ge=1_000_000)
    media_download_timeout_seconds: int = Field(default=600, ge=30, le=3600)  # one download
    # ASR worker (speech recognition). Off by default: the API image has no models.
    asr_enabled: bool = False
    asr_backend: Literal["local", "openai"] = "local"
    asr_model: Literal["small", "large-v3-turbo"] = "large-v3-turbo"  # the model manifest's names
    asr_models_path: Path = Path("/models")  # volume with the Whisper models, one folder each
    asr_compute_type: str = Field(default="int8", min_length=1)  # CTranslate2 compute type
    asr_threads: int = Field(default=4, ge=1)  # CPU threads of one transcription
    asr_concurrency: int = Field(default=1, ge=1)  # transcriptions at once per ASR worker
    asr_memory_limit_mb: int = Field(default=4096, ge=256)  # address space of one child
    asr_timeout_seconds: int = Field(default=10_800, ge=60, le=14_400)  # one whole item (D6)
    asr_input_ttl_seconds: int = Field(default=28_800, ge=600)  # an ASR item's input (D6)
    asr_max_open_per_client: int = Field(default=3, ge=1)  # unfinished ASR jobs per key
    asr_scratch_path: Path = Path("/tmp/asr")  # noqa: S108 - an emptyDir for downloaded media
    # External backend (``ASR_BACKEND=openai``): an OpenAI-compatible transcription endpoint.
    asr_external_base_url: str | None = None  # https only
    asr_external_model: str = Field(default="whisper-1", min_length=1)
    asr_external_api_key: str | None = Field(default=None, repr=False)  # secret
    asr_external_timeout_seconds: float = Field(default=300, ge=1)  # per audio chunk
    # PII worker (personal data in results). Off by default: the API image has neither Presidio
    # nor spaCy nor models.
    pii_enabled: bool = False
    # Entity types handled when a request names none, comma-separated in the env var.
    pii_default_entities: Annotated[list[EntityType], NoDecode] = [
        "PERSON",
        "EMAIL_ADDRESS",
        "PHONE_NUMBER",
        "IBAN_CODE",
        "CREDIT_CARD",
        "IP_ADDRESS",
    ]
    pii_score_threshold: float = Field(default=0.4, ge=0, le=1)  # least confidence of an entity
    # The model for names in a language without one of its own (patterns work in any language).
    pii_fallback_language: Literal["de", "en"] = "en"
    # How long a sync request that needs PII waits for the NLP worker before it becomes a job.
    pii_sync_wait_seconds: float = Field(default=20, ge=1, le=60)
    pii_timeout_seconds: int = Field(default=300, ge=10, le=3600)  # one item
    pii_max_chars: int = Field(default=2_000_000, ge=10_000)  # text of all fields of one result
    pii_max_entities: int = Field(default=1000, ge=1, le=100_000)  # items of ``annotate``
    pii_concurrency: int = Field(default=1, ge=1)  # items at once per NLP worker
    pii_memory_limit_mb: int = Field(default=3072, ge=512)  # address space of one child
    pii_models_path: Path = Path("/models")  # volume with the spaCy models, one folder each
    pii_model_size: Literal["lg", "md"] = "lg"  # size of the spaCy models
    # Key of ``hash``: the same value gets the same hash in every result. At least 32 bytes.
    pii_hash_key: SecretStr | None = None  # secret
    # LLM (structured output with an LLM, semantic chunking): an OpenAI-compatible endpoint that
    # is the operator's, not a caller's target (no SSRF guard). Off by default.
    llm_enabled: bool = False
    llm_base_url: str = "https://api.openai.com/v1"  # https unless llm_allow_http
    llm_allow_http: bool = False  # local development and tests
    llm_model: str = Field(default="gpt-6-luna", min_length=1)
    llm_api_key: SecretStr | None = None  # secret; a request's X-LLM-Api-Key wins over it
    llm_embedding_model: str | None = None  # without it `semantic` chunking answers 400
    llm_temperature: float = Field(default=0, ge=0, le=2)
    llm_max_tokens: int = Field(default=4096, ge=1)  # most tokens of one answer
    llm_timeout_seconds: float = Field(default=120, ge=5, le=600)  # one request to the endpoint
    llm_max_input_tokens: int = Field(default=32_000, ge=1000, le=1_000_000)  # 4 characters each
    llm_concurrency: int = Field(default=4, ge=1)  # LLM items at once per llm worker
    # After SIGTERM the llm worker's items in flight get this long, then they go back to the
    # queue and their LLM calls run again elsewhere (issue #19).
    llm_graceful_seconds: int = Field(default=30, ge=10, le=900)
    # How long a sync request that needs the LLM waits for the llm worker before it becomes a job.
    llm_sync_wait_seconds: float = Field(default=30, ge=1, le=60)
    selector_learn_ttl_seconds: int = Field(default=2_592_000, ge=60)  # learned selectors, 30 days
    structured_regex_timeout_seconds: float = Field(default=0.5, ge=0.05, le=5)  # one pattern
    chunk_max_chunks: int = Field(default=2000, ge=1)  # most chunks of one result
    # Selector rules (caller's, learned, proposed) run in child processes that are killed at a
    # deadline (ruling R19): one run of all rules of a result, its children per process and the
    # address space of one child.
    structured_selector_timeout_seconds: float = Field(default=5, ge=0.1, le=60)
    structured_selector_processes: int = Field(default=1, ge=1)
    structured_selector_memory_mb: int = Field(default=512, ge=256)
    # The post stage's CPU work (patterns, chunking, learning checks) at once per process, on a
    # thread pool of its own (ruling R21).
    post_concurrency: int = Field(default=2, ge=1)

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_case_log_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("browser_blocked_resources")
    @classmethod
    def _known_resource_types(cls, value: str) -> str:
        """Lower-case, no blanks, no repeats; the offending entry is not echoed."""
        types = list(
            dict.fromkeys(item.strip().lower() for item in value.split(",") if item.strip())
        )
        if "document" in types:
            raise ValueError("the resource type document cannot be blocked")
        if any(item not in _RESOURCE_TYPES for item in types):
            raise ValueError(f"unknown resource type; known: {', '.join(_RESOURCE_TYPES)}")
        return ",".join(types)

    @field_validator("asr_external_base_url")
    @classmethod
    def _external_asr_is_https(cls, value: str | None) -> str | None:
        """Empty means unset (the Helm chart renders an unset value so); the URL is not echoed,
        it may carry credentials."""
        if value is None or not value.strip():
            return None
        parts = urlsplit(value.strip())
        if parts.scheme.lower() != "https" or not parts.hostname:
            raise ValueError("the external ASR endpoint must be an https URL")
        return value.strip()

    @field_validator("asr_external_api_key")
    @classmethod
    def _empty_key_is_unset(cls, value: str | None) -> str | None:
        """Empty means unset, as for the endpoint: no key is sent, and a request without the
        caller's key is refused (``extract_api.check_asr_key``)."""
        return value if value is not None and value.strip() else None

    @field_validator("llm_api_key", "llm_embedding_model", mode="before")
    @classmethod
    def _empty_llm_value_is_unset(cls, value: object) -> object:
        """Empty means unset (the Helm chart renders an unset value so): no key is sent by us,
        and `semantic` chunking is refused without an embedding model."""
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("pii_default_entities", mode="before")
    @classmethod
    def _entity_list(cls, value: object) -> object:
        """A comma-separated list (``PERSON,EMAIL_ADDRESS``), in any case, as the env var has
        it; a list from code passes."""
        if isinstance(value, str):
            return [item.strip().upper() for item in value.split(",")]
        return value

    @field_validator("pii_default_entities")
    @classmethod
    def _entities_not_empty_or_repeated(cls, value: list[EntityType]) -> list[EntityType]:
        if not value or len(set(value)) != len(value):
            raise ValueError(f"expected one or more of {', '.join(ENTITY_TYPES)}, each once")
        return value

    @field_validator("pii_hash_key", mode="before")
    @classmethod
    def _empty_hash_key_is_unset(cls, value: object) -> object:
        """Empty means unset (the Helm chart renders an unset secret so)."""
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("pii_hash_key")
    @classmethod
    def _hash_key_is_long_enough(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value().encode()) < 32:
            raise ValueError("the hash key must be at least 32 bytes")
        return value

    @model_validator(mode="after")
    def _document_limit_within_fetch_limit(self) -> Self:
        if self.document_max_bytes > self.fetch_max_bytes:
            raise ValueError("DOCUMENT_MAX_BYTES must not exceed FETCH_MAX_BYTES")
        if self.document_max_bytes > self.docling_input_budget_bytes:
            raise ValueError("DOCUMENT_MAX_BYTES must not exceed DOCLING_INPUT_BUDGET_BYTES")
        # Uploaded media only travel through the input budget when the service transcribes.
        if self.asr_enabled and self.media_upload_max_bytes > self.docling_input_budget_bytes:
            raise ValueError("MEDIA_UPLOAD_MAX_BYTES must not exceed DOCLING_INPUT_BUDGET_BYTES")
        # An ASR item's lease (2 x ASR_TIMEOUT_SECONDS + 10, ruling M10) may run out once and
        # the item run again: its input must still be there.
        if self.asr_enabled and self.asr_input_ttl_seconds <= 2 * self.asr_timeout_seconds + 10:
            raise ValueError(
                "ASR_INPUT_TTL_SECONDS must be longer than the ASR lease "
                "(2 x ASR_TIMEOUT_SECONDS + 10)"
            )
        # The PII hop is a job of pool nlp (N15): without jobs nothing could be processed.
        if self.pii_enabled and not self.jobs_enabled:
            raise ValueError("PII_ENABLED needs JOBS_ENABLED=true")
        # The LLM hop is a job of pool llm (D5): without jobs nothing could be processed.
        if self.llm_enabled and not self.jobs_enabled:
            raise ValueError("LLM_ENABLED needs JOBS_ENABLED=true")
        return self

    @model_validator(mode="after")
    def _no_openai_environment_with_llm(self) -> Self:
        """Issue #19: the names of the refused variables, never their values."""
        if self.llm_enabled:
            found = [name for name in OPENAI_ENV_REFUSED if os.environ.get(name, "").strip()]
            if found:
                raise ValueError(
                    f"{' and '.join(found)} must not be set while LLM_ENABLED is true "
                    "(the OpenAI SDK would read them)"
                )
        return self

    @model_validator(mode="after")
    def _llm_base_url_is_https(self) -> Self:
        """https only, unless LLM_ALLOW_HTTP (a local model server, tests). The URL is not
        echoed: it may carry credentials."""
        parts = urlsplit(self.llm_base_url.strip())
        schemes = {"https", "http"} if self.llm_allow_http else {"https"}
        if parts.scheme.lower() not in schemes or not parts.hostname:
            raise ValueError(
                "LLM_BASE_URL must be an https URL (LLM_ALLOW_HTTP=true also allows http)"
            )
        self.llm_base_url = self.llm_base_url.strip()
        return self

    @model_validator(mode="after")
    def _merge_and_validate_api_keys(self) -> Self:
        keys = list(self.api_keys)
        if self.api_keys_file is not None:
            keys.extend(_read_api_keys_file(self.api_keys_file))

        names: set[str] = set()
        hashes: dict[str, str] = {}  # sha256 -> name of the first key that used it
        for key in keys:
            if key.name in names:
                raise ValueError(f"duplicate API key name: {key.name}")
            names.add(key.name)
            if key.sha256 in hashes:
                # Names only: the hash identifies the secret and stays out of messages.
                raise ValueError(
                    f"API keys {hashes[key.sha256]} and {key.name} share the same sha256"
                )
            hashes[key.sha256] = key.name

        if self.auth_mode == "api_key" and not keys:
            raise ValueError("AUTH_MODE=api_key requires API_KEYS or API_KEYS_FILE")

        self.api_keys = keys
        return self


def llm_available(settings: Settings) -> bool:
    """Whether the service can do LLM work: it is switched on and has the jobs it runs as."""
    return settings.llm_enabled and settings.jobs_enabled


def load_settings() -> Settings:
    """Build ``Settings`` from the environment; any problem becomes a ``ConfigError``."""
    try:
        return Settings()
    except SettingsError:
        # The source error text can quote the raw value, so only the hint is reported.
        raise ConfigError(f"Invalid configuration: {_API_KEYS_HINT}") from None
    except ValidationError as error:
        hint = f" ({_API_KEYS_HINT})" if _mentions_api_keys(error) else ""
        raise ConfigError(f"Invalid configuration: {_describe(error)}{hint}") from None
    except OSError as error:
        raise ConfigError(f"Invalid configuration: cannot read file ({error.strerror})") from None


def _mentions_api_keys(error: ValidationError) -> bool:
    return any(item["loc"][:1] == ("api_keys",) for item in error.errors(include_input=False))


@lru_cache
def get_settings() -> Settings:
    return load_settings()
