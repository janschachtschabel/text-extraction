"""A local fake of a YouTube watch page, for the transcript panel steps (``browser_panel.py``).

``serve_watch_site`` puts eight watch pages on a pytest-httpserver:

* ``/watch/new`` offers YouTube's new transcript view (``ytInitialData`` holds
  ``PAmodern_transcript_view``);
* ``/watch/old`` does not;
* ``/watch/failing`` offers it, but its ``get_panel`` request answers 400;
* ``/watch/chunked`` offers it, and its ``get_panel`` answer is the fixture sent in chunks,
  without a ``Content-Length``;
* ``/watch/hidden-reject`` is ``/watch/new`` with a hidden duplicate "Alle ablehnen" first in
  the consent dialog;
* ``/watch/stuck`` offers it, but its consent dialog stays open whatever is clicked;
* ``/watch/decoy`` offers it, but its click first asks ``get_panel`` for another panel, and that
  answer (``DECOY_BODY``) arrives first;
* ``/watch/gzip`` is ``/watch/decoy`` with both request bodies gzip-compressed
  (``Content-Encoding: gzip``), as YouTube's own page sends them.

Each page has the consent dialog as YouTube shows it (``ytd-consent-bump-v2-lightbox``, whose own
box is 0x0, with an inner ``tp-yt-paper-dialog opened``) and a full-page backdrop that takes
every click while the dialog is open. "Alle ablehnen" closes the dialog and sets ``SOCS=x``,
"Alle akzeptieren" sets ``SOCS=accepted``. The transcript button sits in the hidden
``ytd-video-description-transcript-section-renderer`` until ``tp-yt-paper-button#expand`` is
clicked; its click lets the page ``fetch`` ``POST /youtubei/v1/get_panel`` with
``panelId: "PAmodern_transcript_view"`` in the body, answered with the fixture of the parser
tests. Every click is noted in the root element's ``data-clicks``, so a test can read from the
rendered HTML what was clicked, in order.
"""

import gzip as gzip_module
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Final

from pytest_httpserver import HTTPServer
from werkzeug import Response

PANEL_BODY: Final = Path(__file__).parent.parent.joinpath("media/panel/get_panel.json").read_bytes()
PANEL_PATH: Final = "/youtubei/v1/get_panel"
DECOY_BODY: Final = b'{"decoy": "an answer for another panel"}'
TRANSCRIPT_VIEW: Final = "PAmodern_transcript_view"
_DECOY_PANEL: Final = "engagement-panel-comments-section"
DESCRIPTION: Final = "A short video about the water cycle, from the clouds to the sea."
_CLICKS: Final = re.compile(r'data-clicks="([^"]*)"')


def _watch_page(
    *,
    new_view: bool,
    panel_query: str = "",
    consent_closes: bool = True,
    decoy: bool = False,
    gzip: bool = False,
    hidden_reject: bool = False,
) -> str:
    panel_id = TRANSCRIPT_VIEW if new_view else "engagement-panel-searchable-transcript"
    panel = {"engagementPanelSectionListRenderer": {"panelIdentifier": panel_id}}
    initial_data = json.dumps({"engagementPanels": [panel]})
    panel_url = json.dumps(PANEL_PATH + panel_query)
    decoy_url = json.dumps(PANEL_PATH + "?decoy=1")
    hidden = '<button style="display: none">Alle ablehnen</button>' if hidden_reject else ""
    closes = json.dumps(consent_closes)
    flags = json.dumps({"decoy": decoy, "gzip": gzip})
    ids = json.dumps({"view": panel_id, "decoy": _DECOY_PANEL})
    return f"""<!doctype html>
<html lang="de" data-clicks="">
<head><meta charset="utf-8"><title>Der Wasserkreislauf - YouTube</title></head>
<body>
<script>var ytInitialData = {initial_data};</script>
<ytd-consent-bump-v2-lightbox>
  <tp-yt-paper-dialog opened style="position: fixed; top: 20%; left: 30%; width: 40%;
    z-index: 2002; background: white; display: block">
    <p>Bevor Sie zu YouTube weitergehen</p>
    {hidden}
    <button id="accept">Alle akzeptieren</button>
    <button id="reject">Alle ablehnen</button>
  </tp-yt-paper-dialog>
</ytd-consent-bump-v2-lightbox>
<div id="backdrop" style="position: fixed; inset: 0; z-index: 2001; opacity: 0.5;
  background: black"></div>
<main>
  <h1>Der Wasserkreislauf</h1>
  <p>{DESCRIPTION}</p>
  <tp-yt-paper-button id="expand" style="display: inline-block">...mehr</tp-yt-paper-button>
  <ytd-video-description-transcript-section-renderer id="section" style="display: none">
    <button id="transcript">Transkript anzeigen</button>
  </ytd-video-description-transcript-section-renderer>
</main>
<script>
const note = (name) => {{
  const root = document.documentElement;
  root.dataset.clicks = (root.dataset.clicks + ' ' + name).trim();
}};
const close = (value) => {{
  if (!{closes}) return;
  document.cookie = 'SOCS=' + value + '; path=/';
  document.querySelector('tp-yt-paper-dialog').removeAttribute('opened');
  document.querySelector('tp-yt-paper-dialog').style.display = 'none';
  document.getElementById('backdrop').remove();
}};
document.getElementById('accept').addEventListener('click', () => {{
  note('accept');
  close('accepted');
}});
document.getElementById('reject').addEventListener('click', () => {{
  note('reject');
  close('x');
}});
document.getElementById('expand').addEventListener('click', () => {{
  note('expand');
  document.getElementById('section').style.display = 'block';
}});
const flags = {flags};
const ids = {ids};
const ask = async (url, panelId) => {{
  const text = JSON.stringify({{context: {{}}, panelId: panelId}});
  if (!flags.gzip) return fetch(url, {{method: 'POST', body: text}});
  const stream = new Blob([text]).stream().pipeThrough(new CompressionStream('gzip'));
  const body = await new Response(stream).arrayBuffer();
  return fetch(url, {{method: 'POST', body: body, headers: {{'Content-Encoding': 'gzip'}}}});
}};
document.getElementById('transcript').addEventListener('click', async () => {{
  note('transcript');
  if (flags.decoy) await (await ask({decoy_url}, ids.decoy)).text();
  ask({panel_url}, ids.view);
}});
</script>
</body>
</html>"""


def serve_watch_site(server: HTTPServer) -> None:
    """The watch pages and the ``get_panel`` answers on ``server``."""
    for path, page in (
        ("/watch/new", _watch_page(new_view=True)),
        ("/watch/old", _watch_page(new_view=False)),
        ("/watch/failing", _watch_page(new_view=True, panel_query="?fail=1")),
        ("/watch/chunked", _watch_page(new_view=True, panel_query="?chunked=1")),
        ("/watch/hidden-reject", _watch_page(new_view=True, hidden_reject=True)),
        ("/watch/stuck", _watch_page(new_view=True, consent_closes=False)),
        ("/watch/decoy", _watch_page(new_view=True, decoy=True)),
        ("/watch/gzip", _watch_page(new_view=True, decoy=True, gzip=True)),
    ):
        server.expect_request(path).respond_with_data(page, content_type="text/html")
    # The specific handler first: pytest-httpserver takes the first that matches.
    server.expect_request(PANEL_PATH, method="POST", query_string="fail=1").respond_with_json(
        {"error": {"code": 400, "status": "FAILED_PRECONDITION"}}, status=400
    )
    chunked = Response(_in_chunks(PANEL_BODY), content_type="application/json")
    server.expect_request(
        PANEL_PATH, method="POST", query_string="chunked=1"
    ).respond_with_response(chunked)
    server.expect_request(PANEL_PATH, method="POST", query_string="decoy=1").respond_with_data(
        DECOY_BODY, content_type="application/json"
    )
    server.expect_request(PANEL_PATH, method="POST").respond_with_data(
        PANEL_BODY, content_type="application/json"
    )


def _in_chunks(body: bytes, size: int = 1024) -> Iterator[bytes]:
    """``body`` as a stream: werkzeug then sends no ``Content-Length``."""
    for start in range(0, len(body), size):
        yield body[start : start + size]


def clicks(html: str) -> list[str]:
    """What was clicked in the rendered page, in order."""
    found = _CLICKS.search(html)
    return found.group(1).split() if found else []


def panel_bodies(server: HTTPServer) -> list[bytes]:
    """The decoded bodies of the ``get_panel`` requests ``server`` got, in order."""
    bodies = []
    for request, _ in server.log:
        if request.path == PANEL_PATH:
            body = request.get_data()
            if request.headers.get("Content-Encoding") == "gzip":
                body = gzip_module.decompress(body)
            bodies.append(body)
    return bodies


def panel_requests(server: HTTPServer) -> list[str]:
    """The ``Cookie`` headers of the ``get_panel`` requests ``server`` got, in order."""
    return [
        request.headers.get("Cookie", "") for request, _ in server.log if request.path == PANEL_PATH
    ]
