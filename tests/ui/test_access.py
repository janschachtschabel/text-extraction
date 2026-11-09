"""The "Site rules" choice and the "Browser fallback" select of the UI's forms (rulings P12-S3,
P12-S4, package 16): three steps (``enforce``, ``report``, ``ignore``) offered by role through
`access_offer`; the default step sends nothing; the result view shows the compliance mode and the
unblocked badge."""

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
from text_extraction.ui.forms import (
    ACCESS_FIELD,
    OptionsError,
    access_offer,
    access_value,
    build_body,
)
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

# The forms whose "maximum" hint says what that choice does there: a site crawl never renders, a
# screenshot always does, and a chunked text has no page.
MAXIMUM_HINTS = {
    "site-structure": "hint.access.maximum_site",
    "screenshot": "hint.access.maximum_screenshot",
    "chunk": "hint.access.maximum_chunk",
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


def test_each_step_sends_its_compliance() -> None:
    assert ACCESS_FIELD == "access"
    for op in ("extract", "structured", "screenshot", "chunk", "site-structure"):
        for mode in ("enforce", "report", "ignore"):
            body = build_body(op, {"url": PAGE, "access": mode})
            assert body["compliance"] == mode, (op, mode)
    assert build_body("batch", {"access": "report"})["compliance"] == "report"
    for op in ("extract", "structured", "screenshot", "chunk", "site-structure", "batch"):
        for value in ("default", "maximum", "respect", ""):
            assert "compliance" not in build_body(op, {"url": PAGE, "access": value}), (op, value)
        assert "compliance" not in build_body(op, {"url": PAGE}), op
    # Options may still choose a mode under the default step.
    body = build_body("extract", {"url": PAGE, "options": '{"compliance": "report"}'})
    assert body["compliance"] == "report"
    # A text has no page: chunking a text sends no compliance.
    assert "compliance" not in build_body("chunk", {"text": "Some text.", "access": "ignore"})


def test_switch_and_options_compliance() -> None:
    form = {"url": PAGE, "access": "report", "options": '{"compliance": "enforce"}'}
    with pytest.raises(OptionsError, match=r"^compliance is given twice\.$"):
        build_body("extract", form)
    sent = build_body("extract", {**form, "access": "default"})
    assert sent["compliance"] == "enforce"


# --- who sees the choice -------------------------------------------------------------------------


def test_access_offer_by_role_and_settings() -> None:
    on = make_settings(**BOTH_ON)
    assert access_offer("admin", on).modes == ("enforce", "report", "ignore")
    assert access_offer("user", on).modes == ("enforce", "report")
    assert access_offer("user", on).shown
    fixed = access_offer("user", make_settings(compliance_allow_override=False))
    assert fixed.modes == ("enforce",)
    assert not fixed.shown
    lax = access_offer("user", make_settings(compliance_default="ignore"))
    assert lax.default == "ignore"
    assert "ignore" in lax.modes
    assert access_offer("user", make_settings(compliance_default="report")).modes == (
        "enforce",
        "report",
    )
    assert access_offer("admin", on).unblock
    assert not access_offer("user", on).unblock
    assert not access_offer("admin", make_settings(browser_enabled=True)).unblock
    assert not access_offer("admin", make_settings(browser_unblock_enabled=True)).unblock
    # An admin learns that the unblock browser is off only while the browser is on (package 20).
    assert access_offer("admin", make_settings(browser_enabled=True)).unblock_off
    assert not access_offer("admin", on).unblock_off
    assert not access_offer("admin", make_settings()).unblock_off
    assert not access_offer("user", make_settings(browser_enabled=True)).unblock_off


def test_access_value_presets_a_mode() -> None:
    offer = access_offer("user", make_settings())
    assert access_value(offer, None) == "default"
    assert access_value(offer, "enforce") == "default"
    assert access_value(offer, "report") == "report"
    assert access_value(offer, "ignore") == "default"  # not offered


def radios_of(root: Any, name: str) -> list[Any]:
    (group,) = panel(root, name).xpath(".//fieldset[contains(@class, 'access')]")
    assert group.get("disabled") is None, name
    assert texts(group, "./legend") == [en("access.legend")]
    return list(group.xpath(f".//input[@type='radio'][@name='{ACCESS_FIELD}']"))


def test_choice_offered_by_role() -> None:
    with running(api_keys=KEYS, **BOTH_ON) as client:
        user = app_root(client, AUTH)
        admin = app_root(client, ADMIN)
        for name, prefix in WITH_CHOICE.items():
            radios = radios_of(user, name)
            assert [r.get("value") for r in radios] == ["default", "report"], name
            assert [r.get("data-mode") for r in radios] == ["enforce", "report"]
            assert [r.get("checked") is not None for r in radios] == [True, False]
            assert radios[0].get("id") == f"{prefix}-access-default"
            assert texts(user, f"//label[@for='{prefix}-access-default']") == [
                en("access.enforce") + en("access.default")
            ]
            assert texts(user, f"//label[@for='{prefix}-access-report']") == [en("access.report")]
            assert described(user, radios[0]) == [en("hint.access.enforce")]
            assert described(user, radios[1]) == [en("hint.access.report")]
            radios = radios_of(admin, name)
            assert [r.get("value") for r in radios] == ["default", "report", "ignore"]
            assert [r.get("data-mode") for r in radios] == ["enforce", "report", "ignore"]
            assert [r.get("checked") is not None for r in radios] == [True, False, False]
            assert [r.get("disabled") for r in radios] == [None] * 3
    with running(api_keys=KEYS, browser_enabled=True) as client:
        admin = app_root(client, ADMIN)  # no unblock mode: still three steps, none disabled
        for name in WITH_CHOICE:
            assert len(radios_of(admin, name)) == 3
    with running(api_keys=KEYS, compliance_allow_override=False) as client:
        user = app_root(client, AUTH)
        assert user.xpath(f"//input[@name='{ACCESS_FIELD}']") == []


def test_ignore_hint_depends_on_unblock() -> None:
    with running(api_keys=KEYS, **BOTH_ON) as client:
        admin = app_root(client, ADMIN)
        for name in WITH_CHOICE:
            ignore = radios_of(admin, name)[2]
            hint = MAXIMUM_HINTS.get(name, "hint.access.maximum")
            assert described(admin, ignore) == [en(hint, value="compliance: ignore")], name
    with running(api_keys=KEYS) as client:
        admin = app_root(client, ADMIN)
        for name in WITH_CHOICE:
            ignore = radios_of(admin, name)[2]
            assert described(admin, ignore) == [
                en("hint.access.ignore", value="compliance: ignore")
            ]
    with running(api_keys=KEYS, browser_enabled=True) as client:
        admin = app_root(client, ADMIN)  # the browser on, its unblock mode off (package 20)
        for name in WITH_CHOICE:
            ignore = radios_of(admin, name)[2]
            # A site crawl never renders, so the switched-off unblock browser does not matter.
            hint = (
                "hint.access.ignore"
                if name == "site-structure"
                else "hint.access.ignore_unblock_off"
            )
            assert described(admin, ignore) == [
                en(hint, value="compliance: ignore", setting="BROWSER_UNBLOCK_ENABLED")
            ], name
        assert "BROWSER_UNBLOCK_ENABLED" in en(
            "hint.access.ignore_unblock_off",
            value="compliance: ignore",
            setting="BROWSER_UNBLOCK_ENABLED",
        )


def test_file_form_has_no_access_choice(admin_client: TestClient) -> None:
    admin = app_root(admin_client, ADMIN)
    for name in ("file", "batch-file", "info"):
        assert panel(admin, name).xpath(f".//input[@name='{ACCESS_FIELD}']") == [], name


def test_forged_ignore_from_a_user_is_refused_by_rest() -> None:
    fetcher = FakeFetcher({PAGE: fetched(body=ARTICLE)})
    with running(fetcher, api_keys=KEYS, **BOTH_ON) as client:
        answer = run(client, "extract", url=PAGE, access="ignore")

    root = tree(answer.text)
    assert texts(root, "//*[@class='http-status']") == ["HTTP 403"]
    assert texts(root, "//*[@class='error-code']") == ["forbidden"]
    assert fetcher.calls == []


@pytest.mark.parametrize(
    ("view", "fields"),
    [
        ("batch-urls", {"urls": PAGE}),
        ("batch-urls", {"url": [PAGE]}),  # the site tree's selection
        ("site-structure", {"url": PAGE}),
    ],
    ids=["batch", "tree-batch", "site-structure"],
)
def test_forged_ignore_from_a_user_starts_no_job(view: str, fields: dict[str, Any]) -> None:
    """The job routes refuse a user's forged ``access=ignore`` too (REST's ``resolve_mode``):
    403 ``forbidden``, and no job, so no page is fetched."""
    fetcher = FakeFetcher({PAGE: fetched(body=ARTICLE)})
    with running(fetcher, api_keys=KEYS, **BOTH_ON) as client:
        answer = client.post(
            f"/ui/run/{view}", data={**fields, "access": "ignore"}, headers={**HX, **AUTH}
        )

    assert answer.status_code == 200  # a fragment htmx swaps
    root = tree(answer.text)
    assert root.xpath("//section[contains(@class, 'job')]") == []
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
        "/ui/run/batch-urls", data={"urls": PAGE, "access": "ignore"}, headers=sent
    )
    site = admin_client.post(
        "/ui/run/site-structure", data={"url": PAGE, "access": "ignore"}, headers=sent
    )
    tree_batch = admin_client.post(
        "/ui/run/batch-urls", data={"url": [PAGE], "access": "ignore"}, headers=sent
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
            data={"urls": f"{PAGE}\nnot a url", "access": "ignore"},
            headers=sent,
        ).text
    )
    radios = preview.xpath(f"//form//input[@name='{ACCESS_FIELD}']")
    assert [(r.get("value"), r.get("checked") is not None) for r in radios] == [
        ("default", False),
        ("report", False),
        ("ignore", True),
    ]
    assert radios[0].get("id") == "preview-access-default"
    fake_reads(
        monkeypatch, len(SMALL_SITE), lambda offset, limit: SMALL_SITE[offset : offset + limit]
    )
    shown = admin_client.get("/ui/run/site-tree", params={"id": "a" * 32}, headers=sent)
    (form,) = tree(shown.text).xpath("//form[@class='tree-form']")
    radios = form.xpath(f".//input[@name='{ACCESS_FIELD}']")
    assert [radio.get("value") for radio in radios] == ["default", "report", "ignore"]
    assert radios[0].get("id") == "tree-access-default"
    assert [o.get("disabled") for o in form.xpath(".//select[@name='mode']/option")] == [None] * 3


def test_batch_file_preview_offers_the_choice_to_admins_only() -> None:
    upload = {"file": ("urls.txt", f"{PAGE}\n".encode(), "text/plain")}
    with running(api_keys=KEYS, **BOTH_ON) as client:
        for auth, expected in (
            (ADMIN, ["default", "report", "ignore"]),
            (AUTH, ["default", "report"]),
        ):
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
    terms = texts(root, "//dl[@class='facts']//dt")
    values = texts(root, "//dl[@class='facts']//dd")
    assert dict(zip(terms, values, strict=True))[en("access.compliance")] == "ignore"
    plain = result_data(PAGE)
    root = tree(render_result(op="extract", http_status=200, envelope={"data": plain}))
    assert root.xpath("//*[contains(@class, 'badge-unblocked')]") == []
    terms = texts(root, "//dl[@class='facts']//dt")
    values = texts(root, "//dl[@class='facts']//dd")
    assert dict(zip(terms, values, strict=True))[en("access.compliance")] == "enforce"


def test_a_bot_protection_result_says_what_helps() -> None:
    """Package 20: a refusal that is a bot challenge names the ways past it."""
    for language in ("en", "de"):
        for code, shown in (("bot_protection", True), ("target_http_error", False)):
            envelope = {
                "status": "failed",
                "error": {"code": code, "message": "m", "retryable": False},
                "data": result_data(PAGE, upstream_status=403),
                "trace_id": "t",
            }
            root = tree(
                render_result(op="extract", http_status=200, envelope=envelope, language=language)
            )
            hint = texts(root, "//*[contains(@class, 'error-hint')]")
            expected = [
                " ".join(
                    translate(
                        language, "result.bot_protection_hint", setting="BROWSER_UNBLOCK_ENABLED"
                    ).split()
                )
            ]
            assert hint == (expected if shown else []), (language, code)
    # An unblock render that still met the challenge: the admin did choose "Checks off" already.
    unblocked = {
        "status": "failed",
        "error": {"code": "bot_protection", "message": "m", "retryable": False},
        "data": result_data(PAGE, upstream_status=403, mode_used="browser", unblocked=True),
        "trace_id": "t",
    }
    root = tree(render_result(op="extract", http_status=200, envelope=unblocked))
    assert root.xpath("//*[contains(@class, 'error-hint')]") == []


def test_choice_and_result_follow_the_language(admin_client: TestClient) -> None:
    german = {"X-UI-Language": "de"}
    root = app_root(admin_client, {**ADMIN, **german})
    legends = texts(panel(root, "extract"), ".//fieldset[contains(@class, 'access')]/legend")
    assert legends == [translate("de", "access.legend")]
    (label,) = root.xpath("//label[@for='extract-mode']")
    assert label.text_content() == translate("de", "mode.label")

    answer = run(admin_client, "extract", url=PAGE, access="ignore", headers={**ADMIN, **german})

    result = tree(answer.text)
    terms = texts(result, "//dl[@class='facts']//dt")
    values = texts(result, "//dl[@class='facts']//dd")
    assert dict(zip(terms, values, strict=True))[translate("de", "access.compliance")] == "ignore"


def test_extract_result_lists_server_errors() -> None:
    errors = [
        {
            "level": "Warning",
            "message": "Undefined variable $x",
            "file": "/var/www/a.php",
            "line": 128,
            "count": 2,
        },
        {
            "level": "Notice",
            "message": "Trying to read",
            "file": "/var/www/b.php",
            "line": 7,
            "count": 1,
        },
    ]
    envelope = {"data": result_data(PAGE, server_errors=errors)}

    root = tree(render_result(op="extract", http_status=200, envelope=envelope))
    german = tree(render_result(op="extract", http_status=200, envelope=envelope, language="de"))

    fold = "//details[contains(@class, 'fold')][.//*[@class='server-errors']]"
    assert texts(root, f"{fold}/summary") == ["Server error messages removed from the text (3)"]
    assert texts(root, f"{fold}//li/code") == ["Warning", "Notice"]
    items = texts(root, f"{fold}//li")
    assert "in /var/www/a.php, line 128" in items[0]
    assert "\u00d72" in items[0]
    assert "\u00d7" not in items[1]
    assert texts(german, f"{fold}/summary") == [
        "Fehlermeldungen des Servers aus dem Text entfernt (3)"
    ]
    assert "in /var/www/a.php, Zeile 128" in texts(german, f"{fold}//li")[0]
    assert root.xpath(fold)[0].get("open") is None
    plain = tree(render_result(op="extract", http_status=200, envelope={"data": result_data(PAGE)}))
    assert plain.xpath("//*[@class='server-errors']") == []
