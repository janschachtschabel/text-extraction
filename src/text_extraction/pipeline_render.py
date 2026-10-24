"""Helpers of the pipeline's render path: the render options of a request, the checks of a URL
the browser goes to, and how a render is judged as a fetch.

``Pipeline`` (``pipeline.py``) runs the render path itself (compliance, the outbound gate's turn,
conversion, cache); these are the pure parts of it. Nothing here imports a browser driver.
"""

import hashlib
from ipaddress import ip_address

from text_extraction.envelope import ErrorCode
from text_extraction.extract_models import ExtractRequest
from text_extraction.fetch import FetchError, FetchResult
from text_extraction.netguard import InvalidUrlError, is_public_address, validate_url
from text_extraction.quality import THIN_PAGE_BELOW_WORDS, Quality
from text_extraction.render import (
    CookieData,
    MainResponse,
    Rendered,
    RenderOptions,
    contacted_challenge_host,
    site,
)
from text_extraction.settings import Settings

# Answers whose body may be a bot challenge that unblock mode can get past (B5, B14).
CHALLENGE_STATUSES = frozenset({403, 429, 503})
# The renderer's warning for a challenge that was still there when the wait ended (B14).
CHALLENGE_NOT_CLEARED = "challenge_not_cleared"


def blocked_resources(settings: Settings) -> frozenset[str]:
    """``BROWSER_BLOCKED_RESOURCES`` as a set (the setting is already checked and lower case)."""
    return frozenset(name for name in settings.browser_blocked_resources.split(",") if name)


def caller_cookies(req: ExtractRequest, host: str) -> list[CookieData]:
    """The request's cookies; one without a domain gets the request host."""
    return [
        CookieData(
            name=cookie.name,
            value=cookie.value,
            domain=cookie.domain or host,
            path=cookie.path,
            secure=cookie.secure,
            http_only=cookie.http_only,
        )
        for cookie in req.cookies
    ]


def render_options(
    req: ExtractRequest,
    cookies: list[CookieData],
    *,
    admin: bool,
    unblock: bool,
    blocked: frozenset[str],
) -> RenderOptions:
    """How to render ``req``. The caller's headers go to the request URL's site only (B2 step
    5); an extraction render in standard mode leaves out the ``blocked`` resource types, a
    screenshot or unblock render loads everything (B3). ``evaluate`` actions run for admins."""
    return RenderOptions(
        actions=tuple(req.actions),
        cookies=tuple(cookies),
        headers=dict(req.headers),
        header_host=site(req.url),
        screenshot=req.screenshot,
        full_page=req.full_page,
        allow_evaluate=admin,
        unblock=unblock,
        blocked_resources=frozenset() if unblock or req.screenshot else blocked,
    )


def check_target(url: str) -> None:
    """A URL the browser goes to must be a plain http(s) URL: ``invalid_url`` otherwise."""
    try:
        validate_url(url)
    except InvalidUrlError as error:
        raise FetchError(ErrorCode.INVALID_URL, str(error)) from None


def check_address(host: str, settings: Settings) -> None:
    """``blocked_url`` for an IP literal that is not public (unless private networks are
    allowed). A name passes: the egress proxy refuses it if it resolves to such an address."""
    if settings.fetch_allow_private_networks:
        return
    try:
        address = ip_address(host)
    except ValueError:
        return
    if not is_public_address(address):
        raise FetchError(ErrorCode.BLOCKED_URL, "The target address is not allowed.")


def as_fetched(url: str, rendered: Rendered, content: bytes, elapsed_ms: int) -> FetchResult:
    """The render as a fetch of ``url``: the last document answer's status and headers, and the
    rendered HTML (``content``, UTF-8) as its body, so ``raw_sha256`` is the HTML's hash."""
    last = rendered.responses[-1]
    return FetchResult(
        url=url,
        final_url=rendered.final_url,
        status=last.status,
        headers=dict(last.headers),
        content=content,
        content_type=last.content_type,
        charset="utf-8",
        elapsed_ms=elapsed_ms,
        # Every URL the render arrived at after the request URL, as a fetch lists its hops.
        redirects=[hop for answer in rendered.responses for hop in (*answer.redirects, answer.url)][
            1:
        ],
        not_modified=False,
        raw_sha256=hashlib.sha256(content).hexdigest(),
    )


def challenged_by_hosts(quality: Quality, rendered: Rendered) -> bool:
    """B12: a thin page whose render contacted a challenge service is a bot challenge, though
    ``assess`` did not see one in the page."""
    return (
        quality.word_count < THIN_PAGE_BELOW_WORDS
        and not quality.bot_challenge
        and contacted_challenge_host(rendered.hosts)
    )


def answers_for_gate(rendered: Rendered, *, unblock: bool) -> tuple[MainResponse, ...]:
    """The document answers the outbound gate hears of. An unblock render whose challenge
    cleared leaves out the challenge's 403, 429 or 503 before its 2xx document: the host did not
    ask us to back off, its protection asked us to prove we are a browser. A challenge that
    stayed is reported as it answered (real backoff)."""
    answers = rendered.responses
    if not (unblock and rendered.challenge_cleared and answers and 200 <= answers[-1].status < 300):
        return answers
    return (
        *(answer for answer in answers[:-1] if answer.status not in CHALLENGE_STATUSES),
        answers[-1],
    )


def challenge_not_cleared(rendered: Rendered, *, unblock: bool) -> bool:
    """An unblock render that ended on a challenge: ``bot_protection``, whatever the status of
    the last answer (B15)."""
    return unblock and CHALLENGE_NOT_CLEARED in rendered.warnings
