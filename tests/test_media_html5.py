"""``media/html5.py``: the HTML5 tracks and the embedded players of a page (ruling M8)."""

import json

import pytest

from text_extraction.media.html5 import PageTrack, page_media, track_format
from text_extraction.media.types import MediaEmbed

BASE = "https://school.example/lessons/plants.html"
YT_ID = "dQw4w9WgXcQ"


def page(body: str, *, head: str = "", lang: str = "de") -> str:
    return f'<!doctype html><html lang="{lang}"><head>{head}</head><body>{body}</body></html>'


def test_embeds_listed() -> None:
    """A YouTube iframe (normalised to its watch URL), an ``<audio src>`` file and a JSON-LD
    ``AudioObject`` give three embeds, in that order."""
    ld = {
        "@context": "https://schema.org",
        "@type": "PodcastEpisode",
        "name": "Episode 1",
        "associatedMedia": {
            "@type": "AudioObject",
            "contentUrl": "https://cdn.example/episode1.mp3",
        },
    }
    html = page(
        f'<iframe src="https://www.youtube-nocookie.com/embed/{YT_ID}?rel=0"></iframe>'
        '<audio controls src="/media/intro.mp3"></audio>',
        head=f'<script type="application/ld+json">{json.dumps(ld)}</script>',
    )

    found = page_media(html, BASE, [], 3)

    assert found.embeds == [
        MediaEmbed(
            url=f"https://www.youtube.com/watch?v={YT_ID}", kind="platform", platform="youtube"
        ),
        MediaEmbed(url="https://school.example/media/intro.mp3", kind="file"),
        MediaEmbed(url="https://cdn.example/episode1.mp3", kind="file"),
    ]
    assert found.tracks == []


def test_embeds_from_video_sources_opengraph_and_embed_urls() -> None:
    ld = {
        "@type": "VideoObject",
        "embedUrl": "https://player.vimeo.com/video/76979871",
        "contentUrl": "https://cdn.example/clip.mp4",
    }
    html = page(
        '<video controls><source src="clip.webm" type="video/webm">'
        '<source src="clip.webm"></video>'
        '<iframe src="https://maps.example/embed?x=1"></iframe>',
        head=(
            '<meta property="og:video" content="https://www.youtube.com/embed/'
            f'{YT_ID}"><meta property="og:audio" content="https://cdn.example/a.ogg">'
            f'<script type="application/ld+json">{json.dumps(ld)}</script>'
        ),
    )

    embeds = page_media(html, BASE, [], 3).embeds

    assert embeds == [
        MediaEmbed(url="https://school.example/lessons/clip.webm", kind="file"),
        MediaEmbed(
            url="https://player.vimeo.com/video/76979871", kind="platform", platform="vimeo"
        ),
        MediaEmbed(url="https://cdn.example/clip.mp4", kind="file"),
        MediaEmbed(
            url=f"https://www.youtube.com/watch?v={YT_ID}", kind="platform", platform="youtube"
        ),
        MediaEmbed(url="https://cdn.example/a.ogg", kind="file"),
    ]


def test_an_embed_url_that_is_no_platform_player_is_not_a_file() -> None:
    ld = {"@type": "VideoObject", "embedUrl": "https://player.example/embed/123"}
    html = page("", head=f'<script type="application/ld+json">{json.dumps(ld)}</script>')

    assert page_media(html, BASE, [], 3).embeds == []


def test_an_embedded_playlist_player_is_skipped() -> None:
    """``/embed/videoseries?list=`` is a playlist: it cannot be extracted as one video, so it
    is not listed (a request for it would answer ``unsupported_content_type``)."""
    html = page(
        '<iframe src="https://www.youtube.com/embed/videoseries?list=PLabc_123"></iframe>'
        f'<iframe src="https://www.youtube.com/embed/{YT_ID}"></iframe>'
    )

    assert [embed.url for embed in page_media(html, BASE, [], 3).embeds] == [
        f"https://www.youtube.com/watch?v={YT_ID}"
    ]


@pytest.mark.parametrize(
    "src",
    [
        "javascript:alert(1)",
        "data:audio/mp3;base64,AAAA",
        "blob:https://school.example/1",
        "https://user:secret@cdn.example/a.mp3",
        "ftp://cdn.example/a.mp3",
        "",
    ],
)
def test_file_embeds_need_a_plain_http_url(src: str) -> None:
    """Only a plain http(s) URL is listed: no other scheme and no user information (it would be
    echoed to the caller)."""
    html = page(f'<audio src="{src}"></audio>')

    assert page_media(html, BASE, [], 3).embeds == []


def test_platform_iframes_list_the_canonical_url_and_never_user_information() -> None:
    """A port is dropped by the normalisation; a URL with user information is not listed at
    all, as the pipeline refuses it as a request URL (``invalid_url``)."""
    html = page(
        '<iframe src="https://u:p@www.youtube.com/embed/aaaaaaaaaaa"></iframe>'
        f'<iframe src="https://www.youtube.com:8443/embed/{YT_ID}?u=1"></iframe>'
    )

    assert [embed.url for embed in page_media(html, BASE, [], 3).embeds] == [
        f"https://www.youtube.com/watch?v={YT_ID}"
    ]


def test_embeds_are_distinct_and_at_most_ten() -> None:
    audios = "".join(f'<audio src="/a{index}.mp3"></audio>' for index in range(15))
    html = page('<audio src="/a0.mp3"></audio>' + audios)

    embeds = page_media(html, BASE, [], 3).embeds

    assert [embed.url for embed in embeds] == [
        f"https://school.example/a{index}.mp3" for index in range(10)
    ]


def test_tracks_one_per_element_by_requested_language_then_page_language() -> None:
    html = page(
        '<video src="a.mp4">'
        '<track kind="subtitles" src="a.en.vtt" srclang="en" label="English">'
        '<track kind="subtitles" src="a.fr.vtt" srclang="fr" label="Français">'
        '<track kind="captions" src="a.de.vtt" srclang="de" label="Deutsch">'
        "</video>"
        '<video src="b.mp4">'
        '<track src="b.en.vtt" srclang="en">'
        '<track kind="subtitles" src="b.de.vtt" srclang="de-AT" label="Österreichisch">'
        "</video>",
        lang="de",
    )

    found = page_media(html, BASE, ["fr"], 3)

    assert found.tracks == [
        PageTrack(url="https://school.example/lessons/a.fr.vtt", label="Français", language="fr"),
        PageTrack(
            url="https://school.example/lessons/b.de.vtt", label="Österreichisch", language="de"
        ),
    ]
    assert found.track_languages == ["en", "fr", "de"]
    assert found.truncated is False


def test_track_default_then_first_and_kinds_that_are_not_transcripts() -> None:
    html = page(
        '<video><track kind="chapters" src="ch.vtt" srclang="en">'
        '<track kind="metadata" src="meta.vtt">'
        '<track kind="descriptions" src="desc.vtt" srclang="en">'
        '<track kind="subtitles" src="one.vtt" srclang="es">'
        '<track kind="subtitles" src="two.vtt" srclang="it" default></video>'
        '<audio><track src="first.vtt" srclang="pt"><track src="second.vtt" srclang="pl">'
        "</audio>"
        '<video><track kind="chapters" src="only-chapters.vtt"></video>',
        lang="en",
    )

    found = page_media(html, BASE, [], 3)

    assert [track.url.rsplit("/", 1)[-1] for track in found.tracks] == ["two.vtt", "first.vtt"]
    assert found.tracks[0].label is None


def test_base_href_resolves_track_urls() -> None:
    html = page(
        '<video><track src="t.vtt" srclang="de"></video>',
        head='<base href="https://cdn.example/assets/">',
    )

    assert page_media(html, BASE, [], 3).tracks[0].url == "https://cdn.example/assets/t.vtt"


def test_more_elements_with_tracks_than_the_limit_are_truncated() -> None:
    videos = "".join(
        f'<video><track src="t{index}.vtt" srclang="de"></video>' for index in range(5)
    )

    found = page_media(page(videos), BASE, [], 3)

    assert [track.url.rsplit("/", 1)[-1] for track in found.tracks] == [
        "t0.vtt",
        "t1.vtt",
        "t2.vtt",
    ]
    assert found.truncated is True
    assert page_media(page(videos), BASE, [], 0).tracks == []


def test_an_unusable_track_src_falls_back_to_the_next_track() -> None:
    html = page(
        '<video><track src="javascript:x" srclang="de" default>'
        '<track src="https://u:p@cdn.example/t.vtt" srclang="de">'
        '<track src="ok.vtt" srclang="en"></video>'
    )

    assert [track.url for track in page_media(html, BASE, ["de"], 3).tracks] == [
        "https://school.example/lessons/ok.vtt"
    ]


def test_a_page_without_players_has_nothing() -> None:
    found = page_media(page("<p>Text only.</p><img src='x.png'>"), BASE, [], 3)

    assert found.tracks == []
    assert found.embeds == []
    assert found.empty


def test_broken_markup_and_json_ld_are_survived() -> None:
    html = page(
        "<video><track src='t.vtt'",
        head='<script type="application/ld+json">{"@type": "AudioObject", broken</script>',
    )

    found = page_media(html, BASE, [], 3)

    assert found.embeds == []


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"WEBVTT\n\n00:00.000 --> 00:01.000\nHi", "vtt"),
        (b"\xef\xbb\xbfWEBVTT\n", "vtt"),
        (b"  <?xml version='1.0'?><tt xmlns='http://www.w3.org/ns/ttml'/>", "ttml"),
        (b"1\n00:00:01,000 --> 00:00:02,000\nHi\n", "srt"),
    ],
)
def test_track_format_is_read_from_the_bytes(body: bytes, expected: str) -> None:
    assert track_format(body) == expected
