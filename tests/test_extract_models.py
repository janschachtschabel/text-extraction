"""``structured`` and ``chunking`` in the request, job and result models (package 8): where they
are accepted, how a job stores them and that results and jobs of earlier releases still parse."""

import json
from typing import Any

import pytest
from pydantic import ValidationError

from tests.test_document_models import PACKAGE_4_RESULT
from text_extraction.chunking.types import ChunkingOptions
from text_extraction.extract_models import ExtractRequest, ExtractResult
from text_extraction.file_models import FileForm, form_of, upload_options
from text_extraction.job_models import BatchRequest, JobOptions
from text_extraction.jobs import _options_json
from text_extraction.pool_dispatch import job_options
from text_extraction.structured.types import SelectorRule, StructuredOptions

SCHEMA = {"type": "object", "properties": {"price": {"type": "number"}}}
STRUCTURED = StructuredOptions(schema=SCHEMA, selectors={"price": SelectorRule(css=".price")})
CHUNKING = ChunkingOptions(strategy="sentence", size=500, overlap=50)
URL = "https://example.org/"


def test_old_cached_result_parses_with_null_structured_and_chunks() -> None:
    """A result of package 7 (no ``structured``, ``chunks`` or ``timings.llm_ms``) stays
    readable."""
    package_7 = {**PACKAGE_4_RESULT, "media": None, "pii": None}
    assert "structured" not in package_7
    assert "chunks" not in package_7
    assert "llm_ms" not in package_7["timings"]

    result = ExtractResult.model_validate(package_7)

    assert result.structured is None
    assert result.chunks is None
    assert result.timings.llm_ms == 0
    dumped = result.model_dump(mode="json")
    assert dumped["structured"] is None
    assert dumped["chunks"] is None
    assert dumped["timings"]["llm_ms"] == 0


def test_result_documents_the_new_warnings() -> None:
    description = ExtractResult.model_fields["warnings"].description or ""

    for warning in (
        "structured_value_invalid",
        "pattern_timeout",
        "llm_unavailable",
        "llm_input_truncated",
        "learned_selectors_unavailable",
        "chunks_truncated",
        "chunking_fallback_recursive",
    ):
        assert f"`{warning}`" in description


def test_requests_take_structured_and_chunking() -> None:
    body = {"structured": STRUCTURED.model_dump(), "chunking": {"size": 500}}

    request = ExtractRequest.model_validate({"url": URL, **body})
    batch = BatchRequest.model_validate({"urls": [URL], **body})

    for model in (request, batch):
        assert model.structured == STRUCTURED
        assert model.chunking == ChunkingOptions(size=500)
    plain = ExtractRequest(url=URL)
    assert plain.structured is None
    assert plain.chunking is None


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"structured": {"schema": {"type": "object", "properties": {"a": {"$ref": "x"}}}}}, "a"),
        ({"structured": {"schema": SCHEMA, "selectors": {"price": {"css": "a[["}}}}, "css"),
        ({"structured": {"schema": SCHEMA, "patterns": {"price": {"regex": "(x"}}}}, "regex"),
        ({"structured": {}}, "schema"),
        ({"chunking": {"size": 199}}, "size"),
    ],
)
def test_broken_options_are_rejected_naming_the_field(body: dict[str, Any], field: str) -> None:
    for model, extra in ((ExtractRequest, {"url": URL}), (BatchRequest, {"urls": [URL]})):
        with pytest.raises(ValidationError) as caught:
            model.model_validate({**extra, **body})
        assert field in str(caught.value)


def test_job_options_omit_unset_structured_and_chunking() -> None:
    """Pods of package 7 parse the options strictly: no new key unless one is set."""
    stored = _options_json(JobOptions(compliance="enforce"))
    assert "structured" not in stored
    assert "chunking" not in stored

    options = JobOptions(compliance="enforce", structured=STRUCTURED, chunking=CHUNKING)
    stored = _options_json(options)

    parsed = json.loads(stored)
    assert parsed["structured"]["schema"] == SCHEMA
    assert parsed["structured"]["selectors"] == {
        "price": {"css": ".price", "xpath": None, "attr": None, "all": None}
    }
    assert parsed["chunking"]["size"] == 500
    assert JobOptions.model_validate_json(stored) == options
    # Options stored by package 7 have neither key.
    old = JobOptions.model_validate_json(_options_json(JobOptions(compliance="enforce")))
    assert old.structured is None
    assert old.chunking is None


def test_structured_and_chunking_travel_from_request_to_job_options_and_back() -> None:
    req = ExtractRequest(url=URL, structured=STRUCTURED, chunking=CHUNKING)

    options = job_options(req, "enforce")
    rebuilt = ExtractRequest(url=URL, **options.model_dump())

    assert options.structured == STRUCTURED
    assert options.chunking == CHUNKING
    assert rebuilt.structured == STRUCTURED
    assert rebuilt.chunking == CHUNKING
    plain = job_options(ExtractRequest(url=URL), "enforce")
    assert plain.structured is None
    assert plain.chunking is None


def test_file_form_takes_json_strings() -> None:
    form = FileForm.model_validate(
        {
            "structured": json.dumps({"schema": SCHEMA, "strategy": "patterns"}),
            "chunking": '{"strategy": "markdown", "size": 400}',
        }
    )

    assert form.structured == StructuredOptions(schema=SCHEMA, strategy="patterns")
    assert form.chunking == ChunkingOptions(strategy="markdown", size=400)
    empty = FileForm.model_validate({})
    assert empty.structured is None
    assert empty.chunking is None
    for bad in (
        {"structured": "{not json"},
        {"structured": "mask"},
        {"structured": "{}"},
        {"chunking": "{not json"},
        {"chunking": '{"size": 5}'},
        {"chunking": ""},
    ):
        with pytest.raises(ValidationError):
            FileForm.model_validate(bad)


def test_file_form_structured_and_chunking_survive_the_job_options() -> None:
    form = FileForm(structured=STRUCTURED, chunking=CHUNKING)

    options = upload_options(form, "enforce")

    assert options.structured == STRUCTURED
    assert options.chunking == CHUNKING
    rebuilt = form_of(options)
    assert rebuilt.structured == STRUCTURED
    assert rebuilt.chunking == CHUNKING
    assert upload_options(FileForm(), "enforce").structured is None


def test_file_form_schema_says_string() -> None:
    properties = FileForm.model_json_schema()["properties"]

    assert properties["structured"]["type"] == "string"
    assert properties["chunking"]["type"] == "string"
