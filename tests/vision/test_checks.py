"""The request checks of ``describe_images`` (ruling V-S2)."""

from typing import Any

import pytest

from text_extraction.errors import ServiceError
from text_extraction.pii.types import PiiOptions
from text_extraction.settings import Settings
from text_extraction.structured.checks import (
    LLM_DISABLED_MESSAGE,
    LLM_KEY_MISSING_MESSAGE,
    wants_llm,
)
from text_extraction.vision.checks import (
    DESCRIBE_METADATA_MESSAGE,
    DESCRIBE_PII_MESSAGE,
    check_describe_request,
)
from text_extraction.vision.types import DescribeImagesOptions

DESCRIBE = DescribeImagesOptions()
PII = PiiOptions(mode="mask")


def no_llm() -> Settings:
    return Settings(auth_mode="none")


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "auth_mode": "none",
        "llm_enabled": True,
        "llm_api_key": "sk-operator-key",
    }
    values.update(overrides)
    return Settings(**values)


def refused(status: int, message: str, **kwargs: Any) -> None:
    options: dict[str, Any] = {
        "describe": DESCRIBE,
        "pii": None,
        "metadata": True,
        "settings": settings(),
        "llm_key": None,
    }
    options.update(kwargs)
    with pytest.raises(ServiceError) as caught:
        check_describe_request(
            options["describe"],
            options["pii"],
            options["metadata"],
            options["settings"],
            llm_key=options["llm_key"],
        )
    assert caught.value.http_status == status
    assert caught.value.code == "invalid_request"
    assert caught.value.message == message


def test_messages() -> None:
    assert DESCRIBE_PII_MESSAGE == (
        "describe_images sends images to the LLM and cannot be combined with pii."
    )
    assert DESCRIBE_METADATA_MESSAGE == "describe_images needs metadata: true."


def test_without_the_option_nothing_is_checked() -> None:
    # Not even a service without an LLM refuses a request without describe_images.
    check_describe_request(None, PII, False, no_llm(), llm_key=None)


def test_accepted_with_the_env_key_or_the_header_key() -> None:
    check_describe_request(DESCRIBE, None, True, settings(), llm_key=None)
    check_describe_request(DESCRIBE, None, True, settings(llm_api_key=None), llm_key="sk-caller")


def test_400_without_llm() -> None:
    refused(400, LLM_DISABLED_MESSAGE, settings=no_llm(), llm_key="sk-caller")


def test_400_without_any_key() -> None:
    refused(400, LLM_KEY_MISSING_MESSAGE, settings=settings(llm_api_key=None))


def test_422_with_pii() -> None:
    refused(422, DESCRIBE_PII_MESSAGE, pii=PII)


def test_400_checks_come_before_the_422_checks() -> None:
    """V-S2's order (Task 2 review m4): a request that also has ``pii`` and ``metadata: false``
    gets the 400 of a missing LLM or key first."""
    refused(
        400,
        LLM_KEY_MISSING_MESSAGE,
        settings=settings(llm_api_key=None),
        pii=PII,
        metadata=False,
    )
    refused(400, LLM_DISABLED_MESSAGE, settings=no_llm(), pii=PII, metadata=False)
    refused(422, DESCRIBE_PII_MESSAGE, pii=PII, metadata=False)


def test_422_with_metadata_false() -> None:
    refused(422, DESCRIBE_METADATA_MESSAGE, metadata=False)


def test_wants_llm_for_describe_images() -> None:
    assert wants_llm(None, None, settings(), describe=DESCRIBE) is True
    assert wants_llm(None, None, settings()) is False
    assert wants_llm(None, None, settings(), describe=None) is False
    # No LLM on the service: nothing to wait for (the request is refused anyway).
    assert wants_llm(None, None, no_llm(), describe=DESCRIBE) is False
