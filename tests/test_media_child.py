"""The media children (``media/pool.py``, ``media/child.py``): yt-dlp and youtube-transcript-api
in child processes behind the egress proxy.

The sandbox reaches no platform, so the real yt-dlp runs against a local page with the
test-only extractor allowlist ``generic|html5`` (a regular expression, as yt-dlp's
``allowed_extractors`` takes them: the generic extractor finds ``<video>`` elements through the
``html5`` one) and a proxy policy that lets loopback through, only here. The rest of the policy is
the production one, so a redirect to a private address is still refused.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from typing import Any

import pytest
from prometheus_client import CollectorRegistry
from pytest_httpserver import HTTPServer

from text_extraction.browser_proxy import EgressProxy, ProxyContext
from text_extraction.documents.pool import ConversionError, ConversionTimeout, DocumentPool
from text_extraction.media.captions import parse_captions
from text_extraction.media.pool import (
    MEDIA_TARGET,
    MediaFetcher,
    _refusal,
    create_media_fetcher,
    create_media_pool,
)
from text_extraction.media.sites import MediaTarget
from text_extraction.netguard import is_public_address
from text_extraction.settings import Settings

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
type Payload = bytes | dict[str, Any]  # of DocumentPool.run
FAKE_TARGET = "tests.media_pool_targets"
LOCAL_EXTRACTOR = "generic|html5"
VTT = (
    "WEBVTT\n\n"
    "00:00:01.000 --> 00:00:02.500\nDas ist der erste Satz.\n\n"
    "00:00:03.000 --> 00:00:04.000\nUnd der zweite.\n"
)


def loopback_or_public(address: IPv4Address | IPv6Address) -> bool:
    """The production policy, plus loopback for the local test site."""
    return address.is_loopback or is_public_address(address)


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"auth_mode": "none", "media_concurrency": 1}
    values.update(overrides)
    return Settings(**values)


def run[T](scenario: Callable[[], Awaitable[T]]) -> T:
    return asyncio.run(asyncio.wait_for(scenario(), 90))


def deadline_in(seconds: float) -> float:
    return asyncio.get_running_loop().time() + seconds


@asynccontextmanager
async def fetcher(
    config: Settings,
    *,
    target: str = MEDIA_TARGET,
    policy: Callable[[IPv4Address | IPv6Address], bool] = loopback_or_public,
    env: dict[str, str] | None = None,
) -> AsyncIterator[MediaFetcher]:
    pool = DocumentPool(
        config.media_concurrency,
        config.media_memory_limit_mb,
        target=target,
        max_result_bytes=config.media_subtitle_max_bytes + 2 * 1024 * 1024,
        extra_env={"PYTHONPATH": REPO_ROOT, **(env or {})},
    )
    proxy = EgressProxy(config, policy=policy, registry=CollectorRegistry())
    media = MediaFetcher(config, pool, proxy, allowed_extractors={LOCAL_EXTRACTOR, "youtube"})
    try:
        yield media
    finally:
        await media.stop()


def local_target(url: str, video_id: str = "local") -> MediaTarget:
    return MediaTarget("arte", url, LOCAL_EXTRACTOR, video_id)


def page(track_src: str) -> str:
    return (
        "<html><head><title>Lesson</title></head><body>"
        '<video controls><source src="clip.mp4" type="video/mp4">'
        f'<track kind="subtitles" src="{track_src}" srclang="de" label="Deutsch"></video>'
        "</body></html>"
    )


def record_proxied_requests(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None]]:
    """``(method, path)`` of every request the egress proxy forwards (path None: a tunnel)."""
    forwarded: list[tuple[str, str | None]] = []
    tunnel = EgressProxy._tunnel

    async def recording(
        proxy: EgressProxy, context: Any, request: Any, reader: Any, writer: Any
    ) -> None:
        forwarded.append((request.method, request.path))
        await tunnel(proxy, context, request, reader, writer)

    monkeypatch.setattr(EgressProxy, "_tunnel", recording)
    return forwarded


def serve_page(httpserver: HTTPServer, track_src: str = "de.vtt") -> str:
    httpserver.expect_request("/lesson.html").respond_with_data(
        page(track_src), content_type="text/html"
    )
    return httpserver.url_for("/lesson.html")


# --- the real yt-dlp against a local site ------------------------------------------------------


def test_ytdlp_info_through_proxy_with_local_site(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = serve_page(httpserver)
    httpserver.expect_request("/de.vtt").respond_with_data(VTT, content_type="text/vtt")
    seen: list[frozenset[str]] = []
    proxied = record_proxied_requests(monkeypatch)

    async def scenario() -> None:
        async with fetcher(settings()) as media:
            open_context = media.proxy.open_context

            def spy(max_bytes: int | None = None) -> Any:
                context = open_context(max_bytes)
                contexts.append((context, max_bytes))
                return context

            contexts: list[Any] = []
            media.proxy.open_context = spy  # type: ignore[method-assign]
            probe = await media.probe(local_target(url), ["de"], deadline_in(60))

            assert probe.error is None
            assert not probe.blocked
            assert probe.manual == {"de": [probe.manual["de"][0]]}
            assert probe.track is not None
            assert (probe.track.key, probe.track.language) == ("de", "de")
            assert probe.track.source == "manual_subtitles"
            assert probe.track.format == "vtt"
            cues = parse_captions(probe.track.body, probe.track.format, probe.track.charset)
            assert [cue.text for cue in cues] == ["Das ist der erste Satz.", "Und der zweite."]
            assert len(contexts) == 1
            context, max_bytes = contexts[0]
            assert max_bytes == media._settings.media_extract_max_bytes
            seen.append(context.hosts)
            assert context.bytes > len(VTT)

    run(scenario)
    assert seen == [frozenset({"127.0.0.1"})]
    paths = [request.path for request, _response in httpserver.log]
    assert paths[0] == "/lesson.html"
    assert "/de.vtt" in paths
    # Every request the site saw, the track's included, is one the proxy forwarded.
    assert [path for _method, path in proxied] == paths
    agents = {request.headers.get("User-Agent") for request, _response in httpserver.log}
    assert len(agents) == 1
    assert next(iter(agents)).startswith("text-extraction/")  # type: ignore[union-attr]


def test_proxy_refuses_private_redirect(httpserver: HTTPServer) -> None:
    url = serve_page(httpserver)
    httpserver.expect_request("/de.vtt").respond_with_data(
        "", status=302, headers={"Location": "http://169.254.169.254/latest/de.vtt"}
    )

    async def scenario() -> None:
        async with fetcher(settings()) as media:
            probe = await media.probe(local_target(url), ["de"], deadline_in(60))

            assert probe.track is None
            assert probe.error == "blocked_address"
            assert probe.title is not None  # the metadata still came
            assert not probe.blocked

    run(scenario)


def test_track_byte_cap(httpserver: HTTPServer) -> None:
    url = serve_page(httpserver)
    big = VTT + "".join(
        f"\n00:{n // 60:02d}:{n % 60:02d}.000 --> 00:{n // 60:02d}:{n % 60:02d}.500\nSatz {n}.\n"
        for n in range(200)
    )
    httpserver.expect_request("/de.vtt").respond_with_data(big, content_type="text/vtt")

    async def scenario() -> None:
        async with fetcher(settings(media_subtitle_max_bytes=2048)) as media:
            probe = await media.probe(local_target(url), ["de"], deadline_in(60))

            assert len(big) > 2048
            assert probe.track is None
            assert probe.error == "too_large"

    run(scenario)


def test_hls_subtitle_playlist_is_concatenated(httpserver: HTTPServer) -> None:
    """arte and Vimeo deliver subtitles as an HLS playlist of VTT segments (M18)."""
    url = serve_page(httpserver, "subs/de.m3u8")
    playlist = (
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:600\n#EXT-X-MEDIA-SEQUENCE:0\n"
        "#EXTINF:600.0,\nseg-0.vtt\n#EXTINF:600.0,\nseg-1.vtt\n#EXT-X-ENDLIST\n"
    )
    second = (
        "WEBVTT\nX-TIMESTAMP-MAP=MPEGTS:900000,LOCAL:00:00:00.000\n\n"
        "00:10:01.000 --> 00:10:02.000\nNach der Pause.\n"
    )
    httpserver.expect_request("/subs/de.m3u8").respond_with_data(
        playlist, content_type="application/vnd.apple.mpegurl"
    )
    httpserver.expect_request("/subs/seg-0.vtt").respond_with_data(VTT, content_type="text/vtt")
    httpserver.expect_request("/subs/seg-1.vtt").respond_with_data(second, content_type="text/vtt")

    async def scenario() -> None:
        async with fetcher(settings()) as media:
            probe = await media.probe(local_target(url), ["de"], deadline_in(60))

            assert probe.error is None
            assert probe.track is not None
            assert probe.track.format == "vtt"
            cues = parse_captions(probe.track.body, "vtt")
            assert [cue.text for cue in cues] == [
                "Das ist der erste Satz.",
                "Und der zweite.",
                "Nach der Pause.",
            ]

    run(scenario)


def test_hls_subtitle_playlist_over_the_cap_is_too_large(httpserver: HTTPServer) -> None:
    url = serve_page(httpserver, "subs/de.m3u8")
    segments = "".join(f"#EXTINF:10.0,\nseg-{n}.vtt\n" for n in range(5))
    httpserver.expect_request("/subs/de.m3u8").respond_with_data(
        f"#EXTM3U\n#EXT-X-TARGETDURATION:10\n{segments}#EXT-X-ENDLIST\n"
    )
    for n in range(5):
        httpserver.expect_request(f"/subs/seg-{n}.vtt").respond_with_data(
            VTT + "\n" + "x" * 600 + "\n", content_type="text/vtt"
        )

    async def scenario() -> None:
        async with fetcher(settings(media_subtitle_max_bytes=2048)) as media:
            probe = await media.probe(local_target(url), ["de"], deadline_in(60))

            assert probe.track is None
            assert probe.error == "too_large"

    run(scenario)


# Makes urllib bypass the proxy for loopback: the condition under which yt-dlp's urllib handler
# dialed an ``ftp`` URL directly (review of Task 5).
NO_PROXY_LOOPBACK = {"no_proxy": "127.0.0.1,localhost", "NO_PROXY": "127.0.0.1,localhost"}


class Listener:
    """A TCP listener on loopback that only notes the connections it gets."""

    def __init__(self) -> None:
        self.socket = socket.create_server(("127.0.0.1", 0))
        self.socket.settimeout(0.2)
        self.port = self.socket.getsockname()[1]
        self.connections = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _address = self.socket.accept()
            except TimeoutError:
                continue
            self.connections += 1
            connection.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join()
        self.socket.close()


@pytest.mark.parametrize("scheme", ["ftp", "ws", "file"])
def test_hls_segment_with_a_non_http_scheme_is_never_fetched(
    httpserver: HTTPServer, scheme: str
) -> None:
    """A track body that starts with ``#EXTM3U`` is read as a playlist: a segment URI of
    another scheme must not open a connection. yt-dlp's urllib handler takes ``ftp`` and, when
    the environment names the host in ``no_proxy`` (the child gets an allowlisted environment,
    but a guard must not rest on that), dials it directly, past the egress proxy."""
    url = serve_page(httpserver, "subs/de.m3u8")
    target = Listener()
    uri = (
        "file:///etc/hostname" if scheme == "file" else f"{scheme}://127.0.0.1:{target.port}/s.vtt"
    )
    httpserver.expect_request("/subs/de.m3u8").respond_with_data(
        f"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10.0,\n{uri}\n#EXT-X-ENDLIST\n"
    )

    async def scenario() -> None:
        async with fetcher(settings(), env=NO_PROXY_LOOPBACK) as media:
            probe = await media.probe(local_target(url), ["de"], deadline_in(60))

            assert probe.track is None
            assert probe.error == "track_failed"

    try:
        run(scenario)
        time.sleep(0.3)
        assert target.connections == 0
    finally:
        target.close()


_SCHEME_SCRIPT = """
import json, sys
from text_extraction.media import child

proxy_port, target_port = int(sys.argv[1]), int(sys.argv[2])
payload = {"proxy": f"http://u:p@127.0.0.1:{proxy_port}", "extractors": ["youtube"],
           "user_agent": "ua/1", "timeout": 3}
outcomes = {}
with child.youtube_dl(payload) as ydl:
    for url in (f"ftp://127.0.0.1:{target_port}/x", f"ws://127.0.0.1:{target_port}/x",
                f"wss://127.0.0.1:{target_port}/x", "file:///etc/hostname",
                "data:text/plain,hello", f"http://127.0.0.1:{target_port}/x"):
        try:
            ydl.urlopen(url)
            outcomes[url.split(":")[0]] = "opened"
        except Exception as error:
            outcomes[url.split(":")[0]] = type(error).__name__
print(json.dumps(outcomes))
"""


def test_ytdlp_requests_are_http_only_and_go_through_the_proxy() -> None:
    """Every request of the child's yt-dlp, its extractors' included, is http(s) and leaves
    through the proxy: other schemes have no request handler at all."""
    proxy, target = Listener(), Listener()
    try:
        output = subprocess.run(  # noqa: S603 - our own interpreter and script
            [sys.executable, "-c", _SCHEME_SCRIPT, str(proxy.port), str(target.port)],
            env={**os.environ, **NO_PROXY_LOOPBACK},
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout
        time.sleep(0.3)
        outcomes = json.loads(output)

        for scheme in ("ftp", "ws", "wss", "file", "data"):
            assert outcomes[scheme] != "opened", scheme
        assert outcomes["http"] != "opened"  # the dummy proxy answers nothing useful
        assert target.connections == 0
        assert proxy.connections >= 1  # the http request went to the proxy, not the target
    finally:
        proxy.close()
        target.close()


def test_a_page_without_a_track_has_metadata_and_no_error(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/plain.html").respond_with_data(
        '<html><head><title>Plain</title></head><body><video src="clip.mp4"></video></body></html>',
        content_type="text/html",
    )

    async def scenario() -> None:
        async with fetcher(settings()) as media:
            probe = await media.probe(
                local_target(httpserver.url_for("/plain.html")), [], deadline_in(60)
            )

            assert probe.track is None
            assert probe.error is None
            assert probe.manual == {}
            assert probe.title is not None

    run(scenario)


def test_ytdlp_options_follow_ruling_m4() -> None:
    script = textwrap.dedent(
        """
        import json
        from text_extraction.media.child import ytdlp_params
        params = ytdlp_params({
            "proxy": "http://u:p@127.0.0.1:1", "extractors": ["youtube"],
            "user_agent": "ua/1", "timeout": 25,
        })
        params.pop("logger")
        print(json.dumps(params))
        """
    )
    output = subprocess.run(  # noqa: S603 - our own interpreter and script
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    ).stdout
    params = json.loads(output)

    assert params["proxy"] == "http://u:p@127.0.0.1:1"
    assert params["allowed_extractors"] == ["youtube"]
    assert params["socket_timeout"] == 10
    assert params["noplaylist"] is True
    assert params["skip_download"] is True
    assert params["cachedir"] is False
    assert params["extractor_retries"] == 0
    assert params["http_headers"] == {"User-Agent": "ua/1"}
    assert params["js_runtimes"] == {}
    assert params["remote_components"] == []
    assert "cookiefile" not in params
    assert "cookiesfrombrowser" not in params


# A fake youtube-transcript-api in the child module, run in a fresh interpreter: importing the
# child module silences logging process-wide.
_YTA_SCRIPT = """
import json, sys
from types import SimpleNamespace
from youtube_transcript_api import TranscriptsDisabled
from text_extraction.media import child

scenario = sys.argv[1]
SNIPPETS = [SimpleNamespace(text="Hallo & tschüss", start=1.0, duration=1.5)]

class Transcript:
    def __init__(self, code, generated, snippets=SNIPPETS):
        self.language_code, self.is_generated, self.language = code, generated, code.upper()
        self._snippets = snippets
    def fetch(self):
        return self._snippets

class Api:
    def __init__(self, http_client):
        assert http_client.proxies == {"http": "http://u:p@proxy", "https": "http://u:p@proxy"}
        assert http_client.headers["User-Agent"] == "ua/1"
        assert http_client.trust_env is False
    def list(self, video_id):
        if scenario == "disabled":
            raise TranscriptsDisabled(video_id)
        if scenario == "empty":
            return [Transcript("de", True, [])]
        if scenario == "auto_only":
            return [Transcript("de", True)]
        return [Transcript("en", False), Transcript("de", True)]

child.YouTubeTranscriptApi = Api
reply = child.youtube_transcript({
    "video_id": "abc", "proxy": "http://u:p@proxy", "user_agent": "ua/1", "timeout": 5,
    "languages": ["de"], "subtitle_max_bytes": 60 if scenario == "tiny" else 5000,
})
if reply["track"]:
    reply["track"]["body"] = reply["track"]["body"].decode()
print(json.dumps(reply))
"""


def yta_reply(scenario: str) -> dict[str, Any]:
    output = subprocess.run(  # noqa: S603 - our own interpreter and script
        [sys.executable, "-c", _YTA_SCRIPT, scenario], capture_output=True, text=True, check=True
    ).stdout
    reply: dict[str, Any] = json.loads(output)
    return reply


def test_youtube_transcript_keys_generated_tracks_as_original_and_sends_json3() -> None:
    reply = yta_reply("auto_only")

    assert reply["automatic"] == {"de-orig": [{"ext": "json3", "name": "DE"}]}
    assert reply["original_language"] == "de"
    track = reply["track"]
    assert (track["key"], track["language"], track["source"]) == ("de-orig", "de", "auto_subtitles")
    assert track["format"] == "json3"
    cues = parse_captions(track["body"].encode(), "json3")
    assert [(cue.start, cue.end, cue.text) for cue in cues] == [(1.0, 2.5, "Hallo & tschüss")]


def test_youtube_transcript_prefers_requested_auto_over_other_manual() -> None:
    reply = yta_reply("mixed")

    assert reply["manual"] == {"en": [{"ext": "json3", "name": "EN"}]}
    assert reply["track"]["key"] == "de-orig"


@pytest.mark.parametrize(
    ("scenario", "error"), [("disabled", None), ("empty", "empty_track"), ("tiny", "too_large")]
)
def test_youtube_transcript_without_a_track(scenario: str, error: str | None) -> None:
    reply = yta_reply(scenario)

    assert reply["track"] is None
    assert reply["error"] == error


def test_empty_youtube_track_is_blocked_after_ytdlp_fails_too() -> None:
    """An empty 200 caption body is how YouTube asks for a PO token: with yt-dlp blocked as
    well, the probe is ``blocked`` (D1)."""

    async def scenario() -> None:
        async with fetcher(settings(), target=FAKE_TARGET) as media:
            replies = {
                "youtube_transcript": {"error": "empty_track", "track": None},
                "ytdlp_info": {"error": "empty_track", "track": None, "title": "T"},
            }

            async def fake(function_name: str, payload: Payload, deadline: float) -> Any:
                return replies[function_name]

            media.pool.run = fake  # type: ignore[method-assign]
            probe = await media.probe(youtube("emptyvideo0"), ["de"], deadline_in(30))

            assert probe.blocked
            assert probe.title == "T"
            assert probe.error == "empty_track"

    run(scenario)


# --- fake children -----------------------------------------------------------------------------


def youtube(video_id: str) -> MediaTarget:
    return MediaTarget(
        "youtube", f"https://www.youtube.com/watch?v={video_id}", "youtube", video_id
    )


def test_child_never_imported_in_parent() -> None:
    """The parent of the media children never imports yt-dlp or youtube-transcript-api: not
    when the API, the workers and the media pool load, not after a probe. In a fresh
    interpreter, as other tests import yt-dlp in this one."""
    script = textwrap.dedent(
        f"""
        import asyncio, sys
        import text_extraction.app, text_extraction.worker, text_extraction.browser_worker
        from prometheus_client import CollectorRegistry
        from text_extraction.browser_proxy import EgressProxy
        from text_extraction.documents.pool import DocumentPool
        from text_extraction.media.pool import MediaFetcher
        from text_extraction.media.sites import MediaTarget
        from text_extraction.settings import Settings

        async def main():
            config = Settings(auth_mode="none", media_concurrency=1)
            pool = DocumentPool(1, 1024, target={FAKE_TARGET!r},
                                extra_env={{"PYTHONPATH": {REPO_ROOT!r}}})
            media = MediaFetcher(config, pool, EgressProxy(config, registry=CollectorRegistry()))
            target = MediaTarget("youtube", "https://www.youtube.com/watch?v=x", "youtube", "x")
            loop = asyncio.get_running_loop()
            try:
                probe = await media.probe(target, ["de"], loop.time() + 60)
            finally:
                await media.stop()
            assert probe.track is not None
            print(sorted(name for name in ("yt_dlp", "youtube_transcript_api") if name in
                         sys.modules))

        asyncio.run(main())
        """
    )
    output = subprocess.run(  # noqa: S603 - our own interpreter and script
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, timeout=120
    ).stdout

    assert output.strip() == "[]"


def test_youtube_probe_takes_the_track_of_youtube_transcript_and_metadata_of_ytdlp() -> None:
    calls: list[tuple[str, bool | None]] = []

    async def scenario() -> None:
        async with fetcher(settings(), target=FAKE_TARGET) as media:
            run_call = media.pool.run

            async def spy(function_name: str, payload: Payload, deadline: float) -> Any:
                assert isinstance(payload, dict)
                calls.append((function_name, payload.get("fetch_track")))
                return await run_call(function_name, payload, deadline)

            media.pool.run = spy  # type: ignore[method-assign]
            probe = await media.probe(youtube("okvideo0000"), ["de"], deadline_in(30))

            assert probe.title == "A video"  # from ytdlp_info
            assert probe.duration == 60.0
            assert probe.track is not None
            assert probe.track.source == "manual_subtitles"
            assert probe.error is None
            assert not probe.blocked

    run(scenario)
    # yt-dlp is not asked for the track youtube-transcript-api already fetched.
    assert calls == [("youtube_transcript", None), ("ytdlp_info", False)]


@pytest.mark.parametrize("refusal", ["RequestBlocked", "IpBlocked", "PoTokenRequired"])
def test_youtube_blocked_maps_to_blocked(refusal: str) -> None:
    calls: list[str] = []

    async def scenario() -> None:
        config = settings()
        async with fetcher(config, target=FAKE_TARGET, env={"FAKE_REFUSAL": refusal}) as media:
            run_call = media.pool.run

            async def spy(function_name: str, payload: Payload, deadline: float) -> Any:
                calls.append(function_name)
                return await run_call(function_name, payload, deadline)

            media.pool.run = spy  # type: ignore[method-assign]
            probe = await media.probe(youtube("blocked"), ["de"], deadline_in(30))

            assert probe.blocked
            assert probe.track is None
            assert probe.error == "blocked"
            assert probe.title is None

    run(scenario)
    assert calls == ["youtube_transcript", "ytdlp_info"]  # one call each, no retry storm


def test_child_deadline_kills_and_times_out() -> None:
    async def scenario() -> None:
        async with fetcher(settings(), target=FAKE_TARGET) as media:
            await media.start()
            loop = asyncio.get_running_loop()
            deadline = deadline_in(1.5)
            started = time.monotonic()
            with pytest.raises(ConversionTimeout):
                await media.probe(local_target("https://example.org/", "sleep"), [], deadline)
            assert loop.time() < deadline + 1
            assert time.monotonic() - started < 2.5

            probe = await media.probe(
                local_target("https://example.org/", "ok"), [], deadline_in(30)
            )
            assert probe.track is not None

    run(scenario)


def test_targets_with_an_extractor_not_allowed_or_without_a_video_are_refused() -> None:
    async def scenario() -> None:
        config = settings()
        media = MediaFetcher(
            config, create_media_pool(config), EgressProxy(config, registry=CollectorRegistry())
        )
        target = MediaTarget("arte", "https://example.org/", "generic", "x")
        with pytest.raises(ValueError, match="not allowed"):
            await media.probe(target, [], deadline_in(5))
        playlist = MediaTarget(
            "youtube", "https://www.youtube.com/playlist?list=PL1", "youtube", None
        )
        with pytest.raises(ValueError, match="playlist"):
            await media.probe(playlist, [], deadline_in(5))
        assert media.pool.child_pids() == []  # refused before anything started
        await media.stop()

    run(scenario)


def test_create_media_fetcher_follows_the_settings() -> None:
    config = settings(
        media_concurrency=3, media_memory_limit_mb=768, media_subtitle_max_bytes=100_000
    )
    media = create_media_fetcher(config, CollectorRegistry())

    assert media.pool._processes == 3
    assert media.pool._memory_limit_mb == 768
    assert media.pool._target == MEDIA_TARGET
    assert media.pool._max_result_bytes > 100_000
    assert media.proxy._policy is is_public_address  # the production policy


# --- wiring ------------------------------------------------------------------------------------


class Recorder:
    """Replaces ``MediaFetcher.start``/``stop`` and the document pool's, noting the calls."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[str] = []
        recorder = self

        async def media_start(_media: MediaFetcher) -> None:
            recorder.events.append("media start")

        async def media_stop(_media: MediaFetcher) -> None:
            recorder.events.append("media stop")

        async def nothing(*_args: Any) -> None:
            pass

        monkeypatch.setattr(MediaFetcher, "start", media_start)
        monkeypatch.setattr(MediaFetcher, "stop", media_stop)
        monkeypatch.setattr(DocumentPool, "start", nothing)
        monkeypatch.setattr(DocumentPool, "shutdown", nothing)


def test_app_starts_the_media_fetcher_before_it_serves_and_stops_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fakeredis import FakeAsyncRedis
    from fastapi.testclient import TestClient

    from text_extraction.app import create_public_app

    recorder = Recorder(monkeypatch)
    app = create_public_app(settings(), FakeAsyncRedis(), CollectorRegistry(), start_documents=True)

    assert isinstance(app.state.media_fetcher, MediaFetcher)
    with TestClient(app):
        assert recorder.events == ["media start"]
    assert recorder.events == ["media start", "media stop"]


def test_app_without_media_has_no_media_fetcher(monkeypatch: pytest.MonkeyPatch) -> None:
    from fakeredis import FakeAsyncRedis
    from fastapi.testclient import TestClient

    from text_extraction.app import create_public_app

    recorder = Recorder(monkeypatch)
    app = create_public_app(
        settings(media_enabled=False), FakeAsyncRedis(), CollectorRegistry(), start_documents=True
    )

    assert app.state.media_fetcher is None
    with TestClient(app):
        pass
    assert recorder.events == []


def test_worker_starts_the_media_fetcher_before_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    from fakeredis import FakeAsyncRedis

    from text_extraction import worker

    async def nothing() -> None:
        pass

    recorder = Recorder(monkeypatch)
    monkeypatch.setattr(worker, "_load_language_model", nothing)
    parts = worker.build_worker(settings(), FakeAsyncRedis(), CollectorRegistry())
    try:
        assert isinstance(parts.media, MediaFetcher)
        asyncio.run(worker.start_up(parts))
        assert recorder.events == ["media start"]
    finally:
        parts.pool.shutdown()

    plain = worker.build_worker(
        settings(media_enabled=False), FakeAsyncRedis(), CollectorRegistry()
    )
    try:
        assert plain.media is None
        asyncio.run(worker.start_up(plain))
        assert recorder.events == ["media start"]
    finally:
        plain.pool.shutdown()


def test_browser_worker_builds_a_media_fetcher_when_media_is_enabled() -> None:
    from fakeredis import FakeAsyncRedis

    from text_extraction import browser_worker

    parts = browser_worker.build_browser_worker(
        settings(browser_enabled=True), FakeAsyncRedis(), CollectorRegistry()
    )
    plain = browser_worker.build_browser_worker(
        settings(browser_enabled=True, media_enabled=False), FakeAsyncRedis(), CollectorRegistry()
    )
    try:
        assert isinstance(parts.media, MediaFetcher)
        assert plain.media is None
    finally:
        parts.conversion_pool.shutdown()
        plain.conversion_pool.shutdown()


# --- error codes -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("blocked", "blocked"),  # a bot check outranks a refused side request
        ("rate_limited", "rate_limited"),
        ("too_large", "too_large"),
        ("track_failed", "blocked_address"),
        ("extractor_error", "blocked_address"),
        ("crashed", "blocked_address"),
    ],
)
def test_a_proxy_refusal_wins_only_over_generic_failures(code: str, expected: str) -> None:
    context = ProxyContext(1000, lambda _context: None)
    context._refuse("169.254.169.254", "blocked_address")

    assert _refusal(context, code) == expected


def test_blocked_survives_a_refused_side_request() -> None:
    """A call that hit a bot check and also had a request refused by the proxy is still
    ``blocked`` (D1)."""

    async def scenario() -> None:
        async with fetcher(settings(), target=FAKE_TARGET) as media:
            open_context = media.proxy.open_context

            def refused_context(max_bytes: int | None = None) -> ProxyContext:
                context = open_context(max_bytes)
                context._refuse("10.0.0.1", "blocked_address")
                return context

            async def fake(function_name: str, payload: Payload, deadline: float) -> Any:
                if function_name == "youtube_transcript":
                    raise ConversionError("RequestBlocked", "")
                raise ConversionError("DownloadError", "ERROR: Sign in to confirm you're not a bot")

            media.proxy.open_context = refused_context  # type: ignore[method-assign]
            media.pool.run = fake  # type: ignore[method-assign]
            probe = await media.probe(youtube("blockedvid0"), ["de"], deadline_in(30))

            assert probe.blocked
            assert probe.error == "blocked"

    run(scenario)


# --- rate limits and deadlines (Review Focus 1, the tunnel's lifetime) -------------------------

RATE_LIMIT_TARGET = "tests.media_pool_rate_limit_targets"


def watch_url_env(httpserver: HTTPServer) -> dict[str, str]:
    return {"FAKE_WATCH_URL": httpserver.url_for("/watch") + "?v={video_id}"}


def test_a_youtube_429_is_rate_limited_with_its_retry_after(httpserver: HTTPServer) -> None:
    """youtube-transcript-api raises ``IpBlocked`` for an HTTP 429; the child's session notes
    the status, so the probe says ``rate_limited`` (for the gate's backoff), with the answer's
    ``Retry-After``, and yt-dlp is not asked to call YouTube again."""
    httpserver.expect_request("/watch").respond_with_data(
        "slow down", status=429, headers={"Retry-After": "120"}
    )

    async def scenario() -> None:
        config = settings()
        async with fetcher(
            config, target=RATE_LIMIT_TARGET, env=watch_url_env(httpserver)
        ) as media:
            probe = await media.probe(youtube("ratelimited"), ["de"], deadline_in(30))

            assert probe.rate_limited
            assert probe.retry_after == "120"
            assert probe.error == "rate_limited"
            assert probe.blocked  # no track, and the platform refused the service (D1)
            assert probe.track is None

    run(scenario)
    assert len(httpserver.log) == 1  # one request to YouTube, through the proxy


def test_a_youtube_bot_check_without_429_stays_blocked(httpserver: HTTPServer) -> None:
    """The same library exception (``IpBlocked``) for a reCAPTCHA page is a block, not a rate
    limit: only the status decides."""
    httpserver.expect_request("/watch").respond_with_data(
        '<html><div class="g-recaptcha"></div></html>', content_type="text/html"
    )

    async def scenario() -> None:
        config = settings()
        env = watch_url_env(httpserver)
        async with fetcher(config, target=RATE_LIMIT_TARGET, env=env) as media:
            probe = await media.probe(youtube("botcheck000"), ["de"], deadline_in(30))

            assert not probe.rate_limited
            assert probe.blocked
            assert probe.retry_after is None

    run(scenario)


def test_the_probe_deadline_never_outlives_the_proxy_tunnel() -> None:
    """A tunnel lives ``call_seconds`` + grace from its start; a probe never gets a deadline
    later than ``call_seconds`` from its own start, so no tunnel it opens dies under it."""
    seen: list[tuple[float, float]] = []

    async def scenario() -> None:
        config = settings()
        media = MediaFetcher(
            config,
            create_media_pool(config),
            EgressProxy(config, registry=CollectorRegistry(), tunnel_timeout=8 + 5),
            call_seconds=8,
        )

        async def started() -> None:
            pass

        async def fake(function_name: str, payload: Payload, deadline: float) -> Any:
            assert isinstance(payload, dict)
            seen.append((deadline, payload["timeout"]))
            return {"title": "A video", "manual": {}, "automatic": {}}

        media.pool.start = started  # type: ignore[method-assign]
        media.pool.run = fake  # type: ignore[method-assign]
        before = asyncio.get_running_loop().time()
        await media.probe(youtube("deadline000"), [], before + 300)
        assert len(seen) == 2  # youtube_transcript and ytdlp_info
        for deadline, timeout in seen:
            assert deadline <= before + 8 + 0.5
            assert timeout <= 8 + 0.5
        assert media.call_seconds == 8
        await media.stop()

    run(scenario)


def test_media_calls_fit_the_tunnel_wherever_the_pipeline_runs() -> None:
    from fakeredis import FakeAsyncRedis

    from text_extraction import browser_worker
    from text_extraction.browser_proxy import TUNNEL_GRACE_SECONDS

    api = create_media_fetcher(settings(extract_timeout_seconds=25), CollectorRegistry())
    assert api.call_seconds == 25
    assert api.proxy.tunnel_timeout == 25 + TUNNEL_GRACE_SECONDS

    # The browser worker's pipeline has BROWSER_TIMEOUT_SECONDS (or the unblock timeout).
    for unblock, expected in ((False, 90), (True, 120)):
        parts = browser_worker.build_browser_worker(
            settings(
                browser_enabled=True,
                extract_timeout_seconds=25,
                browser_timeout_seconds=90,
                browser_unblock_enabled=unblock,
                browser_unblock_timeout_seconds=120,
            ),
            FakeAsyncRedis(),
            CollectorRegistry(),
        )
        try:
            assert parts.media is not None
            assert parts.media.call_seconds == expected
            assert parts.media.proxy.tunnel_timeout == expected + TUNNEL_GRACE_SECONDS
        finally:
            parts.conversion_pool.shutdown()


# --- the formats of one track (final fix round) ------------------------------------------------

SRT = "1\n00:00:01,000 --> 00:00:02,000\nDas ist der erste Satz.\n"


def serve_two_formats(httpserver: HTTPServer) -> str:
    """A page whose German track comes as WebVTT (ranked first) and as SRT."""
    httpserver.expect_request("/lesson.html").respond_with_data(
        "<html><head><title>Lesson</title></head><body>"
        '<video controls><source src="clip.mp4" type="video/mp4">'
        '<track kind="subtitles" src="de.vtt" srclang="de" label="Deutsch">'
        '<track kind="subtitles" src="de.srt" srclang="de" label="Deutsch"></video>'
        "</body></html>",
        content_type="text/html",
    )
    httpserver.expect_request("/de.srt").respond_with_data(SRT, content_type="text/plain")
    return httpserver.url_for("/lesson.html")


@pytest.mark.parametrize(
    "vtt_answer",
    [
        {"response_data": "gone", "status": 404},
        {"response_data": "", "status": 200},
        {"response_data": "WEBVTT\n\nnot a cue at all", "status": 200},
    ],
    ids=["fetch_error", "empty", "unreadable"],
)
def test_a_format_that_fails_gives_way_to_the_next(
    httpserver: HTTPServer, vtt_answer: dict[str, Any]
) -> None:
    url = serve_two_formats(httpserver)
    httpserver.expect_request("/de.vtt").respond_with_data(
        vtt_answer["response_data"], status=vtt_answer["status"], content_type="text/vtt"
    )

    async def scenario() -> None:
        async with fetcher(settings()) as media:
            probe = await media.probe(local_target(url), ["de"], deadline_in(60))

            assert probe.error is None
            assert probe.track is not None
            assert probe.track.format == "srt"
            cues = parse_captions(probe.track.body, probe.track.format, probe.track.charset)
            assert [cue.text for cue in cues] == ["Das ist der erste Satz."]

    run(scenario)


def test_a_proxy_refusal_of_a_format_ends_the_track(httpserver: HTTPServer) -> None:
    url = serve_two_formats(httpserver)
    httpserver.expect_request("/de.vtt").respond_with_data(
        "", status=302, headers={"Location": "http://169.254.169.254/latest/de.vtt"}
    )

    async def scenario() -> None:
        async with fetcher(settings()) as media:
            probe = await media.probe(local_target(url), ["de"], deadline_in(60))

            assert probe.track is None
            assert probe.error == "blocked_address"

    run(scenario)
    assert "/de.srt" not in [request.path for request, _response in httpserver.log]


@pytest.mark.parametrize(
    ("type_name", "message", "code"),
    [
        (
            "DownloadError",
            "ERROR: [zdf] x-100: Failed to download video metadata: HTTP Error 503: "
            "Service Unavailable",
            "platform_error",
        ),
        (
            "DownloadError",
            "ERROR: [3sat] x: Unable to download webpage: HTTP Error 502",
            "platform_error",
        ),
        (
            "DownloadError",
            "ERROR: [zdf] x: Unable to download JSON metadata: timed out",
            "connect_error",
        ),
        ("DownloadError", "ERROR: [zdf] x: <urlopen error [Errno 104] reset>", "connect_error"),
        (
            "DownloadError",
            "ERROR: [zdf] x: Remote end closed connection without response",
            "connect_error",
        ),
        ("DownloadError", "ERROR: [zdf] x: IncompleteRead(100 bytes read)", "connect_error"),
        (
            "DownloadError",
            "ERROR: [zdf] x-100: Failed to download fallback metadata: HTTP Error 404: Not Found",
            "unavailable",
        ),
        ("DownloadError", "ERROR: [zdf] x-100: Unable to extract PTMD URL", "no_video"),
        (
            "DownloadError",
            "ERROR: [3sat] x: Unable to extract player JSON; please report",
            "no_video",
        ),
        (
            "DownloadError",
            "ERROR: [3sat] x: An extractor error has occurred. (caused by KeyError)",
            "extractor_error",
        ),
        ("DownloadError", "ERROR: [youtube] x: HTTP Error 429: Too Many Requests", "rate_limited"),
        (
            "DownloadError",
            "ERROR: [vimeo] 47677979: Cannot download embed-only video without embedding URL. "
            "Please call yt-dlp with the URL of the page that embeds this video.",
            "unavailable",
        ),
    ],
)
def test_ytdlp_failures_are_classified_before_the_generic_code(
    type_name: str, message: str, code: str
) -> None:
    """yt-dlp runs without retries: a server error or a broken connection is a failure that may
    pass, not an ``extractor_error``; a ZDF or 3sat page without a video has codes of its own
    (the fallback document 404, no PTMD URL, no player)."""
    from text_extraction.media.pool import _failure_code

    assert _failure_code(ConversionError(type_name, message)) == code
