import re

import pytest

from text_extraction.media.sites import EXTRACTORS, MediaTarget, media_target

YT_ID = "dQw4w9WgXcQ"
YT_CANONICAL = f"https://www.youtube.com/watch?v={YT_ID}"


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={YT_ID}",
        f"https://youtube.com/watch?v={YT_ID}",
        f"http://www.youtube.com/watch?v={YT_ID}",
        f"https://m.youtube.com/watch?v={YT_ID}",
        f"https://music.youtube.com/watch?v={YT_ID}",
        f"https://www.youtube.com/watch?feature=share&v={YT_ID}&list=PLabc123",
        f"https://www.youtube.com/watch?v={YT_ID}&t=42s#comments",
        f"https://www.youtube.com/shorts/{YT_ID}",
        f"https://www.youtube.com/shorts/{YT_ID}?feature=share",
        f"https://www.youtube.com/embed/{YT_ID}?start=10&rel=0",
        f"https://www.youtube.com/live/{YT_ID}?si=abc",
        f"https://www.youtube-nocookie.com/embed/{YT_ID}",
        f"https://youtu.be/{YT_ID}",
        f"https://youtu.be/{YT_ID}?t=30",
        f"HTTPS://WWW.YOUTUBE.COM/watch?v={YT_ID}",
    ],
)
def test_youtube_url_forms(url: str) -> None:
    assert media_target(url) == MediaTarget("youtube", YT_CANONICAL, "youtube", YT_ID)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=short",
        f"https://www.youtube.com/watch?v={YT_ID}x",
        "https://www.youtube.com/watch?v=dQw4w9WgXc!",
        "https://www.youtube.com/shorts/",
        "https://www.youtube.com/embed/..%2F..%2Fx",
        "https://youtu.be/",
        "https://youtu.be/not an id",
        "https://www.youtube.com/",
        "https://www.youtube.com/channel/UC1234567890123456789012",
        f"https://www.youtube-nocookie.com/watch?v={YT_ID}",
    ],
)
def test_youtube_without_a_valid_video_id_is_no_target(url: str) -> None:
    assert media_target(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/playlist?list=PLabc_123-xyz",
        "https://www.youtube.com/watch?list=PLabc_123-xyz",
    ],
)
def test_youtube_playlist_without_video_is_none_target_with_flag(url: str) -> None:
    target = media_target(url)

    assert target is not None
    assert target.platform == "youtube"
    assert target.extractor == "youtube"
    assert target.video_id is None
    assert target.canonical_url == "https://www.youtube.com/playlist?list=PLabc_123-xyz"


def test_a_video_in_a_playlist_is_the_video() -> None:
    target = media_target(f"https://www.youtube.com/watch?v={YT_ID}&list=PLabc")

    assert target is not None
    assert target.video_id == YT_ID


@pytest.mark.parametrize(
    "url",
    [
        f"https://youtube.com.evil.example/watch?v={YT_ID}",
        f"https://evilyoutube.com/watch?v={YT_ID}",
        f"https://notyoutu.be/{YT_ID}",
        f"https://youtu.be.evil.example/{YT_ID}",
        f"https://evil.example/watch?v={YT_ID}&u=youtube.com",
        f"https://evil.example/youtube.com/watch?v={YT_ID}",
        f"https://youtube.com@evil.example/watch?v={YT_ID}",
        "https://evilvimeo.com/123456",
        "https://vimeo.com.evil.example/123456",
        "https://notzdf.de/video/x.html",
    ],
)
def test_youtube_lookalike_hosts_refused(url: str) -> None:
    assert media_target(url) is None


@pytest.mark.parametrize(
    "url",
    ["https://vimeo.com/76979871", "https://www.vimeo.com/76979871?share=copy"],
)
def test_vimeo_page(url: str) -> None:
    """``vimeo.com/<id>`` needs a login in yt-dlp; the player URL works without (M18)."""
    assert media_target(url) == MediaTarget(
        "vimeo", "https://player.vimeo.com/video/76979871", "vimeo", "76979871"
    )


def test_vimeo_player() -> None:
    assert media_target("https://player.vimeo.com/video/76979871?h=abc") == MediaTarget(
        "vimeo", "https://player.vimeo.com/video/76979871", "vimeo", "76979871"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://vimeo.com/",
        "https://vimeo.com/channels/staffpicks",
        "https://vimeo.com/user123",
        "https://player.vimeo.com/video/abc",
    ],
)
def test_vimeo_pages_without_a_video_are_no_target(url: str) -> None:
    assert media_target(url) is None


def test_ard_zdf_arte_3sat_forms() -> None:
    ard = media_target("https://www.ardmediathek.de/video/tagesschau/abc/Y3JpZDovL2Q?x=1#t")
    assert ard is not None
    assert (ard.platform, ard.extractor) == ("ard", "ardmediathek")
    assert ard.canonical_url == "https://www.ardmediathek.de/video/tagesschau/abc/Y3JpZDovL2Q"
    assert ard.video_id == "Y3JpZDovL2Q"

    zdf = media_target("http://www.zdf.de/nachrichten/heute/clip-100.html?utm=x#frag")
    assert zdf is not None
    assert (zdf.platform, zdf.extractor) == ("zdf", "zdf")
    assert zdf.canonical_url == "https://www.zdf.de/nachrichten/heute/clip-100.html"
    assert zdf.video_id == "clip-100"

    arte = media_target("https://www.arte.tv/de/videos/103517-001-A/some-title/?x=1")
    assert arte is not None
    assert (arte.platform, arte.extractor) == ("arte", "artetv")
    assert arte.canonical_url == "https://www.arte.tv/de/videos/103517-001-A/some-title/"
    assert arte.video_id == "103517-001-A"

    sat = media_target("https://www.3sat.de/wissen/nano/clip-102.html?a=b")
    assert sat is not None
    assert (sat.platform, sat.extractor) == ("3sat", "3sat")
    assert sat.canonical_url == "https://www.3sat.de/wissen/nano/clip-102.html"
    assert sat.video_id == "clip-102"


def test_zdf_video_urls_without_html() -> None:
    """The new ZDF URLs ``zdf.de/video/<section>/<show>/<episode>`` (M18)."""
    zdf = media_target("https://www.zdf.de/video/dokus/terra-x-112/ep-bau-der-pyramiden-100?a=1")
    assert zdf is not None
    assert (zdf.platform, zdf.extractor) == ("zdf", "zdf")
    assert (
        zdf.canonical_url == "https://www.zdf.de/video/dokus/terra-x-112/ep-bau-der-pyramiden-100"
    )
    assert zdf.video_id == "ep-bau-der-pyramiden-100"


def test_extractor_names_match_exactly_one_extractor_of_the_pinned_yt_dlp() -> None:
    """yt-dlp's ``allowed_extractors`` matches ``IE_NAME`` as a case-insensitive full regex; an
    unknown name silently loads nothing (M18). Each name must load exactly its extractor."""
    from yt_dlp.extractor import gen_extractor_classes

    expected = {
        "youtube": "YoutubeIE",
        "vimeo": "VimeoIE",
        "ard": "ARDBetaMediathekIE",
        "zdf": "ZDFIE",
        "arte": "ArteTVIE",
        "3sat": "DreiSatIE",
    }
    names = {extractor.IE_NAME.lower(): extractor.__name__ for extractor in gen_extractor_classes()}

    assert set(EXTRACTORS) == set(expected)
    for platform, name in EXTRACTORS.items():
        matched = [cls for ie_name, cls in names.items() if re.fullmatch(name, ie_name, re.I)]
        assert matched == [expected[platform]], platform


@pytest.mark.parametrize(
    "url",
    [
        "https://www.ardmediathek.de/",
        "https://www.ardmediathek.de/sendung/tagesschau/abc",
        "https://www.zdf.de/",
        "https://www.zdf.de/nachrichten",
        "https://www.3sat.de/wissen",
        "https://www.arte.tv/de/",
        "https://www.arte.tv/de/videos/",
        "https://www.arte.tv/de/guide/",
        "https://www.arte.tv/videos/103517-001-A/",
    ],
)
def test_mediathek_pages_without_a_video_are_no_target(url: str) -> None:
    assert media_target(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "https://example.com/video.mp4",
        "https://www.wikipedia.org/wiki/YouTube",
        "ftp://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "file:///etc/passwd",
        "not a url",
        "",
        "http://[::1",
    ],
)
def test_non_media_url_none(url: str) -> None:
    assert media_target(url) is None
