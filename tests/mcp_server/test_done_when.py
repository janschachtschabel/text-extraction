"""Package 10's done-when test for MCP: every tool over the transport Claude Code uses.

It runs in the default suite without network. The public app (``MCP_ENABLED`` and
``UI_ENABLED`` on, ``AUTH_MODE=api_key``) is served by uvicorn on ``127.0.0.1`` in a thread, so
its lifespan runs; the real extract ``Worker`` (with ``ExtractRunner`` and ``SiteRunner``, as in
``tests/site/test_done_when.py``) runs in another thread on the app's fakeredis server. A small
site (robots.txt, a sitemap, a start page linking three pages, an image, a PDF) is served by
pytest-httpserver on ``127.0.0.1`` (``FETCH_ALLOW_PRIVATE_NETWORKS=true``,
``OUTBOUND_MIN_DELAY_MS=0``). The client is ``fastmcp.Client`` over
``StreamableHttpTransport`` with the key as a bearer header. ``tests/ui/test_done_when.py``
drives the UI against the same setup (`serving`)."""

import asyncio
import base64
import contextlib
import hashlib
import json
import re
import socket
import threading
import time
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import pytest
import uvicorn
from fakeredis import FakeAsyncRedis, FakeServer
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcp.shared.exceptions import MCPError
from prometheus_client import CollectorRegistry
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from tests.site.test_done_when import ExtractWorker
from text_extraction.app import create_public_app
from text_extraction.settings import Settings

USER_KEY = "user-key-0123456789-done-when"
ADMIN_KEY = "admin-key-0123456789-done-when"
CORPUS = Path(__file__).parent.parent / "documents" / "corpus"
PDF = (CORPUS / "text.pdf").read_bytes()
PNG = (CORPUS / "page.png").read_bytes()
TITLE = "Bruchrechnung für die Sekundarstufe"
TEXT = (
    "Die Lernwerkstatt sammelt Unterrichtsmaterial für die Sekundarstufe. Jede Einheit beginnt "
    "mit einer kurzen Einführung, zeigt ein durchgerechnetes Beispiel und endet mit Aufgaben, "
    "die die Schülerinnen und Schüler allein oder zu zweit bearbeiten. Lehrkräfte finden zu "
    "jeder Einheit Hinweise zur Differenzierung, Vorschläge für die Sicherung im Plenum und "
    "Material zum Ausdrucken. Alle Texte stehen unter einer freien Lizenz und dürfen im "
    "Unterricht verändert und weitergegeben werden."
)
SUBPAGES = ("a", "b", "c")
# The tools of ruling P10-S5.
EXPECTED_TOOLS = {
    "extract_url",
    "extract_file",
    "screenshot_url",
    "extract_structured",
    "chunk",
    "batch_extract",
    "site_structure",
    "get_job",
    "get_job_screenshot",
    "cancel_job",
    "service_info",
}
METADATA_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "license": {"type": "string"}},
}
STRUCTURED = {"strategy": "metadata", "schema": METADATA_SCHEMA}


def page_html(title: str, body: str, *, head: str = "", links: str = "") -> str:
    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><title>{title}</title>{head}</head>
<body><nav><a href="/">Start</a></nav>
<article><h1>{title}</h1><p>{body}</p><p>{body}</p>{links}</article>
<footer><a href="/impressum.html">Impressum</a></footer>
</body></html>
"""


def serve_site(httpserver: HTTPServer) -> str:
    """Serve the sample site on ``httpserver``; returns its origin. The start page has JSON-LD
    (a name and a licence), links to ``/a.html``, ``/b.html`` and ``/c.html`` and shows
    ``/bild.png``; ``/sitemap.xml`` lists the four pages and robots.txt names it. Anything else
    (the TDM files, ``/impressum.html``) is a 404."""
    origin = httpserver.url_for("/").rstrip("/")
    json_ld = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "LearningResource",
            "name": TITLE,
            "license": "https://creativecommons.org/licenses/by-sa/4.0/",
        }
    )
    links = "".join(f'<p><a href="/{name}.html">Einheit {name}</a></p>' for name in SUBPAGES)
    links += '<p><img src="/bild.png" alt="Ein Bruch als Kreis"></p>'
    files: dict[str, tuple[bytes, str]] = {
        "/": (
            page_html(
                TITLE,
                TEXT,
                head=f'<script type="application/ld+json">{json_ld}</script>',
                links=links,
            ).encode(),
            "text/html; charset=utf-8",
        ),
        "/robots.txt": (
            f"User-agent: *\nAllow: /\nSitemap: {origin}/sitemap.xml\n".encode(),
            "text/plain",
        ),
        "/sitemap.xml": (
            (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                + "".join(
                    f"<url><loc>{origin}{path}</loc></url>"
                    for path in ("/", *(f"/{name}.html" for name in SUBPAGES))
                )
                + "</urlset>"
            ).encode(),
            "application/xml",
        ),
        "/bild.png": (PNG, "image/png"),
        "/material/arbeitsblatt.pdf": (PDF, "application/pdf"),
    }
    for name in (*SUBPAGES, "langsam"):
        files[f"/{name}.html"] = (
            page_html(f"Einheit {name}", TEXT).encode(),
            "text/html; charset=utf-8",
        )

    def handle(request: Request) -> Response:
        if request.path not in files:
            return Response("Not found", status=404, content_type="text/plain")
        body, content_type = files[request.path]
        return Response(body, content_type=content_type)

    httpserver.expect_request(re.compile(".*")).respond_with_handler(handle)
    return origin


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "auth_mode": "api_key",
        "api_keys": [
            {
                "name": "alice",
                "role": "user",
                "sha256": hashlib.sha256(USER_KEY.encode()).hexdigest(),
            },
            {
                "name": "root",
                "role": "admin",
                "sha256": hashlib.sha256(ADMIN_KEY.encode()).hexdigest(),
            },
        ],
        "mcp_enabled": True,
        "ui_enabled": True,
        "fetch_allow_private_networks": True,
        "outbound_min_delay_ms": 0,
        "rate_limit_per_minute": 10_000,
    }
    values.update(overrides)
    return Settings(**values)


@dataclass(frozen=True)
class Live:
    """The running service: its base URL, its settings and its fakeredis server; ``origin`` is
    the sample site's."""

    base_url: str
    settings: Settings
    server: FakeServer
    origin: str

    def run[T](self, work: Callable[[FakeAsyncRedis], Coroutine[Any, Any, T]]) -> T:
        """Run ``work(valkey)`` on a loop of its own, with a client of the app's server."""

        async def call() -> T:
            valkey = FakeAsyncRedis(server=self.server)
            try:
                result: T = await work(valkey)
                return result
            finally:
                await valkey.aclose()

        return asyncio.run(call())


@contextlib.contextmanager
def serving(httpserver: HTTPServer, **overrides: Any) -> Iterator[Live]:
    """The public app with ``make_settings(**overrides)`` on a free port of ``127.0.0.1`` and an
    extract worker on its fakeredis server, the sample site on ``httpserver``."""
    origin = serve_site(httpserver)
    settings = make_settings(**overrides)
    server = FakeServer()
    app = create_public_app(settings, FakeAsyncRedis(server=server), CollectorRegistry())
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not uvicorn_server.started:
        assert time.monotonic() < deadline, "the server did not start"
        assert thread.is_alive(), "the server stopped"
        time.sleep(0.05)
    try:
        with ExtractWorker(settings, server):
            yield Live(f"http://127.0.0.1:{port}", settings, server, origin)
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=15)
        assert not thread.is_alive(), "the server did not stop"


def mcp_client(
    live: Live, key: str | None = USER_KEY, statuses: list[int] | None = None
) -> Client[Any]:
    """A client of the MCP endpoint with ``key`` as its bearer token (``None``: no
    ``Authorization`` header); with ``statuses``, the HTTP status of each answer is recorded
    there."""

    async def record(response: httpx2.Response) -> None:
        if statuses is not None:
            statuses.append(response.status_code)

    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
        auth: httpx2.Auth | None = None,
        **kwargs: Any,
    ) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            headers=headers,
            timeout=timeout,
            auth=auth,
            event_hooks={"response": [record]},
            **kwargs,
        )

    transport = StreamableHttpTransport(
        f"{live.base_url}/mcp",
        headers={"Authorization": f"Bearer {key}"} if key is not None else {},
        httpx_client_factory=factory,
    )
    return Client(transport, timeout=60)


def data_of(result: Any) -> dict[str, Any]:
    """The envelope's ``data`` of a tool result that is no error."""
    assert result.is_error is False, result.structured_content
    data: dict[str, Any] = result.structured_content["data"]
    return data


def refused_code(result: Any) -> str:
    assert result.is_error is True, result.structured_content
    code: str = result.structured_content["error"]["code"]
    return code


@pytest.fixture
def live(httpserver: HTTPServer) -> Iterator[Live]:
    with serving(httpserver) as running:
        yield running


def test_every_tool_over_streamable_http(live: Live) -> None:
    page_url = f"{live.origin}/"
    three_urls = [f"{live.origin}/{name}.html" for name in SUBPAGES]

    async def scenario() -> None:
        async with mcp_client(live) as client:
            assert {tool.name for tool in await client.list_tools()} == EXPECTED_TOOLS

            page = await client.call_tool("extract_url", {"url": page_url})
            assert page.structured_content["status"] == "success"
            assert TITLE in data_of(page)["content"]

            batch = await client.call_tool("batch_extract", {"urls": three_urls})
            job_id = data_of(batch)["id"]
            assert batch.content[0].text.startswith(f"Continues as job {job_id} (batch)")
            done = await client.call_tool(
                "get_job", {"job_id": job_id, "wait_seconds": 20, "limit": 2}
            )
            assert data_of(done)["state"] == "completed"
            assert data_of(done)["next_offset"] == 2
            assert data_of(done)["items"][0]["run"]["pool"] == "extract"
            assert data_of(done)["items"][0]["result"]["status"] == "success"

            pdf = await client.call_tool(
                "extract_file",
                {"content_base64": base64.b64encode(PDF).decode(), "file_name": "a.pdf"},
            )
            assert data_of(pdf)["document"]["kind"] == "pdf"

            site = await client.call_tool("site_structure", {"url": page_url, "depth": 1})
            assert data_of(site)["kind"] == "site"
            crawled = await client.call_tool(
                "get_job", {"job_id": data_of(site)["id"], "wait_seconds": 20, "limit": 50}
            )
            assert data_of(crawled)["state"] == "completed"
            page_urls = {
                item["url"]
                for item in data_of(crawled)["items"]
                if item["result"]["data"]["site"]["role"] == "page"
            }
            assert page_urls >= {page_url, *three_urls}

            refused = await client.call_tool(
                "extract_url",
                {"url": page_url, "options": {"compliance": "ignore"}},
                raise_on_error=False,
            )
            assert refused_code(refused) == "forbidden"

            structured = await client.call_tool(
                "extract_structured",
                {
                    "url": page_url,
                    "structured": {"strategy": "metadata", "schema": METADATA_SCHEMA},
                },
            )
            values = data_of(structured)["structured"]["values"]
            assert values["name"] == TITLE
            assert values["license"] == "https://creativecommons.org/licenses/by-sa/4.0/"

            chunks = await client.call_tool(
                "chunk", {"url": page_url, "chunking": {"strategy": "recursive", "size": 200}}
            )
            assert len(data_of(chunks)["chunks"]) > 1

            screenshot = await client.call_tool(
                "screenshot_url", {"url": page_url}, raise_on_error=False
            )
            assert refused_code(screenshot) == "invalid_request"  # BROWSER_ENABLED is off

            other = await client.call_tool("batch_extract", {"urls": three_urls[:1]})
            other_id = data_of(other)["id"]
            cancelled = await client.call_tool("cancel_job", {"job_id": other_id})
            assert data_of(cancelled) == {"id": other_id, "kind": "batch"}
            gone = await client.call_tool("get_job", {"job_id": other_id}, raise_on_error=False)
            assert refused_code(gone) == "not_found"
            no_shot = await client.call_tool(
                "get_job_screenshot", {"job_id": other_id}, raise_on_error=False
            )
            assert refused_code(no_shot) == "not_found"

            info = await client.call_tool("service_info", {})
            assert (data_of(info)["principal"], data_of(info)["role"]) == ("alice", "user")

    asyncio.run(scenario())


@pytest.mark.parametrize("key", ["wrong-key-0123456789", None])
def test_a_wrong_key_cannot_connect(live: Live, key: str | None) -> None:
    statuses: list[int] = []

    async def connect() -> None:
        async with mcp_client(live, key, statuses) as client:
            await client.list_tools()

    with pytest.raises(MCPError):
        asyncio.run(connect())
    assert statuses
    assert set(statuses) == {401}  # the gate refuses every request
