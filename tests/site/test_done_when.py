"""Package 9's done-when test: ``POST /v1/site-structure`` end to end on two sample sites.

It runs in the default suite without network: the sites are static files under ``sites/``
served by pytest-httpserver on ``127.0.0.1`` (``FETCH_ALLOW_PRIVATE_NETWORKS=true``), and the
extract worker is the real ``Worker`` with ``ExtractRunner`` and ``SiteRunner`` on the real
``Fetcher``, in a thread of its own on the fakeredis server of the app, as in
``tests/structured/test_done_when.py``. ``JOB_ITEM_MAX_BYTES`` is at its minimum, so a large
sitemap's entries arrive in parts. The results are read the way a caller reads them:
``GET /v1/jobs/{id}`` paged with ``limit=50``.

Site A (``sites/a``): robots.txt with ``Disallow: /intern/`` and two ``Sitemap:`` lines, an
index (a plain urlset and a gzipped one) and a text sitemap; 14 pages (the start page and three
levels below it) with the typed-links elements (header, breadcrumb, nav, main, footer, pagination,
``area``, head ``link``, ``iframe``), a 404 link, a redirect, a cycle, a ``nofollow`` link, a
PDF link and an external link, crawled two clicks deep. Site B (``sites/b``): no robots.txt,
``/sitemap.xml`` found by the guess, 300 entries, crawled zero clicks deep.
"""

import asyncio
import gzip
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from tests.test_jobs_api import ALICE, make_settings
from text_extraction.app import create_public_app
from text_extraction.conversion_pool import ConversionPool
from text_extraction.jobs import JobStore
from text_extraction.pipeline import create_outbound, create_pipeline
from text_extraction.scheduler import FairQueue
from text_extraction.settings import Settings
from text_extraction.site.runner import SiteRunner
from text_extraction.site.store import SiteStore
from text_extraction.site.urls import normalize_url
from text_extraction.worker import ExtractRunner, Worker

SITES = Path(__file__).parent / "sites"
TEXT = (
    "Die Lernwerkstatt sammelt Unterrichtsmaterial für die Sekundarstufe. Jede Einheit beginnt "
    "mit einer kurzen Einführung, zeigt ein durchgerechnetes Beispiel und endet mit Aufgaben, "
    "die die Schülerinnen und Schüler allein oder zu zweit bearbeiten. Lehrkräfte finden zu "
    "jeder Einheit Hinweise zur Differenzierung, Vorschläge für die Sicherung im Plenum und "
    "Material zum Ausdrucken. Alle Texte stehen unter einer freien Lizenz und dürfen im "
    "Unterricht verändert und weitergegeben werden."
)
PAGE_SIZE = 50
JOB_ITEM_MAX_BYTES = 10_000

# Site A: what its three sitemaps list (paths on the server's origin).
SITE_A_SITEMAP_PATHS = {
    "/",
    "/mathe/",
    "/physik/",
    "/physik/optik.html",  # seiten.xml
    "/material/arbeitsblatt.pdf",
    "/mathe/brueche.html",  # material.xml.gz
    "/kontakt.html",
    "/impressum.html",
    "/mathe/level3.html",  # sitemap.txt
}
# Every page item of site A: the start page, its links (depth 1) and theirs (depth 2).
SITE_A_PAGE_PATHS = {
    "/",
    "/mathe/",
    "/physik/",
    "/alt.html",  # answered by /neu.html after a redirect
    "/fehlt.html",  # 404
    "/seite-2.html",
    "/kontakt.html",
    "/impressum.html",
    "/mathe/brueche.html",
    "/mathe/dezimalzahlen.html",
    "/physik/optik.html",
    "/physik/mechanik.html",
}
REDIRECTS = {"/alt.html": "/neu.html"}
SITE_B_ENTRIES = 300


# --- the sites ---


def serve_site(httpserver: HTTPServer, name: str, *, sitemap_entries: int = 0) -> str:
    """Serve ``sites/<name>`` at the server's root with ``{{ORIGIN}}`` and ``{{TEXT}}`` filled
    in: ``sitemaps/material.xml`` gzipped as ``sitemaps/material.xml.gz``, the redirects of
    ``REDIRECTS`` as 301, and with ``sitemap_entries`` a generated ``/sitemap.xml`` of that many
    URLs. Anything else (the TDM files, ``/fehlt.html``) is a 404. Returns the origin."""
    origin = httpserver.url_for("/").rstrip("/")
    root = SITES / name
    files: dict[str, tuple[bytes, str]] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = "/" + path.relative_to(root).as_posix()
        body = path.read_text(encoding="utf-8").replace("{{ORIGIN}}", origin)
        body = body.replace("{{TEXT}}", TEXT)
        if relative == "/sitemaps/material.xml":
            files[relative + ".gz"] = (gzip.compress(body.encode()), "application/gzip")
            continue
        if relative.endswith("/index.html"):
            relative = relative.removesuffix("index.html")
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".xml": "application/xml",
            ".txt": "text/plain; charset=utf-8",
        }[path.suffix]
        files[relative] = (body.encode(), content_type)
    if sitemap_entries:
        urls = "".join(
            f"<url><loc>{origin}/aufgabe-{number}.html</loc><lastmod>2026-10-01</lastmod></url>"
            for number in range(1, sitemap_entries + 1)
        )
        sitemap = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
        )
        files["/sitemap.xml"] = (sitemap.encode(), "application/xml")

    def handle(request: Request) -> Response:
        if request.path in REDIRECTS and name == "a":
            return Response(status=301, headers={"Location": REDIRECTS[request.path]})
        if request.path not in files:
            return Response("Not found", status=404, content_type="text/plain")
        body, content_type = files[request.path]
        return Response(body, content_type=content_type)

    httpserver.expect_request(re.compile(".*")).respond_with_handler(handle)
    return origin


def requested_paths(httpserver: HTTPServer) -> set[str]:
    return {request.path for request, _ in httpserver.log}


# --- the service: the public app and an extract worker on one fakeredis server ---


async def _short_sleep(seconds: float) -> None:
    await asyncio.sleep(min(seconds, 0.02))


class ExtractWorker:
    """The real extract ``Worker`` with ``ExtractRunner`` and ``SiteRunner`` (as
    ``build_worker`` wires them, one slot) on a loop of its own, on ``server``."""

    def __init__(self, settings: Settings, server: FakeServer) -> None:
        self._settings = settings
        self._server = server
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        settings = self._settings
        valkey = FakeAsyncRedis(server=self._server)
        registry = CollectorRegistry()
        gate, fetcher = create_outbound(settings, valkey)
        pool = ConversionPool(1)
        sitemap_pool = ConversionPool(1, name="sitemap")
        try:
            pipeline = create_pipeline(settings, valkey, fetcher, registry, pool, gate=gate)
            store = JobStore(valkey, settings)
            queue = FairQueue(valkey, settings)
            site = SiteRunner(
                pipeline,
                store,
                SiteStore(valkey, store, settings),
                queue,
                settings,
                sitemap_pool,
                registry=registry,
            )
            worker = Worker(
                settings,
                valkey,
                ExtractRunner(pipeline, store, settings, None, registry=registry, site=site),
                store,
                queue,
                registry,
                concurrency=1,
                sleep=_short_sleep,
            )
            self._ready.set()
            await worker.run(self._stop)
        finally:
            await fetcher.aclose()
            pool.shutdown()
            sitemap_pool.shutdown()

    def __enter__(self) -> "ExtractWorker":
        self._thread.start()
        assert self._ready.wait(10)
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._loop is not None
        assert self._stop is not None
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(10)
        assert not self._thread.is_alive(), "the extract worker did not stop"


class Service:
    def __init__(self, client: TestClient, settings: Settings, server: FakeServer) -> None:
        self.client = client
        self.settings = settings
        self.server = server

    def crawl(self, **body: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """``POST /v1/site-structure``, the job run to its end by an extract worker, then the
        job and all its items as ``GET /v1/jobs/{id}`` pages them (``limit=50``)."""
        with ExtractWorker(self.settings, self.server):
            response = self.client.post("/v1/site-structure", json=body, headers=ALICE)
            assert response.status_code == 202, response.text
            job_id = response.json()["data"]["id"]
            job = self._wait(job_id)
        items: list[dict[str, Any]] = []
        offset: int | None = 0
        while offset is not None:
            page = self.client.get(
                f"/v1/jobs/{job_id}", params={"offset": offset, "limit": PAGE_SIZE}, headers=ALICE
            )
            assert page.status_code == 200, page.text
            data = page.json()["data"]
            assert len(data["items"]) <= PAGE_SIZE
            items += data["items"]
            offset = data["next_offset"]
        assert [item["index"] for item in items] == list(range(job["progress"]["total"]))
        return job, items

    def _wait(self, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + 60
        while True:
            response = self.client.get(f"/v1/jobs/{job_id}", params={"limit": 1}, headers=ALICE)
            data: dict[str, Any] = response.json()["data"]
            if data["state"] == "completed" or time.monotonic() > deadline:
                return data
            time.sleep(0.05)


@pytest.fixture
def service() -> Iterator[Service]:
    settings = make_settings(
        fetch_allow_private_networks=True,
        outbound_min_delay_ms=0,
        job_item_max_bytes=JOB_ITEM_MAX_BYTES,
        rate_limit_enabled=False,  # the test polls the job
    )
    server = FakeServer()
    app = create_public_app(settings, FakeAsyncRedis(server=server), CollectorRegistry())
    with TestClient(app) as client:
        yield Service(client, settings, server)


# --- reading the items ---


def site_of(item: dict[str, Any]) -> dict[str, Any]:
    site: dict[str, Any] = item["result"]["data"]["site"]
    return site


def pages(items: list[dict[str, Any]], depth_max: int | None = None) -> list[dict[str, Any]]:
    return [
        item
        for item in items
        if site_of(item)["role"] == "page"
        and (depth_max is None or site_of(item)["depth"] <= depth_max)
    ]


def by_role(items: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
    return [item for item in items if site_of(item)["role"] == role]


def listed_locs(items: list[dict[str, Any]]) -> set[str]:
    """Every entry of every sitemap file and further part."""
    return {
        entry["loc"]
        for item in items
        if site_of(item)["role"] in ("sitemap", "sitemap_part") and site_of(item)["sitemap"]
        for entry in site_of(item)["sitemap"]["entries"]
    }


def links(item: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = site_of(item)["links"] or []
    return found


def link(items: list[dict[str, Any]], url: str, source: str | None = None) -> dict[str, Any]:
    """The start page's first link to ``url`` (a path on the origin, or absolute), of
    ``source`` when given."""
    origin = items[0]["url"].rstrip("/")
    absolute = url if url.startswith("https://") else origin + url
    return next(
        found
        for found in links(items[0])
        if found["url"] == absolute and source in (None, found["source"])
    )


def followed_links(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The links of all pages that lead to an item."""
    return [found for item in pages(items) for found in links(item) if found["target"] is not None]


def canonical(url: str) -> str:
    """The URL of the item that answers ``url``: a redirect's target is its source's item."""
    normalized = normalize_url(url)
    assert normalized is not None
    for source, target in REDIRECTS.items():
        if normalized.endswith(target):
            return normalized.removesuffix(target) + source
    return normalized


def path_of(url: str, origin: str) -> str:
    return url.removeprefix(origin)


def failed_items(items: list[dict[str, Any]], origin: str) -> dict[str, str]:
    return {
        path_of(item["url"], origin): item["result"]["error"]["code"]
        for item in items
        if item["result"]["status"] == "failed"
    }


# --- the tests ---


def test_site_a_links_sitemaps_and_compliance(service: Service, httpserver: HTTPServer) -> None:
    origin = serve_site(httpserver, "a")

    job, items = service.crawl(url=f"{origin}/", depth=2, max_pages=100)

    assert job["state"] == "completed"
    assert all(item["state"] == "done" for item in items)
    items_by_index = {item["index"]: item for item in items}
    assert listed_locs(items) == {origin + path for path in SITE_A_SITEMAP_PATHS}
    assert {i["url"] for i in pages(items, depth_max=2)} == {
        origin + path for path in SITE_A_PAGE_PATHS
    }
    assert len(pages(items)) == len(SITE_A_PAGE_PATHS)  # each page once
    assert not any(i["url"].endswith("/level3.html") for i in pages(items))  # depth 2 kept
    assert link(items, "/intern/geheim.html")["crawl"] == "blocked"
    assert "/intern/geheim.html" not in requested_paths(httpserver)
    assert link(items, "/kontakt.html")["region"] == "footer"
    assert link(items, "/mathe/")["region"] == "breadcrumb"
    assert link(items, "https://h5p.example.org/embed/1")["source"] == "iframe"
    assert link(items, "/material/arbeitsblatt.pdf")["crawl"] == "file"
    assert all(
        items_by_index[found["target"]]["url"] == canonical(found["url"])
        for found in followed_links(items)
    )  # targets point at the right item
    assert failed_items(items, origin) == {"/fehlt.html": "target_http_error"}  # the only one

    # The start page's other typed links and what the crawl did with them.
    assert link(items, "/physik/")["region"] == "nav"
    head_next = link(items, "/seite-2.html", source="link")  # <link rel="next"> in the head
    assert (head_next["region"], head_next["crawl"]) == ("pagination", "listed")
    pager = link(items, "/seite-2.html", source="a")
    assert (pager["region"], pager["crawl"]) == ("pagination", "queued")
    assert link(items, "/", source="a")["region"] == "header"
    assert link(items, "/alt.html")["region"] == "main"
    assert link(items, "/forum.html")["crawl"] == "nofollow"
    assert link(items, "https://h5p.example.org/embed/1")["crawl"] == "listed"
    external = link(items, "https://de.wikipedia.org/wiki/Bruchrechnung")
    assert (external["kind"], external["crawl"]) == ("external", "out_of_scope")
    canonical_link = link(items, "/", source="link")
    assert (canonical_link["rel"], canonical_link["crawl"]) == (["canonical"], "listed")
    area = link(items, "/physik/", source="area")
    assert area["target"] == link(items, "/physik/", source="a")["target"]  # one item, two links
    for path in ("/forum.html", "/material/arbeitsblatt.pdf", "/mathe/level3.html"):
        assert path not in requested_paths(httpserver)

    # The cycle: deeper pages link back to items that exist, and depth 3 is not fetched.
    brueche = next(i for i in pages(items) if i["url"] == f"{origin}/mathe/brueche.html")
    assert site_of(brueche)["depth"] == 2
    back = {path_of(found["url"], origin): found for found in links(brueche)}
    assert (back["/"]["crawl"], back["/"]["target"]) == ("seen", 0)  # too deep, but known
    assert back["/mathe/level3.html"]["crawl"] == "depth"
    assert back["/mathe/level3.html"]["target"] is None
    # The redirect: /neu.html is the item of /alt.html, found by its alias. This needs /alt.html
    # (depth 1) to run before /mathe/brueche.html (depth 2) links /neu.html: true here, with one
    # worker slot and the queue's FIFO per host. A link to /neu.html seen before /alt.html ran
    # would make an item of its own (architecture.md, "Site structure", known limits).
    alt = next(i for i in pages(items) if i["url"] == f"{origin}/alt.html")
    assert alt["result"]["data"]["final_url"] == f"{origin}/neu.html"
    assert back["/neu.html"]["target"] == alt["index"]
    assert "/neu.html" in requested_paths(httpserver)
    # Structure only: no page keeps its text.
    assert all(i["result"]["data"]["content"] is None for i in items)

    # robots.txt and the sitemaps: the two listed files, the index's two children.
    [robots] = by_role(items, "robots")
    assert site_of(robots)["sitemaps"] == [f"{origin}/sitemaps/index.xml", f"{origin}/sitemap.txt"]
    sitemaps = {path_of(i["url"], origin): site_of(i) for i in by_role(items, "sitemap")}
    assert set(sitemaps) == {
        "/sitemaps/index.xml",
        "/sitemap.txt",
        "/sitemaps/seiten.xml",
        "/sitemaps/material.xml.gz",
    }
    index = sitemaps["/sitemaps/index.xml"]["sitemap"]
    assert index["format"] == "index"
    assert [path_of(child["url"], origin) for child in index["children"]] == [
        "/sitemaps/seiten.xml",
        "/sitemaps/material.xml.gz",
    ]
    assert all(child["crawl"] == "queued" for child in index["children"])
    assert all(
        items_by_index[child["target"]]["url"] == child["url"] for child in index["children"]
    )
    assert sitemaps["/sitemaps/seiten.xml"]["depth"] == 1
    assert sitemaps["/sitemaps/material.xml.gz"]["sitemap"]["format"] == "urlset"
    assert sitemaps["/sitemaps/material.xml.gz"]["sitemap"]["entry_count"] == 2
    assert sitemaps["/sitemap.txt"]["sitemap"]["format"] == "text"
    assert by_role(items, "sitemap_part") == []  # small files fit their item
    assert "/sitemap.xml" not in requested_paths(httpserver)  # robots.txt listed sitemaps


def test_site_b_guessed_sitemap_in_parts(service: Service, httpserver: HTTPServer) -> None:
    origin = serve_site(httpserver, "b", sitemap_entries=SITE_B_ENTRIES)

    job, items = service.crawl(url=f"{origin}/", depth=0)

    assert job["state"] == "completed"
    assert job["progress"]["failed"] == 0
    [start] = pages(items)
    assert [found["crawl"] for found in links(start)] == ["depth"]
    [robots] = by_role(items, "robots")
    assert robots["result"]["status"] == "success"
    assert site_of(robots)["sitemaps"] == [f"{origin}/sitemap.xml"]
    [sitemap] = by_role(items, "sitemap")
    info = site_of(sitemap)["sitemap"]
    assert (info["format"], info["guessed"], info["entry_count"]) == ("urlset", True, 300)
    parts = by_role(items, "sitemap_part")
    assert len(parts) == info["parts"] - 1 > 0  # too many entries for one item
    assert [site_of(part)["sitemap"]["part"] for part in parts] == list(range(2, info["parts"] + 1))
    assert all(site_of(part)["parent"] == sitemap["index"] for part in parts)
    assert all(part["url"] == sitemap["url"] for part in parts)
    assert listed_locs(items) == {
        f"{origin}/aufgabe-{number}.html" for number in range(1, SITE_B_ENTRIES + 1)
    }
    entries = [entry for item in [sitemap, *parts] for entry in site_of(item)["sitemap"]["entries"]]
    assert len(entries) == SITE_B_ENTRIES  # each entry once, in order
    assert entries[0] == {"loc": f"{origin}/aufgabe-1.html", "lastmod": "2026-10-01"}
    assert "/aufgabe-1.html" not in requested_paths(httpserver)  # entries are not crawled
