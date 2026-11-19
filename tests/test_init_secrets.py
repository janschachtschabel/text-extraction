"""text-extraction-init: the one-shot service of the root docker-compose.yml that creates the
stack's secrets on its first start (Hostinger rule: the first deploy works without variables)."""

import hashlib
import json
import re
import stat
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from text_extraction import init_secrets
from text_extraction.settings import ApiKey

FILES = (
    "valkey-password",
    "admin-key",
    "valkey.conf",
    "valkey-url",
    "api-keys.json",
    "searxng-secret",
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_creates_every_file(tmp_path: Path) -> None:
    result = init_secrets.ensure_secrets(tmp_path)

    created = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert created == sorted(FILES)
    password = (tmp_path / "valkey-password").read_text().strip()
    admin_key = (tmp_path / "admin-key").read_text().strip()
    assert len(password) == 48
    assert password.isalnum()
    assert len(admin_key) == 64
    assert admin_key.isalnum()
    assert result.admin_key_created == admin_key


def test_valkey_url_carries_the_password(tmp_path: Path) -> None:
    init_secrets.ensure_secrets(tmp_path)

    password = (tmp_path / "valkey-password").read_text().strip()
    url = (tmp_path / "valkey-url").read_text().strip()
    assert url == f"redis://:{password}@valkey:6379/0"


def test_valkey_conf_sets_the_password_and_a_ping_only_health_user(tmp_path: Path) -> None:
    init_secrets.ensure_secrets(tmp_path)

    password = (tmp_path / "valkey-password").read_text().strip()
    lines = [
        line
        for line in (tmp_path / "valkey.conf").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    assert lines == [
        f"user default on >{password} ~* &* +@all",
        "user healthcheck on nopass -@all +ping",
    ]


def test_api_keys_file_holds_the_hash_of_the_admin_key(tmp_path: Path) -> None:
    init_secrets.ensure_secrets(tmp_path)

    admin_key = (tmp_path / "admin-key").read_text().strip()
    keys = [
        ApiKey.model_validate(entry)
        for entry in json.loads((tmp_path / "api-keys.json").read_text())
    ]
    assert keys == [
        ApiKey(name="admin", role="admin", sha256=hashlib.sha256(admin_key.encode()).hexdigest())
    ]


def test_file_modes(tmp_path: Path) -> None:
    init_secrets.ensure_secrets(tmp_path)

    # Valkey runs as its own user and reads its config; everything else is for the app user only.
    assert _mode(tmp_path / "valkey.conf") == 0o644
    for name in ("valkey-password", "admin-key", "valkey-url", "api-keys.json"):
        assert _mode(tmp_path / name) == 0o600, name


def test_second_run_keeps_the_secrets(tmp_path: Path) -> None:
    init_secrets.ensure_secrets(tmp_path)
    before = {name: (tmp_path / name).read_text() for name in FILES}

    result = init_secrets.ensure_secrets(tmp_path)

    assert {name: (tmp_path / name).read_text() for name in FILES} == before
    assert result.admin_key_created is None


def test_derived_files_are_rebuilt_from_the_kept_secrets(tmp_path: Path) -> None:
    init_secrets.ensure_secrets(tmp_path)
    expected = {name: (tmp_path / name).read_text() for name in FILES}
    for name in ("valkey.conf", "valkey-url", "api-keys.json"):
        (tmp_path / name).unlink()

    init_secrets.ensure_secrets(tmp_path)

    assert {name: (tmp_path / name).read_text() for name in FILES} == expected


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    """The secrets volume and SearXNG's own config volume (``/secrets``, ``/searxng``)."""
    secrets_dir, searxng_dir = tmp_path / "secrets", tmp_path / "searxng"
    secrets_dir.mkdir()
    searxng_dir.mkdir()
    return secrets_dir, searxng_dir


def _ensure(dirs: tuple[Path, Path]) -> init_secrets.SecretsResult:
    return init_secrets.ensure_secrets(dirs[0], searxng_dir=dirs[1])


def _searxng_key(searxng_dir: Path) -> str:
    settings = yaml.safe_load((searxng_dir / "settings.yml").read_text())
    return str(settings["server"]["secret_key"])


def test_searxng_settings_carry_a_generated_key(dirs: tuple[Path, Path]) -> None:
    secrets_dir, searxng_dir = dirs
    result = _ensure(dirs)

    assert result.searxng_problem is None
    key = _searxng_key(searxng_dir)
    assert re.fullmatch(r"[0-9a-f]{64}", key)
    assert (secrets_dir / "searxng-secret").read_text().strip() == key
    # The packaged template, with nothing changed but the placeholder.
    template = files("text_extraction").joinpath("data/searxng-settings.yml").read_text("utf-8")
    rendered = (searxng_dir / "settings.yml").read_text()
    assert rendered == template.replace("ultrasecretkey", key)


def test_searxng_volume_holds_only_its_settings(dirs: tuple[Path, Path]) -> None:
    secrets_dir, searxng_dir = dirs
    _ensure(dirs)

    # SearXNG mounts only its own volume: no secret of the stack, not even its key's file.
    assert sorted(p.name for p in searxng_dir.iterdir()) == ["settings.yml"]
    assert not (secrets_dir / "searxng").exists()


def test_searxng_key_differs_from_the_other_secrets(dirs: tuple[Path, Path]) -> None:
    secrets_dir, searxng_dir = dirs
    _ensure(dirs)

    others = {(secrets_dir / n).read_text().strip() for n in ("valkey-password", "admin-key")}
    assert _searxng_key(searxng_dir) not in others


def test_second_run_keeps_the_searxng_key(dirs: tuple[Path, Path]) -> None:
    searxng_dir = dirs[1]
    _ensure(dirs)
    key = _searxng_key(searxng_dir)
    (searxng_dir / "settings.yml").write_text("stale: true\n")

    _ensure(dirs)

    # Rendered again from the template on every start, so an updated template arrives, with the
    # kept key.
    assert _searxng_key(searxng_dir) == key


def test_searxng_files_are_readable_by_the_searxng_user(dirs: tuple[Path, Path]) -> None:
    secrets_dir, searxng_dir = dirs
    searxng_dir.chmod(0o700)
    _ensure(dirs)

    # SearXNG runs as its own user (977) on a volume that init's user owns; the key it is
    # rendered from stays with the app user.
    assert _mode(searxng_dir) == 0o755
    assert _mode(searxng_dir / "settings.yml") == 0o644
    assert _mode(secrets_dir / "searxng-secret") == 0o600


def test_without_a_searxng_folder_only_the_key_is_kept(tmp_path: Path) -> None:
    result = init_secrets.ensure_secrets(tmp_path)

    assert result.searxng_problem is None
    assert (tmp_path / "searxng-secret").is_file()
    assert not (tmp_path / "searxng").exists()


def test_an_unwritable_searxng_folder_does_not_stop_the_stack(tmp_path: Path) -> None:
    # A new image with an old compose file has no searxng-config volume: /searxng is then part
    # of the read-only root. The other secrets must still be written, or no service starts.
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    blocked = tmp_path / "searxng"
    blocked.write_text("a file where the folder should be\n")

    result = init_secrets.ensure_secrets(secrets_dir, searxng_dir=blocked)

    assert result.searxng_problem is not None
    assert str(blocked) in result.searxng_problem
    created = sorted(p.name for p in secrets_dir.iterdir())
    assert created == sorted(FILES)


def test_valkey_host_option(tmp_path: Path) -> None:
    init_secrets.ensure_secrets(tmp_path, valkey_host="cache")

    assert "@cache:6379/0" in (tmp_path / "valkey-url").read_text()


def test_a_broken_secret_is_refused(tmp_path: Path) -> None:
    (tmp_path / "valkey-password").write_text("not a password!\n")

    with pytest.raises(init_secrets.SecretsError, match="valkey-password"):
        init_secrets.ensure_secrets(tmp_path)


@pytest.mark.parametrize("domain", ["", "api.example.org", "srv123456.hstgr.cloud", "localhost"])
def test_domain_accepted(domain: str) -> None:
    assert init_secrets.domain_problem(domain) is None


@pytest.mark.parametrize(
    "domain",
    ["https://api.example.org", "api.example.org/", "api.example.org:8443", "-bad.example", "a b"],
)
def test_domain_refused(domain: str) -> None:
    problem = init_secrets.domain_problem(domain)

    assert problem is not None
    assert "host name" in problem


def test_main_creates_the_secrets_and_logs_the_key_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DOMAIN", raising=False)

    searxng_dir = tmp_path / "searxng"
    arguments = ["--secrets-dir", str(tmp_path), "--searxng-dir", str(searxng_dir)]
    with pytest.raises(SystemExit) as first:
        init_secrets.main(arguments)
    first_out = capsys.readouterr().out
    with pytest.raises(SystemExit) as second:
        init_secrets.main(arguments)
    second_out = capsys.readouterr().out

    admin_key = (tmp_path / "admin-key").read_text().strip()
    assert first.value.code == 0
    assert second.value.code == 0
    assert admin_key in first_out
    assert admin_key not in second_out
    assert "--show-admin-key" in second_out
    password = (tmp_path / "valkey-password").read_text().strip()
    assert password not in first_out + second_out
    searxng_key = (tmp_path / "searxng-secret").read_text().strip()
    assert searxng_key not in first_out + second_out
    assert _searxng_key(searxng_dir) == searxng_key


def test_main_writes_the_other_secrets_when_searxng_settings_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DOMAIN", raising=False)
    blocked = tmp_path / "searxng"
    blocked.write_text("not a folder\n")
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()

    with pytest.raises(SystemExit) as exited:
        init_secrets.main(["--secrets-dir", str(secrets_dir), "--searxng-dir", str(blocked)])

    assert exited.value.code == 0
    assert "init_searxng_settings_failed" in capsys.readouterr().out
    assert (secrets_dir / "valkey-url").is_file()


def test_main_defaults_to_the_searxng_mount_point() -> None:
    assert str(init_secrets.DEFAULT_SEARXNG_DIR) == "/searxng"


def test_main_show_admin_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DOMAIN", raising=False)
    init_secrets.ensure_secrets(tmp_path)
    admin_key = (tmp_path / "admin-key").read_text().strip()

    with pytest.raises(SystemExit) as exited:
        init_secrets.main(["--secrets-dir", str(tmp_path), "--show-admin-key"])

    assert exited.value.code == 0
    assert capsys.readouterr().out == admin_key + "\n"


def test_main_show_admin_key_without_secrets_fails(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exited:
        init_secrets.main(["--secrets-dir", str(tmp_path), "--show-admin-key"])

    assert exited.value.code == 1


def test_main_refuses_a_url_as_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DOMAIN", "https://api.example.org")

    with pytest.raises(SystemExit) as exited:
        init_secrets.main(["--secrets-dir", str(tmp_path)])

    assert exited.value.code == 1
    assert "DOMAIN" in capsys.readouterr().out
