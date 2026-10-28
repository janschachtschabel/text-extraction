import json
from typing import Any

import pytest
from pydantic import ValidationError

from tests.test_document_models import PACKAGE_4_RESULT
from text_extraction.extract_models import ExtractRequest, ExtractResult
from text_extraction.file_models import FileForm, form_of, upload_options
from text_extraction.job_models import BatchRequest, JobOptions
from text_extraction.jobs import _options_json
from text_extraction.pii.types import (
    ENTITY_TYPES,
    PiiEntity,
    PiiOptions,
    PiiReport,
    model_names,
)
from text_extraction.pool_dispatch import job_options


def test_cached_result_without_pii_still_parses() -> None:
    """A result of package 6 (no ``pii`` key, no ``timings.pii_ms``) stays readable."""
    package_6 = {**PACKAGE_4_RESULT, "media": None}
    assert "pii" not in package_6
    assert "pii_ms" not in package_6["timings"]

    result = ExtractResult.model_validate(package_6)

    assert result.pii is None
    assert result.timings.pii_ms == 0


def test_pii_bare_string_is_mode() -> None:
    assert PiiOptions.model_validate("mask") == PiiOptions(mode="mask")
    request = ExtractRequest.model_validate({"url": "https://example.org/", "pii": "hash"})
    assert request.pii == PiiOptions(mode="hash")
    batch = BatchRequest.model_validate({"urls": ["https://example.org/"], "pii": "redact"})
    assert batch.pii == PiiOptions(mode="redact")
    with pytest.raises(ValidationError):
        PiiOptions.model_validate("scramble")


def test_pii_defaults_are_left_to_the_settings() -> None:
    options = PiiOptions(mode="annotate")

    assert options.entities is None
    assert options.language == "auto"
    assert options.score_threshold is None


def test_pii_options_validated() -> None:
    assert PiiOptions(mode="mask", entities=["PERSON", "URL"]).entities == ["PERSON", "URL"]
    assert len(ENTITY_TYPES) == 9
    for bad in (
        {},  # the mode is required
        {"mode": "mask", "language": "fr"},
        {"mode": "mask", "score_threshold": 1.1},
        {"mode": "mask", "score_threshold": -0.1},
        {"mode": "mask", "unknown": 1},
    ):
        with pytest.raises(ValidationError):
            PiiOptions.model_validate(bad)
    assert PiiOptions(mode="mask", score_threshold=0.0).score_threshold == 0.0
    assert PiiOptions(mode="mask", language="de").language == "de"


@pytest.mark.parametrize(
    "entities",
    [[], ["PERSON", "PERSON"], ["PERSON", "SSN"], ["person"]],
    ids=["empty", "duplicate", "unknown", "lower case"],
)
def test_pii_entities_unknown_or_duplicate_rejected(entities: list[str]) -> None:
    with pytest.raises(ValidationError):
        PiiOptions(mode="mask", entities=entities)  # type: ignore[arg-type]


def test_job_options_omit_pii_when_unset() -> None:
    """Pods of package 6 parse the options strictly: no ``pii`` key unless one is set."""
    stored = _options_json(JobOptions(compliance="enforce"))
    assert '"pii"' not in stored

    options = JobOptions(compliance="enforce", pii=PiiOptions(mode="mask", entities=["PERSON"]))
    stored = _options_json(options)

    assert json.loads(stored)["pii"] == {
        "mode": "mask",
        "entities": ["PERSON"],
        "language": "auto",
        "score_threshold": None,
    }
    assert JobOptions.model_validate_json(stored) == options


def test_pii_travels_from_request_to_job_options_and_back() -> None:
    req = ExtractRequest(url="https://example.org/", pii=PiiOptions(mode="hash", language="de"))

    options = job_options(req, "enforce")
    rebuilt = ExtractRequest(url=req.url, **options.model_dump())

    assert options.pii == PiiOptions(mode="hash", language="de")
    assert rebuilt.pii == req.pii
    assert job_options(ExtractRequest(url="https://example.org/"), "enforce").pii is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("mask", PiiOptions(mode="mask")),
        (
            '{"mode": "annotate", "entities": ["PERSON"], "score_threshold": 0.7}',
            PiiOptions(mode="annotate", entities=["PERSON"], score_threshold=0.7),
        ),
        (None, None),
    ],
)
def test_file_form_pii_is_a_mode_or_a_json_object(value: str | None, expected: Any) -> None:
    form = FileForm.model_validate({} if value is None else {"pii": value})

    assert form.pii == expected


@pytest.mark.parametrize("value", ["scramble", "{not json", '{"mode": "mask", "x": 1}', "[]", ""])
def test_file_form_pii_bad_input_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        FileForm.model_validate({"pii": value})


def test_file_form_pii_survives_the_job_options() -> None:
    form = FileForm(pii=PiiOptions(mode="redact"))

    options = upload_options(form, "enforce")

    assert options.pii == PiiOptions(mode="redact")
    assert form_of(options).pii == form.pii
    assert upload_options(FileForm(), "enforce").pii is None


def test_report_shapes() -> None:
    report = PiiReport(mode="mask", language="de", entities={"PERSON": 2})
    assert report.items is None
    assert report.hash_scope is None

    item = PiiEntity(
        field="content", type="EMAIL_ADDRESS", start=3, end=9, score=0.9, text="a@b.de"
    )
    annotated = PiiReport(
        mode="annotate", language="en", entities={"EMAIL_ADDRESS": 1}, items=[item]
    )
    assert PiiReport.model_validate_json(annotated.model_dump_json()) == annotated
    with pytest.raises(ValidationError):
        PiiReport(mode="mask", language="fr", entities={})  # type: ignore[arg-type]


def test_model_names_follow_the_size() -> None:
    assert model_names("lg") == {"de": "de_core_news_lg", "en": "en_core_web_lg"}
    assert model_names("md") == {"de": "de_core_news_md", "en": "en_core_web_md"}
    with pytest.raises(ValueError, match="size"):
        model_names("sm")  # type: ignore[arg-type]
