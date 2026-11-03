"""The screenshot form's presets and formats (package 14): each preset is one exact REST body,
``custom`` reads the fields, a REST 422 is shown at its control, and a JPEG or WebP screenshot is
shown as a ``data:`` image."""

import base64
from typing import Any

import lxml.html
import pytest
from fastapi.testclient import TestClient

from tests.fakes import PAGE
from tests.test_extract_api import BrowserCase
from tests.ui.conftest import run, running
from text_extraction.ui.forms import OptionsError, build_body
from text_extraction.ui.i18n import translate

JPEG = b"\xff\xd8\xff\xe0" + b"\x02" * 120
WEBP = b"RIFF\x24\x00\x00\x00WEBP" + b"\x03" * 120
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 120
HTML = b"<html>not an image</html>"


def form(**values: str) -> dict[str, Any]:
    """A screenshot form as the page posts it (wait 25)."""
    return {"url": PAGE, "wait_seconds": "25", **values}


def tree(html: str) -> Any:
    return lxml.html.fragment_fromstring(html, create_parent="div")


PRESET_BODIES = [
    ("page", {"full_page": True}),
    (None, {"full_page": True}),  # a form without the choice is the default preset
    (
        "viewport",
        {"full_page": False, "viewport": {"width": 1920, "height": 1080}},
    ),
    (
        "social",
        {
            "full_page": False,
            "viewport": {"width": 1200, "height": 630},
            "format": "webp",
            "quality": 80,
        },
    ),
    (
        "thumb",
        {
            "full_page": False,
            "viewport": {"width": 1280, "height": 800},
            "format": "webp",
            "width": 400,
        },
    ),
    ("mobile", {"full_page": True, "viewport": {"width": 390, "height": 844}}),
]


@pytest.mark.parametrize(("preset", "expected"), PRESET_BODIES)
def test_a_preset_is_one_exact_rest_body(preset: str | None, expected: dict[str, Any]) -> None:
    values = form() if preset is None else form(preset=preset)

    body = build_body("screenshot", values)

    assert body == {"url": PAGE, **expected, "wait_seconds": 25}


def test_a_preset_ignores_the_custom_fields() -> None:
    values = form(
        preset="thumb",
        viewport_width="800",
        format="jpeg",
        quality="50",
        width="100",
        full_page="on",
    )

    assert build_body("screenshot", values)["viewport"] == {"width": 1280, "height": 800}
    assert build_body("screenshot", values)["format"] == "webp"


def test_custom_reads_the_fields_and_leaves_out_the_defaults() -> None:
    defaults = form(
        preset="custom",
        viewport_width="1920",
        viewport_height="1080",
        format="png",
        quality="",
        width="",
        full_page="on",
    )
    assert build_body("screenshot", defaults) == {
        "url": PAGE,
        "full_page": True,
        "wait_seconds": 25,
    }

    chosen = form(
        preset="custom",
        viewport_width="800",
        viewport_height="600",
        format="jpeg",
        quality="55",
        width="400",
    )
    assert build_body("screenshot", chosen) == {
        "url": PAGE,
        "full_page": False,  # an unchecked box
        "viewport": {"width": 800, "height": 600},
        "format": "jpeg",
        "quality": 55,
        "width": 400,
        "wait_seconds": 25,
    }


def test_custom_with_one_viewport_side_takes_the_default_of_the_other() -> None:
    body = build_body("screenshot", form(preset="custom", viewport_width="1024", full_page="on"))

    assert body["viewport"] == {"width": 1024, "height": 1080}


def test_custom_number_that_is_no_number_names_its_control() -> None:
    with pytest.raises(OptionsError) as raised:
        build_body("screenshot", form(preset="custom", quality="high"))
    assert raised.value.field == "quality"

    with pytest.raises(OptionsError) as raised:
        build_body("screenshot", form(preset="custom", viewport_height="tall"))
    assert raised.value.field == "viewport_height"


def test_an_unknown_preset_is_refused() -> None:
    with pytest.raises(OptionsError) as raised:
        build_body("screenshot", form(preset="poster"))
    assert raised.value.field == "preset"


def test_options_may_give_what_a_preset_leaves_open() -> None:
    body = build_body("screenshot", form(options='{"format": "jpeg", "quality": 40}'))
    assert (body["format"], body["quality"]) == ("jpeg", 40)

    with pytest.raises(OptionsError, match="format"):
        build_body("screenshot", form(preset="social", options='{"format": "png"}'))


def test_a_422_is_shown_at_its_control(client: TestClient) -> None:
    html = run(client, "screenshot", **form(preset="custom", format="png", quality="50")).text

    root = tree(html)
    [error] = root.xpath("//div[@data-error-for]")
    assert error.get("data-error-for") == "screenshot-quality"
    assert error.get("id") == "screenshot-quality-error"
    assert error.get("role") == "alert"
    shown = " ".join(error.text_content().split())
    assert "quality needs format jpeg or webp" in shown
    assert root.xpath("//*[@class='http-status']")[0].text_content() == "HTTP 422"


def test_a_422_of_the_width_is_shown_at_the_width(client: TestClient) -> None:
    html = run(
        client,
        "screenshot",
        **form(preset="custom", viewport_width="400", viewport_height="300", width="500"),
    ).text

    [error] = tree(html).xpath("//div[@data-error-for]")
    assert error.get("data-error-for") == "screenshot-width"


def test_a_422_of_a_field_loc_is_shown_at_the_field(client: TestClient) -> None:
    html = run(client, "screenshot", **form(preset="custom", format="jpeg", quality="500")).text

    [error] = tree(html).xpath("//div[@data-error-for]")
    assert error.get("data-error-for") == "screenshot-quality"


def test_a_422_of_another_form_is_not_tied(client: TestClient) -> None:
    html = run(client, "extract", url=PAGE, options='{"mode2": 1}', wait_seconds="25").text

    assert tree(html).xpath("//div[@data-error-for]") == []


@pytest.mark.parametrize(
    ("name", "content", "media_type"),
    [
        ("screenshot.jpg", JPEG, "image/jpeg"),
        ("screenshot.webp", WEBP, "image/webp"),
        ("screenshot.png", PNG, "image/png"),
    ],
)
def test_the_result_shows_the_screenshot_in_its_format(
    name: str, content: bytes, media_type: str
) -> None:
    case = BrowserCase(ui_enabled=True)
    with TestClient(case.app) as client, case.worker(artifact=content, artifact_name=name):
        html = run(client, "screenshot", **form(preset="social")).text

    [image] = tree(html).xpath("//img[@class='screenshot']")
    assert image.get("src") == f"data:{media_type};base64," + base64.b64encode(content).decode()


def test_a_file_that_is_no_image_is_noted() -> None:
    case = BrowserCase(ui_enabled=True)
    with TestClient(case.app) as client, case.worker(artifact=HTML, artifact_name="screenshot.jpg"):
        html = run(client, "screenshot", **form(preset="social")).text

    root = tree(html)
    assert root.xpath("//img[@class='screenshot']") == []
    [note] = root.xpath("//*[@class='screenshot-note']")
    assert " ".join(note.text_content().split()) == translate("en", "screenshot.not_image")
    assert "PNG, JPEG or WebP" in translate("en", "screenshot.not_image")


def test_the_form_offers_the_presets_and_the_custom_fields() -> None:
    with running(auth_mode="none", browser_enabled=True) as client:
        page = lxml.html.document_fromstring(client.get("/ui", params={"lang": "en"}).text)

    [select] = page.xpath("//form[@id='screenshot-form']//select[@name='preset']")
    assert select.get("id") == "screenshot-preset"
    assert [(o.get("value"), o.get("selected") is not None) for o in select.xpath("option")] == [
        ("page", True),
        ("viewport", False),
        ("social", False),
        ("thumb", False),
        ("mobile", False),
        ("custom", False),
    ]
    [group] = page.xpath("//form[@id='screenshot-form']//fieldset[@id='screenshot-custom']")
    # Without JavaScript the fieldset is usable: ui.js disables it unless "custom" is chosen.
    assert group.get("disabled") is None
    assert [c.get("name") for c in group.xpath(".//input | .//select")] == [
        "viewport_width",
        "viewport_height",
        "format",
        "quality",
        "width",
        "full_page",
    ]
    assert [c.get("id") for c in group.xpath(".//input | .//select")] == [
        "screenshot-viewport-width",
        "screenshot-viewport-height",
        "screenshot-format",
        "screenshot-quality",
        "screenshot-width",
        "screenshot-full-page",
    ]
    assert [o.get("value") for o in group.xpath(".//select[@name='format']/option")] == [
        "png",
        "jpeg",
        "webp",
    ]
    quality = group.xpath(".//input[@name='quality']")[0]
    assert (quality.get("min"), quality.get("max")) == ("1", "100")
    width = group.xpath(".//input[@name='width']")[0]
    assert (width.get("min"), width.get("max")) == ("16", "3840")
