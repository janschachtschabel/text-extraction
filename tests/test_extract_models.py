"""``structured`` and ``chunking`` in the request, job and result models (package 8): where they
are accepted, how a job stores them and that results and jobs of earlier releases still parse."""

import json
from typing import Any

import pytest
from pydantic import ValidationError

from tests.test_document_models import PACKAGE_4_RESULT
from text_extraction.chunking.types import ChunkingOptions
from text_extraction.extract_models import ChunkRequest, ExtractRequest, ExtractResult
from text_extraction.file_models import FileForm, form_of, upload_options
from text_extraction.job_models import BatchRequest, JobOptions
from text_extraction.jobs import _options_json
from text_extraction.pool_dispatch import job_options
from text_extraction.site.types import SiteInfo
from text_extraction.structured.types import SelectorRule, StructuredOptions
from text_extraction.vision.types import (
    IMAGE_NOT_DESCRIBED_WARNING,
    DescribeImagesOptions,
    ImageDescriptionReport,
)

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
        "nothing_to_learn",
        "selector_learning_failed",
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


# --- describe_images (issue #20, rulings V-S1 and V-S3) -------------------------------------------

DESCRIBE = DescribeImagesOptions(max_images=3)


def test_requests_take_describe_images() -> None:
    body = {"describe_images": {"max_images": 3}}

    request = ExtractRequest.model_validate({"url": URL, **body})
    batch = BatchRequest.model_validate({"urls": [URL], **body})
    chunk = ChunkRequest.model_validate({"url": URL, "chunking": {"size": 500}, **body})

    for model in (request, batch, chunk):
        assert model.describe_images == DESCRIBE
    assert ExtractRequest(url=URL).describe_images is None
    assert BatchRequest(urls=[URL]).describe_images is None
    for bad in ({"max_images": 0}, {"max_images": 21}, {"detail": "high"}):
        with pytest.raises(ValidationError) as caught:
            ExtractRequest.model_validate({"url": URL, "describe_images": bad})
        assert "describe_images" in str(caught.value)


def test_an_extract_request_never_describes_an_upload() -> None:
    assert ExtractRequest(url=URL, describe_images=DESCRIBE).describe_upload is False
    assert ExtractRequest(url=URL).describe_upload is False


def test_job_options_omit_unset_describe_images() -> None:
    """Pods of the release before it parse the options strictly: no new key unless one is
    set."""
    stored = _options_json(JobOptions(compliance="enforce"))
    assert "describe_images" not in json.loads(stored)

    options = JobOptions(compliance="enforce", describe_images=DESCRIBE)
    stored = _options_json(options)

    assert json.loads(stored)["describe_images"] == {"max_images": 3}
    assert JobOptions.model_validate_json(stored) == options
    old = JobOptions.model_validate_json(_options_json(JobOptions(compliance="enforce")))
    assert old.describe_images is None


def test_describe_images_travels_from_request_to_job_options_and_back() -> None:
    req = ExtractRequest(url=URL, describe_images=DESCRIBE)

    options = job_options(req, "enforce")
    rebuilt = ExtractRequest(url=URL, **options.model_dump())

    assert options.describe_images == DESCRIBE
    assert rebuilt.describe_images == DESCRIBE
    assert job_options(ExtractRequest(url=URL), "enforce").describe_images is None


def test_file_form_takes_describe_images_json() -> None:
    assert FileForm.model_validate({"describe_images": '{"max_images": 2}'}).describe_images == (
        DescribeImagesOptions(max_images=2)
    )
    assert FileForm.model_validate({"describe_images": "{}"}).describe_images == (
        DescribeImagesOptions()
    )
    assert FileForm.model_validate({}).describe_images is None
    for bad in ("x", '{"max_images": 21}', '{"detail": "high"}'):
        with pytest.raises(ValidationError) as caught:
            FileForm.model_validate({"describe_images": bad})
        assert "describe_images" in str(caught.value)
    assert FileForm.model_json_schema()["properties"]["describe_images"]["type"] == "string"


def test_file_form_describe_images_survives_the_job_options() -> None:
    form = FileForm(describe_images=DESCRIBE)

    options = upload_options(form, "enforce")

    assert options.describe_images == DESCRIBE
    assert form_of(options).describe_images == DESCRIBE
    assert upload_options(FileForm(), "enforce").describe_images is None


def test_chunk_text_refuses_describe_images() -> None:
    with pytest.raises(ValidationError) as caught:
        ChunkRequest.model_validate(
            {"text": "# Notes", "chunking": {"size": 500}, "describe_images": {}}
        )
    assert "describe_images" in str(caught.value)


def test_old_cached_result_parses_with_null_descriptions() -> None:
    """A stored result without the fields (an image without ``description``, no
    ``image_descriptions``) stays readable."""
    old = {
        **PACKAGE_4_RESULT,
        "media": None,
        "pii": None,
        "metadata": {"images": [{"url": "https://example.org/a.png", "alt": "A"}]},
    }
    assert "image_descriptions" not in old

    result = ExtractResult.model_validate(old)

    assert result.image_descriptions is None
    assert result.metadata is not None
    assert result.metadata.images[0].description is None
    dumped = result.model_dump(mode="json")
    assert dumped["image_descriptions"] is None
    assert dumped["metadata"]["images"] == [
        {"url": "https://example.org/a.png", "alt": "A", "description": None}
    ]


def test_result_takes_an_image_description_report() -> None:
    report = ImageDescriptionReport(asked=3, described=1, skipped=1, failed=1)
    result = ExtractResult.model_validate(
        {**PACKAGE_4_RESULT, "image_descriptions": report.model_dump()}
    )

    assert result.image_descriptions == report


def test_result_documents_image_not_described() -> None:
    description = ExtractResult.model_fields["warnings"].description or ""

    assert f"`{IMAGE_NOT_DESCRIBED_WARNING}`" in description


def test_old_cached_result_parses_with_site_null() -> None:
    """A stored result without ``site`` (every result before package 9) stays readable, and a
    result of the other routes answers ``site: null``."""
    old = {**PACKAGE_4_RESULT, "media": None, "pii": None}
    assert "site" not in old

    result = ExtractResult.model_validate(old)

    assert result.site is None
    assert result.model_dump(mode="json")["site"] is None


def test_result_takes_site_info() -> None:
    site = SiteInfo(role="robots", depth=0, parent=0, sitemaps=["https://a.example/sitemap.xml"])

    result = ExtractResult.model_validate({**PACKAGE_4_RESULT, "site": site.model_dump()})

    assert result.site == site


def test_result_documents_the_site_warnings() -> None:
    description = ExtractResult.model_fields["warnings"].description or ""

    for warning in (
        "sitemap_not_found",
        "sitemap_truncated",
        "sitemap_urls_truncated",
        "sitemap_files_truncated",
        "links_truncated",
        "max_pages_reached",
        "site_memory_guard",
    ):
        assert f"`{warning}`" in description
