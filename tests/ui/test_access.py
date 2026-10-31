"""The "Site rules" choice and the "Browser fallback" select of the UI's forms (rulings P12-S3,
P12-S4): ``respect`` sends no ``compliance``, ``maximum`` sends ``compliance: "ignore"``; only
admins see the choice, enabled only while browser mode and unblock mode are on; the result view
shows the compliance mode and the unblocked badge."""

import hashlib
from collections.abc import Iterator
from typing import Any

import lxml.html
import pytest
from fastapi.testclient import TestClient

from tests.fakes import ARTICLE, PAGE, FakeFetcher, fetched
from tests.ui.conftest import AUTH, HX, USER_KEY, make_settings, run, running
from tests.ui.site_items import result_data
from tests.ui.test_jobs_views import SMALL_SITE, fake_reads
from text_extraction.ui.forms import ACCESS_FIELD, OptionsError, access_choice, build_body
from text_extraction.ui.forward import forward
from text_extraction.ui.i18n import translate
from text_extraction.ui.results import render_result

ADMIN_KEY = "admin-key-0123456789-raw-secret"
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}
KEYS = [
    {"name": "alice", "role": "user", "sha256": hashlib.sha256(USER_KEY.encode()).hexdigest()},
    {"name": "root", "role": "admin", "sha256": hashlib.sha256(ADMIN_KEY.encode()).hexdigest()},
]
BOTH_ON: dict[str, Any] = {"browser_enabled": True, "browser_unblock_enabled": True}
# The panels whose forms offer the choice, and the prefix of their ids.
WITH_CHOICE = {
    "extract": "extract",
    "structured": "structured",
    "screenshot": "screenshot",
    "chunk": "chunk",
    "batch": "batch",
    "site-structure": "site",
}


def tree(html: str) -> Any:
    return lxml.html.fragment_fromstring(html, create_parent="div")


def texts(root: Any, xpath: str) -> list[str]:
    return [" ".join(node.text_content().split()) for node in root.xpath(xpath)]


def described(root: Any, control: Any) -> list[str]:
    """The texts of the elements that ``aria-describedby`` of ``control`` names."""
    ids = (control.get("aria-describedby") or "").split()
    found = []
    for name in ids:
        (element,) = root.xpath(f"//*[@id='{name}']")
        found.append(" ".join(element.text_content().split()))
    return found


def en(key: str, **params: object) -> str:
    return translate("en", key, **params)


@pytest.fixture
def admin_client() -> Iterator[TestClient]:
    with running(api_keys=KEYS, **BOTH_ON) as client:
        yield client


def app_root(client: TestClient, auth: dict[str, str]) -> Any:
    answer = run(client, "login", headers=auth)
    assert answer.status_code == 200
    return tree(answer.text)


def panel(root: Any, name: str) -> Any:
    (found,) = root.xpath(f"//section[@data-panel='{name}']")
    return found


# --- the bodies ----------------------------------------------------------------------------------


def test_maximum_adds_compliance_ignore() -> None:
    assert ACCESS_FIELD == "access"
    for op in ("extract", "structured", "screenshot", "chunk", "site-structure"):
        body = build_body(op, {"url": PAGE, "access": "maximum"})
        assert body["compliance"] == "ignore", op
    assert build_body("batch", {"access": "maximum"})["compliance"] == "ignore"


def test_respect_sends_no_compliance() -> None:
    for op in ("extract", "structured", "screenshot", "chunk", "site-structure", "batch"):
        assert "compliance" not in build_body(op, {"url": PAGE, "access": "respect"}), op
        assert "compliance" not in build_body(op, {"url": PAGE}), op
    # Options may still choose a mode under respect.
    body = build_body("extract", {"url": PAGE, "options": '{"compliance": "report"}'})
    assert body["compliance"] == "report"
    # A text has no page: chunking a text sends no compliance.
    assert "compliance" not in build_body("chunk", {"text": "Some text.", "access": "maximum"})


def test_maximum_and_options_compliance_is_given_twice() -> None:
    form = {"url": PAGE, "access": "maximum", "options": '{"compliance": "report"}'}
    with pytest.raises(OptionsError, match=r"^compliance is given twice\.$"):
        build_body("extract", form)


# --- who sees the choice -------------------------------------------------------------------------


def test_access_choice_by_role_and_switches() -> None:
    on = make_settings(**BOTH_ON)
    assert access_choice("user", on) == "hidden"
    assert access_choice("admin", on) == "enabled"
    assert access_choice("admin", make_settings(browser_enabled=True)) == "disabled"
    assert access_choice("admin", make_settings(browser_unblock_enabled=True)) == "disabled"
    assert access_choice("admin", make_settings()) == "disabled"


def test_choice_shown_only_to_admins() -> None:
    with running(api_keys=KEYS, **BOTH_ON) as client:
        user = app_root(client, AUTH)
        assert user.xpath(f"//input[@name='{ACCESS_FIELD}']") == []
        admin = app_root(client, ADMIN)
        for name, prefix in WITH_CHOICE.items():
            (group,) = panel(admin, name).xpath(".//fieldset[contains(@class, 'access')]")
            assert texts(group, "./legend") == [en("access.legend")]
            assert group.get("disabled") is None, name
            radios = group.xpath(f".//input[@type='radio'][@name='{ACCESS_FIELD}']")
            assert [radio.get("value") for radio in radios] == ["respect", "maximum"]
            assert [radio.get("checked") is not None for radio in radios] == [True, False]
            assert [radio.get("disabled") for radio in radios] == [None, None]
            respect, maximum = radios
            assert respect.get("id") == f"{prefix}-access-respect"
            assert texts(group, f".//label[@for='{prefix}-access-maximum']") == [
                en("access.maximum")
            ]
            assert described(admin, respect) == [en("hint.access.respect")]
            maximum_hint = (
                "hint.access.maximum_site" if name == "site-structure" else ("hint.access.maximum")
            )
            assert described(admin, maximum) == [en(maximum_hint, value="compliance: ignore")]
    with running(api_keys=KEYS, browser_enabled=True) as client:
        admin = app_root(client, ADMIN)
        off = en("hint.access.off", browser="BROWSER_ENABLED", unblock="BROWSER_UNBLOCK_ENABLED")
        for name in WITH_CHOICE:
            (group,) = panel(admin, name).xpath(".//fieldset[contains(@class, 'access')]")
            assert group.get("disabled") is not None, name
            assert off in texts(group, ".//*[contains(@class, 'hint')]")
            for radio in group.xpath(f".//input[@name='{ACCESS_FIELD}']"):
                assert radio.get("disabled") is not None
                assert off in described(admin, radio)


def test_file_form_has_no_access_choice(admin_client: TestClient) -> None:
    admin = app_root(admin_client, ADMIN)
    for name in ("file", "batch-file", "info"):
        assert panel(admin, name).xpath(f".//input[@name='{ACCESS_FIELD}']") == [], name


def test_forged_maximum_from_a_user_is_refused_by_rest() -> None:
    fetcher = FakeFetcher({PAGE: fetched(body=ARTICLE)})
    with running(fetcher, api_keys=KEYS, **BOTH_ON) as client:
        answer = run(client, "extract", url=PAGE, access="maximum")

    root = tree(answer.text)
    assert texts(root, "//*[@class='http-status']") == ["HTTP 403"]
    assert texts(root, "//*[@class='error-code']") == ["forbidden"]
    assert fetcher.calls == []


# --- the browser fallback ------------------------------------------------------------------------


def test_mode_select_reads_as_browser_fallback(admin_client: TestClient) -> None:
    admin = app_root(admin_client, ADMIN)
    for name in ("extract", "structured", "batch"):
        (select,) = panel(admin, name).xpath(".//select[@name='mode']")
        (label,) = admin.xpath(f"//label[@for='{select.get('id')}']")
        assert " ".join(label.text_content().split()) == en("mode.label")
        options = select.xpath("./option")
        assert [(o.get("value"), o.text_content()) for o in options] == [
            ("auto", en("mode.auto")),
            ("fast", en("mode.fast")),
            ("browser", en("mode.browser")),
        ]
        assert [o.get("selected") is not None for o in options] == [True, False, False]
        assert [o.get("disabled") for o in options] == [None, None, None]
        # Its own hint and, with the choice on, one that says "never" does not reach the browser.
        assert described(admin, select) == [en("hint.mode"), en("mode.fast_with_maximum")]


def test_browser_option_disabled_while_browser_mode_is_off() -> None:
    with running(api_keys=KEYS) as client:
        for auth in (AUTH, ADMIN):
            root = app_root(client, auth)
            for name in ("extract", "structured", "batch"):
                (select,) = panel(root, name).xpath(".//select[@name='mode']")
                (browser,) = select.xpath("./option[@value='browser']")
                assert browser.get("disabled") is not None, name
                assert select.xpath("./option[@value='auto']")[0].get("selected") is not None
                assert en("mode.no_browser", setting="BROWSER_ENABLED") in described(root, select)
                # The choice is off (disabled or hidden), so no "never" hint.
                assert en("mode.fast_with_maximum") not in described(root, select)
        # A batch preview of a form that asked for "always" falls back to the default.
        listed = {"urls": f"{PAGE}\nnot a url", "mode": "browser"}
        preview = client.post("/ui/run/batch-urls", data=listed, headers={**HX, **AUTH})
        options = tree(preview.text).xpath("//select[@name='mode']/option[@selected]")
        assert [option.get("value") for option in options] == ["auto"]


# --- batch, batch file, tree and site structure --------------------------------------------------


@pytest.fixture
def forwarded(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """The (path, JSON body) of each call the job views forward."""
    calls: list[tuple[str, dict[str, Any]]] = []
    real = forward

    async def recording(request: Any, method: str, path: str, **kwargs: Any) -> Any:
        if kwargs.get("json") is not None:
            calls.append((path, kwargs["json"]))
        return await real(request, method, path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("text_extraction.ui.job_views.forward", recording)
    return calls


def test_batch_tree_and_site_carry_the_access_choice(
    admin_client: TestClient,
    forwarded: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = {**HX, **ADMIN}
    batch = admin_client.post(
        "/ui/run/batch-urls", data={"urls": PAGE, "access": "maximum"}, headers=sent
    )
    site = admin_client.post(
        "/ui/run/site-structure", data={"url": PAGE, "access": "maximum"}, headers=sent
    )
    tree_batch = admin_client.post(
        "/ui/run/batch-urls", data={"url": [PAGE], "access": "maximum"}, headers=sent
    )
    respected = admin_client.post("/ui/run/batch-urls", data={"urls": PAGE}, headers=sent)

    assert [answer.status_code for answer in (batch, site, tree_batch, respected)] == [200] * 4
    assert [(path, body.get("compliance")) for path, body in forwarded] == [
        ("/v1/batch", "ignore"),
        ("/v1/site-structure", "ignore"),
        ("/v1/batch", "ignore"),
        ("/v1/batch", None),
    ]

    # The batch preview keeps the choice; the tree offers it.
    preview = tree(
        admin_client.post(
            "/ui/run/batch-urls",
            data={"urls": f"{PAGE}\nnot a url", "access": "maximum"},
            headers=sent,
        ).text
    )
    radios = preview.xpath(f"//form//input[@name='{ACCESS_FIELD}']")
    assert [(r.get("value"), r.get("checked") is not None) for r in radios] == [
        ("respect", False),
        ("maximum", True),
    ]
    assert radios[0].get("id") == "preview-access-respect"
    fake_reads(
        monkeypatch, len(SMALL_SITE), lambda offset, limit: SMALL_SITE[offset : offset + limit]
    )
    shown = admin_client.get("/ui/run/site-tree", params={"id": "a" * 32}, headers=sent)
    (form,) = tree(shown.text).xpath("//form[@class='tree-form']")
    radios = form.xpath(f".//input[@name='{ACCESS_FIELD}']")
    assert [radio.get("value") for radio in radios] == ["respect", "maximum"]
    assert radios[0].get("id") == "tree-access-respect"
    assert [o.get("disabled") for o in form.xpath(".//select[@name='mode']/option")] == [None] * 3


def test_batch_file_preview_offers_the_choice_to_admins_only() -> None:
    upload = {"file": ("urls.txt", f"{PAGE}\n".encode(), "text/plain")}
    with running(api_keys=KEYS, **BOTH_ON) as client:
        for auth, expected in ((ADMIN, ["respect", "maximum"]), (AUTH, [])):
            answer = client.post("/ui/run/batch-file", files=upload, headers={**HX, **auth})
            radios = tree(answer.text).xpath(f"//input[@name='{ACCESS_FIELD}']")
            assert [radio.get("value") for radio in radios] == expected


# --- the result ----------------------------------------------------------------------------------


def test_result_shows_the_unblocked_badge() -> None:
    compliance = {"mode": "ignore", "decision": "allowed", "robots": "skipped"}
    data = result_data(PAGE, mode_used="browser", unblocked=True, compliance=compliance)
    envelope = {"status": "success", "data": data, "trace_id": "t"}

    root = tree(render_result(op="extract", http_status=200, envelope=envelope))
    german = tree(render_result(op="extract", http_status=200, envelope=envelope, language="de"))

    assert texts(root, "//*[contains(@class, 'badge-unblocked')]") == [en("access.unblocked")]
    assert texts(german, "//*[contains(@class, 'badge-unblocked')]") == [
        translate("de", "access.unblocked")
    ]
    terms = texts(root, "//dl[@class='facts']/dt")
    values = texts(root, "//dl[@class='facts']/dd")
    assert dict(zip(terms, values, strict=True))[en("access.compliance")] == "ignore"
    plain = result_data(PAGE)
    root = tree(render_result(op="extract", http_status=200, envelope={"data": plain}))
    assert root.xpath("//*[contains(@class, 'badge-unblocked')]") == []
    terms = texts(root, "//dl[@class='facts']/dt")
    values = texts(root, "//dl[@class='facts']/dd")
    assert dict(zip(terms, values, strict=True))[en("access.compliance")] == "enforce"


def test_choice_and_result_follow_the_language(admin_client: TestClient) -> None:
    german = {"X-UI-Language": "de"}
    root = app_root(admin_client, {**ADMIN, **german})
    legends = texts(panel(root, "extract"), ".//fieldset[contains(@class, 'access')]/legend")
    assert legends == [translate("de", "access.legend")]
    (label,) = root.xpath("//label[@for='extract-mode']")
    assert label.text_content() == translate("de", "mode.label")

    answer = run(admin_client, "extract", url=PAGE, access="maximum", headers={**ADMIN, **german})

    result = tree(answer.text)
    terms = texts(result, "//dl[@class='facts']/dt")
    values = texts(result, "//dl[@class='facts']/dd")
    assert dict(zip(terms, values, strict=True))[translate("de", "access.compliance")] == "ignore"
