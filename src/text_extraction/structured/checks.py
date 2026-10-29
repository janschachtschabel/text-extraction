"""Request checks of ``structured`` that need more than the options themselves (ruling R4).

The options are validated when the request is parsed (``structured/types.py``); what depends on
the caller is checked where the request is handled, by the extract, file and batch routes:

* An XPath rule needs an admin key: XPath 1.0 has no cost limit lxml could enforce, and an
  expression such as ``//*//*//*`` runs for minutes on a large page (rule 5: no evaluation of
  caller code unless the caller is admin). Other keys use CSS.
* A CSS rule of a non-admin key may use ``:has(`` at most once, which bounds the nesting of
  relational selectors. A selector the LLM proposes for ``learn_selectors`` keeps to the same
  limit (``css_within_limits``), since a learned rule later runs for that key.

The messages never repeat a selector.

``check_structured_request`` (ruling S12) checks what depends on the service and on the
combination of options, for every route that takes ``structured`` or ``chunking``: 400 for LLM
work while the service has no LLM, for ``semantic`` chunking without ``LLM_EMBEDDING_MODEL`` and
for LLM work without any key (neither ``X-LLM-Api-Key`` nor ``LLM_API_KEY``); 422 for chunking
with ``format: html``, for ``pii`` with what reads the raw page (strategies ``metadata`` and
``selectors``, ``tables``, ``learn_selectors``, caller ``selectors``; ruling D3 a) and for
``learn_selectors`` with a strategy that never calls the LLM. ``wants_llm`` says whether a request
may need the LLM, for the default wait and the key check. ``llm_key_of`` reads the caller's key
from the ``X-LLM-Api-Key`` header as ``asr_dispatch.asr_key_of`` reads ``X-ASR-Api-Key``.
"""

import re
from typing import Final

from text_extraction.auth import Principal
from text_extraction.chunking.types import ChunkingOptions
from text_extraction.convert.html import OutputFormat
from text_extraction.envelope import ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.pii.types import PiiOptions
from text_extraction.settings import Settings, llm_available
from text_extraction.structured.types import StructuredOptions

XPATH_NEEDS_ADMIN_MESSAGE = "XPath selectors need an admin key; use CSS."
HAS_LIMIT_MESSAGE = "A CSS selector may use :has() at most once without an admin key."
MAX_HAS = 1
_HAS = re.compile(r":has\s*\(", re.IGNORECASE)

LLM_KEY_HEADER: Final = "X-LLM-Api-Key"
_LLM_KEY: Final = re.compile(r"[\x21-\x7e]{1,1024}", re.ASCII)
_LLM_KEY_BAD_MESSAGE: Final = (
    f"The {LLM_KEY_HEADER} header must be 1 to 1024 visible ASCII characters."
)
LLM_DISABLED_MESSAGE: Final = "LLM features are not enabled on this service."
SEMANTIC_MODEL_MESSAGE: Final = "Semantic chunking needs LLM_EMBEDDING_MODEL."
LLM_KEY_MISSING_MESSAGE: Final = f"No LLM key: send {LLM_KEY_HEADER}."
CHUNKING_FORMAT_MESSAGE: Final = "Chunking needs format markdown or text."
LEARN_STRATEGY_MESSAGE: Final = "learn_selectors needs strategy llm or auto."
_PII_RAW_MESSAGE: Final = "{what} reads the raw page and cannot be combined with pii."


def css_within_limits(css: str) -> bool:
    """Whether a CSS selector keeps to the limits of a non-admin key: ``:has(`` at most
    ``MAX_HAS`` times."""
    return len(_HAS.findall(css)) <= MAX_HAS


def check_selector_access(structured: StructuredOptions | None, principal: Principal) -> None:
    """422 ``invalid_request`` for a non-admin key's XPath rule or a CSS rule with more than
    one ``:has(``."""
    if structured is None or principal.role == "admin":
        return
    for rule in structured.selectors.values():
        if rule.xpath is not None:
            raise ServiceError(ErrorCode.INVALID_REQUEST, XPATH_NEEDS_ADMIN_MESSAGE, 422)
        if rule.css is not None and not css_within_limits(rule.css):
            raise ServiceError(ErrorCode.INVALID_REQUEST, HAS_LIMIT_MESSAGE, 422)


def llm_key_of(value: str | None) -> str | None:
    """The caller's key from ``X-LLM-Api-Key``, None without the header; 400 for a value that is
    not 1 to 1024 visible ASCII characters (the answer never repeats it)."""
    if value is None:
        return None
    if _LLM_KEY.fullmatch(value) is None:
        raise ServiceError(ErrorCode.INVALID_REQUEST, _LLM_KEY_BAD_MESSAGE, 400)
    return value


def _calls_llm(structured: StructuredOptions | None) -> bool:
    """Whether the structured options may call the LLM: a schema with strategy ``llm`` or
    ``auto``. ``learn_selectors`` only comes with those two (422 otherwise), so it counts as the
    strategy it comes with (ruling R8). Tables alone never do."""
    return (
        structured is not None
        and structured.schema is not None
        and structured.strategy in ("llm", "auto")
    )


def wants_llm(
    structured: StructuredOptions | None, chunking: ChunkingOptions | None, settings: Settings
) -> bool:
    """Whether the request may need the LLM: the service has one and the options use it
    (``llm`` or ``auto`` with a schema, ``semantic`` chunking). ``auto`` may end up without an
    LLM call, but the request waits for the llm worker as long as it might need it."""
    if not llm_available(settings):
        return False
    return _calls_llm(structured) or (chunking is not None and chunking.strategy == "semantic")


def check_structured_request(
    structured: StructuredOptions | None,
    chunking: ChunkingOptions | None,
    pii: PiiOptions | None,
    req_format: OutputFormat,
    settings: Settings,
    *,
    llm_key: str | None,
) -> None:
    """The request checks of ``structured`` and ``chunking`` (ruling S12; the module docstring).
    ``llm_key`` is the caller's ``X-LLM-Api-Key``. The messages never repeat an option's value."""
    if structured is None and chunking is None:
        return
    semantic = chunking is not None and chunking.strategy == "semantic"
    llm_feature = semantic or (
        structured is not None and (structured.strategy == "llm" or structured.learn_selectors)
    )
    if llm_feature and not llm_available(settings):
        raise ServiceError(ErrorCode.INVALID_REQUEST, LLM_DISABLED_MESSAGE, 400)
    if semantic and settings.llm_embedding_model is None:
        raise ServiceError(ErrorCode.INVALID_REQUEST, SEMANTIC_MODEL_MESSAGE, 400)
    if (
        wants_llm(structured, chunking, settings)
        and llm_key is None
        and settings.llm_api_key is None
    ):
        raise ServiceError(ErrorCode.INVALID_REQUEST, LLM_KEY_MISSING_MESSAGE, 400)
    if chunking is not None and req_format == "html":
        raise ServiceError(ErrorCode.INVALID_REQUEST, CHUNKING_FORMAT_MESSAGE, 422)
    if (
        structured is not None
        and structured.learn_selectors
        and structured.strategy not in ("llm", "auto")
    ):
        raise ServiceError(ErrorCode.INVALID_REQUEST, LEARN_STRATEGY_MESSAGE, 422)
    if pii is not None and structured is not None:
        _check_pii(structured)


def _check_pii(structured: StructuredOptions) -> None:
    """422 for ``pii`` with a strategy or an option that reads the raw page (ruling D3 a): the
    masking runs on the extracted content, not on the HTML."""
    if structured.strategy in ("metadata", "selectors"):
        what = f"Strategy {structured.strategy}"
    elif structured.tables:
        what = "tables"
    elif structured.learn_selectors:
        what = "learn_selectors"
    elif structured.selectors:
        what = "selectors"
    else:
        return
    raise ServiceError(ErrorCode.INVALID_REQUEST, _PII_RAW_MESSAGE.format(what=what), 422)
