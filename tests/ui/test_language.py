"""Choosing the UI's language (rulings P12-S6, P12-S7): the page reads the ``lang`` query, then
``Accept-Language``, then ``UI_DEFAULT_LANGUAGE``; ``/ui/run/*`` reads ``X-UI-Language`` first.
Every UI answer says its ``Content-Language``; an unknown value is ignored and never reflected.
The switch is two links, no cookie, and the API stays English."""

import json
from typing import Any, cast

import lxml.html
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

from tests.ui.conftest import AUTH, HX, USER_KEY, running
from text_extraction.ui.forward import forward
from text_extraction.ui.i18n import LANGUAGE_HEADER
from text_extraction.ui.security import CONTENT_SECURITY_POLICY

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)


def login(client: TestClient, key: str = USER_KEY, **headers: str) -> Any:
    return client.post("/ui/run/login", headers={**HX, "Authorization": f"Bearer {key}", **headers})


def html_lang(answer: Any) -> str:
    root = lxml.html.document_fromstring(answer.text)
    return str(root.get("lang"))


@pytest.mark.parametrize(
    ("query", "headers", "overrides", "expected"),
    [
        ("?lang=de", {}, {}, "de"),
        ("?lang=en", {"Accept-Language": "de-DE,de;q=0.9"}, {}, "en"),
        ("", {"Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}, {}, "de"),
        ("", {"Accept-Language": "fr"}, {}, "en"),
        ("", {}, {}, "en"),
        ("", {}, {"ui_default_language": "de"}, "de"),
        ("", {"Accept-Language": "en"}, {"ui_default_language": "de"}, "en"),
    ],
)
def test_page_language_from_query_header_and_default(
    query: str, headers: dict[str, str], overrides: dict[str, Any], expected: str
) -> None:
    with running(**overrides) as client:
        answer = client.get(f"/ui{query}", headers=headers)

    assert answer.status_code == 200
    assert html_lang(answer) == expected
    assert answer.headers["content-language"] == expected
    heading = "Anmelden" if expected == "de" else "Log in"
    assert f'<h2 id="login-title">{heading}</h2>' in answer.text


@pytest.mark.parametrize(
    "value", ["<script>", "%3Cscript%3E", "fr", "DE", "de-DE", "en,de", "", "de\x00"]
)
def test_unknown_lang_is_ignored_and_not_reflected(value: str) -> None:
    with running() as client:
        page = client.get("/ui", params={"lang": value})
        # A header cannot carry a control character: the fragment gets "de\x00" as "de?".
        fragment = login(client, **{LANGUAGE_HEADER: value.replace("\x00", "?")})

    for answer in (page, fragment):
        assert answer.status_code == 200
        assert answer.headers["content-language"] == "en"
        assert "<script>" not in answer.text
        assert "script%3e" not in answer.text.lower()
    assert html_lang(page) == "en"
    assert "Log out" in fragment.text


def test_run_fragment_uses_the_language_header() -> None:
    with running(ui_default_language="de") as client:
        german = login(client, **{LANGUAGE_HEADER: "de", "Accept-Language": "en"})
        english = login(client, **{LANGUAGE_HEADER: "en", "Accept-Language": "de"})
        from_accept = login(client, **{"Accept-Language": "en-GB"})
        from_default = login(client)
        refused = login(client, "wrong-key-0123456789", **{LANGUAGE_HEADER: "de"})

    assert german.headers["content-language"] == "de"
    assert "Abmelden" in german.text
    assert "Log out" not in german.text
    assert english.headers["content-language"] == "en"
    assert "Log out" in english.text
    assert from_accept.headers["content-language"] == "en"
    assert from_default.headers["content-language"] == "de"
    assert "Abmelden" in from_default.text
    assert refused.headers["content-language"] == "de"
    assert "Anmelden" in refused.text


def test_no_cookie_and_csp_unchanged() -> None:
    assert CSP == CONTENT_SECURITY_POLICY
    with running() as client:
        answers = [
            client.get("/ui", params={"lang": "de"}),
            client.get("/ui", headers={"Accept-Language": "de"}),
            login(client, **{LANGUAGE_HEADER: "de"}),
            login(client, "wrong-key-0123456789", **{LANGUAGE_HEADER: "de"}),
        ]
        for answer in answers:
            assert answer.status_code == 200
            assert "set-cookie" not in answer.headers
            assert answer.headers["content-security-policy"] == CSP
        assert not client.cookies


def test_language_header_is_not_forwarded() -> None:
    with running() as client:
        app = cast(FastAPI, client.app)

        @app.get("/v1/test-echo")
        async def echo(request: Request) -> JSONResponse:
            return JSONResponse({"headers": dict(request.headers)})

        @app.post("/ui/test-forward")
        async def outer(request: Request) -> JSONResponse:
            got = await forward(request, "GET", "/v1/test-echo")
            return JSONResponse({"body": got.body})

        sent = {**AUTH, LANGUAGE_HEADER: "de", "Accept-Language": "de-DE,de;q=0.9"}
        answer = client.post("/ui/test-forward", headers=sent).json()

    headers = answer["body"]["headers"]
    assert headers["authorization"] == f"Bearer {USER_KEY}"
    assert LANGUAGE_HEADER not in headers
    assert "accept-language" not in headers


def test_api_texts_stay_english() -> None:
    body = {"url": "https://example.org/", "unknown_field": 1}
    with running() as client:
        plain = client.post("/v1/extract", json=body, headers=AUTH)
        asked = client.post(
            "/v1/extract",
            json=body,
            headers={**AUTH, "X-UI-Language": "de", "Accept-Language": "de"},
        )

    assert asked.status_code == plain.status_code == 422
    assert asked.json()["error"] == plain.json()["error"]
    message = asked.json()["error"]["message"]
    assert message.isascii()
    assert "content-language" not in asked.headers


def _switch(fragment: str) -> list[dict[str, str | None]]:
    root = lxml.html.fragment_fromstring(fragment, create_parent="div")
    navs = root.xpath("//nav[contains(concat(' ', @class, ' '), ' language-switch ')]")
    assert len(navs) == 1
    assert navs[0].get("aria-label")
    return [
        {
            "text": link.text_content().strip(),
            "href": link.get("href"),
            "lang": link.get("lang"),
            "hreflang": link.get("hreflang"),
            "current": link.get("aria-current"),
        }
        for link in navs[0].xpath(".//a")
    ]


@pytest.mark.parametrize("language", ["en", "de"])
def test_switch_marks_the_active_language(language: str) -> None:
    with running() as client:
        login_page = client.get("/ui", params={"lang": language}).text
        app_fragment = login(client, **{LANGUAGE_HEADER: language}).text
    with running(auth_mode="none") as client:
        open_page = client.get("/ui", params={"lang": language}).text

    for markup in (login_page, app_fragment, open_page):
        links = _switch(markup)
        assert sorted(str(link["text"]) for link in links) == ["Deutsch", "English"]
        for link in links:
            code = link["lang"]
            assert code in ("de", "en")
            assert link["hreflang"] == code
            assert link["href"] == f"/ui?lang={code}"
            assert link["current"] == ("true" if code == language else None)


def test_page_carries_the_status_texts_for_ui_js() -> None:
    with running() as client:
        german = client.get("/ui", params={"lang": "de"})
        english = client.get("/ui")

    body = lxml.html.document_fromstring(german.text).body
    assert body.get("data-text-request-failed") == "Anfrage fehlgeschlagen: {status} {reason}"
    assert body.get("data-text-network-error") == "Netzwerkfehler"
    assert body.get("data-text-selected") == "{count} ausgewählt"
    body = lxml.html.document_fromstring(english.text).body
    assert body.get("data-text-request-failed") == "Request failed: {status} {reason}"
    assert body.get("data-text-network-error") == "Network error"
    assert body.get("data-text-selected") == "{count} selected"


def test_ui_js_sends_and_stores_the_language() -> None:
    from tests.ui.test_shell import UI_PACKAGE

    script = (UI_PACKAGE / "static" / "ui.js").read_text()

    assert '"te.lang"' in script
    assert '"X-UI-Language"' in script
    assert "localStorage" in script
    assert "location.replace" in script
    assert "document.cookie" not in script
    assert json.dumps("Request failed: ") not in script
