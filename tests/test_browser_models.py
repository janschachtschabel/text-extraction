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
from text_extraction.browser_options import DEFAULT_VIEWPORT, ScreenshotOptions, Viewport
from text_extraction.compliance import ComplianceReport
from text_extraction.extract_models import ExtractRequest, ExtractResult, Timings
from text_extraction.job_models import JobOptions
from text_extraction.screenshot_api import screenshot_options_of

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
    BrowserCookie(name="n" * 256, value="v" * 3840)  # 4096 bytes together, as Chromium allows
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


def _short_id(value: object) -> str:
    return repr(value)[:24]


# What Chromium 1243's add_cookies refuses ("Invalid cookie"), probed against the real browser.
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("sid", "a;b-secret"),  # ends the cookie early
        ("sid", "nul\x00-secret"),
        ("sid", "line\x01-secret"),
        ("sid", "tab\t-secret"),
        ("sid", "cr\r-secret"),
        ("sid", "lf\n-secret"),
        ("sid", "del\x7f-secret"),
        ("sid", " lead-secret"),  # a space at either end
        ("sid", "trail-secret "),
        ("sid", " "),
        ("a;b", "v-secret"),
        ("a=b", "v-secret"),  # the first = ends a name
        ("a\tb", "v-secret"),
        ("a\x00b", "v-secret"),
        ("n\x7f", "v-secret"),
        (" sid", "v-secret"),
        ("sid ", "v-secret"),
        ("n", "x" * 4096),  # name and value over 4096 bytes together
        ("n" * 256, "x" * 3841),
        ("n", "\u00e9" * 2048),  # counted in UTF-8 bytes
    ],
    ids=_short_id,
)
def test_cookies_chromium_refuses_are_rejected_without_their_value(name: str, value: str) -> None:
    # M-5: these made add_cookies fail, which ended as a retryable internal_error.
    with pytest.raises(ValidationError) as info:
        BrowserCookie(name=name, value=value)
    shown = str(info.value)
    assert "secret" not in shown
    assert value.strip() == "" or value not in shown


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("sid", ""),
        ("sid", "two words"),  # a space inside
        ("sid", "com,ma"),
        ("sid", "back\\slash"),
        ("sid", 'quo"te'),
        ("sid", '"quoted value"'),
        ("sid", "a=b=c"),
        ("sid", "caf\u00e9 \u00a0nbsp"),
        ("sid", "{json:[1,2]}"),
        ("a b", "v"),  # a space inside a name
        ("n,m", "v"),
        ("n(m):x", "v"),
        ("n", "x" * 4095),  # 4096 bytes together
        ("n", "\u00e9" * 2047),
    ],
    ids=_short_id,
)
def test_cookies_chromium_accepts_are_accepted(name: str, value: str) -> None:
    cookie = BrowserCookie(name=name, value=value)
    assert (cookie.name, cookie.value) == (name, value)


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
        lambda: BrowserCookie(name="sid", value=f"{secret};"),
        lambda: BrowserSecrets(headers={"cookie": secret}),
        lambda: ACTIONS.validate_python([{"type": "type", "selector": "", "text": secret}]),
    ):
        with pytest.raises(ValidationError) as info:
            build()
        errors.append(str(info.value))

    assert len(errors) == 9
    for text in shown + errors:
        assert secret not in text


@pytest.mark.parametrize("options", [{"screenshot": True}, {"full_page": False}])
def test_job_options_refuse_fast_mode_with_screenshot(options: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        JobOptions(mode="fast", compliance="enforce", **options)

    assert JobOptions(mode="browser", compliance="enforce", **options).mode == "browser"


def test_extract_and_screenshot_requests_share_the_option_limits() -> None:
    too_many = [{"type": "scroll"}] * 21
    for model in (ExtractRequest, ScreenshotRequest):
        with pytest.raises(ValidationError):
            model.model_validate({"url": "https://example.org/", "actions": too_many})
        with pytest.raises(ValidationError):
            model.model_validate({"url": "https://example.org/", "wait_seconds": 61})
        fields = model.model_fields
        assert fields["actions"].repr is False
        assert fields["cookies"].repr is False
        assert fields["headers"].repr is False


def test_screenshot_options_defaults() -> None:
    options = ScreenshotOptions()

    assert options.viewport == DEFAULT_VIEWPORT == Viewport(width=1920, height=1080)
    assert (options.format, options.quality, options.width) == ("png", None, None)
    assert options.effective_quality() == 80
    assert ScreenshotOptions(format="jpeg", quality=55).effective_quality() == 55


def test_screenshot_request_builds_options() -> None:
    body = ScreenshotRequest.model_validate(
        {
            "url": URL,
            "viewport": {"width": 1200, "height": 630},
            "format": "webp",
            "quality": 70,
            "width": 600,
        }
    )

    assert screenshot_options_of(body) == ScreenshotOptions(
        viewport=Viewport(width=1200, height=630), format="webp", quality=70, width=600
    )


def test_screenshot_request_defaults_send_no_options() -> None:
    assert screenshot_options_of(ScreenshotRequest(url=URL)) is None


@pytest.mark.parametrize(
    "body",
    [{"format": "jpeg"}, {"width": 600}, {"viewport": {"width": 800, "height": 600}}],
)
def test_screenshot_request_with_any_option_sends_options(body: dict[str, Any]) -> None:
    options = screenshot_options_of(ScreenshotRequest.model_validate({"url": URL, **body}))

    assert options is not None
    assert options == ScreenshotOptions.model_validate(body)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"format": "png", "quality": 50}, "quality needs format jpeg or webp"),
        ({"quality": 50}, "quality needs format jpeg or webp"),
        (
            {"viewport": {"width": 800, "height": 600}, "width": 801},
            "width cannot be larger than the viewport's width",
        ),
        ({"width": 1921}, "width cannot be larger than the viewport's width"),
        ({"viewport": {"width": 319, "height": 600}}, "greater than or equal to 320"),
        ({"viewport": {"width": 3841, "height": 600}}, "less than or equal to 3840"),
        ({"viewport": {"width": 800, "height": 239}}, "greater than or equal to 240"),
        ({"viewport": {"width": 800, "height": 2161}}, "less than or equal to 2160"),
        ({"width": 15}, "greater than or equal to 16"),
        ({"format": "jpeg", "quality": 0}, "greater than or equal to 1"),
        ({"format": "jpeg", "quality": 101}, "less than or equal to 100"),
        ({"format": "gif"}, "png"),
        ({"dpi": 2}, "Extra inputs are not permitted"),
        ({"viewport": {"width": "800", "height": 600}}, "valid integer"),
    ],
)
def test_screenshot_options_refused(body: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ScreenshotOptions.model_validate(body)
    with pytest.raises(ValidationError, match=message):
        ScreenshotRequest.model_validate({"url": URL, **body})


def test_screenshot_options_accept_the_limits() -> None:
    ScreenshotOptions(viewport=Viewport(width=320, height=240), width=16)
    options = ScreenshotOptions(
        viewport=Viewport(width=3840, height=2160), format="webp", quality=100, width=3840
    )

    assert options.width == 3840


def test_extract_request_takes_screenshot_options_with_a_screenshot() -> None:
    options = {"format": "webp", "quality": 60, "width": 800}
    request = ExtractRequest.model_validate(
        {"url": URL, "screenshot": True, "screenshot_options": options}
    )

    assert request.screenshot_options == ScreenshotOptions.model_validate(options)
    assert ExtractRequest(url=URL).screenshot_options is None


@pytest.mark.parametrize("mode", ["auto", "browser", "fast"])
def test_extract_request_refuses_options_without_screenshot(mode: str) -> None:
    with pytest.raises(ValidationError, match="screenshot_options needs screenshot"):
        ExtractRequest.model_validate(
            {"url": URL, "mode": mode, "screenshot_options": {"format": "jpeg"}}
        )


def test_extract_request_refuses_options_in_fast_mode_with_a_screenshot() -> None:
    with pytest.raises(ValidationError, match="mode fast cannot take"):
        ExtractRequest.model_validate(
            {
                "url": URL,
                "mode": "fast",
                "screenshot": True,
                "screenshot_options": {"format": "jpeg"},
            }
        )
