"""Hostile probes against a running service (audit R7): run by hand, never in CI.

    uv run python -m tests.e2e.audit_probes --base-url http://localhost:8080 [--api-key KEY]
    uv run python -m tests.e2e.audit_probes --ssrf --base-url http://localhost:8080

The default run starts a hostile site farm on ``--farm-host`` (127.0.0.5) and ``--farm-port``
(8999) and asks the service for its pages. The service must be allowed to reach the farm:
start it with ``FETCH_ALLOW_PRIVATE_NETWORKS=true`` (and ``BROWSER_ENABLED=true`` with a browser
worker for ``--browser``). The farm serves a redirect loop, a 30 MB body, a gzip bomb, a body
that drips one byte a second, a PNG declared ``text/html``, a Latin-1 page behind a UTF-8 header
and one plain article as a control.

``--ssrf`` runs the address probes instead, against a service started with
``FETCH_ALLOW_PRIVATE_NETWORKS=false`` (the default): loopback, link-local metadata and private
addresses in their various spellings, and URL schemes that must never be fetched.

Give each run an empty Valkey database: the compliance files' outcomes are cached per host, so
a farm run (private networks allowed) would leave the loopback hosts' robots.txt as
"unavailable" for a later ``--ssrf`` run, which then answers ``connection_error``.

Every probe names the answer it expects (the envelope's ``error.code``, or ``success`` with a
warning); the run prints a Markdown table and exits 1 when a probe got something else.
"""

import argparse
import contextlib
import functools
import sys
import threading
import time
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

_TOPICS = (
    "photosynthesis",
    "glaciers",
    "volcanoes",
    "tides",
    "rivers",
    "clouds",
    "deserts",
    "forests",
    "coral reefs",
    "earthquakes",
)
ARTICLE = (
    "<!doctype html><html><head><title>Control</title></head><body><article><h1>Nature</h1>"
    + "".join(
        f"<p>Section {n} explains how {topic} shape the planet over long periods of time, why "
        f"{topic} matter for people, and what Größe, Käse and Übung have to do with them.</p>"
        for n, topic in enumerate(_TOPICS * 2)
    )
    + "</article></body></html>"
)
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 2000
BIG_BYTES = 30_000_000
BOMB_DECODED_BYTES = 1_000_000_000
DRIP_SECONDS = 120


@functools.cache
def gzip_bomb() -> bytes:
    """``BOMB_DECODED_BYTES`` of HTML whitespace, about 1 MB on the wire."""
    packer = zlib.compressobj(9, zlib.DEFLATED, 31)  # 31: a gzip header
    chunk = b" " * 1_000_000
    parts = [packer.compress(b"<!doctype html><html><body><p>")]
    parts.extend(packer.compress(chunk) for _ in range(BOMB_DECODED_BYTES // len(chunk)))
    parts.append(packer.compress(b"</p></body></html>") + packer.flush())
    return b"".join(parts)


class Farm(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            if path == "/robots.txt":
                self._send(b"User-agent: *\nAllow: /\n", "text/plain")
            elif path == "/ok":
                self._send(ARTICLE.encode(), "text/html; charset=utf-8")
            elif path == "/loop":
                self._send(b"", "text/html", status=302, headers={"Location": "/loop"})
            elif path == "/big":
                self._big()
            elif path == "/bomb":
                self._send(gzip_bomb(), "text/html", headers={"Content-Encoding": "gzip"})
            elif path == "/drip":
                self._drip()
            elif path == "/png-as-html":
                self._send(PNG, "text/html")
            elif path == "/latin1":
                self._send(ARTICLE.encode("cp1252"), "text/html; charset=utf-8")
            else:
                self._send(b"not found", "text/plain", status=404)

    def _send(
        self,
        body: bytes,
        content_type: str,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _big(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(BIG_BYTES))
        self.end_headers()
        chunk = b"<p>" + b"x" * 996 + b"</p>"
        for _ in range(BIG_BYTES // len(chunk)):
            self.wfile.write(chunk)

    def _drip(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(DRIP_SECONDS))
        self.end_headers()
        for _ in range(DRIP_SECONDS):
            self.wfile.write(b" ")
            self.wfile.flush()
            time.sleep(1)


@contextlib.contextmanager
def farm(host: str, port: int) -> Iterator[str]:
    server = ThreadingHTTPServer((host, port), Farm)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


@dataclass(frozen=True)
class Probe:
    name: str
    body: dict[str, Any]
    expected: str  # an error code, or "success"
    warning: str | None = None  # a warning the success must carry


def farm_probes(base: str, *, browser: bool) -> list[Probe]:
    probes = [
        Probe("control article", {"url": f"{base}/ok"}, "success"),
        Probe("redirect loop", {"url": f"{base}/loop"}, "target_http_error"),
        Probe("30 MB body", {"url": f"{base}/big"}, "too_large"),
        Probe("gzip bomb", {"url": f"{base}/bomb"}, "too_large"),
        Probe("slow drip", {"url": f"{base}/drip"}, "timeout"),
        Probe(
            "PNG declared text/html (B1)",
            {"url": f"{base}/png-as-html"},
            "unsupported_content_type",
        ),
        Probe(
            "Latin-1 behind a UTF-8 header (B2)",
            {"url": f"{base}/latin1"},
            "success",
            warning="charset_mismatch",
        ),
    ]
    if browser:
        probes += [
            Probe("browser: control article", {"url": f"{base}/ok", "mode": "browser"}, "success"),
            Probe(
                "browser: gzip bomb (B3)", {"url": f"{base}/bomb", "mode": "browser"}, "too_large"
            ),
        ]
    return [Probe(p.name, {"cache": False, **p.body}, p.expected, p.warning) for p in probes]


def ssrf_probes() -> list[Probe]:
    blocked = [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://[::1]/",
        "http://0x7f000001/",  # a DNS form: the resolver's answer is refused
        "http://169.254.169.254/latest/meta-data/",
        "http://[::ffff:169.254.169.254]/",
        "http://10.0.0.1/",
        "http://192.168.0.1/",
        "http://[fd00::1]/",
        "http://0.0.0.0/",
    ]
    probes = [Probe(f"private address {url}", {"url": url}, "blocked_url") for url in blocked]
    for url in ("file:///etc/passwd", "javascript:alert(1)", "ftp://example.org/", "gopher://x/"):
        probes.append(Probe(f"scheme {url.split(':')[0]}", {"url": url}, "invalid_url"))
    # digits that are no IPv4 literal (each is 127.0.0.1 to the resolver): refused unread
    for url in ("http://2130706433/", "http://0177.0.0.1/", "http://127.1/"):
        probes.append(Probe(f"numeric host {url}", {"url": url}, "invalid_url"))
    return [Probe(p.name, {"cache": False, **p.body}, p.expected) for p in probes]


def outcome(response: httpx.Response) -> tuple[str, list[str]]:
    """The envelope's error code (or ``success``) and its warnings."""
    try:
        envelope = response.json()
    except ValueError:
        return f"HTTP {response.status_code}, no JSON", []
    error = envelope.get("error") or {}
    data = envelope.get("data") or {}
    warnings = data.get("warnings") or [] if isinstance(data, dict) else []
    if envelope.get("status") == "success":
        return "success", warnings
    return str(error.get("code") or f"HTTP {response.status_code}"), warnings


def run(probes: list[Probe], call: Callable[[dict[str, Any]], httpx.Response]) -> list[str]:
    rows = ["| Probe | Expected | Got | Seconds | |", "| --- | --- | --- | --- | --- |"]
    failed = []
    for probe in probes:
        started = time.monotonic()
        try:
            got, warnings = outcome(call(probe.body))
        except httpx.HTTPError as error:
            got, warnings = f"{type(error).__name__}", []
        seconds = time.monotonic() - started
        ok = got == probe.expected and (probe.warning is None or probe.warning in warnings)
        shown = got if probe.warning is None else f"{got} {warnings}"
        expected = (
            probe.expected if probe.warning is None else f"{probe.expected} + {probe.warning}"
        )
        rows.append(
            f"| {probe.name} | {expected} | {shown} | {seconds:.1f} | {'ok' if ok else 'FAIL'} |"
        )
        if not ok:
            failed.append(probe.name)
    print("\n".join(rows))
    return failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--farm-host", default="127.0.0.5")
    parser.add_argument("--farm-port", type=int, default=8999)
    parser.add_argument("--ssrf", action="store_true", help="run the address probes instead")
    parser.add_argument("--browser", action="store_true", help="add the browser-mode probes")
    args = parser.parse_args(argv)
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=180) as client:

        def call(body: dict[str, Any]) -> httpx.Response:
            return client.post("/v1/extract", json=body)

        if args.ssrf:
            failed = run(ssrf_probes(), call)
        else:
            with farm(args.farm_host, args.farm_port) as base:
                failed = run(farm_probes(base, browser=args.browser), call)
    if failed:
        print(f"\n{len(failed)} probe(s) failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
