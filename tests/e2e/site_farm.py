"""A farm of test sites for the end-to-end batch run: one threaded HTTP server per loopback
address, all on one port.

    python -m tests.e2e.site_farm --port 18000 --hosts 10 --record farm-record.json

Every host serves ``/p/<n>.html``, a 600-word article sent after a random 150-600 ms delay, and
answers 404 after about 300 ms to anything else (``robots.txt``, ``/.well-known/tdmrep.json``,
``ai.txt``): slow enough that turns overlap and cold turns share their file loads. Each article
links to ten others (``related``), so a crawl from any page finds as many pages as its caps allow,
and no link answers 404. It also serves the sitemaps ``/sitemap-<name>.xml`` of ``SITEMAPS``, built
on the fly (and kept in memory once built): ``200k`` lists 200,000 pages, ``50m-1`` to ``50m-3``
list 50,000 pages each with an image caption that fills the file to just under 50 MiB. It records,
per host, the start and end (``time.monotonic()`` of this process) of every request it sees,
whatever the path. On SIGTERM or SIGINT it writes the record as JSON to ``--record`` and exits.
Once every server listens it prints ``ready port=<port>`` on stdout. ``--port 0`` lets the system
pick a free port (one host only, with ``--bind``).

The addresses are ``127.0.0.2`` … ``127.0.0.<hosts + 1>``: Linux routes all of ``127.0.0.0/8``
to the loopback interface, so they need no setup. ``--bind <address>`` serves one host on that
address instead (``--hosts`` is ignored): the load test's farm container listens with
``--bind 0.0.0.0`` and is reached by its compose service name.

``--sitemap-sites`` makes every host listen on the ports ``port + offset`` of
``SITEMAP_SITES`` too, where ``robots.txt`` lists those sitemaps (the main port's answers 404).
robots.txt belongs to an origin, so a crawl picks its sitemaps by the port of its start page.
The record holds these ports' requests under ``<host>:<port>``. The farm builds those sitemaps
before it listens (``prebuild_sitemaps``), for the origins ``http://<name>:<port + offset>``
with the names of ``--sitemap-host`` (default: the hosts it serves), so no timed request waits
for a build; the ``Host`` the client sends picks the origin.
"""

import argparse
import functools
import json
import random
import re
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import FrameType
from typing import Any

_PAGE = re.compile(r"/p/(\d{1,6})\.html")
_SITEMAP = re.compile(r"/sitemap-([0-9a-z-]+)\.xml")
_PAGES = 1_000_000  # /p/0.html to /p/999999.html
_ARTICLE_WORDS = 600
RELATED_LINKS = 10
_MIN_DELAY_SECONDS = 0.15
_MAX_DELAY_SECONDS = 0.6
_FILE_DELAY_SECONDS = 0.3

_TEXT = (
    "the school library opened a new reading room where pupils and teachers meet after "
    "lessons to study history science music and art together with friends from other "
    "classes every week a different group prepares short talks about rivers forests "
    "mountains cities old machines famous inventions simple experiments and stories from "
    "many countries the room has long tables bright windows quiet corners and shelves full "
    "of books maps journals and drawings made by children of all ages visitors often say "
    "that learning feels easier when people share questions answers and ideas in an open "
    "friendly place where nobody is in a hurry"
)
_VOCABULARY = _TEXT.split()

# The sitemaps the farm serves: name -> (first page, pages listed, bytes the file fills with
# its image captions, or None for plain entries). 50,000 URLs and 50 MiB are the sitemap
# protocol's limits per file; the default SITE_SITEMAP_MAX_BYTES is 50 MiB, so these stay just
# under it.
SITEMAP_50M_BYTES = 50 * 2**20 - 64 * 2**10
SITEMAPS: dict[str, tuple[int, int, int | None]] = {
    "200k": (0, 200_000, None),
    "50m-1": (0, 50_000, SITEMAP_50M_BYTES),
    "50m-2": (50_000, 50_000, SITEMAP_50M_BYTES),
    "50m-3": (100_000, 50_000, SITEMAP_50M_BYTES),
}
# The sites of ``--sitemap-sites``: port offset -> the sitemaps its robots.txt lists.
SITEMAP_SITES: dict[int, tuple[str, ...]] = {
    1: ("200k",),
    2: ("50m-1",),
    3: ("50m-1", "50m-2", "50m-3"),
}
_SITEMAP_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
    'xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">\n'
)
_SITEMAP_TAIL = "</urlset>\n"


def host_addresses(count: int) -> list[str]:
    """The farm's host addresses: ``127.0.0.2`` onwards."""
    if not 1 <= count <= 250:
        raise ValueError("the farm has 1 to 250 hosts")
    return [f"127.0.0.{n}" for n in range(2, count + 2)]


def related(number: int) -> list[int]:
    """The pages article ``number`` links to: ``number * 10 + 1`` to ``number * 10 + 10``,
    modulo the farm's page count. Ten links a page reach 1,110 pages within depth 3."""
    return [(number * RELATED_LINKS + offset) % _PAGES for offset in range(1, RELATED_LINKS + 1)]


def article(number: int) -> bytes:
    """A deterministic HTML page with a 600-word article and links to its ``related`` pages."""
    rng = random.Random(number)  # noqa: S311 - test content, not security related
    words: list[str] = []
    paragraphs: list[str] = []
    while len(words) < _ARTICLE_WORDS:
        sentences = []
        for _ in range(rng.randint(3, 6)):
            sentence = [rng.choice(_VOCABULARY) for _ in range(rng.randint(8, 16))]
            words.extend(sentence)
            sentences.append(" ".join(sentence).capitalize() + ".")
        paragraphs.append("<p>" + " ".join(sentences) + "</p>")
    body = "\n".join(paragraphs)
    links = " ".join(f"<a href='/p/{page}.html'>Report {page}</a>" for page in related(number))
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        f"<title>Reading room report {number}</title></head>\n"
        f"<body><nav>{links}</nav>\n<article><h1>Reading room report {number}</h1>\n"
        f"{body}\n</article>\n<footer>Test site farm</footer></body></html>\n"
    ).encode()


def robots(origin: str, sitemaps: tuple[str, ...]) -> bytes:
    """A robots.txt that allows everything and lists ``sitemaps`` under ``origin``."""
    lines = ["User-agent: *", "Allow: /"]
    lines += [f"Sitemap: {origin}/sitemap-{name}.xml" for name in sitemaps]
    return ("\n".join(lines) + "\n").encode()


def _caption(length: int) -> str:
    text = (_TEXT + " ") * (length // (len(_TEXT) + 1) + 1)
    return text[:length]


@functools.cache
def sitemap(name: str, origin: str) -> bytes:
    """The sitemap ``name`` of ``SITEMAPS``, its URLs under ``origin``. With a size, every
    entry gets an image whose caption pads it to the same length, so the file fills the size
    to within one entry."""
    first, pages, size = SITEMAPS[name]
    numbers = range(first, first + pages)

    def entry(number: int, caption: str | None) -> str:
        loc = f"<url><loc>{origin}/p/{number}.html</loc><lastmod>2026-10-01</lastmod>"
        if caption is None:
            return loc + "</url>\n"
        image = f"<image:image><image:loc>{origin}/img/{number}.png</image:loc>"
        return f"{loc}{image}<image:caption>{caption}</image:caption></image:image></url>\n"

    if size is None:
        entries = [entry(number, None) for number in numbers]
    else:
        width = (size - len(_SITEMAP_HEAD) - len(_SITEMAP_TAIL)) // pages
        entries = []
        for number in numbers:
            bare = len(entry(number, ""))
            entries.append(entry(number, _caption(width - bare)))
    return (_SITEMAP_HEAD + "".join(entries) + _SITEMAP_TAIL).encode()


class _Record:
    """Requests seen per host, safe to append to from many threads."""

    def __init__(self, hosts: list[str]) -> None:
        self._lock = threading.Lock()
        self._requests: dict[str, list[dict[str, Any]]] = {host: [] for host in hosts}

    def track(self, host: str) -> None:
        """List ``host`` in the record, with or without requests."""
        with self._lock:
            self._requests.setdefault(host, [])

    def hosts(self) -> list[str]:
        with self._lock:
            return list(self._requests)

    def add(self, host: str, path: str, status: int, start: float, end: float) -> None:
        with self._lock:
            self._requests.setdefault(host, []).append(
                {"path": path, "status": status, "start": start, "end": end}
            )

    def dump(self, path: Path) -> None:
        with self._lock:
            # The offset turns the monotonic times into epoch seconds, for matching with logs.
            offset = time.time() - time.monotonic()
            data = {"clock": "monotonic", "epoch_offset": offset, "hosts": self._requests}
            path.write_text(json.dumps(data))


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, as the service's HTTP client expects
    server: "_FarmServer"

    def do_GET(self) -> None:
        start = time.monotonic()
        match = _PAGE.fullmatch(self.path)
        listed = _SITEMAP.fullmatch(self.path)
        sitemaps = self.server.sitemaps
        if match:
            time.sleep(random.uniform(_MIN_DELAY_SECONDS, _MAX_DELAY_SECONDS))  # noqa: S311
            status, body, kind = 200, article(int(match.group(1))), "text/html; charset=utf-8"
        elif self.path == "/robots.txt" and sitemaps:
            status, body, kind = 200, robots(self._origin(), sitemaps), "text/plain; charset=utf-8"
        elif listed and listed.group(1) in SITEMAPS:
            body = sitemap(listed.group(1), self._origin())
            status, kind = 200, "application/xml"
        else:
            time.sleep(_FILE_DELAY_SECONDS)
            status, body, kind = 404, b"not found\n", "text/plain; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.server.record.add(self.server.host, self.path, status, start, time.monotonic())

    def _origin(self) -> str:
        """The origin the client asked for (its ``Host`` header), else the server's address."""
        host = self.headers.get("Host") or "{}:{}".format(*self.server.server_address[:2])
        return f"http://{host}"

    def log_message(self, format: str, *args: Any) -> None:
        pass  # the record is the log


class _FarmServer(ThreadingHTTPServer):
    """One host on one port; ``sitemaps`` are what its robots.txt lists (none: 404), and the
    record keeps its requests under ``record_as``."""

    daemon_threads = True

    def __init__(
        self,
        host: str,
        port: int,
        record: _Record,
        *,
        sitemaps: tuple[str, ...] = (),
        record_as: str | None = None,
    ) -> None:
        super().__init__((host, port), _Handler)
        self.host = record_as or host
        self.record = record
        self.sitemaps = sitemaps


def build_servers(
    hosts: list[str], port: int, record: _Record, *, sitemap_sites: bool
) -> list[_FarmServer]:
    """A server per host on ``port`` and, with ``sitemap_sites``, one per host and site of
    ``SITEMAP_SITES`` on ``port + offset``; each is listed in ``record``."""
    servers = [_FarmServer(host, port, record) for host in hosts]
    for host in hosts:
        for offset, sitemaps in SITEMAP_SITES.items() if sitemap_sites else ():
            name = f"{host}:{port + offset}"
            servers.append(
                _FarmServer(host, port + offset, record, sitemaps=sitemaps, record_as=name)
            )
    for server in servers:
        record.track(server.host)
    return servers


def prebuild_sitemaps(names: list[str], port: int) -> None:
    """Build the sitemaps of every site of ``SITEMAP_SITES`` for ``http://<name>:<port>``,
    the origins the clients will ask for, so their first request is served from memory."""
    for name in names:
        for offset, sitemaps in SITEMAP_SITES.items():
            for listed in sitemaps:
                sitemap(listed, f"http://{name}:{port + offset}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--hosts", type=int, default=10)
    parser.add_argument(
        "--bind", default=None, metavar="ADDRESS", help="serve one host on this address"
    )
    parser.add_argument("--record", type=Path, required=True, help="JSON file written on exit")
    parser.add_argument(
        "--sitemap-sites",
        action="store_true",
        help="also listen on port + 1 to port + 3, whose robots.txt lists the farm's sitemaps",
    )
    parser.add_argument(
        "--sitemap-host",
        action="append",
        default=None,
        metavar="NAME",
        help="the host name the clients use (repeatable), for the sitemaps built at start-up; "
        "default: the served hosts",
    )
    args = parser.parse_args(argv)

    hosts = [args.bind] if args.bind else host_addresses(args.hosts)
    if args.port == 0 and len(hosts) > 1:
        parser.error("--port 0 needs --bind: every host must listen on the same port")
    if args.port == 0 and args.sitemap_sites:
        parser.error("--sitemap-sites needs a fixed --port: its sites are at port + offset")
    if args.sitemap_sites:
        prebuild_sitemaps(args.sitemap_host or hosts, args.port)
    record = _Record([])
    servers = build_servers(hosts, args.port, record, sitemap_sites=args.sitemap_sites)
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()

    stop = threading.Event()

    def on_signal(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    print(f"ready port={servers[0].server_address[1]}", flush=True)
    while not stop.wait(0.2):
        pass
    for server in servers:
        server.shutdown()
        server.server_close()
    record.dump(args.record)
    sys.exit(0)


if __name__ == "__main__":
    main()
