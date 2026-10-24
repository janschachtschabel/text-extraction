"""Parts of unblock mode without a browser: how it tells a bot challenge and a checkbox widget
(B12, B14), and how a cleared challenge's cookies keep their scope for the reuse."""

import asyncio
from collections.abc import Sequence
from typing import Any, cast

import pytest
from patchright.async_api import Error as PatchrightError

from tests.fakes import ARTICLE
from text_extraction import browser_unblock
from text_extraction.browser import _cookie_param
from text_extraction.browser_render import Loaded, cookie_data
from text_extraction.browser_unblock import ChallengeWait, is_challenge, is_checkbox_widget
from text_extraction.settings import Settings

CHALLENGE = """<!doctype html><html><head><title>Just a moment...</title></head>
<body><p>Checking your browser before accessing the site.</p></body></html>"""
THIN = """<!doctype html><html><head><title>Contact</title></head>
<body><p>Write to us, we answer within a day.</p></body></html>"""
# What Chrome itself contacts in the background (seen with Patchright's Chromium).
CHROME_BACKGROUND = frozenset(
    {"www.google.com", "accounts.google.com", "android.clients.google.com"}
)


def test_challenge_pages_and_thin_pages_behind_a_challenge_host() -> None:
    assert is_challenge(CHALLENGE, frozenset())
    assert not is_challenge(ARTICLE, frozenset({"challenges.cloudflare.com"}))  # long page
    assert is_challenge(THIN, frozenset({"challenges.cloudflare.com"}))
    assert is_challenge(THIN, frozenset({"newassets.hcaptcha.com"}))


def test_chromes_background_hosts_are_no_challenge_hosts() -> None:
    assert not is_challenge(THIN, CHROME_BACKGROUND)
    assert not is_challenge(ARTICLE, CHROME_BACKGROUND)


@pytest.mark.parametrize(
    ("url", "widget"),
    [
        ("https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/", True),
        ("https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html#frame=checkbox", True),
        (
            "https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html#frame=challenge",
            False,
        ),
        ("https://www.google.com/recaptcha/api2/anchor", False),
        ("https://example.org/turnstile", False),
        ("about:blank", False),
    ],
)
def test_checkbox_widgets(url: str, widget: bool) -> None:
    assert is_checkbox_widget(url) is widget


@pytest.mark.parametrize(
    ("domain", "kept"),
    [
        (".example.org", ".example.org"),  # a domain cookie: also sent to www.example.org
        ("www.example.org", "www.example.org"),  # host-only
    ],
)
def test_cookie_scope_survives_the_round_trip(domain: str, kept: str) -> None:
    # As ``BrowserContext.cookies()`` reports them (Chromium: a leading dot for a domain cookie).
    reported: Any = {
        "name": "cf_clearance",
        "value": "token",
        "domain": domain,
        "path": "/",
        "expires": 2e9,
        "httpOnly": True,
        "secure": True,
        "sameSite": "None",
    }
    cookie = cookie_data(reported)
    assert cookie.domain == kept
    assert cookie.expires == 2e9
    assert _cookie_param(cookie)["domain"] == kept  # what ``add_cookies`` gets back


def test_a_session_cookie_has_no_expiry() -> None:
    reported: Any = {"name": "s", "value": "v", "domain": "example.org", "expires": -1}
    assert cookie_data(reported).expires is None


# --- the challenge wait, with a stand-in page ---------------------------------------------------


class _Element:
    async def bounding_box(self) -> dict[str, float]:
        return {"x": 10.0, "y": 20.0, "width": 300.0, "height": 65.0}

    async def is_visible(self) -> bool:
        return True


class _Locator:
    @property
    def first(self) -> "_Locator":
        return self

    async def bounding_box(self, **_options: Any) -> None:
        raise PatchrightError("no box inside")


class _Frame:
    def __init__(self, url: str, parent: "_Frame | None") -> None:
        self.url = url
        self.parent_frame = parent

    def is_detached(self) -> bool:
        return False

    async def frame_element(self) -> _Element:
        return _Element()

    def locator(self, selector: str) -> _Locator:
        return _Locator()


class _Mouse:
    def __init__(self, fail_down: bool) -> None:
        self.fail_down = fail_down
        self.downs = 0

    async def move(self, x: float, y: float, steps: int = 1) -> None:
        pass

    async def down(self) -> None:
        self.downs += 1
        if self.fail_down:
            raise PatchrightError("the page went away")

    async def up(self) -> None:
        pass


class _Page:
    url = "https://example.org/"

    def __init__(self, *, fail_down: bool = False) -> None:
        main = _Frame("https://example.org/", None)
        widget = "https://challenges.cloudflare.com/turnstile/if/"
        self.frames = [main, _Frame(widget, main), _Frame(widget, main)]
        self.mouse = _Mouse(fail_down)


def test_a_press_that_fails_still_counts_as_the_one_click() -> None:
    page = _Page(fail_down=True)
    wait = ChallengeWait(Settings(auth_mode="none"), is_checkbox_widget)

    clicked = asyncio.run(wait._click_checkbox(cast(Any, page)))

    assert clicked is True
    assert page.mouse.downs == 1  # not again on the second widget frame


def test_the_click_timer_restarts_only_after_a_real_click(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_unblock, "CHECKBOX_AFTER_SECONDS", 0.3)
    monkeypatch.setattr(browser_unblock, "_POLL_SECONDS", 0.1)
    attempts: list[float] = []

    async def scenario() -> Sequence[str]:
        loop = asyncio.get_running_loop()
        started = loop.time()
        wait = ChallengeWait(
            Settings(auth_mode="none", browser_unblock_challenge_wait_seconds=1.0),
            is_checkbox_widget,
        )

        async def click(page: Any) -> bool:
            attempts.append(loop.time() - started)
            return len(attempts) >= 2  # no widget at the first look, then there is one

        monkeypatch.setattr(wait, "_click_checkbox", click)

        async def read() -> str:
            return CHALLENGE

        loaded = Loaded(
            page=cast(Any, _Page()),
            deadline=loop.time() + 10,
            read=read,
            hosts=frozenset,
            raise_kept=lambda: None,
        )
        return await wait(loaded)

    warnings = asyncio.run(scenario())

    # The look that found nothing did not push the next one 0.3 s further.
    assert attempts[1] - attempts[0] < 0.25
    assert "challenge_checkbox_clicked" in warnings
    assert "challenge_not_cleared" in warnings
