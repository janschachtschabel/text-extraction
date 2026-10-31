"""The load profile (`tests/load/scenarios.py`, ruling P11-S4): every scenario sends load to the
local site farm only, and the profile holds every part of the ruling."""

from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import BaseModel

from tests.load.scenarios import REPO_ROOT, SCENARIOS, Scenario
from text_extraction.extract_models import ChunkRequest, ExtractRequest, StructuredRequest
from text_extraction.file_models import FileForm
from text_extraction.job_models import BatchRequest
from text_extraction.site.types import SiteStructureRequest

FARM = "http://farm.load.test:18000"


def strings(value: object) -> Iterator[str]:
    """Every string anywhere in a JSON value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def urls(body: dict[str, object]) -> list[str]:
    return [text for text in strings(body) if "://" in text]


def by_name(name: str) -> Scenario:
    return next(scenario for scenario in SCENARIOS if scenario.name == name)


def test_every_scenario_targets_the_farm() -> None:
    for scenario in SCENARIOS:
        for index in (0, scenario.count - 1):
            body = scenario.body(index, FARM)
            found = urls(body)
            if scenario.kind != "upload":  # an upload sends a file of the repo, no URL
                assert found, scenario.name
            for url in found:
                assert url.startswith(FARM + "/"), (scenario.name, url)


def test_scenario_names_are_unique_and_the_plan_is_sane() -> None:
    names = Counter(scenario.name for scenario in SCENARIOS)
    assert all(count == 1 for count in names.values()), names
    for scenario in SCENARIOS:
        assert scenario.count >= 1, scenario.name
        assert 1 <= scenario.concurrency <= scenario.count, scenario.name
        assert scenario.settle_seconds >= 0, scenario.name
        assert scenario.endpoint.startswith("/"), scenario.name


def test_the_profile_holds_the_parts_of_p11_s4() -> None:
    plan = {scenario.name: scenario for scenario in SCENARIOS}

    assert {plan["sync-fast-c10"].concurrency, plan["sync-fast-c30"].concurrency} == {10, 30}
    assert len(urls(plan["batch-1000"].body(0, FARM))) == 1000
    assert plan["browser-100"].count == 100
    assert plan["upload-docs-50"].count == 50
    assert plan["upload-ocr-10"].count == 10
    assert plan["pii-200"].count == 200
    assert plan["asr-10"].count == 10
    assert plan["structured-selectors-200"].count == 200
    assert plan["structured-llm-50"].count == 50
    assert plan["chunk-semantic-50"].count == 50  # the semantic-chunk cost note (P11-S6)
    assert plan["site-crawl"].count == 1
    assert plan["mcp-20"].count == 20


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.name)
def test_each_body_is_a_valid_request_of_its_route(scenario: Scenario) -> None:
    """The bodies are checked against the service's own request models, so a renamed field
    fails here and not in a load run."""
    body = scenario.body(scenario.count - 1, FARM)
    models: dict[str, type[BaseModel]] = {
        "/v1/extract": ExtractRequest,
        "/v1/structured": StructuredRequest,
        "/v1/chunk": ChunkRequest,
        "/v1/batch": BatchRequest,
        "/v1/site-structure": SiteStructureRequest,
    }
    if scenario.kind == "upload":
        assert scenario.endpoint == "/v1/extract/file"
        fields = dict(body)
        file = fields.pop("file")
        assert isinstance(file, str)
        assert (REPO_ROOT / file).is_file(), file
        assert not Path(file).is_absolute()
        assert all(isinstance(value, str) for value in fields.values())
        FileForm.model_validate(fields)
    elif scenario.kind == "mcp":
        assert scenario.endpoint == "/mcp"
        assert body["method"] == "tools/call"
        params = body["params"]
        assert isinstance(params, dict)
        assert params["name"] == "extract_url"
    else:
        models[scenario.endpoint].model_validate(body)


def test_document_uploads_mix_a_long_pdf_and_an_office_file() -> None:
    """Both MarkItDown paths get load: a 120-page PDF and a DOCX, one after the other."""
    scenario = by_name("upload-docs-50")
    files = {str(scenario.body(index, FARM)["file"]) for index in range(scenario.count)}

    assert files == {"tests/documents/corpus/long.pdf", "tests/documents/corpus/lesson.docx"}


def test_pages_differ_between_requests_of_a_scenario() -> None:
    """Every request asks for its own page, so no answer is a copy of another."""
    scenario = by_name("sync-fast-c30")

    assert urls(scenario.body(0, FARM)) != urls(scenario.body(1, FARM))
