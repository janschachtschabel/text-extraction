"""The playground's choices of the optional features (package 19, P19-R3): OCR, personal data,
transcription, the structured strategy and the chunking method. Each is optional (nothing is
sent at its default) and is disabled with a hint while the service does not offer it."""

from typing import Any

import lxml.html
import pytest

from tests.fakes import PAGE
from tests.ui.conftest import USER_KEY, make_settings, running
from text_extraction.ui.forms import OptionsError, build_body, feature_offer
from text_extraction.ui.i18n import translate

ALL_ON: dict[str, Any] = {
    "docling_enabled": True,
    "pii_enabled": True,
    "asr_enabled": True,
    "llm_enabled": True,
}


def tree(html: str) -> Any:
    return lxml.html.fragment_fromstring(html, create_parent="div")


def texts(root: Any, xpath: str) -> list[str]:
    return [" ".join(node.text_content().split()) for node in root.xpath(xpath)]


def extract_form(**values: str) -> dict[str, str]:
    form = {"url": PAGE, "format": "markdown", "mode": "auto", "metadata": "on"}
    form.update(values)
    return form


# --- the bodies -----------------------------------------------------------------------------------


def test_defaults_send_none_of_the_features() -> None:
    body = build_body("extract", extract_form(ocr="auto", pii="", wait_seconds="25"))
    assert "ocr" not in body
    assert "pii" not in body
    assert "transcribe" not in body


def test_extract_sends_the_chosen_features() -> None:
    body = build_body("extract", extract_form(ocr="always", pii="mask", transcribe="on"))
    assert body["ocr"] == "always"
    assert body["pii"] == "mask"
    assert body["transcribe"] is True


def test_file_sends_ocr_and_pii() -> None:
    body = build_body("file", {"format": "markdown", "ocr": "never", "pii": "redact"})
    assert body["ocr"] == "never"
    assert body["pii"] == "redact"
    assert "transcribe" not in body  # an upload of audio or video is always transcribed


def test_batch_sends_the_chosen_features() -> None:
    form = {"format": "markdown", "mode": "auto", "ocr": "always", "pii": "hash"}
    body = build_body("batch", form | {"transcribe": "on"})
    assert (body["ocr"], body["pii"], body["transcribe"]) == ("always", "hash", True)


def test_unknown_choice_is_refused() -> None:
    with pytest.raises(OptionsError):
        build_body("extract", extract_form(pii="shout"))
    with pytest.raises(OptionsError):
        build_body("extract", extract_form(ocr="sometimes"))


def test_feature_and_options_json_given_twice() -> None:
    with pytest.raises(OptionsError):
        build_body("extract", extract_form(pii="mask", options='{"pii": "redact"}'))
    # At its default the control gives way to the JSON, as every control does.
    body = build_body("extract", extract_form(pii="", options='{"pii": "redact"}'))
    assert body["pii"] == "redact"


def test_structured_strategy_goes_into_the_object() -> None:
    schema = '{"schema": {"type": "object", "properties": {"title": {"type": "string"}}}}'
    form = {"url": PAGE, "format": "markdown", "mode": "auto", "structured": schema}
    assert build_body("structured", form | {"strategy": "llm"})["structured"]["strategy"] == "llm"
    assert build_body("structured", form | {"strategy": "auto"})["structured"].get(
        "strategy", "auto"
    ) == ("auto")
    # The JSON may still name it, unless the select names another one.
    named = '{"strategy": "selectors", "schema": {"type": "object"}}'
    assert build_body("structured", form | {"structured": named, "strategy": "auto"})["structured"][
        "strategy"
    ] == ("selectors")
    with pytest.raises(OptionsError):
        build_body("structured", form | {"structured": named, "strategy": "llm"})


def test_chunk_method_goes_into_the_object() -> None:
    form = {"url": PAGE, "format": "markdown", "chunking": '{"size": 800}'}
    assert build_body("chunk", form | {"method": "semantic"})["chunking"] == {
        "size": 800,
        "strategy": "semantic",
    }
    # At its default (the API's) the select sends nothing of its own.
    assert build_body("chunk", form | {"method": "recursive"})["chunking"] == {"size": 800}
    with pytest.raises(OptionsError):
        build_body("chunk", form | {"chunking": '{"strategy": "fixed"}', "method": "semantic"})


# --- what the forms offer -------------------------------------------------------------------------


def test_offer_follows_the_switches() -> None:
    off = feature_offer("user", make_settings())
    assert (off.ocr, off.pii, off.transcribe, off.llm) == (False, False, False, False)
    on = feature_offer("user", make_settings(**ALL_ON))
    assert (on.ocr, on.pii, on.transcribe, on.llm) == (True, True, True, True)
    assert on.llm_needs_key  # no LLM_API_KEY: the caller's key field
    assert not feature_offer("user", make_settings(**ALL_ON, llm_api_key="sk-x")).llm_needs_key


def page(**settings: Any) -> Any:
    with running(auth_mode="none", **settings) as client:
        return tree(client.get("/ui?lang=en").text)


@pytest.mark.parametrize("form_id", ["extract-form", "file-form", "batch-form"])
def test_controls_disabled_with_a_hint_while_off(form_id: str) -> None:
    root = page()
    for name in ("ocr", "pii"):
        (control,) = root.xpath(f"//form[@id='{form_id}']//select[@name='{name}']")
        assert control.get("disabled") is not None, name
    hints = texts(root, f"//form[@id='{form_id}']//*[contains(@class, 'feature-off')]")
    assert translate("en", "hint.ocr_off") in hints
    assert translate("en", "hint.pii_off") in hints


@pytest.mark.parametrize("form_id", ["extract-form", "file-form", "batch-form"])
def test_controls_enabled_while_on(form_id: str) -> None:
    root = page(**ALL_ON)
    for name in ("ocr", "pii"):
        (control,) = root.xpath(f"//form[@id='{form_id}']//select[@name='{name}']")
        assert control.get("disabled") is None, name
        (chosen,) = control.xpath(".//option[@selected]")
        assert chosen.get("value") in {"auto", ""}  # nothing chosen by default
    assert root.xpath(f"//form[@id='{form_id}']//*[contains(@class, 'feature-off')]") == []


def test_transcribe_box_follows_asr() -> None:
    (box,) = page().xpath("//form[@id='extract-form']//input[@name='transcribe']")
    assert box.get("disabled") is not None
    (box,) = page(**ALL_ON).xpath("//form[@id='extract-form']//input[@name='transcribe']")
    assert box.get("disabled") is None
    assert box.get("checked") is None


def test_llm_choices_follow_the_llm() -> None:
    root = page()
    (box,) = root.xpath("//form[@id='extract-form']//input[@name='describe_images']")
    assert box.get("disabled") is not None
    (llm,) = root.xpath(
        "//form[@id='structured-form']//select[@name='strategy']/option[@value='llm']"
    )
    assert llm.get("disabled") is not None
    (semantic,) = root.xpath(
        "//form[@id='chunk-form']//select[@name='method']/option[@value='semantic']"
    )
    assert semantic.get("disabled") is not None

    root = page(**ALL_ON)
    (box,) = root.xpath("//form[@id='extract-form']//input[@name='describe_images']")
    assert box.get("disabled") is None
    assert texts(root, "//form[@id='structured-form']//*[contains(@class, 'llm-key-hint')]") == [
        translate("en", "hint.llm_needs_key")
    ]


@pytest.mark.parametrize("language", ["en", "de"])
def test_feature_texts_in_both_languages(language: str) -> None:
    with running(auth_mode="none") as client:
        root = tree(client.get(f"/ui?lang={language}").text)
    labels = texts(root, "//form[@id='extract-form']//label[@for='extract-pii']")
    assert labels == [translate(language, "form.pii")]  # type: ignore[arg-type]


def test_offer_leaves_out_what_the_role_may_not_use() -> None:
    """P19-D1 b: a feature kept for admin keys is not offered to a user key, with its own hint."""
    closed = make_settings(**ALL_ON, ocr_allow_users=False, llm_allow_users=False)
    user = feature_offer("user", closed)
    assert (user.ocr, user.pii, user.transcribe, user.llm) == (False, True, True, False)
    assert user.admin_only == {"ocr", "llm"}
    assert not user.llm_needs_key
    assert user.hint("ocr", "hint.ocr", "hint.ocr_off") == "hint.ocr_admin_only"
    assert user.hint("pii", "hint.pii", "hint.pii_off") == "hint.pii"
    admin = feature_offer("admin", closed)
    assert (admin.ocr, admin.llm, admin.admin_only) == (True, True, frozenset())
    off = feature_offer("user", make_settings(ocr_allow_users=False))
    assert off.hint("ocr", "hint.ocr", "hint.ocr_off") == "hint.ocr_off"  # off for everyone


def test_a_user_key_sees_the_admin_only_hint() -> None:
    hx = {"HX-Request": "true", "Authorization": f"Bearer {USER_KEY}"}
    with running(**ALL_ON, pii_allow_users=False) as client:
        root = tree(client.post("/ui/run/login?lang=en", headers=hx).text)
    (control,) = root.xpath("//form[@id='extract-form']//select[@name='pii']")
    assert control.get("disabled") is not None
    hints = texts(root, "//form[@id='extract-form']//*[contains(@class, 'feature-off')]")
    assert translate("en", "hint.pii_admin_only") in hints


def test_semantic_needs_the_embedding_model() -> None:
    xpath = "//form[@id='chunk-form']//select[@name='method']/option[@value='semantic']"
    (semantic,) = page(**ALL_ON).xpath(xpath)
    assert semantic.get("disabled") is not None  # no LLM_EMBEDDING_MODEL
    root = page(**ALL_ON, llm_embedding_model="embed-1")
    (semantic,) = root.xpath(xpath)
    assert semantic.get("disabled") is None
    assert texts(root, "//form[@id='chunk-form']//*[contains(@class, 'llm-key-hint')]") == [
        translate("en", "hint.llm_needs_key")
    ]


def test_image_count_follows_describe_images() -> None:
    xpath = "//form[@id='extract-form']//input[@name='max_images']"
    (count,) = page().xpath(xpath)
    assert count.get("disabled") is not None
    (count,) = page(**ALL_ON).xpath(xpath)
    assert count.get("disabled") is None
