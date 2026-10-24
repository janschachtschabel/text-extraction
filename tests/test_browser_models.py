from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from text_extraction.browser_models import (
    MAX_ACTIONS,
    Action,
    ArtifactRef,
    BrowserCookie,
    BrowserSecrets,
    ClickAction,
    EvaluateAction,
    ExpandAllAction,
    InfiniteScrollAction,
    ScreenshotRequest,
    ScrollAction,
    SelectAction,
    TypeAction,
    WaitForAction,
    screenshot_view,
    validate_headers,
)
from text_extraction.compliance import ComplianceReport
from text_extraction.extract_models import ExtractRequest, ExtractResult, Timings

ACTIONS = TypeAdapter(list[Action])
URL = "https://www.example.org/page"


def test_actions_parse_by_type() -> None:
    parsed = ACTIONS.validate_python(
        [
            {"type": "click", "selector": "#more"},
            {"type": "scroll"},
            {"type": "wait_for", "selector": ".loaded"},
            {"type": "wait_for", "ms": 500},
            {"type": "type", "selector": "input", "text": "hello"},
            {"type": "press", "key": "Enter"},
            {"type": "select", "selector": "select", "value": "de"},
            {"type": "expand_all"},
            {"type": "infinite_scroll"},
            {"type": "accept_cookies"},
            {"type": "evaluate", "script": "document.title"},
        ]
    )

    assert [type(action).__name__ for action in parsed] == [
        "ClickAction",
        "ScrollAction",
        "WaitForAction",
        "WaitForAction",
        "TypeAction",
        "PressAction",
        "SelectAction",
        "ExpandAllAction",
        "InfiniteScrollAction",
        "AcceptCookiesAction",
        "EvaluateAction",
    ]
    assert parsed[1] == ScrollAction(times=1, pause_ms=500)
    assert parsed[8] == InfiniteScrollAction(max_scrolls=10, pause_ms=1000)
    assert parsed[0] == ClickAction(selector="#more")
    assert parsed[7] == ExpandAllAction()
    assert parsed[10] == EvaluateAction(script="document.title")


def test_actions_are_frozen_and_closed() -> None:
    action = ClickAction(selector="a")
    with pytest.raises(ValidationError):
        action.selector = "b"
    with pytest.raises(ValidationError):
        ACTIONS.validate_python([{"type": "click", "selector": "a", "extra": 1}])
    with pytest.raises(ValidationError):
        ACTIONS.validate_python([{"type": "teleport"}])


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "click", "selector": ""},
        {"type": "click", "selector": "x" * 501},
        {"type": "scroll", "times": 0},
        {"type": "scroll", "times": 51},
        {"type": "scroll", "pause_ms": 5001},
        {"type": "infinite_scroll", "max_scrolls": 0},
        {"type": "infinite_scroll", "pause_ms": -1},
        {"type": "type", "selector": "a", "text": "x" * 1001},
        {"type": "press", "key": "k" * 33},
        {"type": "select", "selector": "a", "value": "v" * 501},
        {"type": "evaluate", "script": "s" * 10001},
    ],
)
def test_action_limits(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ACTIONS.validate_python([bad])


def test_more_than_20_actions_is_rejected() -> None:
    assert MAX_ACTIONS == 20
    click = {"type": "click", "selector": "a"}

    assert len(ACTIONS.validate_python([click] * 20)) == 20
    assert len(ExtractRequest(url=URL, actions=[click] * 20).actions) == 20  # type: ignore[list-item]
    with pytest.raises(ValidationError):
        ExtractRequest(url=URL, actions=[click] * 21)  # type: ignore[list-item]
    with pytest.raises(ValidationError):
        BrowserSecrets(actions=[click] * 21)  # type: ignore[list-item]


def test_wait_for_needs_exactly_one_of_selector_and_ms() -> None:
    assert WaitForAction(selector=".x").ms is None
    assert WaitForAction(ms=10000).selector is None
    with pytest.raises(ValidationError):
        WaitForAction()
    with pytest.raises(ValidationError):
        WaitForAction(selector=".x", ms=100)
    with pytest.raises(ValidationError):
        WaitForAction(ms=10001)


@pytest.mark.parametrize(
    "name",
    [
        "host",
        "Content-Length",
        "connection",
        "transfer-encoding",
        "te",
        "upgrade",
        "cookie",
        "accept-encoding",
        "user-agent",
        "proxy-connection",
        "proxy-authorization",
    ],
)
def test_refused_header_names(name: str) -> None:
    with pytest.raises(ValueError, match="not allowed"):
        validate_headers({name: "x"})
    with pytest.raises(ValidationError):
        ExtractRequest(url=URL, headers={name: "x"})


def test_headers_come_back_lower_cased() -> None:
    assert validate_headers({"X-Api-Token": "abc", "Accept-Language": "de"}) == {
        "x-api-token": "abc",
        "accept-language": "de",
    }
    assert ExtractRequest(url=URL, headers={"X-Test": "1"}).headers == {"x-test": "1"}


def test_header_names_must_be_tokens_and_not_collide() -> None:
    for name in ("", "bad name", "bad:name", "näme"):
        with pytest.raises(ValueError, match="token"):
            validate_headers({name: "x"})
    with pytest.raises(ValueError, match="twice"):
        validate_headers({"X-A": "1", "x-a": "2"})


@pytest.mark.parametrize("value", ["line\nbreak", "tab\tvalue", "café", "nul\x00", "\x7f"])
def test_header_values_must_be_printable_ascii(value: str) -> None:
    with pytest.raises(ValueError, match="printable ASCII"):
        validate_headers({"x-test": value})


def test_header_count_and_value_length_are_limited() -> None:
    assert len(validate_headers({f"x-h{i}": "v" for i in range(20)})) == 20
    with pytest.raises(ValueError, match="at most 20"):
        validate_headers({f"x-h{i}": "v" for i in range(21)})
    assert validate_headers({"x-long": "v" * 4096})
    with pytest.raises(ValueError, match="4096"):
        validate_headers({"x-long": "v" * 4097})


@pytest.mark.parametrize(
    "options",
    [
        {"actions": [{"type": "expand_all"}]},
        {"cookies": [{"name": "a", "value": "b"}]},
        {"headers": {"x-test": "1"}},
        {"screenshot": True},
        {"full_page": False},
    ],
)
def test_fast_mode_rejects_browser_options(options: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="fast"):
        ExtractRequest(url=URL, mode="fast", **options)

    for mode in ("auto", "browser"):
        assert ExtractRequest(url=URL, mode=mode, **options).mode == mode


def test_modes_and_defaults() -> None:
    request = ExtractRequest(url=URL)

    assert request.mode == "auto"
    assert request.actions == []
    assert request.cookies == []
    assert request.headers == {}
    assert request.screenshot is False
    assert request.full_page is True
    assert request.secrets().empty()
    assert ExtractRequest(url=URL, mode="fast").secrets().empty()
    with pytest.raises(ValidationError):
        ExtractRequest(url=URL, mode="turbo")  # type: ignore[arg-type]


def test_secrets_carry_actions_cookies_and_headers() -> None:
    request = ExtractRequest(
        url=URL,
        mode="browser",
        actions=[{"type": "click", "selector": "a"}],  # type: ignore[list-item]
        cookies=[{"name": "sid", "value": "s3cret"}],  # type: ignore[list-item]
        headers={"X-Token": "t0ken"},
    )

    secrets = request.secrets()

    assert not secrets.empty()
    assert secrets.actions == [ClickAction(selector="a")]
    assert secrets.cookies == [BrowserCookie(name="sid", value="s3cret")]
    assert secrets.headers == {"x-token": "t0ken"}
    assert "s3cret" not in repr(request)
    assert "t0ken" not in repr(request)
    assert "s3cret" not in repr(secrets)
    assert BrowserSecrets().empty()
    assert not BrowserSecrets(headers={"x-a": "b"}).empty()


@pytest.mark.parametrize(
    ("url", "domain", "valid"),
    [
        ("https://www.example.org/", "example.org", True),
        ("https://www.example.org/", ".example.org", True),
        ("https://www.example.org/", "www.example.org", True),
        ("https://www.example.org/", "WWW.Example.ORG", True),
        ("https://www.example.org/", None, True),
        ("https://www.example.org/", "other.org", False),
        ("https://www.example.org/", "org", False),
        ("https://www.example.org/", "ple.org", False),
        ("https://www.example.org/", "sub.www.example.org", False),
        ("https://example.org/", "www.example.org", False),
        ("https://localhost/", "localhost", True),
        ("https://192.0.2.7/", "192.0.2.7", True),
        ("https://192.0.2.7/", "0.2.7", False),
    ],
)
def test_cookie_domain_must_match_request_host(url: str, domain: str | None, valid: bool) -> None:
    cookies: Any = [{"name": "a", "value": "b", "domain": domain}]
    builders = (
        lambda: ExtractRequest(url=url, mode="browser", cookies=cookies),
        lambda: ScreenshotRequest(url=url, cookies=cookies),
    )

    for build in builders:
        if valid:
            assert build().cookies
        else:
            with pytest.raises(ValidationError, match="cookie domain"):
                build()


def test_cookie_limits() -> None:
    BrowserCookie(name="n" * 256, value="v" * 4096)
    for name, value in (("n" * 257, "v"), ("n", "v" * 4097)):
        with pytest.raises(ValidationError):
            BrowserCookie(name=name, value=value)
    many = [{"name": f"c{i}", "value": "v"} for i in range(51)]
    with pytest.raises(ValidationError):
        ExtractRequest(url=URL, cookies=many)  # type: ignore[arg-type]
    cookie = BrowserCookie(name="a", value="b")
    assert (cookie.path, cookie.secure, cookie.http_only, cookie.domain) == (
        "/",
        False,
        False,
        None,
    )


def test_screenshot_request_shares_the_limits_of_extract_request() -> None:
    request = ScreenshotRequest(url=URL)

    assert request.full_page is True
    assert request.actions == []
    assert request.secrets().empty()
    with pytest.raises(ValidationError):
        ScreenshotRequest(url=URL, wait_seconds=61)
    with pytest.raises(ValidationError):
        ScreenshotRequest(url=URL, headers={"host": "x"})
    with pytest.raises(ValidationError):
        ScreenshotRequest(url=URL, unknown=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ScreenshotRequest(url="https://example.org/" + "a" * 3000)


def make_result(**fields: Any) -> ExtractResult:
    values: dict[str, Any] = {
        "url": URL,
        "final_url": URL,
        "upstream_status": 200,
        "content_type": "text/html",
        "mode_used": "browser",
        "attempts": 1,
        "language": "en",
        "word_count": 3,
        "title": "A title",
        "format": "markdown",
        "content": "one two three",
        "quality": None,
        "timings": Timings(total_ms=10, compliance_ms=1, fetch_ms=2, convert_ms=3, render_ms=4),
        "compliance": ComplianceReport(mode="enforce"),
        "metadata": None,
        "links": None,
        "fingerprint": None,
        "cache": "bypass",
        "warnings": ["w"],
    }
    values.update(fields)
    return ExtractResult(**values)


def test_screenshot_view_keeps_the_artifact() -> None:
    artifact = ArtifactRef(
        name="page.png",
        url="https://api.example/v1/artifacts/abc",
        content_type="image/png",
        bytes=1234,
        width=1280,
        height=4000,
        expires_at=datetime(2026, 10, 24, 12, 0, tzinfo=UTC),
    )
    result = make_result(screenshot=artifact, unblocked=True, attempts=2)

    view = screenshot_view(result)

    assert view.screenshot == artifact
    assert view.url == URL
    assert view.final_url == URL
    assert view.upstream_status == 200
    assert view.mode_used == "browser"
    assert view.unblocked is True
    assert view.attempts == 2
    assert view.title == "A title"
    assert view.timings.render_ms == 4
    assert view.compliance.mode == "enforce"
    assert view.warnings == ["w"]
    assert "content" not in view.model_dump()
    assert screenshot_view(make_result()).screenshot is None


def test_old_cached_results_still_validate() -> None:
    old = make_result().model_dump(mode="json")
    for name in ("screenshot", "unblocked"):
        del old[name]
    del old["timings"]["render_ms"]
    old["mode_used"] = "fast"

    result = ExtractResult.model_validate(old)

    assert result.screenshot is None
    assert result.unblocked is False
    assert result.timings.render_ms == 0


def test_secret_values_stay_out_of_repr_and_validation_errors() -> None:
    secret = "s3cr3t-value-123"  # noqa: S105 - a marker, not a credential
    shown = [
        repr(TypeAction(selector="input", text=secret)),
        repr(SelectAction(selector="select", value=secret)),
        repr(EvaluateAction(script=f"alert('{secret}')")),
        repr(BrowserCookie(name="sid", value=secret)),
        repr(
            BrowserSecrets(cookies=[BrowserCookie(name="sid", value=secret)], headers={"x": secret})
        ),
        repr(ExtractRequest(url=URL, mode="browser", headers={"x-token": secret})),
        repr(ScreenshotRequest(url=URL, headers={"x-token": secret})),
    ]
    errors: list[str] = []
    bad_cookie: Any = {"name": "sid", "value": secret, "domain": "other.org"}
    for build in (
        lambda: ExtractRequest(url=URL, mode="browser", headers={"host": secret}),
        lambda: ExtractRequest(url=URL, mode="fast", headers={"x-token": secret}),
        lambda: ExtractRequest(url=URL, mode="browser", cookies=[bad_cookie]),
        lambda: ScreenshotRequest(url=URL, headers={"x-token": "\n" + secret}),
        lambda: ScreenshotRequest(url=URL, cookies=[bad_cookie]),
        lambda: BrowserCookie(name="", value=secret),
        lambda: BrowserSecrets(headers={"cookie": secret}),
        lambda: ACTIONS.validate_python([{"type": "type", "selector": "", "text": secret}]),
    ):
        with pytest.raises(ValidationError) as info:
            build()
        errors.append(str(info.value))

    assert len(errors) == 8
    for text in shown + errors:
        assert secret not in text
