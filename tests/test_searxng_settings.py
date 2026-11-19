"""SearXNG's settings.yml as this project ships it (package 25): the template that
``text-extraction-init`` renders into the compose stack and the Helm chart mounts. The engines
with an official, free API (ruling D2 a), google and bing, and the added json engine wlo (ruling
D6) are on, SEARCH_SOURCES names exactly them, and the instance is a private backend of the API:
JSON on, limiter, image proxy and Valkey off."""

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from text_extraction.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
HELM_COPY = ROOT / "deploy/helm/text-extraction/files/searxng-settings.yml"
D2_ENGINES = [
    "wikipedia",
    "wikibooks",
    "wikiversity",
    "wikinews",
    "mwmbl",
    "openalex",
    "arxiv",
    "crossref",
]
# Jan's choice of 2026-11-19 (ruling D6): two scraping engines and WLO's official API.
SCRAPING_ENGINES = ["google", "bing"]
KEPT_ENGINES = D2_ENGINES + SCRAPING_ENGINES
ALL_ENGINES = [*KEPT_ENGINES, "wlo"]
WLO_RENDER = "https://redaktion.openeduhub.net/edu-sharing/components/render/"


def _text() -> str:
    return files("text_extraction").joinpath("data/searxng-settings.yml").read_text("utf-8")


def _settings() -> dict[str, Any]:
    return yaml.safe_load(_text())  # type: ignore[no-any-return]


def test_keeps_exactly_the_d2_and_the_two_scraping_engines() -> None:
    assert _settings()["use_default_settings"]["engines"]["keep_only"] == KEPT_ENGINES


def test_search_sources_default_names_every_engine_of_the_settings() -> None:
    default = Settings.model_fields["search_sources"].default
    assert default == ALL_ENGINES
    assert sorted(default) == sorted(engine["name"] for engine in _settings()["engines"])


def test_every_engine_is_switched_on() -> None:
    # Several are `disabled: true` in SearXNG's own settings.yml (2026.11.2): the override
    # switches each one on, so a search that names no engine reaches all of them too.
    engines = {engine["name"]: engine for engine in _settings()["engines"]}
    assert sorted(engines) == sorted(ALL_ENGINES)
    for name in ALL_ENGINES:
        assert engines[name]["disabled"] is False, name
    assert engines["wikipedia"]["display_type"] == ["list"]


def test_wlo_asks_the_public_edu_sharing_search_and_links_render_pages() -> None:
    wlo = {engine["name"]: engine for engine in _settings()["engines"]}["wlo"]
    assert wlo["engine"] == "json_engine"
    assert wlo["method"] == "POST"
    assert wlo["headers"]["Content-Type"] == "application/json"
    assert wlo["search_url"].startswith(
        "https://redaktion.openeduhub.net/edu-sharing/rest/search/v1/queries/-home-/mds_oeh/"
        "ngsearch?contentType=FILES&maxItems=10&skipCount={pageno}&"
    )
    # json_engine formats the body with str.format: doubled braces are the JSON's own.
    fields = {
        "query": "Photosynthese",
        "lang": "de",
        "pageno": 0,
        "time_range": "",
        "safe_search": "",
    }
    body = json.loads(wlo["request_body"].format(**fields))
    assert body == {"criteria": [{"property": "ngsearchword", "values": ["Photosynthese"]}]}
    # Page n skips (n - 1) * 10 hits, as maxItems is 10.
    assert (wlo["paging"], wlo["page_size"], wlo["first_page_num"]) == (True, 10, 0)
    assert wlo["results_query"] == "nodes"
    assert (wlo["url_prefix"], wlo["url_query"]) == (WLO_RENDER, "ref/id")
    assert wlo["title_query"] == "properties/cclom:title/0"
    assert wlo["content_query"] == "properties/cclom:general_description/0"
    assert wlo["about"]["use_official_api"] is True
    # Longer than SearXNG's 3 s default, within the cap that keeps the search below its timeout.
    assert 3.0 < wlo["timeout"] <= _settings()["outgoing"]["max_request_timeout"]


def test_json_is_on() -> None:
    assert set(_settings()["search"]["formats"]) == {"html", "json"}


def test_a_private_backend_without_limiter_proxy_or_valkey() -> None:
    settings = _settings()
    server = settings["server"]
    assert server["limiter"] is False
    assert server["public_instance"] is False
    assert server["image_proxy"] is False
    assert settings["valkey"] == {"url": False}
    # The file leaves the bind address to the image (Granian reads GRANIAN_HOST and GRANIAN_PORT);
    # SearXNG stays private because compose publishes no port and Helm's NetworkPolicy admits only
    # the API.
    assert "bind_address" not in server
    # No autocomplete requests to a scraping backend.
    assert settings["search"]["autocomplete"] == ""


def test_secret_key_is_the_placeholder_init_replaces() -> None:
    # SearXNG refuses to start with this value, so an unrendered file never runs with a known key.
    assert _settings()["server"]["secret_key"] == "ultrasecretkey"  # noqa: S105 - placeholder
    assert _text().count("ultrasecretkey") == 1


def test_sources_answer_before_the_api_gives_up() -> None:
    # crossref waits 30 s by default; SearXNG caps every engine below SEARCH_TIMEOUT_SECONDS, so a
    # slow source lands in unresponsive_engines (partial) instead of failing the whole search.
    cap = _settings()["outgoing"]["max_request_timeout"]
    assert 0 < cap < Settings.model_fields["search_timeout_seconds"].default


def test_helm_chart_ships_the_same_file() -> None:
    assert HELM_COPY.read_text(encoding="utf-8") == _text()
