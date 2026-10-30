"""Request checks of ``describe_images`` (ruling V-S2), called next to
``check_structured_request`` by the extract, file and batch routes, before anything is fetched
or a job is made:

* 400 ``invalid_request`` while the service has no LLM, and without any key (neither
  ``X-LLM-Api-Key`` nor ``LLM_API_KEY``), with the messages of ``structured``;
* 422 with ``pii`` (decision V3 a): the images and their alt texts would reach the LLM unmasked;
* 422 with ``metadata: false``: the descriptions live on ``metadata.images``.
"""

from typing import Final

from text_extraction.envelope import ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.pii.types import PiiOptions
from text_extraction.settings import Settings, llm_available
from text_extraction.structured.checks import LLM_DISABLED_MESSAGE, LLM_KEY_MISSING_MESSAGE
from text_extraction.vision.types import DescribeImagesOptions

__all__ = ["DESCRIBE_METADATA_MESSAGE", "DESCRIBE_PII_MESSAGE", "check_describe_request"]

DESCRIBE_PII_MESSAGE: Final = (
    "describe_images sends images to the LLM and cannot be combined with pii."
)
DESCRIBE_METADATA_MESSAGE: Final = "describe_images needs metadata: true."


def check_describe_request(
    describe: DescribeImagesOptions | None,
    pii: PiiOptions | None,
    metadata: bool,
    settings: Settings,
    *,
    llm_key: str | None,
) -> None:
    """The request checks of ``describe_images`` (the module docstring). ``llm_key`` is the
    caller's ``X-LLM-Api-Key``; ``metadata`` the request's option of that name."""
    if describe is None:
        return
    if not llm_available(settings):
        raise ServiceError(ErrorCode.INVALID_REQUEST, LLM_DISABLED_MESSAGE, 400)
    if llm_key is None and settings.llm_api_key is None:
        raise ServiceError(ErrorCode.INVALID_REQUEST, LLM_KEY_MISSING_MESSAGE, 400)
    if pii is not None:
        raise ServiceError(ErrorCode.INVALID_REQUEST, DESCRIBE_PII_MESSAGE, 422)
    if not metadata:
        raise ServiceError(ErrorCode.INVALID_REQUEST, DESCRIBE_METADATA_MESSAGE, 422)
