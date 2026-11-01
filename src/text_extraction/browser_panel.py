"""Reading YouTube's new transcript view in a rendered watch page (P13-S3).

``read_transcript_panel`` runs in a render asked for it (``RenderOptions.transcript_panel``),
after the load, the consent step and the idle wait, before the page is read:

1. Consent: an open ``ytd-consent-bump-v2-lightbox tp-yt-paper-dialog`` is declined with its
   first "Alle ablehnen" or "Reject all" button; the step waits until the dialog closes or a
   ``SOCS`` cookie is set. Until then a backdrop takes every click. The outer lightbox is not
   looked at: its own box is 0x0.
2. Detect: without ``PAmodern_transcript_view`` in the page's HTML the video has no new view,
   and nothing is clicked.
3. Open: ``tp-yt-paper-button#expand`` (when visible) shows the description, then the first
   visible transcript button is clicked.
4. Read: the page's own ``POST /youtubei/v1/get_panel`` answer, status 200, at most
   ``max_bytes``. Only an answer to a request for ``PAmodern_transcript_view`` is taken: its
   body (gunzipped when the page sent it with ``Content-Encoding: gzip``, at most 1 MB) must
   name the panel. A body that cannot be read or decoded is accepted; ``parse_panel`` still
   guards the content.

The render sends no request of its own to YouTube: the only request is the page's, started by
the click (CLAUDE.md rule 6). Every wait ends at the step's cap and at ``deadline``. Any failure,
also one of the browser, gives None after a log line with the reason; the caller adds the
warning ``transcript_panel_unavailable``. Logs carry reasons, statuses and sizes only, never a
URL, cookie or text.
"""

import asyncio
import re
import zlib
from typing import Final
from urllib.parse import urlsplit

import structlog
from playwright.async_api import Locator, Page, Request, Response

from text_extraction.browser_driver import DRIVER_ERRORS, DRIVER_TIMEOUTS
from text_extraction.media.panel import TRANSCRIPT_PANEL_UNAVAILABLE

__all__ = ["TRANSCRIPT_PANEL_UNAVAILABLE", "read_transcript_panel"]

_log = structlog.get_logger(__name__)

_NEW_VIEW: Final = "PAmodern_transcript_view"
_PANEL_PATH: Final = "/youtubei/v1/get_panel"
_CONSENT_DIALOG: Final = "ytd-consent-bump-v2-lightbox tp-yt-paper-dialog"
_REJECT: Final = re.compile(r"Alle ablehnen|Reject all", re.IGNORECASE)
_CONSENT_COOKIE: Final = "SOCS"
_EXPAND: Final = "tp-yt-paper-button#expand"
_TRANSCRIPT_BUTTON: Final = "ytd-video-description-transcript-section-renderer button"
# The steps' own caps, in seconds.
_CONSENT_SECONDS: Final = 5.0
_BUTTON_SECONDS: Final = 5.0
_ANSWER_SECONDS: Final = 15.0
_POLL_SECONDS: Final = 0.1
# Reading a request's body or headers: a driver error, or a body that is not base64.
_BODY_ERRORS: Final[tuple[type[Exception], ...]] = (*DRIVER_ERRORS, ValueError)
# A request body larger than this, decompressed, is not looked into.
_MAX_REQUEST_BYTES: Final = 1024 * 1024
# A Content-Length longer than this is over any cap (and too long for a quick ``int``).
_MAX_LENGTH_DIGITS: Final = 15
# True while the consent dialog is in the page and open.
_DIALOG_OPEN: Final = """(selector) => {
  const dialog = document.querySelector(selector);
  return dialog !== null && dialog.hasAttribute('opened');
}"""


class _Unavailable(Exception):  # noqa: N818 - an outcome, not a bug
    """The panel cannot be read; ``reason`` goes into the log."""

    def __init__(self, reason: str, **details: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


async def read_transcript_panel(page: Page, *, deadline: float, max_bytes: int) -> bytes | None:
    """The body of the page's ``get_panel`` answer, or None (logged) when there is none to read.
    ``deadline`` is ``loop.time()`` based; never raises a driver error."""
    try:
        await _decline_consent(page, _end(deadline, _CONSENT_SECONDS))
        if _NEW_VIEW not in await page.content():
            raise _Unavailable("no_new_view")
        button = await _transcript_button(page, _end(deadline, _BUTTON_SECONDS))
        body = await _panel_answer(page, button, _end(deadline, _ANSWER_SECONDS), max_bytes)
    except _Unavailable as unavailable:
        _log.info("transcript_panel_unavailable", reason=unavailable.reason, **unavailable.details)
        return None
    except DRIVER_ERRORS as error:  # the page closed or navigated meanwhile
        _log.info("transcript_panel_unavailable", reason="browser", error=type(error).__name__)
        return None
    _log.info("transcript_panel_read", bytes=len(body))
    return body


def _end(deadline: float, cap: float) -> float:
    """The end of a step: its own cap from now, but never after the render's deadline."""
    now = asyncio.get_running_loop().time()
    end = min(now + cap, deadline)
    if end <= now:
        raise _Unavailable("no_time")
    return end


def _timeout_ms(end: float) -> float:
    """What is left until ``end``, as a driver timeout (never 0: that means none)."""
    return max(end - asyncio.get_running_loop().time(), 0.001) * 1000


async def _decline_consent(page: Page, end: float) -> None:
    """Decline an open consent dialog and wait until it closes or the choice is stored."""
    if not await page.evaluate(_DIALOG_OPEN, _CONSENT_DIALOG):
        return
    reject = page.locator(_CONSENT_DIALOG).locator("button").filter(has_text=_REJECT).first
    try:
        await reject.click(timeout=_timeout_ms(end))
    except DRIVER_TIMEOUTS:
        raise _Unavailable("consent") from None
    while await page.evaluate(_DIALOG_OPEN, _CONSENT_DIALOG) and not await _consent_stored(page):
        if asyncio.get_running_loop().time() + _POLL_SECONDS >= end:
            raise _Unavailable("consent")
        await asyncio.sleep(_POLL_SECONDS)


async def _consent_stored(page: Page) -> bool:
    return any(cookie.get("name") == _CONSENT_COOKIE for cookie in await page.context.cookies())


async def _transcript_button(page: Page, end: float) -> Locator:
    """Show the description, then the first visible transcript button."""
    expand = page.locator(_EXPAND).filter(visible=True).first
    button = page.locator(_TRANSCRIPT_BUTTON).filter(visible=True).first
    try:
        if await expand.count():
            await expand.click(timeout=_timeout_ms(end))
        await button.wait_for(state="visible", timeout=_timeout_ms(end))
    except DRIVER_TIMEOUTS:
        raise _Unavailable("no_button") from None
    return button


async def _panel_answer(page: Page, button: Locator, end: float, max_bytes: int) -> bytes:
    """Click ``button`` and read the ``get_panel`` answer the page's own request gets."""
    try:
        async with page.expect_response(_is_panel_answer, timeout=_timeout_ms(end)) as info:
            await button.click(timeout=_timeout_ms(end))
        response = await info.value
    except DRIVER_TIMEOUTS:
        raise _Unavailable("no_answer") from None
    if response.status != 200:
        raise _Unavailable("status", status=response.status)
    length = response.headers.get("content-length", "")
    if length.isdecimal() and (len(length) > _MAX_LENGTH_DIGITS or int(length) > max_bytes):
        raise _Unavailable("too_large")
    try:
        async with asyncio.timeout_at(end):
            body = await response.body()
    except TimeoutError:
        raise _Unavailable("no_answer") from None
    if len(body) > max_bytes:
        raise _Unavailable("too_large", bytes=len(body))
    return body


def _is_panel_answer(response: Response) -> bool:
    """A ``get_panel`` answer to a request for the transcript view (or one whose body cannot be
    read: then the path decides)."""
    if urlsplit(response.url).path != _PANEL_PATH:
        return False
    body = _request_body(response.request)
    return body is None or _NEW_VIEW.encode() in body


def _request_body(request: Request) -> bytes | None:
    """The request's body, decoded; None when it cannot be read or decoded."""
    try:
        body = request.post_data_buffer
        encoding = request.headers.get("content-encoding", "").strip().lower()
    except _BODY_ERRORS:
        return None
    return decode_request_body(body, encoding)


def decode_request_body(body: bytes | None, encoding: str) -> bytes | None:
    """``body`` without its ``Content-Encoding`` (identity or gzip), at most
    ``_MAX_REQUEST_BYTES``; None when there is none, the encoding is another or decoding fails."""
    if body is None:
        return None
    if encoding in ("", "identity"):
        return body
    if encoding != "gzip":
        return None
    inflate = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)
    try:
        decoded = inflate.decompress(body, _MAX_REQUEST_BYTES + 1)
    except zlib.error:
        return None
    if len(decoded) > _MAX_REQUEST_BYTES or not inflate.eof:
        return None
    return decoded
