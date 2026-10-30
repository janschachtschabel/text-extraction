"""``POST /v1/structured`` and ``POST /v1/chunk`` (ruling S15): the extraction routes for
structured output and for chunking.

``POST /v1/structured`` is ``POST /v1/extract`` with ``structured`` required: the same flow
(``extract_api.respond``), the same checks, the same answers (sync, the P1 job fallback with
``wait_seconds``, 202 for the browser, nlp and llm jobs) and the same headers (``X-ASR-Api-Key``,
``X-LLM-Api-Key``).

``POST /v1/chunk`` takes exactly one of

* ``url``: ``POST /v1/extract`` with ``chunking`` required (and the options of that route), or
* ``text``: the text is the bytes of an upload of ``text/markdown`` or ``text/plain`` (file name
  ``text.md`` or ``text.txt``; ``text_type``), converted by ``Pipeline.extract_upload`` through
  the upload flow of ``POST /v1/extract/file`` (``file_api.answer_upload``): ``mode_used:
  upload``, no fetch, no cache, a job of kind ``file`` when it is handed over. A ``plain`` text
  is ``format: text``, so that its chunks have no headings. A text over ``DOCUMENT_MAX_BYTES`` is
  413 as a body over the limit would be (a text is never media; ruling R14), and a short text is
  fine: the word-count gate (``QUALITY_MIN_WORDS``) does not apply to it (R15). The route
  authenticates and rate-limits before it reads the body (R16, ``file_api.AuthFirstRoute``), and
  its body limit is the upload's file limit + 64 KiB (``file_api.text_body_limit``), not the
  small limit of the other JSON routes.
"""

from typing import Annotated, Any, Final

from fastapi import APIRouter, Body, Depends, Header, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import State

from text_extraction.asr_dispatch import ASR_KEY_HEADER, asr_key_of
from text_extraction.auth import Principal, require_role
from text_extraction.envelope import Envelope
from text_extraction.errors import BodyTooLarge
from text_extraction.extract_api import ASR_KEY_DESCRIPTION, LLM_KEY_DESCRIPTION, respond
from text_extraction.extract_models import ChunkRequest, ExtractResult, StructuredRequest
from text_extraction.file_api import AuthFirstRoute, answer_upload, check_upload_request
from text_extraction.file_models import FileForm
from text_extraction.job_models import JobAccepted
from text_extraction.ratelimit import rate_limited
from text_extraction.settings import Settings
from text_extraction.structured.checks import LLM_KEY_HEADER, llm_key_of

STRUCTURED_PATH: Final = "/v1/structured"
CHUNK_PATH: Final = "/v1/chunk"

_PRODUCT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "price": {"type": "number"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
}
_STRUCTURED_EXAMPLES: Final[dict[str, Any]] = {
    "auto": {
        "summary": "Fields of a JSON schema, found by the cheapest strategy that works",
        "description": "`auto` takes your selectors first (else the ones learned for your key "
        "and the site), then the page's metadata, then your patterns, and asks the LLM only for "
        "the fields still missing.",
        "value": {
            "url": "https://shop.example.org/item/1",
            "structured": {"schema": _PRODUCT_SCHEMA},
        },
    },
    "selectors": {
        "summary": "Your own CSS selectors and patterns, no LLM",
        "value": {
            "url": "https://shop.example.org/item/1",
            "structured": {
                "schema": _PRODUCT_SCHEMA,
                "strategy": "selectors",
                "selectors": {
                    "name": {"css": "h1"},
                    "price": {"css": "[itemprop=price]", "attr": "content"},
                    "tags": {"css": ".tags a", "all": True},
                },
            },
        },
    },
    "tables": {
        "summary": "The page's tables",
        "value": {
            "url": "https://stats.example.org/report",
            "structured": {"tables": True},
            "wait_seconds": 10,
        },
    },
}
_CHUNK_EXAMPLES: Final[dict[str, Any]] = {
    "url": {
        "summary": "Chunk a page",
        "value": {
            "url": "https://docs.example.org/guide",
            "chunking": {"strategy": "markdown", "size": 800, "overlap": 80},
        },
    },
    "text": {
        "summary": "Chunk Markdown you already have",
        "value": {
            "text": "# Guide\n\nFirst section.\n\n## Install\n\nSecond section.\n",
            "text_type": "markdown",
            "chunking": {"strategy": "recursive", "size": 500},
        },
    },
    "semantic": {
        "summary": "Chunk plain text where the meaning shifts",
        "description": "Needs an embedding model (`LLM_EMBEDDING_MODEL`) and an LLM key.",
        "value": {
            "text": "Cats purr. Cats sleep. Dogs bark. Dogs run.",
            "text_type": "plain",
            "chunking": {"strategy": "semantic", "breakpoint_percentile": 90},
        },
    },
}
_ACCEPTED: Final[dict[int | str, dict[str, Any]]] = {
    202: {
        "model": Envelope[JobAccepted],
        "description": "With `wait_seconds`, or when the request waits for an nlp or llm "
        "worker: the work takes longer and became a job.",
    }
}

router = APIRouter()
# `POST /v1/chunk` authenticates and rate-limits before its body is read (ruling R16).
chunk_router = APIRouter(route_class=AuthFirstRoute)


@router.post(
    STRUCTURED_PATH,
    summary="Extract structured fields from a URL",
    dependencies=[Depends(rate_limited)],
    response_model=Envelope[ExtractResult],
    responses=_ACCEPTED,
)
async def structured(
    body: Annotated[StructuredRequest, Body(openapi_examples=_STRUCTURED_EXAMPLES)],
    request: Request,
    principal: Annotated[Principal, Depends(require_role("user"))],
    asr_key: Annotated[
        str | None, Header(alias=ASR_KEY_HEADER, description=ASR_KEY_DESCRIPTION)
    ] = None,
    llm_key: Annotated[
        str | None, Header(alias=LLM_KEY_HEADER, description=LLM_KEY_DESCRIPTION)
    ] = None,
) -> Envelope[ExtractResult] | JSONResponse:
    """Fetch a URL and extract the fields of a JSON schema (and the page's tables) from it.

    The same as `POST /v1/extract` with `structured` required (it is 422 without): the same
    options, checks, headers and answers. `data.structured` has the values, where each came from
    (`metadata`, `selectors`, `patterns`, `llm`) and what is missing; `data.content` is the page
    as Markdown, text or HTML as usual. The strategy `auto` (the default) tries the cheapest
    source first; `llm` and `learn_selectors` need an LLM (400 without one) and a key
    (`X-LLM-Api-Key` or the service's own), and such a request waits `LLM_SYNC_WAIT_SECONDS`
    (30 s) for the llm worker without `wait_seconds`, then answers 202 with a job.
    """
    return await run_structured(
        request.app.state, principal, body, asr_key=asr_key, llm_key=llm_key
    )


async def run_structured(
    state: State,
    principal: Principal,
    body: StructuredRequest,
    *,
    asr_key: str | None,
    llm_key: str | None,
) -> Envelope[ExtractResult] | JSONResponse:
    """The core of ``POST /v1/structured`` (ruling P10-S1). ``asr_key`` and ``llm_key`` are the
    raw values of the ``X-ASR-Api-Key`` and ``X-LLM-Api-Key`` headers (None without them).

    Caller's checks (this core runs none of them): ``principal_for`` (the user role, which every
    key has), then one ``check_rate`` hit."""
    return await respond(
        state,
        body,
        principal,
        "extract",
        asr_key=asr_key_of(asr_key),
        llm_key=llm_key_of(llm_key),
    )


@chunk_router.post(
    CHUNK_PATH,
    summary="Split a URL or a text into chunks",
    response_model=Envelope[ExtractResult],
    responses={
        **_ACCEPTED,
        413: {
            "model": Envelope[None],
            "description": "`text` is over the upload limit (`DOCUMENT_MAX_BYTES`).",
        },
    },
)
async def chunk(
    body: Annotated[ChunkRequest, Body(openapi_examples=_CHUNK_EXAMPLES)],
    request: Request,
    principal: Annotated[Principal, Depends(require_role("user"))],
    asr_key: Annotated[
        str | None, Header(alias=ASR_KEY_HEADER, description=ASR_KEY_DESCRIPTION)
    ] = None,
    llm_key: Annotated[
        str | None, Header(alias=LLM_KEY_HEADER, description=LLM_KEY_DESCRIPTION)
    ] = None,
) -> Envelope[ExtractResult] | Response:
    """Split the content of a page, or a text you send, into chunks for retrieval.

    Exactly one of `url` and `text` (else 422), and `chunking` (required). With `url` this is
    `POST /v1/extract` with `chunking` required, with its options. With `text` (at most
    `DOCUMENT_MAX_BYTES` as UTF-8, else 413) the text is handled as an upload of `text/markdown`
    or `text/plain` (`text_type`, default `markdown`), as by `POST /v1/extract/file`:
    `mode_used: "upload"`, `url: null`, `cache: "bypass"`; a short text is fine (no minimum
    word count), an empty one 422. A `plain` text is `format: text`:
    its chunks have no headings. The chunks are `data.chunks`, each with its offsets into
    `data.content` and its heading path.

    `chunking.strategy` `semantic` embeds the sentences with the LLM endpoint: 400 without
    `LLM_EMBEDDING_MODEL` or a key (`X-LLM-Api-Key` or the service's own), and such a request
    waits `LLM_SYNC_WAIT_SECONDS` (30 s) for the llm worker without `wait_seconds`, then answers
    202 with a job (kind `extract` for a `url`, `file` for a `text`). `format: html` is 422.
    `structured` and `pii` may be combined with `chunking`, as on `POST /v1/extract`.
    """
    return await run_chunk(request.app.state, principal, body, asr_key=asr_key, llm_key=llm_key)


async def run_chunk(
    state: State,
    principal: Principal,
    body: ChunkRequest,
    *,
    asr_key: str | None,
    llm_key: str | None,
) -> Envelope[ExtractResult] | Response:
    """The core of ``POST /v1/chunk`` (ruling P10-S1). ``asr_key`` and ``llm_key`` are the raw
    values of the ``X-ASR-Api-Key`` and ``X-LLM-Api-Key`` headers (None without them).

    Caller's checks (this core runs none of them): ``principal_for`` (the user role, which every
    key has), then one ``check_rate`` hit."""
    if body.url is not None:
        return await respond(
            state,
            body.as_extract(),
            principal,
            "extract",
            asr_key=asr_key_of(asr_key),
            llm_key=llm_key_of(llm_key),
        )
    assert body.text is not None  # noqa: S101 - one of url and text is set; narrows the type
    settings: Settings = state.settings
    data = body.text.encode()
    if len(data) > settings.document_max_bytes:  # a text is never media (R14)
        raise BodyTooLarge(settings.document_max_bytes)
    markdown = body.text_type == "markdown"
    form = FileForm(
        format=body.format if markdown else "text",
        metadata=body.metadata,
        wait_seconds=body.wait_seconds,
        pii=body.pii,
        structured=body.structured,
        chunking=body.chunking,
    )
    _, caller_llm_key = check_upload_request(
        form, principal, settings, asr_header=None, llm_header=llm_key
    )
    return await answer_upload(
        state,
        principal,
        form,
        data,
        "text.md" if markdown else "text.txt",
        "text/markdown" if markdown else "text/plain",
        asr_key=None,
        llm_key=caller_llm_key,
        short_ok=True,  # the caller sent the text: chunking a short one is valid (R15)
    )


router.include_router(chunk_router)
