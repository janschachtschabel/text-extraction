"""The UI shell (rulings P10-S12 to P10-S14): the switch, the page and its static files, the
login with an API key, the security headers, the htmx and same-origin guard of ``/ui/run`` and
the in-process forward."""

import hashlib
import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Annotated, Any, Final, cast

import pytest
from fastapi import Depends, FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient
from starlette.requests import Request
from structlog.testing import capture_logs

from tests.ui.conftest import AUTH, HX, USER_KEY, running
from text_extraction.auth import Principal
from text_extraction.settings import ConfigError, load_settings
from text_extraction.ui.forward import checked_path, forward
from text_extraction.ui.security import ui_principal

UI_PACKAGE = Path(__file__).parents[2] / "src" / "text_extraction" / "ui"
HTMX_FILE = UI_PACKAGE / "static" / "htmx-2.0.11.min.js"
# SHA-256 of package/dist/htmx.min.js of
# https://registry.npmjs.org/htmx.org/-/htmx.org-2.0.11.tgz, whose npm integrity is
# sha512-Thx/WtpeOQqSrqBCw/A1cwGJGg4UrVa3+sW0GmrM3p4gJgO89ecH4qtbnyzDDWFvBTqjnIMCgELTNt636dtamA==
HTMX_SHA256: Final[str] = "d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717"

SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}
BANNER = (
    "This service runs without authentication: anyone who can reach it can use the API and this UI."
)
NOT_VALID = "This key is not valid."
UI_SETTINGS = (
    ("UI_TREE_MAX_URLS", 5_000, 100, 50_000),
    ("UI_BATCH_FILE_MAX_BYTES", 10_485_760, 1_024, 104_857_600),
    ("UI_BATCH_FILE_MAX_ROWS", 100_000, 10, 1_000_000),
    ("UI_IMAGE_MAX_BYTES", 1_000_000, 10_000, 20_000_000),
)


def login(client: TestClient, key: str | None = USER_KEY, **headers: str) -> Any:
    sent = {**HX, **headers}
    if key is not None:
        sent["Authorization"] = f"Bearer {key}"
    return client.post("/ui/run/login", headers=sent)


def test_ui_settings_defaults_and_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    for name, *_ in UI_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("UI_ENABLED", raising=False)
    settings = load_settings()
    assert settings.ui_enabled is False
    for name, default, _, _ in UI_SETTINGS:
        assert getattr(settings, name.lower()) == default

    for name, _, lowest, highest in UI_SETTINGS:
        for accepted in (lowest, highest):
            monkeypatch.setenv(name, str(accepted))
            assert getattr(load_settings(), name.lower()) == accepted
        for refused in (lowest - 1, highest + 1):
            monkeypatch.setenv(name, str(refused))
            with pytest.raises(ConfigError, match=name.lower()):
                load_settings()
        monkeypatch.delenv(name)

    monkeypatch.setenv("UI_ENABLED", "true")
    assert load_settings().ui_enabled is True


def test_disabled_404_and_no_jinja_import() -> None:
    with running(ui_enabled=False) as client:
        assert client.get("/ui").status_code == 404
        assert client.get("/ui/static/ui.js").status_code == 404
        assert login(client).status_code == 404

    script = (
        "import sys\n"
        "from fakeredis import FakeAsyncRedis\n"
        "from prometheus_client import CollectorRegistry\n"
        "from text_extraction.app import create_public_app\n"
        "from text_extraction.settings import Settings\n"
        "create_public_app(Settings(auth_mode='none'), FakeAsyncRedis(), CollectorRegistry())\n"
        "print(sorted(name for name in sys.modules if name.split('.')[0] == 'jinja2'\n"
        "             or name.startswith('text_extraction.ui')))\n"
    )
    found = subprocess.run(  # noqa: S603 - our own interpreter and script
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert found.stdout.strip() == "[]"


def test_page_and_static_served_without_key(client: TestClient) -> None:
    page = client.get("/ui")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    for name in ("htmx-2.0.11.min.js", "ui.js", "ui.css"):
        assert f'"/ui/static/{name}"' in page.text

    for name, kind in (
        ("htmx-2.0.11.min.js", "javascript"),
        ("ui.js", "javascript"),
        ("ui.css", "text/css"),
    ):
        static = client.get(f"/ui/static/{name}")
        assert static.status_code == 200, name
        assert kind in static.headers["content-type"]
        assert static.content == (UI_PACKAGE / "static" / name).read_bytes()


def test_login_page_under_api_key_mode(client: TestClient) -> None:
    page = client.get("/ui").text

    assert 'id="login-form"' in page
    assert 'id="app-shell"' not in page
    assert page.count("<form") == 1
    assert "/ui/run/" not in page.replace("/ui/run/login", "")
    inputs = re.findall(r"<input[^>]*>", page)
    assert len(inputs) == 3  # the API key, the LLM key and the ASR key
    for tag in inputs:
        assert " name=" not in tag
        assert 'autocomplete="off"' in tag
    assert BANNER not in page


def test_login_ok_renders_app_with_principal(client: TestClient) -> None:
    answer = login(client)

    assert answer.status_code == 200
    assert 'id="app-shell"' in answer.text
    assert "alice" in answer.text
    assert '<span class="role">user</span>' in answer.text
    assert "Log out" in answer.text
    assert NOT_VALID not in answer.text
    assert USER_KEY not in answer.text
    assert "hx-trigger" not in answer.headers


@pytest.mark.parametrize("key", [None, "wrong-key-0123456789", f"{USER_KEY}x"])
def test_login_wrong_key_renders_nothing_else(client: TestClient, key: str | None) -> None:
    answer = login(client, key)

    assert answer.status_code == 200
    assert NOT_VALID in answer.text
    assert 'id="app-shell"' not in answer.text
    assert "Log out" not in answer.text
    assert "alice" not in answer.text
    assert answer.headers["hx-trigger"] == "ui-login-failed"
    if key is not None:
        assert key not in answer.text
    # Only the login box comes back, ready for another key.
    assert 'id="login-form"' in answer.text


def test_login_other_refusal_keeps_the_key() -> None:
    with running(rate_limit_per_minute=1) as client:
        assert 'id="app-shell"' in login(client).text
        answer = login(client)

    assert answer.status_code == 200
    assert 'id="app-shell"' not in answer.text
    assert NOT_VALID not in answer.text
    assert "Rate limit exceeded." in answer.text
    assert "hx-trigger" not in answer.headers


def test_auth_none_renders_app_with_banner() -> None:
    with running(auth_mode="none") as client:
        page = client.get("/ui").text

    assert BANNER in page
    assert 'id="app-shell"' in page
    assert "anonymous" in page
    assert 'id="login-form"' not in page


def test_security_headers_on_every_ui_answer(client: TestClient) -> None:
    answers = {
        "page": client.get("/ui"),
        "static": client.get("/ui/static/ui.js"),
        "run": login(client),
        "refused": client.post("/ui/run/login", headers=AUTH),
        "missing": client.get("/ui/static/missing.js"),
        "unknown": client.get("/ui/nope"),
        "too_large": client.post("/ui/run/login", headers=HX, content=b"x" * 70_000),
    }
    assert answers["page"].status_code == 200
    assert answers["run"].status_code == 200
    assert answers["refused"].status_code == 403
    assert answers["missing"].status_code == answers["unknown"].status_code == 404
    assert answers["too_large"].status_code == 413
    for name, answer in answers.items():
        for header, value in SECURITY_HEADERS.items():
            assert answer.headers.get(header) == value, (name, header)
    assert answers["static"].headers["cache-control"] == "public, max-age=3600"
    for name in ("page", "run", "refused", "unknown", "too_large"):
        assert answers[name].headers["cache-control"] == "no-store", name

    # The REST API keeps its own headers.
    api = client.get("/v1/info", headers=AUTH)
    assert "content-security-policy" not in api.headers


def test_unhandled_error_under_ui_keeps_the_headers_and_is_logged() -> None:
    with running() as client, capture_logs() as logs:
        app = cast(FastAPI, client.app)

        @app.get("/ui/test-boom")
        async def boom() -> None:
            raise RuntimeError("boom")

        answer = client.get("/ui/test-boom")

    assert answer.status_code == 500
    assert answer.headers["content-type"].startswith("text/plain")
    for header, value in SECURITY_HEADERS.items():
        assert answer.headers.get(header) == value, header
    assert [log["event"] for log in logs].count("unhandled_exception") == 1


def test_not_in_openapi(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert not [path for path in paths if path.startswith("/ui")]


def test_vendored_htmx_checksum() -> None:
    assert hashlib.sha256(HTMX_FILE.read_bytes()).hexdigest() == HTMX_SHA256


class _TagCheck(HTMLParser):
    """Collects what the CSP would refuse: inline scripts, styles and event handlers."""

    def __init__(self) -> None:
        super().__init__()
        self.problems: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = {name for name, _ in attrs}
        if tag == "script":
            self._in_script = True
            if "src" not in names:
                self.problems.append("script without src")
        if tag == "style":
            self.problems.append("style element")
        for name in names:
            if name == "style":
                self.problems.append(f"style attribute on {tag}")
            if name.startswith(("hx-on", "data-hx-on")):
                self.problems.append(f"{name} on {tag}")
            if name.startswith("on"):
                self.problems.append(f"event handler {name} on {tag}")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script and data.strip():
            self.problems.append("inline script text")


def test_no_inline_script_or_style_or_hx_on() -> None:
    templates = sorted((UI_PACKAGE / "templates").rglob("*.html"))
    assert templates
    for template in templates:
        text = template.read_text()
        assert "hx-on" not in text, template.name
        assert "style=" not in text, template.name
        check = _TagCheck()
        check.feed(text)
        assert check.problems == [], template.name


def test_htmx_config_meta(client: TestClient) -> None:
    page = client.get("/ui").text
    found = re.search(r"<meta name=\"htmx-config\" content='([^']*)'>", page)
    assert found is not None

    assert json.loads(found.group(1)) == {
        "allowEval": False,
        "allowScriptTags": False,
        "includeIndicatorStyles": False,
        "selfRequestsOnly": True,
        "historyEnabled": False,
        "historyCacheSize": 0,
    }


@pytest.mark.parametrize("auth_mode", ["api_key", "none"])
def test_run_requires_hx_request_and_same_origin(auth_mode: str) -> None:
    # One call a minute: a refusal must not reach the inner route (and count), or the allowed
    # login after them would be rate limited.
    with running(auth_mode=auth_mode, rate_limit_per_minute=1) as client:
        refused = [
            client.post("/ui/run/login", headers=AUTH),
            client.post("/ui/run/login", headers={**AUTH, "HX-Request": "false"}),
            login(client, Origin="https://evil.example"),
            login(client, Origin="null"),
            login(client, Origin="http://testserver:8080"),
            login(client, Origin="http://testserver.evil.example"),
            login(client, Origin="http://testserver", Host="evil.example"),
        ]
        for answer in refused:
            assert answer.status_code == 403
            assert answer.headers["content-type"].startswith("text/plain")
            assert "app-shell" not in answer.text

        same_origin = login(client, Origin="http://testserver")

    assert same_origin.status_code == 200
    assert 'id="app-shell"' in same_origin.text


@pytest.mark.parametrize("origin", [None, "http://testserver", "https://testserver"])
def test_run_accepts_the_pages_own_origin(client: TestClient, origin: str | None) -> None:
    headers = {} if origin is None else {"Origin": origin}

    assert 'id="app-shell"' in login(client, **headers).text


def test_forward_passes_only_listed_headers() -> None:
    with running() as client:
        app = cast(FastAPI, client.app)

        @app.get("/v1/test-echo")
        async def echo(request: Request) -> JSONResponse:
            return JSONResponse(
                {
                    "headers": dict(request.headers),
                    "query": dict(request.query_params),
                    "body": (await request.body()).decode(),
                    "method": request.method,
                }
            )

        app.add_api_route("/v1/test-echo", echo, methods=["POST"])

        @app.get("/v1/test-text")
        async def text() -> PlainTextResponse:
            return PlainTextResponse("plain")

        @app.post("/ui/test-forward")
        async def outer(request: Request) -> JSONResponse:
            got = await forward(request, "POST", "/v1/test-echo", json={"a": 1}, params={"q": "1"})
            plain = await forward(request, "GET", "/v1/test-text")
            return JSONResponse(
                {
                    "status": got.status,
                    "body": got.body,
                    "content_type": got.content_type,
                    "plain": [plain.body, plain.content.decode(), plain.content_type],
                    "outer_trace": request.scope["state"]["trace_id"],
                }
            )

        sent = {
            **AUTH,
            "X-LLM-Api-Key": "sk-llm-caller",
            "X-ASR-Api-Key": "sk-asr-caller",
            "Cookie": "session=abc",
            "x-client-trace-id": "outer-trace-7",
            "b3": "80f198ee56343ba8-e457b5a2e4d86bd1-1",
            "X-B3-Sampled": "1",
            "X-Other": "secret",
        }
        answer = client.post("/ui/test-forward", headers=sent).json()
        untraced = client.post("/ui/test-forward", headers=AUTH).json()

    assert answer["status"] == 200
    assert answer["content_type"].startswith("application/json")
    inner = answer["body"]
    assert inner["method"] == "POST"
    assert inner["query"] == {"q": "1"}
    assert json.loads(inner["body"]) == {"a": 1}
    headers = inner["headers"]
    assert headers["authorization"] == f"Bearer {USER_KEY}"
    assert headers["x-llm-api-key"] == "sk-llm-caller"
    assert headers["x-asr-api-key"] == "sk-asr-caller"
    assert headers["x-client-trace-id"] == "outer-trace-7"
    assert headers["b3"] == "80f198ee56343ba8-e457b5a2e4d86bd1-1"
    assert headers["x-b3-sampled"] == "1"
    assert "cookie" not in headers
    assert "x-other" not in headers
    assert answer["plain"] == [None, "plain", "text/plain; charset=utf-8"]

    # Without a trace id of the caller's, the inner request carries the outer one's.
    assert untraced["body"]["headers"]["x-client-trace-id"] == untraced["outer_trace"]
    assert "x-llm-api-key" not in untraced["body"]["headers"]


def test_ui_sets_no_cookie(client: TestClient) -> None:
    answers = [
        client.get("/ui"),
        client.get("/ui/static/htmx-2.0.11.min.js"),
        login(client),
        login(client, "wrong-key-0123456789"),
        client.post("/ui/run/login"),
    ]

    for answer in answers:
        assert "set-cookie" not in answer.headers
    assert not client.cookies


def test_ui_principal_authenticates_and_counts_one_hit() -> None:
    with running(rate_limit_per_minute=2) as client:
        app = cast(FastAPI, client.app)

        @app.get("/ui/test-principal")
        async def view(principal: Annotated[Principal, Depends(ui_principal)]) -> dict[str, str]:
            return {"name": principal.name}

        missing = client.get("/ui/test-principal")
        wrong = client.get("/ui/test-principal", headers={"Authorization": "Bearer wrong"})
        first = client.get("/ui/test-principal", headers=AUTH)
        second = client.get("/ui/test-principal", headers=AUTH)
        third = client.get("/ui/test-principal", headers=AUTH)

    assert missing.status_code == wrong.status_code == 401
    assert first.json() == second.json() == {"name": "alice"}
    assert third.status_code == 429
    assert third.json()["error"]["code"] == "rate_limited"


def test_run_body_limits() -> None:
    with running(request_max_body_bytes=1024, ui_batch_file_max_bytes=2048) as client:
        over_default = client.post("/ui/run/login", headers=HX, content=b"x" * 1025)
        file_route = client.post("/ui/run/file", headers=HX, content=b"x" * 1025)
        over_batch_file = client.post(
            "/ui/run/batch-file", headers=HX, content=b"x" * (2048 + 65_536 + 1)
        )
        batch_file = client.post("/ui/run/batch-file", headers=HX, content=b"x" * 2049)

    assert over_default.status_code == 413
    # Neither route exists before Task 8 and 9: within their limits they reach the router.
    assert file_route.status_code == 404
    assert over_batch_file.status_code == 413
    assert batch_file.status_code == 404


def test_refused_before_the_body_is_read() -> None:
    """The guard answers a foreign or non-htmx request to /ui/run before anything reads the body,
    also for a route with ``File`` and ``Form`` parameters (FastAPI parses those before any
    dependency)."""
    with running() as client:
        app = cast(FastAPI, client.app)
        handled: list[str] = []

        @app.post("/ui/run/test-upload")
        async def upload(
            file: Annotated[UploadFile, File()], note: Annotated[str, Form()]
        ) -> dict[str, str]:
            handled.append(note)
            return {"name": file.filename or ""}

        body = (
            b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.csv"\r\n\r\n'
            b'https://example.org/\r\n--b\r\nContent-Disposition: form-data; name="note"\r\n\r\n'
            b"hi\r\n--b--\r\n"
        )
        base = [
            (b"host", b"testserver"),
            (b"content-type", b"multipart/form-data; boundary=b"),
            (b"content-length", str(len(body)).encode()),
        ]

        async def call(headers: list[tuple[bytes, bytes]]) -> tuple[int, bool]:
            read = False
            sent: list[Any] = []

            async def receive() -> dict[str, Any]:
                nonlocal read
                read = True
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(message: Any) -> None:
                sent.append(message)

            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/ui/run/test-upload",
                "raw_path": b"/ui/run/test-upload",
                "root_path": "",
                "query_string": b"",
                "headers": base + headers,
                "client": ("127.0.0.1", 1),
                "server": ("testserver", 80),
            }
            await app(scope, receive, send)
            return sent[0]["status"], read

        async def calls() -> list[tuple[int, bool]]:
            return [
                await call([(b"hx-request", b"true"), (b"origin", b"https://evil.example")]),
                await call([(b"origin", b"http://testserver")]),
                await call([(b"hx-request", b"true"), (b"origin", b"http://testserver")]),
            ]

        outcomes = client.portal.call(calls)  # type: ignore[union-attr]

    assert outcomes == [(403, False), (403, False), (200, True)]
    assert handled == ["hi"]


@pytest.mark.parametrize(
    "path",
    [
        "http://evil.example/v1/info",
        "//evil.example/v1/info",
        "https:/v1/info",
        "v1/info",
        "/mcp",
        "/v1",
        "/v1/jobs/../../mcp",
        "/v1/jobs/./x",
        "/v1/jobs/%2e%2e/x",
        "/v1/jobs/a%2Fb",
        "/v1/jobs\\..\\mcp",
        "/v1/jobs/a?x=1",
        "/v1/jobs/a#x",
        "/v1/jobs//a",
        "/v1/jobs/a b",
        "/v1/jobs/a\nb",
        "/v1/jobs/\u00e4",
    ],
)
def test_forward_refuses_paths_outside_v1(path: str) -> None:
    with running() as client:
        app = cast(FastAPI, client.app)

        @app.post("/ui/test-forward-path")
        async def outer(request: Request) -> dict[str, str]:
            try:
                await forward(request, "GET", path)
            except ValueError as error:
                return {"refused": str(error)}
            return {"refused": ""}

        with capture_logs() as logs:
            answer = client.post("/ui/test-forward-path").json()

    assert answer["refused"]
    # Only the outer request's access line: nothing was sent.
    assert [log["path"] for log in logs if log["event"] == "request"] == ["/ui/test-forward-path"]


def test_forward_takes_query_parameters_through_params() -> None:
    assert checked_path("/v1/jobs/3f2a/artifacts/screenshot.png") == (
        "/v1/jobs/3f2a/artifacts/screenshot.png"
    )
    with running() as client:
        app = cast(FastAPI, client.app)

        @app.post("/ui/test-forward-params")
        async def outer(request: Request) -> dict[str, Any]:
            got = await forward(request, "GET", "/v1/info", params={"x": "a?b#c"})
            return {"status": got.status, "body": got.body}

        answer = client.post("/ui/test-forward-params", headers=AUTH).json()

    assert answer["status"] == 200
    assert answer["body"]["data"]["principal"] == "alice"
