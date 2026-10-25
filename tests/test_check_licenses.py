"""The licence gate ``scripts/check_licenses.py`` (CLAUDE.md rule 7): GPL and AGPL fail, LGPL
warns, anything else passes; the allowlist excuses reviewed entries; packages of ``uv.lock``
that this environment does not install must be reviewed by hand."""

import importlib.util
import sys
from email.message import Message
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_licenses.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_licenses", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("check_licenses", module)
    spec.loader.exec_module(module)
    return module


check_licenses = _load()


def _dist(root: Path, name: str, version: str = "1.0", *headers: str) -> None:
    """A fake installed distribution: ``<name>-<version>.dist-info/METADATA``."""
    info = root / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)
    lines = ["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}", *headers]
    (info / "METADATA").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run(root: Path, *args: str) -> int:
    code: int = check_licenses.main(["--path", str(root), "--allowlist", "", *args])
    return code


def test_check_licenses_fails_on_gpl(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _dist(tmp_path, "copyleft-lib", "2.3", "License-Expression: GPL-3.0-only")
    _dist(tmp_path, "fine-lib", "1.0", "License-Expression: MIT")

    assert _run(tmp_path) == 1
    out = capsys.readouterr().out
    assert "ERROR copyleft-lib 2.3" in out
    assert "GPL-3.0-only" in out
    assert "fine-lib" not in out


def test_lgpl_is_a_warning_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _dist(tmp_path, "lesser-lib", "1.0", "License-Expression: LGPL-2.1")

    assert _run(tmp_path) == 0
    out = capsys.readouterr().out
    assert "WARNING lesser-lib 1.0" in out
    assert "ERROR" not in out


def test_mit_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _dist(tmp_path, "fine-lib", "1.0", "License-Expression: MIT")

    assert _run(tmp_path) == 0
    out = capsys.readouterr().out
    assert "ERROR" not in out
    assert "WARNING" not in out
    assert "1 distributions" in out


def test_agpl_fails(tmp_path: Path) -> None:
    _dist(tmp_path, "network-lib", "1.0", "License-Expression: AGPL-3.0-or-later")

    assert _run(tmp_path) == 1


@pytest.mark.parametrize(
    ("expression", "code"),
    [
        ("MIT OR GPL-2.0-or-later", 0),  # a choice: we take MIT
        ("(Apache-2.0 OR GPL-3.0-only) AND BSD-3-Clause", 0),
        ("MIT AND GPL-2.0-only", 1),  # both apply
        ("GPL-2.0-only WITH Classpath-exception-2.0", 1),
        ("LGPL-3.0-or-later OR GPL-3.0-or-later", 0),  # LGPL at worst: a warning
    ],
)
def test_spdx_expressions(tmp_path: Path, expression: str, code: int) -> None:
    _dist(tmp_path, "some-lib", "1.0", f"License-Expression: {expression}")

    assert _run(tmp_path) == code


@pytest.mark.parametrize(
    ("classifier", "code", "level"),
    [
        ("License :: OSI Approved :: GNU General Public License v3 (GPLv3)", 1, "ERROR"),
        ("License :: OSI Approved :: GNU Affero General Public License v3", 1, "ERROR"),
        (
            "License :: OSI Approved :: GNU Lesser General Public License v2 or later (LGPLv2+)",
            0,
            "WARNING",
        ),
        (
            "License :: OSI Approved :: GNU Library or Lesser General Public License (LGPL)",
            0,
            "WARNING",
        ),
        ("License :: OSI Approved :: MIT License", 0, None),
    ],
)
def test_classifiers_without_an_expression(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    classifier: str,
    code: int,
    level: str | None,
) -> None:
    _dist(tmp_path, "old-lib", "1.0", "License: UNKNOWN", f"Classifier: {classifier}")

    assert _run(tmp_path) == code
    out = capsys.readouterr().out
    if level is None:
        assert "old-lib" not in out
    else:
        assert f"{level} old-lib 1.0" in out


@pytest.mark.parametrize(
    ("text", "code"),
    [("GNU GPL v3", 1), ("GPLv2+", 1), ("LGPL", 0), ("BSD", 0), ("MIT License", 0)],
)
def test_license_field_without_expression_or_classifier(
    tmp_path: Path, text: str, code: int
) -> None:
    _dist(tmp_path, "older-lib", "1.0", f"License: {text}")

    assert _run(tmp_path) == code


def test_a_permissive_classifier_does_not_hide_a_gpl_licence_field(tmp_path: Path) -> None:
    _dist(
        tmp_path,
        "mixed-lib",
        "1.0",
        "License: GPL-3.0",
        "Classifier: License :: OSI Approved :: MIT License",
    )

    assert _run(tmp_path) == 1


def test_a_licence_text_is_judged_by_its_title(tmp_path: Path) -> None:
    text = "BSD 3-Clause License\n\nBundled: Python's licence, which is GPL-compatible.\n"
    meta = check_licenses.judge(_message({"License": text}))

    assert meta == ("ok", "BSD 3-Clause License")
    gpl = "GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n"
    assert check_licenses.judge(_message({"License": gpl}))[0] == "strong"


def _message(headers: dict[str, str]) -> Message:
    message = Message()
    for key, value in headers.items():
        message[key] = value
    return message


def test_no_licence_metadata_warns(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _dist(tmp_path, "bare-lib", "1.0")

    assert _run(tmp_path) == 0
    assert "WARNING bare-lib 1.0: no licence metadata" in capsys.readouterr().out


def _allowlist(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_the_allowlist_excuses_a_reviewed_false_positive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    site = tmp_path / "site"
    _dist(site, "dual-lib", "1.0", "License: GPL or MIT, at your choice")
    allow = _allowlist(
        tmp_path / "allow.toml",
        '[[allow]]\nname = "dual-lib"\nversion = "1.0"\nlicense = "MIT"\n'
        'reason = "dual-licensed, used under MIT"\n',
    )

    assert check_licenses.main(["--path", str(site), "--allowlist", str(allow)]) == 0
    assert "allowed dual-lib 1.0: MIT (dual-licensed, used under MIT)" in capsys.readouterr().out


def test_the_allowlist_names_a_version(tmp_path: Path) -> None:
    site = tmp_path / "site"
    _dist(site, "dual-lib", "2.0", "License: GPL or MIT, at your choice")
    allow = _allowlist(
        tmp_path / "allow.toml",
        '[[allow]]\nname = "dual-lib"\nversion = "1.0"\nlicense = "MIT"\nreason = "reviewed"\n',
    )

    # A new version is reviewed again.
    assert check_licenses.main(["--path", str(site), "--allowlist", str(allow)]) == 1


def test_a_bad_allowlist_entry_is_refused(tmp_path: Path) -> None:
    allow = _allowlist(tmp_path / "allow.toml", '[[allow]]\nname = "x"\n')

    with pytest.raises(SystemExit, match="license"):
        check_licenses.main(["--path", str(tmp_path), "--allowlist", str(allow)])


LOCK = """
version = 1

[[package]]
name = "app"
version = "0.1.0"
source = { editable = "." }
dependencies = [{ name = "click" }, { name = "httpx", extra = ["http2"] }]

[package.optional-dependencies]
heavy = [{ name = "torch" }]

[package.dev-dependencies]
dev = [{ name = "pytest" }]

[[package]]
name = "click"
version = "8.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "colorama", marker = "sys_platform == 'win32'" }]

[[package]]
name = "colorama"
version = "0.4.6"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "httpx"
version = "0.28.1"
source = { registry = "https://pypi.org/simple" }

[package.optional-dependencies]
http2 = [{ name = "h2" }]

[[package]]
name = "h2"
version = "4.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "pytest"
version = "8.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "torch"
version = "2.0"
source = { registry = "https://download.pytorch.org/whl/cpu" }
"""


def _locked_site(tmp_path: Path) -> tuple[Path, Path]:
    site = tmp_path / "site"
    for name in ("app", "click", "httpx", "h2", "pytest"):
        _dist(site, name, "1.0", "License-Expression: MIT")
    lock = tmp_path / "uv.lock"
    lock.write_text(LOCK, encoding="utf-8")
    return site, lock


def test_a_lock_package_that_is_not_installed_needs_a_review(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    site, lock = _locked_site(tmp_path)

    code = check_licenses.main(["--path", str(site), "--allowlist", "", "--lock", str(lock)])

    assert code == 1
    out = capsys.readouterr().out
    assert "ERROR colorama 0.4.6: in uv.lock but not installed here" in out
    # Reached only through an extra that was not asked for: not this environment's business.
    assert "torch" not in out


def test_a_reviewed_lock_package_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    site, lock = _locked_site(tmp_path)
    allow = _allowlist(
        tmp_path / "allow.toml",
        '[[not_installed]]\nname = "colorama"\nlicense = "BSD-3-Clause"\nreason = "Windows only"\n',
    )

    code = check_licenses.main(
        ["--path", str(site), "--allowlist", str(allow), "--lock", str(lock)]
    )

    assert code == 0
    assert "not installed colorama 0.4.6: BSD-3-Clause (Windows only)" in capsys.readouterr().out


def test_an_extra_of_the_lock_is_followed_when_asked(tmp_path: Path) -> None:
    site, lock = _locked_site(tmp_path)
    allow = _allowlist(
        tmp_path / "allow.toml",
        '[[not_installed]]\nname = "colorama"\nlicense = "BSD-3-Clause"\nreason = "Windows"\n',
    )
    args = ["--path", str(site), "--allowlist", str(allow), "--lock", str(lock)]

    assert check_licenses.main([*args, "--extra", "heavy"]) == 1  # torch is not installed
    _dist(site, "torch", "2.0", "License-Expression: BSD-3-Clause")
    assert check_licenses.main([*args, "--extra", "heavy"]) == 0


def test_this_environment_passes(capsys: pytest.CaptureFixture[str]) -> None:
    """The gate as CI runs it on the default environment."""
    code = check_licenses.main(["--lock", str(ROOT / "uv.lock"), "--extra", "browser"])

    out = capsys.readouterr().out
    assert code == 0, out
    assert "ERROR" not in out
