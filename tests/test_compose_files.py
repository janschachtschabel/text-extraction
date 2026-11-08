"""The two compose files. The root docker-compose.yml runs the published images and follows the
rules of Docker panels such as Hostinger's Docker Manager ("Compose from URL" takes only the root
docker-compose.yml, copies .env.example to .env and offers no field for variables before the first
deploy works); docker-compose.build.yml builds the images. The root file repeats its settings per
service (no anchors), so these tests keep it in step with the build file."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
ROOT_FILE = ROOT_DIR / "docker-compose.yml"
BUILD_FILE = ROOT_DIR / "docker-compose.build.yml"
IMAGE = re.compile(
    r"^ghcr\.io/janschachtschabel/text-extraction(-browser|-docling|-asr|-nlp)?"
    r":\$\{TEXT_EXTRACTION_VERSION:-[0-9]+\.[0-9]+\.[0-9]+\}$"
)
# The build file's image names -> the published image of the same target.
PUBLISHED = {
    "text-extraction:dev": "text-extraction",
    "text-extraction-browser:dev": "text-extraction-browser",
    "text-extraction-docling:dev": "text-extraction-docling",
    "text-extraction-asr:dev": "text-extraction-asr",
    "text-extraction-nlp:dev": "text-extraction-nlp",
}
# Settings only the root file has: the secrets the init service generates.
ROOT_ONLY_ENV = {
    "VALKEY_URL_FILE": "/secrets/valkey-url",
    "API_KEYS_FILE": "/secrets/api-keys.json",
}


def _text() -> str:
    return ROOT_FILE.read_text(encoding="utf-8")


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _root_services() -> dict[str, Any]:
    return _load(ROOT_FILE)["services"]  # type: ignore[no-any-return]


def _app_services() -> dict[str, Any]:
    return {name: s for name, s in _root_services().items() if name not in ("init", "valkey")}


def _build_services() -> dict[str, Any]:
    """The build file's services with their `<<` merges resolved (safe_load does that)."""
    return _load(BUILD_FILE)["services"]  # type: ignore[no-any-return]


# --- Hostinger rules -----------------------------------------------------------------------


def test_no_anchors_aliases_or_merge_keys() -> None:
    events = list(yaml.parse(_text()))
    assert not [e for e in events if getattr(e, "anchor", None)], "no YAML anchors or aliases"
    assert "<<" not in _text()


def test_no_tabs_required_or_nested_variables() -> None:
    text = _text()
    assert "\t" not in text
    assert not re.search(r"\$\{[^}]*:\?", text), "a required variable aborts the first deploy"
    assert not re.search(r"\$\{[^}]*\$\{", text), "no nested variables"


def test_no_build_bind_mount_or_external_network() -> None:
    root = _load(ROOT_FILE)
    for name, service in root["services"].items():
        assert "build" not in service, name
        for volume in service.get("volumes", []):
            source = volume.split(":", 1)[0]
            assert not source.startswith((".", "/", "~")), f"{name}: bind mount {volume}"
    for network in root.get("networks", {}).values():
        assert not network.get("external"), "an external network aborts the deploy when missing"
    assert root["networks"]["default"]["name"] == "${TRAEFIK_NETWORK:-text-extraction-web}"


def test_every_image_is_published() -> None:
    for name, service in _root_services().items():
        if name == "valkey":
            assert service["image"] == _build_services()["valkey"]["image"]
            continue
        assert IMAGE.match(service["image"]), name
        assert service["pull_policy"] == "missing", name


def test_ports_80_and_443_stay_free() -> None:
    ports = [p for s in _root_services().values() for p in s.get("ports", [])]
    assert ports == ["${API_PORT:-8080}:8080"]


def test_api_carries_the_traefik_labels() -> None:
    labels = _root_services()["api"]["labels"]
    router = "traefik.http.routers.${TRAEFIK_ROUTER:-text-extraction}"
    assert labels == [
        "traefik.enable=true",
        "traefik.docker.network=${TRAEFIK_NETWORK:-text-extraction-web}",
        f"{router}.rule=Host(`${{DOMAIN:-}}`)",
        f"{router}.entrypoints=${{TRAEFIK_ENTRYPOINT:-websecure}}",
        f"{router}.tls.certresolver=${{TRAEFIK_CERTRESOLVER:-letsencrypt}}",
        "traefik.http.services.${TRAEFIK_ROUTER:-text-extraction}.loadbalancer.server.port=8080",
    ]
    others = [n for n, s in _root_services().items() if n != "api" and "labels" in s]
    assert others == []


# --- Secrets ---------------------------------------------------------------------------------


def test_init_writes_the_secrets_volume() -> None:
    init = _root_services()["init"]
    assert init["command"] == ["text-extraction-init"]
    assert init["restart"] == "no"
    assert init["volumes"] == ["secrets:/secrets"]
    assert init["environment"]["DOMAIN"] == "${DOMAIN:-}"
    assert "depends_on" not in init


def test_valkey_reads_its_password_from_the_generated_config() -> None:
    valkey = _root_services()["valkey"]
    assert valkey["command"][:2] == ["valkey-server", "/secrets/valkey.conf"]
    assert valkey["command"][2:] == _build_services()["valkey"]["command"][2:][-4:]
    assert "secrets:/secrets:ro" in valkey["volumes"]
    assert "--user healthcheck" in " ".join(valkey["healthcheck"]["test"])
    assert valkey["depends_on"] == {"init": {"condition": "service_completed_successfully"}}


@pytest.mark.parametrize("name", sorted(_app_services()))
def test_app_service_reads_the_secrets_after_init(name: str) -> None:
    service = _app_services()[name]
    assert "secrets:/secrets:ro" in service["volumes"]
    for key, value in ROOT_ONLY_ENV.items():
        assert service["environment"][key] == value
    assert service["depends_on"]["init"] == {"condition": "service_completed_successfully"}
    assert service["depends_on"]["valkey"] == {"condition": "service_healthy"}
    assert service["env_file"] == [{"path": ".env", "required": False}]


# --- In step with the build file ------------------------------------------------------------


def test_same_services_as_the_build_file_plus_init() -> None:
    assert set(_root_services()) == set(_build_services()) | {"init"}


@pytest.mark.parametrize("name", sorted(_app_services()))
def test_app_service_matches_the_build_file(name: str) -> None:
    root = _app_services()[name]
    build = _build_services()[name]

    assert root["image"].split(":", 1)[0].rsplit("/", 1)[1] == PUBLISHED[build["image"]]
    assert root.get("command") == build.get("command")
    environment = {k: v for k, v in root["environment"].items() if k not in ROOT_ONLY_ENV}
    build_environment = {k: v for k, v in build["environment"].items() if k != "VALKEY_URL"}
    assert environment == build_environment
    for key in ("read_only", "tmpfs", "security_opt", "shm_size", "stop_grace_period"):
        assert root.get(key) == build.get(key), key
    assert root.get("healthcheck") == build.get("healthcheck")
    assert [v for v in root["volumes"] if not v.startswith("secrets:")] == build.get("volumes", [])
    build_depends = {k: v for k, v in build["depends_on"].items()}
    assert {k: v for k, v in root["depends_on"].items() if k != "init"} == build_depends
    one_shot = name.endswith("-models")
    assert root["restart"] == ("no" if one_shot else "unless-stopped")


def test_same_volumes_as_the_build_file_plus_secrets() -> None:
    assert set(_load(ROOT_FILE)["volumes"]) == set(_load(BUILD_FILE)["volumes"]) | {"secrets"}
