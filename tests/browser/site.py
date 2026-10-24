"""A local test site for the browser tests, served by ``http.server`` in a thread.

``Site.serve()`` starts one server per port; ``conftest.py`` runs three: the site itself, a
"third party" on a second port that the browser may reach, and a "forbidden" one on a port the
egress proxy refuses. Every server notes the requests it gets (``Site.log``), so a test can
tell what the browser sent and to whom.
"""

import contextlib
import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

from tests.fakes import PARAGRAPH

ARTICLE_TEXT = PARAGRAPH * 4
SCRIPT_TEXT = "The script wrote this sentence about the water cycle into the page. " * 8
XHR_TEXT = "The request to the api delivered this sentence about volcanoes and lava. " * 8
VISIBLE_TAB = "The visible tab explains how glaciers carve valleys over many thousands of years."
HIDDEN_TAB = "The hidden tab talks about something nobody can see on the page right now."
HIDDEN_LEAF = "A hidden leaf of text that only sits in the markup."
SHOWN_CHILD = "A visible child inside a hidden parent stays on the page."
OPTED_OUT = "The visitor confirmed the selection without statistics."
OPTED_IN = "The visitor accepted every cookie."
REVEALED = "The button revealed this paragraph about the tides of the sea."
DETAILS_TEXT = "The collapsed section explains how rivers reach the ocean."
FRAGMENT_TEXT = "The section opened by the fragment link covers the history of the canal."
ARIA_TEXT = "The toggled panel describes how clouds form above warm water."
TYPED_ECHO = "The field received: "
CHOSEN = "The chosen option is: "
LAZY_ITEMS = 30  # the list on /actions stops growing here
CHALLENGE_TEXT = "Checking your browser before accessing the site."
FORM_TEXT = "The form on this page posts itself as soon as the page has loaded."
PAGE_GLOBAL = "A value the page's own script keeps in a global variable."
# The page /speculation and /speculation-header speculate on, and then navigate to.
SPECULATED = "/static?via=speculation"
PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"
PIXEL = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360f8cfc0f01f0005000201e5274fde0000000049454e44ae426082"
)


def page(body: str, *, head: str = "", title: str = "Test page") -> bytes:
    return (
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{title}</title>"
        f"{head}</head><body>{body}</body></html>"
    ).encode()


def article(extra: str = "", *, head: str = "", title: str = "Static article") -> bytes:
    return page(
        f"<article><h1>{title}</h1><p>{ARTICLE_TEXT}</p>{extra}</article>",
        head=head,
        title=title,
    )


@dataclass(frozen=True)
class Seen:
    """One request a server got: its method, path (with query) and headers (lower case)."""

    method: str
    path: str
    headers: dict[str, str]


@dataclass
class Site:
    """The servers of the test site and the requests they got."""

    base: str = ""  # http://127.0.0.1:<port>
    third: str = ""  # the second port: another site the browser may reach
    forbidden: str = ""  # a port the egress proxy refuses
    log: dict[str, list[Seen]] = field(default_factory=dict)  # by base URL
    servers: list[ThreadingHTTPServer] = field(default_factory=list)

    def serve(self) -> None:
        for name in ("base", "third", "forbidden"):
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(self))
            server.daemon_threads = True
            server.block_on_close = False
            url = f"http://127.0.0.1:{server.server_address[1]}"
            setattr(self, name, url)
            self.log[url] = []
            self.servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def close(self) -> None:
        for server in self.servers:
            server.shutdown()
            server.server_close()

    def clear(self) -> None:
        for seen in self.log.values():
            seen.clear()

    def seen(self, base: str, path: str | None = None) -> list[Seen]:
        """Requests ``base`` got, all or those for ``path`` (without the query)."""
        return [s for s in self.log[base] if path is None or s.path.split("?")[0] == path]


def _handler_for(site: Site) -> type[BaseHTTPRequestHandler]:
    class Handler(_Handler):
        pass

    Handler.site = site
    return Handler


def _actions_page() -> bytes:
    """Controls for the page actions: a button that reveals text, a ``<details>``, an
    ``aria-expanded`` toggle, a select, an input that echoes on Enter, a link, and a list that
    grows by ten items each time the page is scrolled to its end, up to ``LAZY_ITEMS``."""
    body = f"""
<h1>Actions</h1>
<button id="reveal" onclick="document.getElementById('shown').textContent = {REVEALED!r}">
  Reveal</button>
<p id="shown"></p>
<details id="more"><summary>More</summary><p>{DETAILS_TEXT}</p></details>
<button id="toggle" aria-expanded="false"
  onclick="this.setAttribute('aria-expanded', 'true');
    document.getElementById('panel').hidden = false;">Toggle</button>
<div id="panel" hidden><p>{ARIA_TEXT}</p></div>
<select id="choice"
  onchange="document.getElementById('chosen').textContent = {CHOSEN!r} + this.value">
  <option value="">none</option><option value="red">Red</option><option value="blue">Blue</option>
</select>
<p id="chosen"></p>
<input id="field" aria-label="field" value="prefilled"
  onkeydown="if (event.key === 'Enter')
    document.getElementById('echoed').textContent = {TYPED_ECHO!r} + this.value">
<p id="echoed"></p>
<a id="go" href="/static">Go to the article</a>
<div id="list"></div>
<script>
let count = 0;
function more() {{
  const list = document.getElementById('list');
  for (let i = 0; i < 10 && count < {LAZY_ITEMS}; i++) {{
    count += 1;
    const item = document.createElement('div');
    item.className = 'item';
    item.style.height = '200px';
    item.textContent = 'Item ' + count;
    list.append(item);
  }}
}}
more();
window.addEventListener('scroll', () => {{
  if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 50)
    setTimeout(more, 50);
}});
</script>"""
    return page(body, title="Actions")


class _Handler(BaseHTTPRequestHandler):
    site: ClassVar[Site]
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        self._note()
        with contextlib.suppress(ConnectionError):  # the browser went away (a closed page)
            self._route()

    def do_POST(self) -> None:
        self._note()
        length = int(self.headers.get("content-length") or 0)
        with contextlib.suppress(ConnectionError):
            self.rfile.read(length)
            self._send(article(title="Posted"))

    def _note(self) -> None:
        host = self.headers.get("host", "")
        base = f"http://{host}"
        if base in self.site.log:
            self.site.log[base].append(
                Seen(self.command, self.path, {k.lower(): v for k, v in self.headers.items()})
            )

    def _send(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "text/html; charset=utf-8",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _route(self) -> None:
        site = self.site
        path = self.path.split("?")[0]
        if path == "/static":
            self._send(article())
        elif path == "/js":
            script = (
                "<script>const p = document.createElement('p');"
                f"p.textContent = {SCRIPT_TEXT!r}; document.getElementById('root').append(p);"
                "</script>"
            )
            self._send(page(f"<main><h1>Scripted</h1><div id='root'></div>{script}</main>"))
        elif path == "/xhr":
            script = (
                "<script>fetch('/api').then(r => r.json()).then(d => {"
                "const p = document.createElement('p'); p.textContent = d.text;"
                "document.getElementById('root').append(p); });</script>"
            )
            self._send(page(f"<main><h1>Fetched</h1><div id='root'></div>{script}</main>"))
        elif path == "/api":
            body = ('{"text": "' + XHR_TEXT + '"}').encode()
            self._send(body, content_type="application/json")
        elif path == "/redirect-http":
            self._send(b"", status=302, headers={"Location": "/static"})
        elif path == "/redirect-third":
            self._send(b"", status=302, headers={"Location": f"{site.third}/echo-headers"})
        elif path == "/loop":
            self._send(b"", status=302, headers={"Location": "/loop"})
        elif path == "/redirect-same":
            self._send(b"", status=302, headers={"Location": "/echo-headers"})
        elif path == "/redirect-js":
            self._send(page("<p>Moving on.</p><script>location.href = '/static';</script>"))
        elif path == "/meta-refresh":
            head = "<meta http-equiv='refresh' content='0; url=/static'>"
            self._send(page("<p>Moving on.</p>", head=head))
        elif path == "/to-private":
            script = "<script>location.href = 'http://169.254.169.254/latest/meta-data/';</script>"
            self._send(page(f"<p>Moving on.</p>{script}"))
        elif path == "/img-private":
            extra = (
                f"<img src='{site.forbidden}/pixel.png' alt=''>"
                f"<script>fetch('{site.forbidden}/api').catch(() => {{}});</script>"
            )
            self._send(article(extra, title="Private subresources"))
        elif path == "/image":
            self._send(article("<img src='/pixel.png' alt=''>", title="With an image"))
        elif path == "/heavy":
            self._send(article("<img src='/big.png' alt=''>", title="Heavy"))
        elif path == "/big.png":
            self._send(b"\0" * 200_000, content_type="image/png")
        elif path == "/pixel.png":
            self._send(PIXEL, content_type="image/png")
        elif path == "/third-party":
            extra = f"<script>fetch('{site.third}/echo-headers').catch(() => {{}});</script>"
            self._send(article(extra, title="Third party"))
        elif path == "/echo-headers":
            rows = "".join(
                f"<li>{name.lower()}: {value}</li>" for name, value in self.headers.items()
            )
            self._send(article(f"<ul id='headers'>{rows}</ul>", title="Headers"))
        elif path == "/hidden-tabs":
            head = (
                "<style>.tab-pane { display: none; } .tab-pane.active { display: block; }"
                ".ghost { visibility: hidden; } .shown { visibility: visible; }</style>"
            )
            tabs = (
                f"<div class='tab-pane active'><p>{VISIBLE_TAB}</p></div>"
                f"<div class='tab-pane'><p>{HIDDEN_TAB}</p></div>"
                f"<p class='ghost'>{HIDDEN_LEAF}</p>"
                f"<div class='ghost'><p class='shown'>{SHOWN_CHILD}</p></div>"
            )
            self._send(article(tabs, head=head, title="Tabs"))
        elif path == "/consent":
            banner = (
                "<div id='cookiebanner'><p>This site uses cookies for statistics.</p>"
                "<button class='js-banner-button-confirm-selection' "
                "onclick=\"document.getElementById('cookiebanner').remove();"
                f"document.getElementById('state').textContent = {OPTED_OUT!r};\">"
                "Confirm selection</button>"
                "<button class='js-banner-button-accept-all' "
                "onclick=\"document.getElementById('cookiebanner').remove();"
                f"document.getElementById('state').textContent = {OPTED_IN!r};\">"
                "Accept all</button></div>"
            )
            self._send(article(f"{banner}<p id='state'></p>", title="Consent"))
        elif path == "/actions":
            self._send(_actions_page())
        elif path == "/nav-toggle":
            markup = (
                "<a id='menu' href='/static' aria-expanded='false'>Menu</a>"
                "<a id='frag' href='#panel' aria-expanded='false' onclick=\""
                "this.setAttribute('aria-expanded', 'true');"
                "document.getElementById('panel').hidden = false; return false;\">Section</a>"
                f"<div id='panel' hidden><p>{FRAGMENT_TEXT}</p></div>"
                "<form action='/static' method='post'>"
                "<button id='send' aria-expanded='false'>Send</button></form>"
            )
            self._send(page(markup, title="Navigating toggles"))
        elif path == "/nav-script":
            markup = (
                "<button id='go' aria-expanded='false' onclick=\"location.href = '/static'\">"
                "Open</button>"
            )
            self._send(page(markup, title="Script toggle"))
        elif path == "/form-click":
            markup = (
                "<form action='/static' method='post'><button id='send'>Send</button></form>"
                "<button id='reveal' onclick=\"this.textContent = 'clicked'\">Reveal</button>"
            )
            self._send(page(markup, title="Form"))
        elif path == "/tall":
            self._send(article("<div style='height: 20000px'></div>", title="Tall"))
        elif path == "/never-idle":
            script = (
                "<script>window.addEventListener('load', () => {"
                "const poll = () => fetch('/poll').then(() => poll(), () => poll()); poll(); });"
                "</script>"
            )
            self._send(article(script, title="Never idle"))
        elif path == "/poll":
            time.sleep(1)
            self._send(b"{}", content_type="application/json")
        elif path == "/form-post":
            form = (
                f"<p>{FORM_TEXT}</p><form method='post' action='/static'>"
                "<input name='q' value='1'></form>"
                "<script>window.addEventListener('load', () => document.forms[0].submit());"
                "</script>"
            )
            self._send(article(form, title="Form"))
        elif path == "/set-domain-cookie":
            # A clearance cookie for the parent domain of the host (www.example.test, say).
            host = (self.headers.get("host") or "").split(":")[0]
            parent = host.split(".", 1)[1] if host.count(".") > 1 else host
            self._send(
                article(title="Domain cookie"),
                headers={"Set-Cookie": f"cf_clearance=domain-wide; Domain={parent}; Path=/"},
            )
        elif path == "/set-cookie":
            self._send(
                article(title="Cookies"),
                headers={"Set-Cookie": "session=abc123; Path=/; HttpOnly"},
            )
        elif path == "/challenge":
            if "cleared=1" in (self.headers.get("cookie") or ""):
                self._send(article(title="Cleared"))
            else:
                script = (
                    "<script>setTimeout(() => { document.cookie = 'cleared=1; path=/';"
                    "location.reload(); }, 2000);</script>"
                )
                self._send(
                    page(f"<p>{CHALLENGE_TEXT}</p>{script}", title="Just a moment..."),
                    status=503,
                )
        elif path == "/checkbox":
            # A checkbox challenge: a widget frame from the second port (standing in for
            # Turnstile) whose box sets the clearance cookie and tells the page, which reloads.
            if "cf_clearance=ok" in (self.headers.get("cookie") or ""):
                self._send(article(title="Cleared"))
            else:
                query = "?stubborn=1" if "stubborn=1" in self.path else ""
                widget = (
                    f"<iframe id='widget' src='{site.third}/turnstile{query}' "
                    "width='300' height='65' style='border: 0'></iframe>"
                    "<script>window.addEventListener('message', (event) => {"
                    "if (event.data === 'cleared') location.reload(); });</script>"
                )
                self._send(
                    page(f"<p>{CHALLENGE_TEXT}</p>{widget}", title="Just a moment..."),
                    status=403,
                )
        elif path == "/turnstile":
            clear = (
                ""
                if "stubborn=1" in self.path
                else "document.cookie = 'cf_clearance=ok; path=/';"
                "parent.postMessage('cleared', '*');"
            )
            box = (
                "<label style='display: block; padding: 20px'>"
                "<input type='checkbox' id='box' onclick=\"fetch('/turnstile-clicked');"
                f'{clear}"> Verify you are human</label>'
            )
            self._send(page(box, title="Widget"))
        elif path == "/turnstile-clicked":
            self._send(b"{}", content_type="application/json")
        elif path == "/navigator":
            script = (
                "<script>document.getElementById('webdriver').textContent ="
                " 'webdriver=' + navigator.webdriver;</script>"
            )
            self._send(article(f"<p id='webdriver'></p>{script}", title="Navigator"))
        elif path == "/global":
            script = f"<script>window.pageGlobal = {PAGE_GLOBAL!r};</script>"
            self._send(article(f"<p id='out'></p>{script}", title="Global"))
        elif path in ("/speculation", "/speculation-header"):
            # Speculation rules fetch documents outside the page's requests; the page then
            # navigates to the speculated URL, which must still pass the navigation check.
            rules = json.dumps(
                {
                    "prefetch": [{"source": "list", "urls": [SPECULATED]}],
                    "prerender": [{"source": "list", "urls": [SPECULATED]}],
                }
            )
            inline = f"<script type='speculationrules'>{rules}</script>"
            later = (
                f"<script>setTimeout(() => {{ location.href = '{SPECULATED}'; }}, 300);</script>"
            )
            if path == "/speculation":
                self._send(article(inline + later, title="Speculation"))
            else:
                headers = {"Speculation-Rules": '"/rules.json"'}
                self._send(article(later, title="Speculation"), headers=headers)
        elif path == "/prefetch-link":
            link = f"<link rel='prefetch' href='{SPECULATED}'>"
            self._send(article(head=link, title="Prefetch link"))
        elif path == "/accept-rules":
            script = (
                "<script>fetch('/api', {headers: {Accept: 'application/speculationrules+json'}})"
                ".then(() => 'delivered', () => 'refused').then(outcome => {"
                "document.getElementById('outcome').textContent = outcome; });</script>"
            )
            self._send(article(f"<p id='outcome'>pending</p>{script}", title="Rules by Accept"))
        elif path == "/rules.json":
            listed = {"prefetch": [{"source": "list", "urls": [SPECULATED]}]}
            self._send(
                json.dumps(listed).encode(), content_type="application/speculationrules+json"
            )
        elif path == "/download.pdf":
            self._send(
                PDF,
                content_type="application/pdf",
                headers={"Content-Disposition": "attachment; filename=worksheet.pdf"},
            )
        else:
            self._send(page("<p>Not found</p>"), status=404)
