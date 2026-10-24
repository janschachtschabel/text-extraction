"""How unblock mode tells a bot challenge and a checkbox widget (B12, B14), without a browser."""

import pytest

from tests.fakes import ARTICLE
from text_extraction.browser_unblock import is_challenge, is_checkbox_widget

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
