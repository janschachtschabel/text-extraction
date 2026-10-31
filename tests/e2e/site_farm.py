"""A farm of test sites for the end-to-end batch run: one threaded HTTP server per loopback
address, all on one port.

    python -m tests.e2e.site_farm --port 18000 --hosts 10 --record farm-record.json

Every host serves ``/p/<n>.html``, a 600-word article sent after a random 150-600 ms delay, and
answers 404 after about 300 ms to anything else (``robots.txt``, ``/.well-known/tdmrep.json``,
``ai.txt``): slow enough that turns overlap and cold turns share their file loads. It
records, per host, the start and end (``time.monotonic()`` of this process) of every request it
sees, whatever the path. On SIGTERM or SIGINT it writes the record as JSON to ``--record`` and
exits. Once every server listens it prints ``ready port=<port>`` on stdout. ``--port 0`` lets the
system pick a free port (one host only, with ``--bind``).

The addresses are ``127.0.0.2`` … ``127.0.0.<hosts + 1>``: Linux routes all of ``127.0.0.0/8``
to the loopback interface, so they need no setup. ``--bind <address>`` serves one host on that
address instead (``--hosts`` is ignored): the load test's farm container listens with
``--bind 0.0.0.0`` and is reached by its compose service name.
"""

import argparse
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
_ARTICLE_WORDS = 600
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


def host_addresses(count: int) -> list[str]:
    """The farm's host addresses: ``127.0.0.2`` onwards."""
    if not 1 <= count <= 250:
        raise ValueError("the farm has 1 to 250 hosts")
    return [f"127.0.0.{n}" for n in range(2, count + 2)]


def article(number: int) -> bytes:
    """A deterministic HTML page with a 600-word article."""
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
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        f"<title>Reading room report {number}</title></head>\n"
        f"<body><nav><a href='/'>Home</a></nav>\n<article><h1>Reading room report {number}</h1>\n"
        f"{body}\n</article>\n<footer>Test site farm</footer></body></html>\n"
    ).encode()


class _Record:
    """Requests seen per host, safe to append to from many threads."""

    def __init__(self, hosts: list[str]) -> None:
        self._lock = threading.Lock()
        self._requests: dict[str, list[dict[str, Any]]] = {host: [] for host in hosts}

    def add(self, host: str, path: str, status: int, start: float, end: float) -> None:
        with self._lock:
            self._requests[host].append(
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
        if match:
            time.sleep(random.uniform(_MIN_DELAY_SECONDS, _MAX_DELAY_SECONDS))  # noqa: S311
            status, body, kind = 200, article(int(match.group(1))), "text/html; charset=utf-8"
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

    def log_message(self, format: str, *args: Any) -> None:
        pass  # the record is the log


class _FarmServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, host: str, port: int, record: _Record) -> None:
        super().__init__((host, port), _Handler)
        self.host = host
        self.record = record


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--hosts", type=int, default=10)
    parser.add_argument(
        "--bind", default=None, metavar="ADDRESS", help="serve one host on this address"
    )
    parser.add_argument("--record", type=Path, required=True, help="JSON file written on exit")
    args = parser.parse_args(argv)

    hosts = [args.bind] if args.bind else host_addresses(args.hosts)
    if args.port == 0 and len(hosts) > 1:
        parser.error("--port 0 needs --bind: every host must listen on the same port")
    record = _Record(hosts)
    servers = [_FarmServer(host, args.port, record) for host in hosts]
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
