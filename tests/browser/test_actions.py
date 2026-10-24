"""Page actions (Task 5): each runs in a real Chromium against the local test site."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

import text_extraction.browser_actions
from tests.browser.conftest import Stack, open_stack
from tests.browser.site import (
    ARIA_TEXT,
    ARTICLE_TEXT,
    CHOSEN,
    LAZY_ITEMS,
    OPTED_IN,
    OPTED_OUT,
    REVEALED,
    TYPED_ECHO,
    Site,
)
from text_extraction.browser_options import (
    AcceptCookiesAction,
    Action,
    ClickAction,
    EvaluateAction,
    ExpandAllAction,
    InfiniteScrollAction,
    PressAction,
    ScrollAction,
    SelectAction,
    TypeAction,
    WaitForAction,
)
from text_extraction.browser_render import run_consent
from text_extraction.browser_scripts import CONSENT_RELAY, CONSENT_WAIT, consent_config
from text_extraction.envelope import ErrorCode
from text_extraction.fetch import FetchError
from text_extraction.render import Rendered, RenderOptions

pytestmark = pytest.mark.browser

SettingsFor = Callable[..., Any]
PRIVATE_TEXT = "a-private-typed-text"
SHOWN = f'<p id="shown">{REVEALED}</p>'
SHOWN_EMPTY = '<p id="shown"></p>'
PRIVATE_SCRIPT = "document.title = 'a-private-script'"


def scenario(
    settings_for: SettingsFor,
    site_: Site,
    body: Callable[[Stack], Awaitable[None]],
    **overrides: Any,
) -> None:
    async def run() -> None:
        async with open_stack(settings_for(**overrides), site_) as stack:
            await body(stack)

    asyncio.run(run())


async def allow(url: str) -> None:
    pass


async def render(
    stack: Stack,
    url: str,
    *actions: Action,
    allow_evaluate: bool = False,
    check: Callable[[str], Awaitable[None]] = allow,
    seconds: float = 20,
) -> Rendered:
    options = RenderOptions(actions=actions, allow_evaluate=allow_evaluate)
    deadline = asyncio.get_running_loop().time() + seconds
    return await stack.renderer.render(url, check, options, deadline=deadline)


def test_click_reveals_text(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        plain = await render(stack, f"{site.base}/actions")
        assert SHOWN_EMPTY in plain.html
        rendered = await render(stack, f"{site.base}/actions", ClickAction(selector="#reveal"))
        assert SHOWN in rendered.html
        assert rendered.warnings == ()

    scenario(settings_for, site, body)


def test_type_and_press_enter(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        rendered = await render(
            stack,
            f"{site.base}/actions",
            TypeAction(selector="#field", text="glaciers"),
            PressAction(key="Enter"),
        )
        assert f"{TYPED_ECHO}glaciers" in rendered.html
        again = await render(
            stack,
            f"{site.base}/actions",
            TypeAction(selector="#field", text="volcanoes"),
            PressAction(key="Enter", selector="#field"),
        )
        assert f"{TYPED_ECHO}volcanoes" in again.html

    scenario(settings_for, site, body)


def test_select_option(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        rendered = await render(
            stack, f"{site.base}/actions", SelectAction(selector="#choice", value="blue")
        )
        assert f"{CHOSEN}blue" in rendered.html

    scenario(settings_for, site, body)


def test_expand_all_opens_details_and_aria_toggles(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        rendered = await render(stack, f"{site.base}/actions", ExpandAllAction())
        assert '<details id="more" open="">' in rendered.html
        assert ARIA_TEXT in rendered.html
        assert rendered.warnings == ()
        plain = await render(stack, f"{site.base}/actions")
        assert '<details id="more">' in plain.html
        assert ARIA_TEXT not in plain.html  # the hidden panel is stripped

    scenario(settings_for, site, body)


def test_scroll_loads_the_next_part(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        plain = await render(stack, f"{site.base}/actions")
        assert "Item 11<" not in plain.html
        rendered = await render(stack, f"{site.base}/actions", ScrollAction(times=3, pause_ms=200))
        assert "Item 11<" in rendered.html

    scenario(settings_for, site, body)


def test_infinite_scroll_loads_more_and_stops(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        rendered = await render(
            stack,
            f"{site.base}/actions",
            InfiniteScrollAction(max_scrolls=50, pause_ms=300),
        )
        assert f"Item {LAZY_ITEMS}<" in rendered.html
        assert f"Item {LAZY_ITEMS + 1}<" not in rendered.html
        assert rendered.warnings == ()
        assert loop.time() - started < 15  # it stopped when the page stopped growing, not at 50
        limited = await render(
            stack, f"{site.base}/actions", InfiniteScrollAction(max_scrolls=1, pause_ms=300)
        )
        assert "Item 11<" in limited.html
        assert "Item 21<" not in limited.html

    scenario(settings_for, site, body)


def test_wait_for_an_element(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        rendered = await render(
            stack,
            f"{site.base}/actions",
            ClickAction(selector="#reveal"),
            WaitForAction(selector="#shown:not(:empty)"),
            WaitForAction(ms=100),
        )
        assert SHOWN in rendered.html
        assert rendered.warnings == ()

    scenario(settings_for, site, body)


def test_failed_action_stops_the_rest_with_warning(
    settings_for: SettingsFor, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(text_extraction.browser_actions, "ELEMENT_WAIT_SECONDS", 0.5)

    async def body(stack: Stack) -> None:
        rendered = await render(
            stack,
            f"{site.base}/actions",
            ClickAction(selector="#reveal"),
            ClickAction(selector="#does-not-exist"),
            ExpandAllAction(),
        )
        assert SHOWN in rendered.html
        assert '<details id="more">' in rendered.html  # the third did not run
        assert ARTICLE_TEXT.split(".")[0] not in rendered.html  # still the actions page
        assert rendered.warnings == ("action_1_failed",)

    scenario(settings_for, site, body)


def test_click_navigation_goes_through_check(settings_for: SettingsFor, site: Site) -> None:
    checked: list[str] = []

    async def check(target: str) -> None:
        checked.append(target)

    async def refuse(target: str) -> None:
        raise FetchError(ErrorCode.BLOCKED_BY_ROBOTS, "The site's robots.txt forbids it.")

    async def body(stack: Stack) -> None:
        rendered = await render(
            stack, f"{site.base}/actions", ClickAction(selector="#go"), check=check
        )
        assert checked == [f"{site.base}/static"]
        assert rendered.final_url == f"{site.base}/static"
        assert ARTICLE_TEXT.split(".")[0] in rendered.html
        assert [r.url for r in rendered.responses] == [
            f"{site.base}/actions",
            f"{site.base}/static",
        ]

        site.clear()
        refused = await render(
            stack,
            f"{site.base}/actions",
            ClickAction(selector="#go"),
            ClickAction(selector="#reveal"),
            check=refuse,
        )
        assert site.seen(site.base, "/static") == []  # never sent
        assert refused.final_url == f"{site.base}/actions"
        assert SHOWN_EMPTY in refused.html  # the action after the refusal did not run
        assert refused.warnings == ("action_0_failed",)

    scenario(settings_for, site, body)


def test_evaluate_requires_admin_flag(settings_for: SettingsFor, site: Site) -> None:
    script = (
        "document.getElementById('shown').textContent = 'evaluated';"
        " await new Promise(resolve => setTimeout(resolve, 10));"
    )

    async def body(stack: Stack) -> None:
        with pytest.raises(ValueError, match="evaluate"):
            await render(stack, f"{site.base}/actions", EvaluateAction(script=script))
        rendered = await render(
            stack, f"{site.base}/actions", EvaluateAction(script=script), allow_evaluate=True
        )
        assert '<p id="shown">evaluated</p>' in rendered.html
        assert rendered.warnings == ()
        broken = await render(
            stack,
            f"{site.base}/actions",
            EvaluateAction(script="throw new Error('boom')"),
            ClickAction(selector="#reveal"),
            allow_evaluate=True,
        )
        assert broken.warnings == ("action_0_failed",)
        assert SHOWN_EMPTY in broken.html

    scenario(settings_for, site, body)


def test_accept_cookies_opts_in(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        rendered = await render(stack, f"{site.base}/consent", AcceptCookiesAction())
        assert f'<p id="state">{OPTED_IN}</p>' in rendered.html
        assert "cookiebanner" not in rendered.html
        assert rendered.warnings == ()
        plain = await render(stack, f"{site.base}/consent")
        assert f'<p id="state">{OPTED_OUT}</p>' in plain.html  # without the action: opt-out

    scenario(settings_for, site, body)


def test_accept_cookies_without_a_banner_is_nothing_to_accept(
    settings_for: SettingsFor, site: Site
) -> None:
    async def body(stack: Stack) -> None:
        rendered = await render(
            stack, f"{site.base}/actions", AcceptCookiesAction(), ClickAction(selector="#reveal")
        )
        assert rendered.warnings == ()
        assert SHOWN in rendered.html  # the next action ran

    scenario(settings_for, site, body)


def test_run_consent_reports_what_happened(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        results: dict[str, str] = {}
        for name, path, opt in [
            ("out", "/consent", "optOut"),
            ("in", "/consent", "optIn"),
            ("none_out", "/static", "optOut"),
            ("none_in", "/static", "optIn"),
        ]:
            async with stack.pool.page(RenderOptions()) as pooled:
                await pooled.page.goto(f"{site.base}{path}")
                results[name] = await run_consent(pooled.page, opt)  # type: ignore[arg-type]
                if name in ("out", "in"):
                    state = await pooled.page.text_content("#state")
                    assert state == (OPTED_OUT if name == "out" else OPTED_IN)
        assert results == {
            "out": "done",
            "in": "done",
            "none_out": "nothing",
            "none_in": "nothing",
        }

    scenario(settings_for, site, body)


def _report(lifecycle: str) -> dict[str, Any]:
    return {"type": "report", "state": {"lifecycle": lifecycle}}


@pytest.mark.parametrize(
    ("opt", "messages", "outcome"),
    [
        ("optOut", [{"type": "optOutResult", "result": True}], "done"),
        ("optOut", [_report("optOutSucceeded")], "done"),
        ("optOut", [{"type": "optOutResult", "result": True}, _report("done")], "done"),
        ("optOut", [{"type": "optOutResult", "result": False}], "failed"),
        ("optOut", [_report("optOutFailed")], "failed"),
        ("optOut", [_report("nothingDetected")], "nothingDetected"),
        ("optIn", [_report("optInSucceeded")], "done"),
        ("optIn", [{"type": "optInResult", "result": True}, _report("done")], "done"),
        ("optIn", [{"type": "optInResult", "result": False}, _report("optInFailed")], "failed"),
        ("optOut", [_report("started"), _report("runningOptOut")], "timeout"),
        # The engine ran the other action, or ended without saying how.
        ("optIn", [{"type": "optOutResult", "result": True}, _report("done")], "timeout"),
        ("optOut", [_report("done")], "timeout"),
    ],
)
def test_consent_outcomes_are_read_from_results_and_states(
    settings_for: SettingsFor, site: Site, opt: str, messages: list[dict[str, Any]], outcome: str
) -> None:
    run = (
        "async ({ opt, messages, args }) => {"
        f" const install = {CONSENT_RELAY}; const wait = {CONSENT_WAIT};"
        " install(args); window.autoconsentReceiveMessage = () => {};"
        " const waiting = wait({ opt, timeoutMs: 300, again: false });"
        " for (const message of messages) window.autoconsentSendMessage(message);"
        " return waiting; }"
    )

    async def body(stack: Stack) -> None:
        async with stack.pool.page(RenderOptions()) as pooled:
            await pooled.page.goto(f"{site.base}/static")
            args = {"rulesJson": "[]", "config": consent_config(opt)}
            found = await pooled.page.evaluate(
                run, {"opt": opt, "messages": messages, "args": args}
            )
        assert found == outcome

    scenario(settings_for, site, body)


def test_actions_respect_the_deadline(settings_for: SettingsFor, site: Site) -> None:
    async def body(stack: Stack) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        rendered = await render(
            stack,
            f"{site.base}/actions",
            ScrollAction(times=50, pause_ms=5000),
            ClickAction(selector="#reveal"),
            seconds=5,
        )
        assert loop.time() - started < 5
        assert rendered.warnings == ("action_0_failed",)
        assert "Actions" in rendered.html  # extracted as it was
        assert SHOWN_EMPTY in rendered.html

    scenario(settings_for, site, body)


def test_secrets_stay_out_of_logs_and_warnings(
    settings_for: SettingsFor,
    site: Site,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(text_extraction.browser_actions, "ELEMENT_WAIT_SECONDS", 0.5)

    async def body(stack: Stack) -> None:
        rendered = await render(
            stack,
            f"{site.base}/actions",
            TypeAction(selector="#field", text=PRIVATE_TEXT),
            EvaluateAction(script=PRIVATE_SCRIPT),
            ClickAction(selector="#does-not-exist"),
            allow_evaluate=True,
        )
        assert rendered.warnings == ("action_2_failed",)

    scenario(settings_for, site, body, browser_idle_wait_seconds=1)
    output = capsys.readouterr()
    assert PRIVATE_TEXT not in output.out + output.err
    assert "a-private-script" not in output.out + output.err
