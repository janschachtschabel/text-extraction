"""Which optional features a key may use (package 19, decision P19-D1 b).

``OCR_ALLOW_USERS``, ``PII_ALLOW_USERS``, ``LLM_ALLOW_USERS`` and ``ASR_ALLOW_USERS`` (default
true) keep a feature for admin keys and refuse it to user keys, after the pattern of
``COMPLIANCE_ALLOW_OVERRIDE``. A feature that is switched off (``*_ENABLED``) is off for every
key; its own checks answer 400 for it, so ``check_feature_access`` leaves it to them.

What a user key may not use (ruling P19-R4):

- **Asked for explicitly:** 403 ``forbidden`` before anything runs: ``ocr: always``, ``pii``,
  ``transcribe: true``, an audio or video upload, strategy ``llm``, ``learn_selectors``,
  ``describe_images`` and ``semantic`` chunking.
- **Used implicitly:** the request runs as if the feature were off. ``ocr: auto`` becomes
  ``never`` (``restricted_ocr``: the text layer, no OCR), and strategy ``auto`` asks no LLM for
  missing fields (``settings_for``: the post stage runs with ``LLM_ENABLED`` off for the key and
  answers the warning ``llm_unavailable``). Jobs keep the owner's role, so their workers apply
  the same rule.
"""

from typing import Final, Literal

from text_extraction.auth import Principal, Role
from text_extraction.chunking.types import ChunkingOptions
from text_extraction.documents.types import OcrOption
from text_extraction.envelope import ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.pii.types import PiiOptions
from text_extraction.settings import Settings
from text_extraction.structured.types import StructuredOptions
from text_extraction.vision.types import DescribeImagesOptions

__all__ = [
    "Feature",
    "allowed",
    "check_feature_access",
    "forbidden_message",
    "restricted_ocr",
    "settings_for",
]

Feature = Literal["ocr", "pii", "llm", "asr"]

_NAMES: Final[dict[Feature, str]] = {
    "ocr": "OCR",
    "pii": "PII handling",
    "llm": "The LLM",
    "asr": "Transcription",
}
# The switch that turns each feature on, for every key.
_ENABLED: Final[dict[Feature, str]] = {
    "ocr": "docling_enabled",
    "pii": "pii_enabled",
    "llm": "llm_enabled",
    "asr": "asr_enabled",
}


def forbidden_message(feature: Feature) -> str:
    """The 403 text for a user key that asked for ``feature``."""
    return f"{_NAMES[feature]} is for admin keys only on this service."


def _available(feature: Feature, settings: Settings) -> bool:
    """Whether the service has the feature at all: its switch and the jobs it runs as."""
    return bool(getattr(settings, _ENABLED[feature])) and settings.jobs_enabled


def allowed(feature: Feature, role: str | None, settings: Settings) -> bool:
    """Whether a key of ``role`` may use ``feature`` (if the service has it): admins always,
    users while the feature's ``*_ALLOW_USERS`` is on. No role (no key) counts as a user."""
    if role == "admin":
        return True
    return bool(getattr(settings, f"{feature}_allow_users"))


def _role(principal: Principal | None) -> Role | None:
    return principal.role if principal is not None else None


def settings_for(settings: Settings, principal: Principal | None) -> Settings:
    """``settings`` as ``principal`` sees them: the features it may not use switched off. The
    same object when it may use them all."""
    off = {
        _ENABLED[feature]: False
        for feature in _ENABLED
        if _available(feature, settings) and not allowed(feature, _role(principal), settings)
    }
    return settings.model_copy(update=off) if off else settings


def restricted_ocr(ocr: OcrOption, principal: Principal, settings: Settings) -> OcrOption:
    """``ocr`` for ``principal``: ``auto`` is ``never`` for a key that may not use OCR."""
    if (
        ocr == "auto"
        and _available("ocr", settings)
        and not allowed("ocr", principal.role, settings)
    ):
        return "never"
    return ocr


def _asks_llm(
    structured: StructuredOptions | None,
    chunking: ChunkingOptions | None,
    describe: DescribeImagesOptions | None,
) -> bool:
    """Whether the options ask for the LLM by name: strategy ``llm``, ``learn_selectors``,
    ``semantic`` chunking or ``describe_images``. ``auto`` does not: it runs without."""
    if describe is not None:
        return True
    if chunking is not None and chunking.strategy == "semantic":
        return True
    return structured is not None and (structured.strategy == "llm" or structured.learn_selectors)


def check_feature_access(
    principal: Principal,
    settings: Settings,
    *,
    ocr: OcrOption = "auto",
    pii: PiiOptions | None = None,
    transcribe: bool = False,
    media_upload: bool = False,
    structured: StructuredOptions | None = None,
    chunking: ChunkingOptions | None = None,
    describe: DescribeImagesOptions | None = None,
) -> None:
    """403 ``forbidden`` when a user key asks explicitly for a feature the service keeps for
    admin keys (the module docstring). ``media_upload``: the upload is audio or video, which is
    always transcribed. A feature the service does not have is left to its own 400."""
    asked: list[tuple[Feature, bool]] = [
        ("ocr", ocr == "always"),
        ("pii", pii is not None),
        ("asr", transcribe or media_upload),
        ("llm", _asks_llm(structured, chunking, describe)),
    ]
    for feature, wanted in asked:
        if (
            wanted
            and _available(feature, settings)
            and not allowed(feature, principal.role, settings)
        ):
            raise ServiceError(ErrorCode.FORBIDDEN, forbidden_message(feature), 403)
