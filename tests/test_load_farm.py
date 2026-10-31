"""The site farm's crawl fixtures (``tests/e2e/site_farm.py``, ruling P11-S7): linked pages for
a crawl at the caps, the sitemaps of the load test, and the sites whose robots.txt lists them."""

import re
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.e2e import site_farm
from tests.e2e.site_farm import SITEMAP_SITES, SITEMAPS, article, related, robots, sitemap
from text_extraction.settings import Settings
from text_extraction.site.sitemaps import parse_sitemap

DEFAULTS = Settings.model_fields
ORIGIN = "http://farm:8001"
_HREF = re.compile(r"href='([^']*)'")


@pytest.fixture(autouse=True)
def _drop_the_built_sitemaps() -> Iterator[None]:
    """The farm keeps every sitemap it built (up to 50 MiB each); the tests do not."""
    yield
    sitemap.cache_clear()


def reachable(start: int, depth: int) -> set[int]:
    """The farm pages a crawl from ``start`` finds within ``depth`` links."""
    found, level = {start}, {start}
    for _ in range(depth):
        level = {page for number in level for page in related(number)} - found
        found |= level
    return found


def test_every_article_links_to_its_related_pages_and_nothing_else() -> None:
    """No link of a farm page answers 404, so a crawl of the farm has no failed item."""
    for number in (0, 7, 8000, 999_999):
        hrefs = _HREF.findall(article(number).decode())
        assert hrefs == [f"/p/{page}.html" for page in related(number)], number
        assert len(hrefs) == site_farm.RELATED_LINKS
        assert all(0 <= page < 1_000_000 for page in related(number))


def test_the_links_reach_the_page_caps_of_a_crawl() -> None:
    """Default caps (depth 2, 100 pages) and SITE_MAX_PAGES at depth 3 both run into their page
    cap, not out of pages."""
    assert len(reachable(8000, 2)) > 100
    assert len(reachable(8100, 3)) > DEFAULTS["site_max_pages"].default
    assert DEFAULTS["site_max_depth"].default >= 3


def test_the_200k_sitemap_lists_the_crawls_whole_entry_budget() -> None:
    body = sitemap("200k", ORIGIN)
    parsed = parse_sitemap(body, f"{ORIGIN}/sitemap-200k.xml", max_entries=1_000_000)

    assert parsed.error is None
    assert parsed.format == "urlset"
    assert not parsed.truncated
    assert parsed.entry_count == DEFAULTS["site_max_sitemap_urls"].default == 200_000
    assert len(body) < DEFAULTS["site_sitemap_max_bytes"].default
    assert parsed.entries[0].loc == f"{ORIGIN}/p/0.html"
    assert parsed.entries[-1].loc == f"{ORIGIN}/p/199999.html"


@pytest.mark.parametrize("name", ["50m-1", "50m-2", "50m-3"])
def test_a_50m_sitemap_fills_just_under_the_fetch_cap(name: str) -> None:
    """50,000 URLs (the protocol's limit per file) padded by image captions to just under
    SITE_SITEMAP_MAX_BYTES, so the fetch is not cut and the parse reads the whole file."""
    body = sitemap(name, ORIGIN)
    parsed = parse_sitemap(body, f"{ORIGIN}/sitemap-{name}.xml", max_entries=1_000_000)

    assert 49 * 2**20 < len(body) < DEFAULTS["site_sitemap_max_bytes"].default
    assert parsed.error is None
    assert not parsed.truncated
    assert parsed.entry_count == 50_000
    assert all(entry.loc.startswith(f"{ORIGIN}/p/") for entry in parsed.entries)


def test_the_50m_sitemaps_list_different_pages() -> None:
    """Three files, not one file three times."""
    firsts = set()
    for name in ("50m-1", "50m-2", "50m-3"):
        parsed = parse_sitemap(sitemap(name, ORIGIN), ORIGIN, max_entries=1, stop_at_cap=True)
        firsts.add(parsed.entries[0].loc)
    assert len(firsts) == 3
    assert set(SITEMAPS) == {"200k", "50m-1", "50m-2", "50m-3"}


def test_robots_lists_the_sitemaps_under_the_origin() -> None:
    text = robots(ORIGIN, ("50m-1", "50m-2")).decode()

    assert "User-agent: *" in text
    assert "Disallow" not in text
    assert re.findall(r"^Sitemap: (.*)$", text, re.MULTILINE) == [
        f"{ORIGIN}/sitemap-50m-1.xml",
        f"{ORIGIN}/sitemap-50m-2.xml",
    ]


def test_the_sitemap_sites_give_each_scenario_its_own_robots() -> None:
    assert SITEMAP_SITES == {
        1: ("200k",),
        2: ("50m-1",),
        3: ("50m-1", "50m-2", "50m-3"),
    }
    assert all(name in SITEMAPS for names in SITEMAP_SITES.values() for name in names)


def _get(url: str) -> tuple[int, str, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=30) as answer:  # noqa: S310 - local farm
            return answer.status, answer.headers["Content-Type"], answer.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers["Content-Type"], error.read()


@contextmanager
def serving(sitemaps: tuple[str, ...]) -> Iterator[int]:
    """One farm server on a free port, with ``sitemaps`` in its robots.txt."""
    record = site_farm._Record(["127.0.0.1"])
    server = site_farm._FarmServer("127.0.0.1", 0, record, sitemaps=sitemaps)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_a_sitemap_site_serves_its_robots_and_sitemaps() -> None:
    with serving(("200k",)) as port:
        origin = f"http://127.0.0.1:{port}"
        status, kind, body = _get(f"{origin}/robots.txt")
        assert status == 200
        assert kind.startswith("text/plain")
        assert f"Sitemap: {origin}/sitemap-200k.xml" in body.decode()

        status, kind, body = _get(f"{origin}/sitemap-200k.xml")
        assert status == 200
        assert kind == "application/xml"
        assert body == sitemap("200k", origin)

        assert _get(f"{origin}/sitemap-unknown.xml")[0] == 404


def test_the_main_site_has_no_robots_txt() -> None:
    with serving(()) as port:
        origin = f"http://127.0.0.1:{port}"
        assert _get(f"{origin}/robots.txt")[0] == 404
        assert _get(f"{origin}/p/3.html")[0] == 200


def _free_block(size: int) -> int:
    """A port whose ``size - 1`` successors are free too (checked by binding them)."""
    for _ in range(50):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            base: int = probe.getsockname()[1]
        if base + size > 65535:
            continue
        sockets: list[socket.socket] = []
        try:
            for port in range(base, base + size):
                held = socket.socket()
                sockets.append(held)
                held.bind(("127.0.0.1", port))
            return base
        except OSError:
            continue
        finally:
            for held in sockets:
                held.close()
    raise RuntimeError("no block of free ports")


def test_main_with_sitemap_sites_listens_at_the_offsets(tmp_path: Path) -> None:
    base = _free_block(1 + max(SITEMAP_SITES))
    record = site_farm._Record([])
    servers = site_farm.build_servers(["127.0.0.1"], base, record, sitemap_sites=True)
    try:
        ports = {server.server_address[1]: server.sitemaps for server in servers}
        assert ports == {base: (), **{base + k: names for k, names in SITEMAP_SITES.items()}}
        names = {server.host for server in servers}
        assert names == {"127.0.0.1", *(f"127.0.0.1:{base + k}" for k in SITEMAP_SITES)}
        # Every server is in the record from the start, so its record lists it even unused.
        assert set(record.hosts()) == names
    finally:
        for server in servers:
            server.server_close()
    [plain] = site_farm.build_servers(["127.0.0.1"], 0, record, sitemap_sites=False)
    plain.server_close()
    assert plain.sitemaps == ()


def test_sitemap_sites_need_a_fixed_port(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        site_farm.main(
            ["--bind", "127.0.0.1", "--port", "0", "--sitemap-sites", "--record", str(tmp_path)]
        )
