"""The playground's accessibility in its markup (rulings P12-S8 to P12-S10), rendered as an admin
in both languages: every control has a label and every field a hint tied to it, ids are unique,
every ARIA reference has its target, the tabs use a roving tabindex, a progress bar has a name
and the buttons of the refreshing progress view keep their ids, a form error names its field,
and each tab starts with an intro and a help link into the user guide of the page's language.
The axe-core of the browser tests is vendored unmodified and pinned."""

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import lxml.html
import pytest
from fastapi.testclient import TestClient

from tests.ui.conftest import HX, running
from tests.ui.site_items import page_item, result_data, sitemap_item
from tests.ui.test_jobs_views import fake_reads, job_store, portal
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractResult
from text_extraction.ui.i18n import (
    DOCS_BASE_URL,
    LANGUAGE_HEADER,
    LANGUAGES,
    Language,
    help_url,
    translate,
)
from text_extraction.ui.routes import TABS

VENDOR = Path(__file__).parent / "vendor"
AXE_FILE: Final = VENDOR / "axe-core-4.14.0.min.js"
AXE_LICENSE: Final = VENDOR / "axe-core-LICENSE.txt"
# axe.min.js of the npm tarball axe-core-4.14.0.tgz, as its sri-history.json names it.
AXE_SHA256: Final = "20c09fe157a8a34a30e241aaa1fcdade657734f08ab379ecfbeb7d45cc46e878"
ORIGIN = "https://example.org"
# The controls a user fills in; a hidden input, a button and a submit are none.
CONTROLS = (
    "//input[not(@type='hidden') and not(@type='submit') and not(@type='button')]"
    " | //select | //textarea"
)
REFERENCES = ("aria-describedby", "aria-labelledby", "aria-controls")
SITE = [
    page_item(0, f"{ORIGIN}/", title="Home", depth=0),
    page_item(1, f"{ORIGIN}/docs/intro", title="Intro"),
    sitemap_item(2, f"{ORIGIN}/sitemap.xml", [{"loc": f"{ORIGIN}/docs/more"}]),
]


def _document(html: str) -> Any:
    return lxml.html.document_fromstring(html)


def _fragment(html: str) -> Any:
    return lxml.html.fragment_fromstring(html, create_parent="div")


def _headers(language: Language, **extra: str) -> dict[str, str]:
    return {**HX, LANGUAGE_HEADER: language, **extra}


@pytest.fixture
def admin() -> Iterator[TestClient]:
    """The app without authentication (every caller is an admin), with browser and unblock mode
    on, so the forms offer "Site rules"."""
    with running(auth_mode="none", browser_enabled=True, browser_unblock_enabled=True) as client:
        yield client


def _page(client: TestClient, language: Language) -> Any:
    response = client.get("/ui", params={"lang": language})
    assert response.status_code == 200
    return _document(response.text)


def _batch(client: TestClient, count: int) -> str:
    """A batch of ``count`` URLs started over REST, with a result for its first item."""
    listed = [f"{ORIGIN}/page-{number}" for number in range(count)]
    response = client.post("/v1/batch", json={"urls": listed})
    assert response.status_code == 202, response.text
    job_id: str = response.json()["data"]["id"]
    envelope = Envelope[ExtractResult](
        status="success", data=ExtractResult.model_validate(result_data(listed[0]))
    )
    portal(client).call(job_store(client).write_result, job_id, 0, envelope)
    return job_id


def _progress(client: TestClient, language: Language, job_id: str, **extra: str) -> str:
    response = client.get(
        "/ui/run/progress", params={"id": job_id, **extra}, headers=_headers(language)
    )
    assert response.status_code == 200
    return response.text


def _fragments(
    client: TestClient, language: Language, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """The fragments the page shows in its result area: a result, a form error, a batch
    preview, a progress view and a site tree."""
    headers = _headers(language)
    result = client.post(
        "/ui/run/extract", data={"url": f"{ORIGIN}/learn/photosynthesis.html"}, headers=headers
    )
    form_error = client.post(
        "/ui/run/extract",
        data={"url": f"{ORIGIN}/", "options": "{"},
        headers=_headers(language, **{"HX-Trigger": "extract-form"}),
    )
    preview = client.post(
        "/ui/run/batch-urls", data={"urls": f"{ORIGIN}/a\nnot a url"}, headers=headers
    )
    progress = _progress(client, language, _batch(client, 25))
    fake_reads(monkeypatch, len(SITE), lambda offset, limit: SITE[offset : offset + limit])
    tree = client.get("/ui/run/site-tree", params={"id": "a" * 32}, headers=headers)
    return {
        "result": _fragment(result.text),
        "form_error": _fragment(form_error.text),
        "preview": _fragment(preview.text),
        "progress": _fragment(progress),
        "tree": _fragment(tree.text),
    }


def _login_pages(language: Language) -> dict[str, Any]:
    """The login page, and the login box after a wrong key."""
    with running() as client:
        login = client.get("/ui", params={"lang": language})
        refused = client.post(
            "/ui/run/login",
            headers=_headers(language, Authorization="Bearer wrong-key-0123456789"),
        )
    return {"login": _document(login.text), "login_error": _fragment(refused.text)}


def _ids(root: Any) -> list[str]:
    return [element.get("id") for element in root.xpath("//*[@id]")]


def _labelled(control: Any, root: Any) -> bool:
    control_id = control.get("id")
    if control.get("aria-label") or control.get("aria-labelledby"):
        return True
    if control_id and root.xpath(f"//label[@for='{control_id}']"):
        return True
    return any(parent.tag == "label" for parent in control.iterancestors())


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_control_has_a_label(
    admin: TestClient, language: Language, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = {
        "page": _page(admin, language),
        **_fragments(admin, language, monkeypatch),
        **_login_pages(language),
    }

    unlabelled = {
        name: [
            control.get("id") or control.get("name")
            for control in root.xpath(CONTROLS)
            if not _labelled(control, root)
        ]
        for name, root in roots.items()
    }

    assert {name: found for name, found in unlabelled.items() if found} == {}


def _described(control: Any, root: Any) -> bool:
    """Whether ``control``, or the group it is in, names a hint in ``aria-describedby``."""
    for element in (control, *control.iterancestors("fieldset")):
        for target in (element.get("aria-describedby") or "").split():
            if root.xpath(f"//*[@id='{target}' and contains(@class, 'hint')]"):
                return True
    return False


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_field_has_a_hint(admin: TestClient, language: Language) -> None:
    page = _page(admin, language)

    fields = page.xpath("//*[@role='tabpanel']//*[self::input or self::select or self::textarea]")
    without = [
        field.get("id") or field.get("name")
        for field in fields
        if field.get("type") != "hidden" and not _described(field, page)
    ]

    assert fields
    assert without == []


@pytest.mark.parametrize("language", LANGUAGES)
def test_ids_are_unique_on_the_page(
    admin: TestClient, language: Language, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _page(admin, language)
    page_ids = _ids(page)
    fragments = _fragments(admin, language, monkeypatch)

    assert len(page_ids) == len(set(page_ids))
    for name, fragment in {**fragments, **_login_pages(language)}.items():
        ids = _ids(fragment)
        assert len(ids) == len(set(ids)), name
        if name in fragments:  # shown in the page's result area
            assert set(ids) & set(page_ids) == set(), name


@pytest.mark.parametrize("language", LANGUAGES)
def test_described_by_targets_exist(
    admin: TestClient, language: Language, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _page(admin, language)
    roots = {"page": page, **_fragments(admin, language, monkeypatch), **_login_pages(language)}

    missing: dict[str, list[str]] = {}
    for name, root in roots.items():
        known = set(_ids(root)) | set(_ids(page))
        for attribute in REFERENCES:
            for element in root.xpath(f"//*[@{attribute}]"):
                targets = element.get(attribute).split()
                assert targets, (name, attribute)
                missing.setdefault(name, []).extend(set(targets) - known)

    assert {name: found for name, found in missing.items() if found} == {}


@pytest.mark.parametrize("language", LANGUAGES)
def test_tabs_have_roving_tabindex(admin: TestClient, language: Language) -> None:
    page = _page(admin, language)

    lists = page.xpath("//*[@role='tablist']")
    tabs = page.xpath("//*[@role='tablist']/*[@role='tab']")
    panels = page.xpath("//*[@role='tabpanel']")

    assert len(lists) == 1
    assert lists[0].get("aria-label")
    assert [tab.get("aria-selected") for tab in tabs] == ["true"] + ["false"] * (len(TABS) - 1)
    assert [tab.get("tabindex") for tab in tabs] == ["0"] + ["-1"] * (len(TABS) - 1)
    assert [panel.get("id") for panel in panels] == [tab.get("aria-controls") for tab in tabs]
    assert [panel.get("aria-labelledby") for panel in panels] == [tab.get("id") for tab in tabs]
    assert [panel.get("tabindex") for panel in panels] == ["0"] * len(TABS)
    assert [panel.get("hidden") is None for panel in panels] == [True] + [False] * (len(TABS) - 1)


@pytest.mark.parametrize("language", LANGUAGES)
def test_progress_has_an_accessible_name(admin: TestClient, language: Language) -> None:
    job_id = _batch(admin, 3)

    bars = _fragment(_progress(admin, language, job_id)).xpath("//progress")

    assert len(bars) == 1
    assert bars[0].get("aria-label") == translate(language, "progress.bar", id=job_id)


@pytest.mark.parametrize("language", LANGUAGES)
def test_progress_buttons_have_stable_ids(admin: TestClient, language: Language) -> None:
    job_id = _batch(admin, 25)

    def buttons(html: str) -> list[str | None]:
        return [button.get("id") for button in _fragment(html).xpath("//button")]

    first = buttons(_progress(admin, language, job_id))
    again = buttons(_progress(admin, language, job_id, live="1"))

    assert first == again
    assert None not in first
    assert len(first) == len(set(first))
    assert {f"job-{job_id}-next", f"job-{job_id}-cancel", f"job-{job_id}-show-0"} <= set(first)


@pytest.mark.parametrize("language", LANGUAGES)
def test_progress_announces_its_states(admin: TestClient, language: Language) -> None:
    job_id = _batch(admin, 3)

    live = _fragment(_progress(admin, language, job_id, live="1")).xpath("//*[@data-announce]")

    state = translate(language, "job_state.queued")
    assert [element.get("data-announce") for element in live] == [
        translate(language, "status.job", id=job_id, state=state)
    ]


@pytest.mark.parametrize("language", LANGUAGES)
def test_help_link_points_to_the_guide_in_the_page_language(
    admin: TestClient, language: Language
) -> None:
    page = _page(admin, language)

    for tab in TABS:
        panel = page.xpath(f"//*[@id='panel-{tab.name}']")[0]
        heading, intro = panel.xpath("./h2")[0], panel.xpath("./p[@class='intro']")[0]
        assert heading.text_content() == translate(language, tab.label_key)
        assert intro.xpath("./text()")[0].strip() == translate(language, tab.intro_key)
        assert [link.get("href") for link in intro.xpath("./a")] == [
            help_url(language, tab.help_page)
        ]
    extract = page.xpath("//*[@id='panel-extract']/p[@class='intro']/a")[0]
    assert extract.get("href") == f"{DOCS_BASE_URL}/{language}/single-url.md"
    assert DOCS_BASE_URL == (
        "https://github.com/janschachtschabel/text-extraction/blob/main/docs/user-guide"
    )
    assert help_url("de", "single-url") == f"{DOCS_BASE_URL}/de/single-url.md"


@pytest.mark.parametrize("language", LANGUAGES)
def test_form_error_names_its_field(admin: TestClient, language: Language) -> None:
    def error_of(trigger: str, **fields: str) -> Any:
        response = admin.post(
            "/ui/run/extract", data=fields, headers=_headers(language, **{"HX-Trigger": trigger})
        )
        return _fragment(response.text).xpath("//*[@role='alert']")[0]

    options = error_of("extract-form", url=f"{ORIGIN}/", options="[1]")
    wait = error_of("extract-form", url=f"{ORIGIN}/", wait_seconds="soon")
    unknown = error_of("evil-form", url=f"{ORIGIN}/", options="{")

    assert options.get("data-error-for") == "extract-options"
    assert options.get("id") == "extract-options-error"
    assert wait.get("data-error-for") == "extract-wait-seconds"
    assert unknown.get("data-error-for") is None
    assert unknown.get("id") is None


def test_vendored_axe_is_pinned() -> None:
    assert hashlib.sha256(AXE_FILE.read_bytes()).hexdigest() == AXE_SHA256
    licence = AXE_LICENSE.read_text(encoding="utf-8")
    assert licence.startswith("Mozilla Public License, version 2.0")
