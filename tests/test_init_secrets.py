"""text-extraction-init: the one-shot service of the root docker-compose.yml that creates the
stack's secrets on its first start (Hostinger rule: the first deploy works without variables)."""

import hashlib
import json
import stat
from pathlib import Path

import pytest

from text_extraction import init_secrets
from text_extraction.settings import ApiKey

FILES = ("valkey-password", "admin-key", "valkey.conf", "valkey-url", "api-keys.json")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_creates_every_file(tmp_path: Path) -> None:
    result = init_secrets.ensure_secrets(tmp_path)

    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(FILES)
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

    with pytest.raises(SystemExit) as first:
        init_secrets.main(["--secrets-dir", str(tmp_path)])
    first_out = capsys.readouterr().out
    with pytest.raises(SystemExit) as second:
        init_secrets.main(["--secrets-dir", str(tmp_path)])
    second_out = capsys.readouterr().out

    admin_key = (tmp_path / "admin-key").read_text().strip()
    assert first.value.code == 0
    assert second.value.code == 0
    assert admin_key in first_out
    assert admin_key not in second_out
    assert "--show-admin-key" in second_out
    password = (tmp_path / "valkey-password").read_text().strip()
    assert password not in first_out + second_out


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


def _new_key_output(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as exited:
        init_secrets.main(argv)
    captured = capsys.readouterr()
    return int(exited.value.code or 0), captured.out, captured.err


def _value_after(label: str, out: str) -> str:
    """The indented line that follows the line starting with ``label``."""
    lines = out.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith(label))
    return lines[index + 1].strip()


def test_new_key_prints_the_key_and_matching_api_keys_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _new_key_output(["--secrets-dir", str(tmp_path), "--new-key", "jan"], capsys)

    assert code == 0
    key = _value_after("Key", out)
    assert len(key) == 64
    assert int(key, 16) >= 0
    entry = {"name": "jan", "role": "user", "sha256": hashlib.sha256(key.encode()).hexdigest()}
    env_line = _value_after(".env", out)
    assert env_line.startswith("API_KEYS=[")
    assert json.loads(env_line.removeprefix("API_KEYS=")) == [entry]
    assert _value_after("Shell", out) == f"export {env_line[:9]}'{env_line[9:]}'"
    panel_value = _value_after("Docker panel", out)
    assert panel_value.startswith("[")
    keys = [ApiKey.model_validate(item) for item in json.loads(panel_value)]
    assert [item.model_dump(exclude_none=True) for item in keys] == [entry]
    assert list(tmp_path.iterdir()) == []  # touches no secret


def test_new_key_admin_role(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = _new_key_output(["--new-key", "ops", "--role", "admin"], capsys)

    assert code == 0
    assert json.loads(_value_after("Docker panel", out))[0]["role"] == "admin"


def test_two_new_keys_differ(capsys: pytest.CaptureFixture[str]) -> None:
    _, first, _ = _new_key_output(["--new-key", "a"], capsys)
    _, second, _ = _new_key_output(["--new-key", "b"], capsys)

    assert _value_after("Key", first) != _value_after("Key", second)


@pytest.mark.parametrize("name", ["admin", "", "two words", "x" * 65, 'quo"te'])
def test_new_key_refuses_a_name(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _new_key_output(["--new-key", name], capsys)

    assert code == 2
    assert out == ""
    assert "name" in err


def test_new_key_and_show_admin_key_exclude_each_other(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, _ = _new_key_output(["--new-key", "jan", "--show-admin-key"], capsys)

    assert code == 2


def test_role_without_new_key_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _new_key_output(["--secrets-dir", str(tmp_path), "--role", "admin"], capsys)

    assert code == 2
    assert "--role" in err
    assert list(tmp_path.iterdir()) == []  # the full init did not run
